package plugins

import (
	"archive/zip"
	"bytes"
	"errors"
	"strconv"
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
	_ "modernc.org/sqlite"
)

func testManager(t *testing.T) (*Manager, *sql.DB) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "test.db")
	db, e := sql.Open("sqlite", path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if _, e = db.Exec(`CREATE TABLE app_settings(key TEXT PRIMARY KEY,value_json TEXT,updated_at TEXT)`); e != nil {
		t.Fatal(e)
	}
	return New(db, store.NewWriteGate(db, path), dir, Options{}), db
}
func fixture(id string) Manifest {
	return Manifest{ID: id, Name: "Webhook", Version: "1.0.0", APIVersion: 1, Kind: "notification.webhook", Domains: []string{"example.com"}, Endpoint: "https://example.com/notify", Headers: map[string]string{"Authorization": "Bearer {{config.token}}"}, Body: map[string]any{"subject": "{{subject}}", "text": "{{body}}"}, ConfigSchema: Schema{Type: "object", Properties: map[string]Field{"token": {Type: "string", Format: "password"}}, Required: []string{"token"}}}
}
func archive(t *testing.T, files map[string][]byte) []byte {
	t.Helper()
	var b bytes.Buffer
	z := zip.NewWriter(&b)
	for name, data := range files {
		w, e := z.Create(name)
		if e != nil {
			t.Fatal(e)
		}
		w.Write(data)
	}
	if e := z.Close(); e != nil {
		t.Fatal(e)
	}
	return b.Bytes()
}
func pack(t *testing.T, m Manifest) []byte {
	b, e := json.Marshal(m)
	if e != nil {
		t.Fatal(e)
	}
	return archive(t, map[string][]byte{"manifest.json": b})
}

func TestLocalInstallConfigAndRemovalRetainBusinessData(t *testing.T) {
	m, db := testManager(t)
	ctx := context.Background()
	if _, e := db.Exec(`CREATE TABLE business_history(value TEXT); INSERT INTO business_history VALUES('keep')`); e != nil {
		t.Fatal(e)
	}
	if _, e := m.InstallLocal(ctx, pack(t, fixture("example.webhook"))); e != nil {
		t.Fatal(e)
	}
	yes := true
	if _, e := m.Update(ctx, "example.webhook", &yes, nil); e == nil {
		t.Fatal("enabled without required credentials")
	}
	if _, e := m.Update(ctx, "example.webhook", &yes, map[string]any{"token": "test-secret"}); e != nil {
		t.Fatal(e)
	}
	active, e := m.Active(ctx)
	if e != nil || active == nil || active.Config["token"] != "test-secret" {
		t.Fatalf("active=%v err=%v", active, e)
	}
	recipient, e := Recipient(ctx, db)
	if e != nil || !strings.HasPrefix(recipient, "plugin:example.webhook:") {
		t.Fatalf("recipient=%q err=%v", recipient, e)
	}
	var raw string
	db.QueryRow(`SELECT value_json FROM app_settings WHERE key='plugins.registry'`).Scan(&raw)
	if strings.Contains(raw, "test-secret") {
		t.Fatal("plaintext credential persisted")
	}
	listed, e := m.List(ctx)
	if e != nil {
		t.Fatal(e)
	}
	b, _ := json.Marshal(listed)
	if bytes.Contains(b, []byte("test-secret")) {
		t.Fatal("credential exposed")
	}
	second := New(db, m.gate, m.dataDir, Options{})
	got, e := second.Active(ctx)
	if e != nil || got.Config["token"] != "test-secret" {
		t.Fatal("restart lost configuration", e)
	}
	if _, e := m.Update(ctx, "example.webhook", nil, map[string]any{"token": "changed"}); e != nil {
		t.Fatal(e)
	}
	next, _ := Recipient(ctx, db)
	if recipient == next {
		t.Fatal("recipient identity ignored config change")
	}
	if e := m.Remove(ctx, "example.webhook"); e != nil {
		t.Fatal(e)
	}
	var count int
	db.QueryRow(`SELECT count(*) FROM business_history`).Scan(&count)
	if count != 1 {
		t.Fatal("uninstall removed history")
	}
}

