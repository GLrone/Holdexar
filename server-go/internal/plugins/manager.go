package plugins

import (
	"archive/zip"
	"bytes"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"sort"
	"strings"
	"sync"

	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const registryKey = "plugins.registry"
const catalogKey = "plugins.catalog"
const maxPackage = 256 << 10

type Installed struct {
	Manifest          Manifest       `json:"manifest"`
	Enabled           bool           `json:"enabled"`
	Config            map[string]any `json:"config"`
	ConfiguredSecrets []string       `json:"configuredSecrets,omitempty"`
	SHA256            string         `json:"sha256"`
	URL               string         `json:"url,omitempty"`
	Unsigned          bool           `json:"unsigned"`
	TargetDigest      string         `json:"targetDigest"`
	Compatible        bool           `json:"compatible"`
	Trust             string         `json:"trust"`
	Configured        bool           `json:"configured"`
	Error             string         `json:"error,omitempty"`
}
type Selection struct {
	ID      string `json:"id"`
	Version string `json:"version"`
	SHA256  string `json:"sha256"`
	URL     string `json:"url"`
}
type Listing struct {
	Manifest Manifest `json:"manifest"`
	SHA256   string   `json:"sha256"`
	URL      string   `json:"url"`
}
type CatalogResult struct {
	APIVersion int       `json:"apiVersion"`
	Plugins    []Listing `json:"catalog"`
	Source     string    `json:"source"`
	Cached     bool      `json:"cached"`
	Error      string    `json:"error,omitempty"`
}
type Options struct {
	Settings    settings.Options
	BuiltinList func(context.Context) (any, error)
	BuiltinSet  func(context.Context, string, bool) error
	Test        func(context.Context, string) error
}
type Manager struct {
	db      *sql.DB
	gate    *store.WriteGate
	dataDir string
	options Options
	client  *http.Client
	mu      sync.Mutex
}

func New(db *sql.DB, gate *store.WriteGate, dataDir string, options Options) *Manager {
	return &Manager{db: db, gate: gate, dataDir: dataDir, options: options, client: NewSafeClient()}
}

type rowQuery interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func readRegistry(ctx context.Context, q rowQuery) ([]Installed, error) {
	var raw string
	e := q.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, registryKey).Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) {
		return []Installed{}, nil
	}
	if e != nil {
		return nil, e
	}
	var entries []Installed
	if e = json.Unmarshal([]byte(raw), &entries); e != nil {
		return nil, errors.New("installed plugin state is invalid")
	}
	if entries == nil {
		entries = []Installed{}
	}
	active := 0
	seen := map[string]bool{}
	for _, entry := range entries {
		if e = entry.Manifest.Validate(); e != nil {
			return nil, errors.New("installed plugin manifest is invalid")
		}
		if seen[entry.Manifest.ID] {
			return nil, errors.New("duplicate installed plugin state")
		}
		seen[entry.Manifest.ID] = true
		if entry.Enabled {
			active++
		}
	}
	if active > 1 {
		return nil, errors.New("multiple active notification plugins")
	}
	return entries, nil
}

func RecipientFor(entry Installed) string {
	return "plugin:" + entry.Manifest.ID + ":" + entry.TargetDigest
}
func Recipient(ctx context.Context, q rowQuery) (string, error) {
	entries, e := readRegistry(ctx, q)
	if e != nil {
		return "", e
	}
	for _, entry := range entries {
		if entry.Enabled {
			if len(entry.TargetDigest) != 64 {
				return "", errors.New("plugin notification target is invalid")
			}
			return RecipientFor(entry), nil
		}
	}
	return "", nil
}
func (m *Manager) Recipient(ctx context.Context) (string, error) { return Recipient(ctx, m.db) }

func (m *Manager) load(ctx context.Context) ([]Installed, error) {
	entries, e := readRegistry(ctx, m.db)
	if e != nil {
		return nil, e
	}
	for i := range entries {
		if e = m.unseal(&entries[i]); e != nil {
			return nil, e
		}
	}
	return entries, nil
}