func TestPackageRejectsUnsafeOrExecutableMembers(t *testing.T) {
	m, _ := testManager(t)
	b, _ := json.Marshal(fixture("example.webhook"))
	for _, name := range []string{"../manifest.json", "/manifest.json", "a/manifest.json", "main.py", "C:\\manifest.json"} {
		files := map[string][]byte{"manifest.json": b, name: b}
		if _, e := m.InstallLocal(context.Background(), archive(t, files)); e == nil {
			t.Fatalf("accepted member %q", name)
		}
	}
	var out bytes.Buffer
	z := zip.NewWriter(&out)
	for i := 0; i < 2; i++ {
		w, _ := z.Create("manifest.json")
		w.Write(b)
	}
	z.Close()
	if _, e := m.InstallLocal(context.Background(), out.Bytes()); e == nil {
		t.Fatal("duplicate member accepted")
	}
}

func TestManifestAndTemplatesRejectUnknownOrUnsafeInputs(t *testing.T) {
	for _, edit := range []func(*Manifest){func(m *Manifest) { m.Endpoint = "http://example.com/a" }, func(m *Manifest) { m.Endpoint = "https://127.0.0.1/a" }, func(m *Manifest) { m.Headers["Cookie"] = "secret" }, func(m *Manifest) { m.Body = "{{config.missing}}" }, func(m *Manifest) { m.Kind = "python" }} {
		v := fixture("example.webhook")
		edit(&v)
		if e := v.Validate(); e == nil {
			t.Fatal("unsafe manifest accepted")
		}
	}
	v := fixture("example.webhook")
	if e := v.Validate(); e != nil {
		t.Fatal(e)
	}
	if e := ValidateConfig(v.ConfigSchema, map[string]any{"token": "ok", "unknown": true}, true); e == nil {
		t.Fatal("unknown config accepted")
	}
	subject := "quote\"\nline"
	b, headers, e := Render(v, map[string]any{"token": "abc"}, subject, "test")
	if e != nil {
		t.Fatal(e)
	}
	var parsed map[string]any
	if json.Unmarshal(b, &parsed) != nil || parsed["subject"] != subject || headers["Authorization"] != "Bearer abc" {
		t.Fatal("template corrupted JSON")
	}
}

func TestSafeClientBlocksLocalNetworkAndRedirects(t *testing.T) {
	for _, u := range []string{"https://127.0.0.1/a", "https://[::1]/", "https://169.254.169.254/", "https://100.64.0.1/", "https://localhost/", "http://example.com/", "https://user:pass@example.com/"} {
		if e := ValidateHTTPS(u, nil); e == nil {
			t.Fatalf("accepted %s", u)
		}
	}
	client := NewSafeClient()
	if e := client.CheckRedirect(&http.Request{}, nil); e == nil {
		t.Fatal("redirect accepted")
	}
	req, _ := http.NewRequest("GET", "https://127.0.0.1/", nil)
	if _, e := client.Do(req); e == nil {
		t.Fatal("client dialed loopback")
	}
}

func TestSafeTransportRejectsNonHTTPSBeforeDial(t *testing.T) {
	base := roundTripFunc(func(*http.Request) (*http.Response, error) {
		t.Fatal("unsafe URL reached the transport")
		return nil, nil
	})
	transport := safeTransport{base: base}
	for _, target := range []string{"http://example.com/", "https://user:password@example.com/", "https://127.0.0.1/"} {
		r, _ := http.NewRequest("GET", target, nil)
		if _, e := transport.RoundTrip(r); e == nil {
			t.Fatal("unsafe URL accepted")
		}
	}
}