func (m *Manager) unseal(entry *Installed) error {
	for name, field := range entry.Manifest.ConfigSchema.Properties {
		if field.Format != "password" {
			continue
		}
		token, ok := entry.Config[name].(string)
		if !ok || token == "" {
			continue
		}
		if !secretbox.IsEncrypted(token) {
			return errors.New("plugin credential is not encrypted")
		}
		plain, e := secretbox.DecryptSecretIn(m.dataDir, token, "plugin:"+entry.Manifest.ID+":"+name)
		if e != nil {
			return errors.New("plugin credential is unavailable")
		}
		entry.Config[name] = plain
	}
	return nil
}
func (m *Manager) save(ctx context.Context, entries []Installed) error {
	raw, e := json.Marshal(entries)
	if e != nil {
		return e
	}
	var sealed []Installed
	if e = json.Unmarshal(raw, &sealed); e != nil {
		return e
	}
	for i := range sealed {
		for name, field := range sealed[i].Manifest.ConfigSchema.Properties {
			if field.Format != "password" {
				continue
			}
			plain, _ := sealed[i].Config[name].(string)
			if plain == "" {
				continue
			}
			token, e := secretbox.EncryptSecretIn(m.dataDir, plain, "plugin:"+sealed[i].Manifest.ID+":"+name)
			if e != nil {
				return errors.New("plugin credential could not be protected")
			}
			sealed[i].Config[name] = token
		}
		sealed[i].ConfiguredSecrets = nil
	}
	return settings.SetValues(ctx, m.gate, m.options.Settings, map[string]any{registryKey: sealed})
}
func safeEntry(entry Installed) Installed {
	c := map[string]any{}
	entry.Compatible = entry.Manifest.Validate() == nil
	entry.Trust = "unsigned"
	entry.Configured = ValidateConfig(entry.Manifest.ConfigSchema, entry.Config, true) == nil
	entry.ConfiguredSecrets = nil
	for name, value := range entry.Config {
		if entry.Manifest.ConfigSchema.Properties[name].Format == "password" {
			if value != "" {
				entry.ConfiguredSecrets = append(entry.ConfiguredSecrets, name)
			}
			continue
		}
		if value, ok := value.(string); ok && secretbox.IsEncrypted(value) {
			continue
		}
		c[name] = value
	}
	sort.Strings(entry.ConfiguredSecrets)
	entry.Config = c
	return entry
}
func (m *Manager) List(ctx context.Context) ([]Installed, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	entries, e := readRegistry(ctx, m.db)
	if e != nil {
		return nil, e
	}
	for i := range entries {
		entryErr := m.unseal(&entries[i])
		if entryErr == nil {
			entryErr = ValidateConfig(entries[i].Manifest.ConfigSchema, entries[i].Config, false)
		}
		entries[i] = safeEntry(entries[i])
		if entryErr != nil {
			entries[i].Configured = false
			entries[i].Error = "Plugin configuration is unavailable; disable it or enter its credentials again."
		}
	}
	return entries, nil
}
func (m *Manager) Active(ctx context.Context) (*Installed, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	entries, e := m.load(ctx)
	if e != nil {
		return nil, e
	}
	for _, entry := range entries {
		if entry.Enabled {
			if e := ValidateConfig(entry.Manifest.ConfigSchema, entry.Config, true); e != nil {
				return nil, e
			}
			if targetDigest(entry) != entry.TargetDigest {
				return nil, errors.New("plugin notification target does not match its configuration")
			}
			return &entry, nil
		}
	}
	return nil, nil
}
func (m *Manager) Config(ctx context.Context, id string) (map[string]any, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	entries, e := m.load(ctx)
	if e != nil {
		return nil, e
	}
	for _, entry := range entries {
		if entry.Manifest.ID == id {
			return entry.Config, nil
		}
	}
	return nil, errors.New("plugin is not installed")
}