func TestHTTPRejectsUnknownFieldsAndMasksSecrets(t *testing.T) {
	m, _ := testManager(t)
	mux := http.NewServeMux()
	m.Register(mux)
	r := httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("POST", "/api/v1/plugins/install", strings.NewReader(`{"id":"a","unknown":true}`)))
	if r.Code < 400 {
		t.Fatal("unknown install field accepted")
	}
	r = httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("POST", "/api/v1/plugins/install-local", bytes.NewReader(pack(t, fixture("example.webhook")))))
	if r.Code != 200 {
		t.Fatal(r.Code, r.Body.String())
	}
	r = httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("PUT", "/api/v1/plugins/example.webhook", strings.NewReader(`{"enabled":true,"config":{"token":"never-echo"}}`)))
	if r.Code != 200 || strings.Contains(r.Body.String(), "never-echo") {
		t.Fatal(r.Code, r.Body.String())
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestCatalogInstallVerifiesHashAndFailureKeepsActiveVersion(t *testing.T) {
	m, db := testManager(t)
	ctx := context.Background()
	manifest := fixture("example.webhook")
	raw := pack(t, manifest)
	sum := sha256.Sum256(raw)
	listing := Listing{Manifest: manifest, SHA256: hex.EncodeToString(sum[:]), URL: "https://example.com/plugin.hlx"}
	catalog, _ := json.Marshal(map[string]any{"apiVersion": 1, "plugins": []Listing{listing}})
	offline := false
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if offline {
			w.WriteHeader(503)
			return
		}
		if r.URL.Path == "/index.json" {
			w.Write(catalog)
		} else {
			w.Write(raw)
		}
	}))
	defer server.Close()
	m.client = &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		copy := r.Clone(r.Context())
		target, _ := http.NewRequest("GET", server.URL+r.URL.Path, nil)
		copy.URL = target.URL
		return server.Client().Transport.RoundTrip(copy)
	})}
	found, e := m.Discover(ctx, "https://example.com/index.json", "Webhook")
	if e != nil || found.Cached || len(found.Plugins) != 1 {
		t.Fatal(found, e)
	}
	selection := Selection{ID: manifest.ID, Version: manifest.Version, SHA256: listing.SHA256, URL: listing.URL}
	forged := selection
	forged.URL = "https://example.com/other"
	if _, e = m.Install(ctx, forged); e == nil {
		t.Fatal("forged selection accepted")
	}
	if _, e = m.Install(ctx, selection); e != nil {
		t.Fatal(e)
	}
	yes := true
	if _, e = m.Update(ctx, manifest.ID, &yes, map[string]any{"token": "original-secret"}); e != nil {
		t.Fatal(e)
	}
	original, _ := Recipient(ctx, db)
	raw = []byte("corrupt replacement")
	if _, e = m.Install(ctx, selection); e == nil {
		t.Fatal("bad package accepted")
	}
	after, _ := Recipient(ctx, db)
	active, e := m.Active(ctx)
	if e != nil || active == nil || active.Config["token"] != "original-secret" || original != after {
		t.Fatal("failed update changed active state", e)
	}
	offline = true
	cached, e := m.Discover(ctx, "https://example.com/index.json", "")
	if e != nil || !cached.Cached || cached.Error == "" || len(cached.Plugins) != 1 {
		t.Fatal(cached, e)
	}
	restarted := New(db, m.gate, m.dataDir, Options{})
	old, e := restarted.readCatalog(ctx)
	if e != nil || len(old.Plugins) != 1 || old.Error == "" || old.Source != "https://example.com/index.json" {
		t.Fatal("offline catalog lost", e)
	}
	manifest.Version = "2.0.0"
	delete(manifest.ConfigSchema.Properties, "token")
	manifest.ConfigSchema.Required = nil
	manifest.Headers = nil
	if _, e = m.InstallLocal(ctx, pack(t, manifest)); e == nil {
		t.Fatal("incompatible config upgrade accepted")
	}
}

func TestOneManagerSerializesConcurrentInstallsAndChannelSelection(t *testing.T) {
	m, _ := testManager(t)
	ctx := context.Background()
	var group sync.WaitGroup
	errs := make(chan error, 8)
	for i := 0; i < 8; i++ {
		manifest := fixture("example.channel" + string(rune('a'+i)))
		raw := pack(t, manifest)
		group.Add(1)
		go func() { defer group.Done(); _, e := m.InstallLocal(ctx, raw); errs <- e }()
	}
	group.Wait()
	close(errs)
	for e := range errs {
		if e != nil {
			t.Fatal(e)
		}
	}
	entries, e := m.List(ctx)
	if e != nil || len(entries) != 8 {
		t.Fatal("concurrent install lost state", len(entries), e)
	}
	yes := true
	for _, entry := range entries {
		if _, e = m.Update(ctx, entry.Manifest.ID, &yes, map[string]any{"token": "secret"}); e != nil {
			t.Fatal(e)
		}
	}
	entries, _ = m.List(ctx)
	count := 0
	for _, entry := range entries {
		if entry.Enabled {
			count++
		}
	}
	if count != 1 {
		t.Fatal("multiple active channels", count)
	}
}