func targetDigest(entry Installed) string {
	raw, _ := json.Marshal(struct {
		Manifest Manifest
		Config   map[string]any
	}{entry.Manifest, entry.Config})
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func parsePackage(raw []byte) (Manifest, error) {
	if len(raw) > maxPackage {
		return Manifest{}, errors.New("plugin package is too large")
	}
	z, e := zip.NewReader(bytes.NewReader(raw), int64(len(raw)))
	if e != nil {
		return Manifest{}, errors.New("invalid plugin archive")
	}
	if len(z.File) != 1 {
		return Manifest{}, errors.New("plugin package must contain only manifest.json")
	}
	file := z.File[0]
	if file.Name != "manifest.json" || !file.Mode().IsRegular() || file.UncompressedSize64 > maxManifest || file.Flags&1 != 0 {
		return Manifest{}, errors.New("unsupported plugin archive member")
	}
	reader, e := file.Open()
	if e != nil {
		return Manifest{}, errors.New("plugin manifest cannot be read")
	}
	defer reader.Close()
	data, e := io.ReadAll(io.LimitReader(reader, maxManifest+1))
	if e != nil || len(data) > maxManifest {
		return Manifest{}, errors.New("plugin manifest is damaged or too large")
	}
	var manifest Manifest
	if e = strictJSON(data, &manifest); e != nil {
		return Manifest{}, e
	}
	if e = manifest.Validate(); e != nil {
		return Manifest{}, e
	}
	return manifest, nil
}

func (m *Manager) install(ctx context.Context, raw []byte, source string, unsigned bool, expected *Listing) (Installed, error) {
	manifest, e := parsePackage(raw)
	if e != nil {
		return Installed{}, e
	}
	sum := sha256.Sum256(raw)
	hash := hex.EncodeToString(sum[:])
	if expected != nil {
		if hash != expected.SHA256 {
			return Installed{}, errors.New("plugin package checksum does not match its catalog")
		}
		a, _ := json.Marshal(manifest)
		b, _ := json.Marshal(expected.Manifest)
		if !bytes.Equal(a, b) {
			return Installed{}, errors.New("plugin manifest does not match its catalog")
		}
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	entries, e := m.load(ctx)
	if e != nil {
		return Installed{}, e
	}
	entry := Installed{Manifest: manifest, Config: map[string]any{}, SHA256: hash, URL: source, Unsigned: unsigned}
	index := -1
	for i, old := range entries {
		if old.Manifest.ID == manifest.ID {
			if old.Manifest.Endpoint != manifest.Endpoint {
				return Installed{}, errors.New("plugin update changes its notification destination; remove it and review a new installation")
			}
			for _, domain := range manifest.Domains {
				if !slices.Contains(old.Manifest.Domains, domain) {
					return Installed{}, errors.New("plugin update expands its network permissions; remove it and review a new installation")
				}
			}
			for name, field := range old.Manifest.ConfigSchema.Properties {
				if field.Format == "password" && old.Config[name] != nil && old.Config[name] != "" && manifest.ConfigSchema.Properties[name].Format != "password" {
					return Installed{}, errors.New("clear the saved secret before changing its configuration privacy")
				}
			}
			index = i
			entry.Enabled = old.Enabled
			entry.Config = old.Config
			break
		}
	}
	for k, f := range manifest.ConfigSchema.Properties {
		if _, ok := entry.Config[k]; !ok && f.Default != nil {
			entry.Config[k] = f.Default
		}
	}
	if e = ValidateConfig(manifest.ConfigSchema, entry.Config, entry.Enabled); e != nil {
		return Installed{}, e
	}
	entry.TargetDigest = targetDigest(entry)
	if index < 0 {
		entries = append(entries, entry)
	} else {
		entries[index] = entry
	}
	if e = m.save(ctx, entries); e != nil {
		return Installed{}, e
	}
	return safeEntry(entry), nil
}
func (m *Manager) InstallLocal(ctx context.Context, raw []byte) (Installed, error) {
	return m.install(ctx, raw, "", true, nil)
}
func (m *Manager) Install(ctx context.Context, selection Selection) (Installed, error) {
	catalog, e := m.readCatalog(ctx)
	if e != nil {
		return Installed{}, e
	}
	var selected *Listing
	for _, entry := range catalog.Plugins {
		if entry.Manifest.ID == selection.ID && entry.Manifest.Version == selection.Version && entry.SHA256 == selection.SHA256 && entry.URL == selection.URL {
			copy := entry
			selected = &copy
			break
		}
	}
	if selected == nil {
		return Installed{}, errors.New("plugin selection is not in the loaded catalog")
	}
	raw, e := m.download(ctx, selected.URL, maxPackage)
	if e != nil {
		return Installed{}, e
	}
	return m.install(ctx, raw, selected.URL, true, selected)
}
func (m *Manager) Update(ctx context.Context, id string, enabled *bool, config map[string]any) (Installed, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if enabled != nil && !*enabled && len(config) == 0 {
		entries, e := readRegistry(ctx, m.db)
		if e != nil {
			return Installed{}, e
		}
		for i := range entries {
			if entries[i].Manifest.ID == id {
				entries[i].Enabled = false
				if e = settings.SetValues(ctx, m.gate, m.options.Settings, map[string]any{registryKey: entries}); e != nil {
					return Installed{}, e
				}
				return safeEntry(entries[i]), nil
			}
		}
		return Installed{}, errors.New("plugin is not installed")
	}
	entries, e := m.load(ctx)
	if e != nil {
		return Installed{}, e
	}
	for i := range entries {
		entry := &entries[i]
		if entry.Manifest.ID != id {
			continue
		}
		if enabled != nil {
			entry.Enabled = *enabled
		}
		if entry.Config == nil {
			entry.Config = map[string]any{}
		}
		for k, v := range config {
			entry.Config[k] = v
		}
		if e = ValidateConfig(entry.Manifest.ConfigSchema, entry.Config, entry.Enabled); e != nil {
			return Installed{}, e
		}
		if entry.Enabled {
			for j := range entries {
				if j != i {
					entries[j].Enabled = false
				}
			}
		}
		entry.TargetDigest = targetDigest(*entry)
		if e = m.save(ctx, entries); e != nil {
			return Installed{}, e
		}
		return safeEntry(*entry), nil
	}
	return Installed{}, errors.New("plugin is not installed")
}
func (m *Manager) Remove(ctx context.Context, id string) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	entries, e := readRegistry(ctx, m.db)
	if e != nil {
		return e
	}
	for i, entry := range entries {
		if entry.Manifest.ID == id {
			return settings.SetValues(ctx, m.gate, m.options.Settings, map[string]any{registryKey: append(entries[:i], entries[i+1:]...)})
		}
	}
	return errors.New("plugin is not installed")
}

func (m *Manager) readCatalog(ctx context.Context) (CatalogResult, error) {
	var raw string
	e := m.db.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, catalogKey).Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) {
		return CatalogResult{APIVersion: 1, Plugins: []Listing{}}, nil
	}
	if e != nil {
		return CatalogResult{}, e
	}
	var result CatalogResult
	if e = json.Unmarshal([]byte(raw), &result); e != nil {
		return CatalogResult{}, errors.New("saved plugin catalog is invalid")
	}
	return result, nil
}

func (m *Manager) Discover(ctx context.Context, source, query string) (CatalogResult, error) {
	old, e := m.readCatalog(ctx)
	if e != nil {
		return CatalogResult{}, e
	}
	if source == "" {
		source = old.Source
	}
	result := old
	result.Cached = true
	if source != "" {
		raw, e := m.download(ctx, source, 1<<20)
		if e == nil {
			var index struct {
				APIVersion int       `json:"apiVersion"`
				Plugins    []Listing `json:"plugins"`
			}
			e = strictJSON(raw, &index)
			if e == nil && (index.APIVersion != 1 || len(index.Plugins) > 200) {
				e = errors.New("unsupported plugin catalog")
			}
			seen := map[string]bool{}
			if e == nil {
				for _, entry := range index.Plugins {
					if e = entry.Manifest.Validate(); e != nil {
						break
					}
					if e = ValidateHTTPS(entry.URL, nil); e != nil {
						break
					}
					hash, err := hex.DecodeString(entry.SHA256)
					if err != nil || len(hash) != 32 || strings.ToLower(entry.SHA256) != entry.SHA256 {
						e = errors.New("invalid plugin catalog checksum")
						break
					}
					key := entry.Manifest.ID + "@" + entry.Manifest.Version
					if seen[key] {
						e = errors.New("duplicate plugin catalog version")
						break
					}
					seen[key] = true
				}
			}
			if e == nil {
				result = CatalogResult{APIVersion: 1, Plugins: index.Plugins, Source: source}
				if result.Plugins == nil {
					result.Plugins = []Listing{}
				}
				m.mu.Lock()
				e = settings.SetValues(ctx, m.gate, m.options.Settings, map[string]any{catalogKey: result})
				m.mu.Unlock()
			}
		}
		if e != nil {
			m.mu.Lock()
			current, readErr := m.readCatalog(ctx)
			if readErr != nil {
				m.mu.Unlock()
				return CatalogResult{}, readErr
			}
			result = current
			result.Cached = true
			result.Error = "Plugin source could not be refreshed; showing the saved catalog."
			saveErr := settings.SetValues(ctx, m.gate, m.options.Settings, map[string]any{catalogKey: result})
			m.mu.Unlock()
			if saveErr != nil {
				return CatalogResult{}, saveErr
			}
		}
	}
	if result.Plugins == nil {
		result.Plugins = []Listing{}
	}
	needle := strings.ToLower(strings.TrimSpace(query))
	if needle != "" {
		matches := []Listing{}
		for _, entry := range result.Plugins {
			if strings.Contains(strings.ToLower(fmt.Sprint(entry.Manifest.ID, " ", entry.Manifest.Name, " ", entry.Manifest.Description, " ", entry.Manifest.Author)), needle) {
				matches = append(matches, entry)
			}
		}
		result.Plugins = matches
	}
	return result, nil
}