func TestHTTPPluginMetadataAndDiscoverContract(t *testing.T) {
	m, _ := testManager(t)
	m.InstallLocal(context.Background(), pack(t, fixture("example.webhook")))
	mux := http.NewServeMux()
	m.Register(mux)
	r := httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("GET", "/api/v1/plugins", nil))
	var state struct {
		Installed []map[string]any `json:"installed"`
	}
	json.Unmarshal(r.Body.Bytes(), &state)
	entry := state.Installed[0]
	if entry["trust"] != "unsigned" || entry["compatible"] != true || entry["configured"] != false {
		t.Fatal("missing metadata", entry)
	}
	r = httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("GET", "/api/v1/plugins/discover", nil))
	var discovered map[string]any
	json.Unmarshal(r.Body.Bytes(), &discovered)
	if _, ok := discovered["items"]; !ok {
		t.Fatal("discovery does not expose items")
	}
}

func TestUpgradeCannotDowngradeOrExposeExistingSecret(t *testing.T) {
	m, db := testManager(t)
	ctx := context.Background()
	manifest := fixture("example.webhook")
	if _, e := m.InstallLocal(ctx, pack(t, manifest)); e != nil {
		t.Fatal(e)
	}
	yes := true
	if _, e := m.Update(ctx, manifest.ID, &yes, map[string]any{"token": "keep-private"}); e != nil {
		t.Fatal(e)
	}
	var before string
	db.QueryRow(`SELECT value_json FROM app_settings WHERE key='plugins.registry'`).Scan(&before)
	manifest.Version = "2.0.0"
	field := manifest.ConfigSchema.Properties["token"]
	field.Format = ""
	manifest.ConfigSchema.Properties["token"] = field
	if _, e := m.InstallLocal(ctx, pack(t, manifest)); e == nil {
		t.Fatal("secret field privacy downgrade accepted")
	}
	var after string
	db.QueryRow(`SELECT value_json FROM app_settings WHERE key='plugins.registry'`).Scan(&after)
	if before != after || strings.Contains(after, "keep-private") {
		t.Fatal("failed privacy downgrade changed saved configuration")
	}
	active, e := m.Active(ctx)
	if e != nil || active.Manifest.Version != "1.0.0" || active.Config["token"] != "keep-private" {
		t.Fatal("failed upgrade lost active version", e)
	}
}

func TestCatalogHTTPPresentationIsInstallableWithoutChangingIndexContract(t *testing.T) {
	m, _ := testManager(t)
	entry := Listing{Manifest: fixture("example.webhook"), SHA256: strings.Repeat("0", 64), URL: "https://example.com/plugin.hlx"}
	if e := settings.SetValues(context.Background(), m.gate, settings.Options{}, map[string]any{catalogKey: CatalogResult{APIVersion: 1, Plugins: []Listing{entry}}}); e != nil {
		t.Fatal(e)
	}
	mux := http.NewServeMux()
	m.Register(mux)
	for _, endpoint := range []string{"/api/v1/plugins", "/api/v1/plugins/discover"} {
		r := httptest.NewRecorder()
		mux.ServeHTTP(r, httptest.NewRequest("GET", endpoint, nil))
		var out map[string]any
		if e := json.Unmarshal(r.Body.Bytes(), &out); e != nil {
			t.Fatal(e)
		}
		key := "catalog"
		if strings.HasSuffix(endpoint, "discover") {
			key = "items"
		}
		card := out[key].([]any)[0].(map[string]any)
		if card["compatible"] != true || card["trust"] != "unsigned" || card["configured"] != false || card["enabled"] != false {
			t.Fatal("catalog cannot be installed in UI", card)
		}
	}
}

func TestUpgradeCannotExpandEndpointPermissions(t *testing.T) {
	for _, withSecret := range []bool{false, true} {
		for _, change := range []string{"endpoint", "domains"} {
			t.Run(fmt.Sprint(withSecret, "/", change), func(t *testing.T) {
				m, db := testManager(t)
				ctx := context.Background()
				manifest := fixture("example.webhook")
				if _, e := m.InstallLocal(ctx, pack(t, manifest)); e != nil {
					t.Fatal(e)
				}
				if withSecret {
					yes := true
					if _, e := m.Update(ctx, manifest.ID, &yes, map[string]any{"token": "do-not-forward"}); e != nil {
						t.Fatal(e)
					}
				}
				var before string
				db.QueryRow(`SELECT value_json FROM app_settings WHERE key='plugins.registry'`).Scan(&before)
				manifest.Version = "2.0.0"
				manifest.Domains = append(manifest.Domains, "other.example.com")
				if change == "endpoint" {
					manifest.Endpoint = "https://other.example.com/notify"
				}
				if _, e := m.InstallLocal(ctx, pack(t, manifest)); e == nil {
					t.Fatal("expanded endpoint permission accepted")
				}
				var after string
				db.QueryRow(`SELECT value_json FROM app_settings WHERE key='plugins.registry'`).Scan(&after)
				if before != after {
					t.Fatal("failed permission change altered installed state")
				}
			})
		}
	}
}

func TestBadCredentialDoesNotBlockListDisableOrUninstall(t *testing.T) {
	m, db := testManager(t)
	ctx := context.Background()
	for _, id := range []string{"example.broken", "example.healthy"} {
		if _, e := m.InstallLocal(ctx, pack(t, fixture(id))); e != nil {
			t.Fatal(e)
		}
	}
	yes := true
	if _, e := m.Update(ctx, "example.broken", &yes, map[string]any{"token": "do-not-expose"}); e != nil {
		t.Fatal(e)
	}
	entries, e := readRegistry(ctx, db)
	if e != nil {
		t.Fatal(e)
	}
	entries[0].Config["token"] = "enc1:broken:ciphertext"
	if e = settings.SetValues(ctx, m.gate, settings.Options{}, map[string]any{registryKey: entries}); e != nil {
		t.Fatal(e)
	}
	listed, e := m.List(ctx)
	if e != nil || len(listed) != 2 {
		t.Fatal("broken credential blocked list", e)
	}
	b, _ := json.Marshal(listed)
	var out []map[string]any
	json.Unmarshal(b, &out)
	if out[0]["configured"] != false || out[0]["error"] == nil || strings.Contains(string(b), "enc1:") || strings.Contains(string(b), "do-not-expose") {
		t.Fatal("bad credential not safely isolated", string(b))
	}
	mux := http.NewServeMux()
	m.Register(mux)
	r := httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("GET", "/api/v1/plugins", nil))
	if r.Code != 200 {
		t.Fatal("management page unavailable", r.Body.String())
	}
	no := false
	if _, e = m.Update(ctx, "example.broken", &no, nil); e != nil {
		t.Fatal("bad credential blocked disabling", e)
	}
	if e = m.Remove(ctx, "example.broken"); e != nil {
		t.Fatal("bad credential blocked uninstall", e)
	}
	listed, e = m.List(ctx)
	if e != nil || len(listed) != 1 || listed[0].Manifest.ID != "example.healthy" {
		t.Fatal("healthy plugin lost", e)
	}
}

func TestBuiltinRouteDispatchesByIDAndRejectsUnknown(t *testing.T) {
	var flipped []string
	m := New(testManagerDB(t), nil, t.TempDir(), Options{
		BuiltinList: func(context.Context) (any, error) {
			return []map[string]any{
				{"id": "core.epic_free", "name": "Epic 免费游戏", "enabled": true, "running": true},
				{"id": "core.humble_choice", "name": "Humble Choice 每月合集", "enabled": true, "running": false},
			}, nil
		},
		BuiltinSet: func(_ context.Context, id string, enabled bool) error {
			if id == "core.unknown" {
				return errors.New("built-in feature is unknown")
			}
			flipped = append(flipped, id+"="+strconv.FormatBool(enabled))
			return nil
		},
	})
	m.gate = store.NewWriteGate(m.db, filepath.Join(t.TempDir(), "gate.db"))
	mux := http.NewServeMux()
	m.Register(mux)

	r := httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("PUT", "/api/v1/plugins/builtin/core.humble_choice", strings.NewReader(`{"enabled":false}`)))
	if r.Code != 200 || len(flipped) != 1 || flipped[0] != "core.humble_choice=false" {
		t.Fatalf("humble flip=%d %v %s", r.Code, flipped, r.Body.String())
	}
	if !strings.Contains(r.Body.String(), "Humble Choice 每月合集") {
		t.Fatalf("builtin list lost name field: %s", r.Body.String())
	}
	r = httptest.NewRecorder()
	mux.ServeHTTP(r, httptest.NewRequest("PUT", "/api/v1/plugins/builtin/core.unknown", strings.NewReader(`{"enabled":false}`)))
	if r.Code != 400 {
		t.Fatalf("unknown builtin status=%d", r.Code)
	}
}

func testManagerDB(t *testing.T) *sql.DB {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "test.db")
	db, e := sql.Open("sqlite", path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if _, e = db.Exec(`CREATE TABLE app_settings(key TEXT PRIMARY KEY,value_json TEXT,updated_at TEXT)`); e != nil {
		t.Fatal(e)
	}
	return db
}
