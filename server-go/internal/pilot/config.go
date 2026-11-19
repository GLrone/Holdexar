package pilot

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/url"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

const (
	enabledKey   = "pilot.llm.enabled"
	modelKey     = "pilot.llm.model"
	capKey       = "pilot.llm.monthly_cap"
	providersKey = "pilot.llm.providers"
	activeKey    = "pilot.llm.active"
	toolLimitKey = "pilot.ctx.tool_content_limit"

	ToolContentLimitDefault = 3000
	ToolContentLimitMin     = 500
	ToolContentLimitMax     = 20000
	contextWindowMax        = 2000000
	legacyDefaultCap        = 500000
)

type Provider struct {
	ID             string   `json:"id"`
	Name           string   `json:"name"`
	Protocol       string   `json:"protocol"`
	BaseURL        string   `json:"base_url"`
	Models         []string `json:"models"`
	ModelsDisabled []string `json:"models_disabled"`
	ContextWindow  int      `json:"context_window"`
	KeyOrigin      string   `json:"key_origin"`
	HasKey         bool     `json:"has_key"`
}

type ConfigPayload struct {
	Protocol            string     `json:"protocol"`
	Enabled             bool       `json:"enabled"`
	BaseURL             string     `json:"base_url"`
	Model               string     `json:"model"`
	Models              []string   `json:"models"`
	ModelsDisabled      []string   `json:"models_disabled"`
	HasAPIKey           bool       `json:"has_api_key"`
	MonthlyCap          int        `json:"monthly_cap"`
	ContextWindow       *int       `json:"context_window"`
	ContextWindowSource string     `json:"context_window_source"`
	ContextWindowAuto   *int       `json:"context_window_auto"`
	ToolContentLimit    int        `json:"tool_content_limit"`
	UsageInp            int        `json:"usage_inp"`
	UsageOut            int        `json:"usage_out"`
	UsageCalls          int        `json:"usage_calls"`
	UsageTotal          int        `json:"usage_total"`
	Active              string     `json:"active"`
	Providers           []Provider `json:"providers"`
}

type kvStore struct {
	get       func(key string) (json.RawMessage, bool)
	getSecret func(key string) (string, bool)
	set       func(ctx context.Context, values map[string]json.RawMessage, secrets map[string]string) error
}

func providerKeyName(pid string) string { return "pilot.llm.provider_key." + pid }

func usageKey() string { return fmt.Sprintf("pilot.usage.%s", time.Now().Format("200601")) }

func cleanProvider(raw map[string]any) Provider {
	protocol := stringOf(raw["protocol"])
	if protocol == "" {
		protocol = "openai"
	}
	legal := false
	for _, p := range Protocols {
		if p == protocol {
			legal = true
		}
	}
	if !legal {
		protocol = "openai"
	}
	id := stringOf(raw["id"])
	if id == "" {
		id = newProviderID()
	}
	name := clip(strings.TrimSpace(stringOf(raw["name"])), 60)
	return Provider{
		ID: id, Name: name, Protocol: protocol,
		BaseURL:        strings.TrimSpace(stringOf(raw["base_url"])),
		Models:         modelsOf(raw["models"]),
		ModelsDisabled: modelsOf(raw["models_disabled"]),
		ContextWindow:  cleanWindow(raw["context_window"]),
		KeyOrigin:      strings.TrimSpace(stringOf(raw["key_origin"])),
	}
}

func originKey(rawURL string) string {
	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return ""
	}
	scheme := strings.ToLower(u.Scheme)
	if scheme != "http" && scheme != "https" {
		return ""
	}
	host := strings.ToLower(u.Hostname())
	if host == "" {
		return ""
	}
	port := u.Port()
	if port == "" {
		if scheme == "https" {
			port = "443"
		} else {
			port = "80"
		}
	} else {
		p, err := strconv.Atoi(port)
		if err != nil || p < 1 || p > 65535 {
			return ""
		}
		port = strconv.Itoa(p)
	}
	authority := host
	if strings.Contains(host, ":") {
		authority = "[" + host + "]"
	}
	return scheme + "://" + authority + ":" + port
}

func sameOrigin(a, b string) bool {
	oa := originKey(a)
	return oa != "" && oa == originKey(b)
}

func providerOrigin(p Provider) string {
	return originKey(EffectiveBaseURL(p.Protocol, p.BaseURL))
}

func keyOriginMatches(p Provider) bool {
	return p.KeyOrigin != "" && p.KeyOrigin == providerOrigin(p)
}

func newProviderID() string {
	buf := make([]byte, 6)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

func modelsOf(raw any) []string {
	list, ok := raw.([]any)
	if !ok {
		return nil
	}
	out := []string{}
	for _, item := range list {
		if text, ok := item.(string); ok && strings.TrimSpace(text) != "" {
			out = append(out, text)
			if len(out) >= 50 {
				break
			}
		}
	}
	return out
}

func cleanWindow(raw any) int {
	window := jsonInt(raw)
	if window <= 0 || window > contextWindowMax {
		return 0
	}
	return window
}

func cleanToolLimit(raw any) int {
	value := jsonInt(raw)
	if value <= 0 {
		return ToolContentLimitDefault
	}
	if value < ToolContentLimitMin {
		return ToolContentLimitMin
	}
	if value > ToolContentLimitMax {
		return ToolContentLimitMax
	}
	return value
}

func resolveWindow(manual, auto int) (int, string) {
	if manual > 0 {
		return manual, "manual"
	}
	if auto > 0 {
		return auto, "auto"
	}
	return FallbackContextWindow, "fallback"
}

func (s *Service) LoadProviders() []Provider {
	raw, _ := s.kvGet(providersKey)
	var list []map[string]any
	if raw != nil {
		_ = json.Unmarshal(raw, &list)
	}
	out := make([]Provider, 0, len(list))
	for _, item := range list {
		provider := cleanProvider(item)
		provider.HasKey = s.hasProviderKey(provider)
		out = append(out, provider)
	}
	return out
}

func (s *Service) hasStoredKey(pid string) bool {
	if raw, ok := s.kvGet(providerKeyName(pid)); ok {
		trimmed := strings.TrimSpace(string(raw))
		if trimmed != "" && trimmed != `""` && trimmed != "null" {
			return true
		}
	}
	if secret, ok := s.kvGetSecret(providerKeyName(pid)); ok && secret != "" {
		return true
	}
	return false
}

func (s *Service) hasProviderKey(p Provider) bool {
	return s.hasStoredKey(p.ID) && keyOriginMatches(p)
}

// ProviderKey 仅当密钥对应的服务端源站与当前配置仍一致时才返回明文密钥（fail-closed）。
func (s *Service) ProviderKey(pid string) string {
	if pid == "" {
		return ""
	}
	for _, p := range s.LoadProviders() {
		if p.ID == pid {
			if !keyOriginMatches(p) {
				return ""
			}
			key, _ := s.kvGetSecret(providerKeyName(pid))
			return key
		}
	}
	return ""
}

func (s *Service) LoadConfig() ConfigPayload {
	providers := s.LoadProviders()
	activeID := s.kvGetString(activeKey)
	var active *Provider
	for i := range providers {
		if providers[i].ID == activeID {
			active = &providers[i]
			break
		}
	}
	if active == nil && len(providers) > 0 {
		active = &providers[0]
	}
	model := s.kvGetString(modelKey)
	hasKey := false
	if active != nil {
		hasKey = s.hasProviderKey(*active)
	}
	storedWindow := 0
	autoWindow := 0
	if active != nil {
		storedWindow = active.ContextWindow
	}
	if model != "" {
		autoWindow = GuessContextWindow(model)
	}
	window, source := resolveWindow(storedWindow, autoWindow)
	cap := s.monthlyCap()
	usage := s.UsageMonth()
	enabled := false
	if raw, ok := s.kvGet(enabledKey); ok {
		_ = json.Unmarshal(raw, &enabled)
	}
	toolLimit := ToolContentLimitDefault
	if raw, ok := s.kvGet(toolLimitKey); ok {
		var value any
		_ = json.Unmarshal(raw, &value)
		toolLimit = cleanToolLimit(value)
	}
	payload := ConfigPayload{
		Enabled: enabled, Model: model,
		MonthlyCap: cap, ContextWindowAuto: intPtr(autoWindow),
		ToolContentLimit: toolLimit, Active: activeID,
		UsageInp: usage["inp"], UsageOut: usage["out"],
		UsageCalls: usage["calls"], UsageTotal: usage["total"],
		Providers: providers,
	}
	if active != nil {
		payload.Protocol = active.Protocol
		payload.BaseURL = active.BaseURL
		payload.Models = active.Models
		payload.ModelsDisabled = active.ModelsDisabled
		payload.HasAPIKey = hasKey
	}
	if window > 0 {
		payload.ContextWindow = intPtr(window)
		payload.ContextWindowSource = source
	}
	return payload
}

func intPtr(v int) *int { return &v }

func (s *Service) monthlyCap() int {
	raw, ok := s.kvGet(capKey)
	if !ok {
		return 0
	}
	var value any
	_ = json.Unmarshal(raw, &value)
	cap := jsonInt(value)
	if cap < 0 {
		return 0
	}
	if cap == legacyDefaultCap {
		// legacyDefaultCap 视为未显式设限，读时重置为 0。
		s.kvSetRaw(capKey, jsonNumber0())
		return 0
	}
	return cap
}

func (s *Service) UsageMonth() map[string]int {
	out := map[string]int{"inp": 0, "out": 0, "calls": 0, "total": 0}
	raw, ok := s.kvGet(usageKey())
	if !ok {
		return out
	}
	var data map[string]any
	if json.Unmarshal(raw, &data) == nil {
		out["inp"] = jsonInt(data["inp"])
		out["out"] = jsonInt(data["out"])
		out["calls"] = jsonInt(data["calls"])
	} else {
		var total any
		_ = json.Unmarshal(raw, &total)
		out["inp"] = jsonInt(total)
	}
	out["total"] = out["inp"] + out["out"]
	return out
}

const usageUpsertSQL = `
INSERT INTO app_settings(key,value_json,updated_at)
VALUES(?1, json_object('inp', ?2, 'out', ?3, 'calls', 1), ?4)
ON CONFLICT(key) DO UPDATE SET
  updated_at=excluded.updated_at,
  value_json = CASE WHEN json_type(app_settings.value_json)='object'
    THEN json_set(app_settings.value_json,
      '$.inp',   COALESCE(json_extract(app_settings.value_json,'$.inp'),0)+?2,
      '$.out',   COALESCE(json_extract(app_settings.value_json,'$.out'),0)+?3,
      '$.calls', COALESCE(json_extract(app_settings.value_json,'$.calls'),0)+1)
    ELSE json_object('inp', COALESCE(CAST(app_settings.value_json AS INTEGER),0)+?2, 'out', ?3, 'calls', 1)
  END`

func (s *Service) AddUsage(ctx context.Context, inp, outN int) {
	inp = maxInt(inp, 0)
	outN = maxInt(outN, 0)
	if inp == 0 && outN == 0 || s.deps.DB == nil {
		return
	}
	key, stamp := usageKey(), usageStamp()
	if s.gate != nil {
		_ = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			_, err := conn.ExecContext(ctx, usageUpsertSQL, key, inp, outN, stamp)
			return err
		})
		return
	}
	_, _ = s.deps.DB.ExecContext(ctx, usageUpsertSQL, key, inp, outN, stamp)
}

func usageStamp() string {
	return time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func (c *ConfigPayload) LlmReady() bool {
	return c.Enabled && c.BaseURL != "" && c.Model != ""
}

func (s *Service) OverCap() bool {
	cfg := s.LoadConfig()
	return cfg.MonthlyCap > 0 && cfg.UsageTotal >= cfg.MonthlyCap
}

func (s *Service) SaveConfig(ctx context.Context, update map[string]any) error {
	{
		values := map[string]json.RawMessage{}
		secrets := map[string]string{}
		providers := s.LoadProviders()
		activeID := s.kvGetString(activeKey)
		selectedID := stringOf(update["active"])
		var selected *Provider
		for i := range providers {
			if providers[i].ID == selectedID {
				selected = &providers[i]
			}
		}
		if selected != nil {
			activeID = selected.ID
			values[activeKey] = mustJSON(activeID)
		}
		hasPatch := false
		for _, key := range []string{"protocol", "base_url", "models", "models_disabled", "context_window"} {
			if _, ok := update[key]; ok {
				hasPatch = true
			}
		}
		_, hasAPIKey := update["api_key"]
		if hasPatch || hasAPIKey || selected != nil {
			if len(providers) == 0 {
				providers = []Provider{cleanProvider(map[string]any{"name": "默认接入"})}
			}
			targetIdx := -1
			for i := range providers {
				if providers[i].ID == activeID {
					targetIdx = i
					break
				}
			}
			if targetIdx < 0 {
				targetIdx = 0
			}
			prev := providers[targetIdx]
			merged := mergeProvider(prev, update)
			if hasAPIKey {
				keyStr := stringOf(update["api_key"])
				if keyStr != "" {
					merged.KeyOrigin = providerOrigin(merged)
				} else {
					merged.KeyOrigin = ""
				}
				secrets[providerKeyName(merged.ID)] = keyStr
			} else if providerOrigin(merged) != prev.KeyOrigin {
				merged.KeyOrigin = ""
				secrets[providerKeyName(merged.ID)] = ""
			}
			providers[targetIdx] = merged
			model := s.kvGetString(modelKey)
			values[providersKey] = mustJSON(providers)
			values[activeKey] = mustJSON(merged.ID)
			values[modelKey] = mustJSON(activeModel(merged, model))
		}
		if v, ok := update["enabled"]; ok {
			values[enabledKey] = mustJSON(truthy(v))
		}
		if v, ok := update["model"]; ok {
			values[modelKey] = mustJSON(strings.TrimSpace(stringOf(v)))
		}
		if v, ok := update["monthly_cap"]; ok {
			values[capKey] = mustJSON(maxInt(jsonInt(v), 0))
		}
		if v, ok := update["tool_content_limit"]; ok {
			values[toolLimitKey] = mustJSON(cleanToolLimit(v))
		}
		return s.kvSet(ctx, values, secrets)
	}
}

func (s *Service) CreateProvider(ctx context.Context, update map[string]any) (Provider, error) {
	var created Provider
	err := func() error {
		raw := map[string]any{}
		for key, value := range update {
			raw[key] = value
		}
		providers := s.LoadProviders()
		delete(raw, "id")
		delete(raw, "key_origin")
		provider := cleanProvider(raw)
		if key := stringOf(update["api_key"]); key != "" {
			provider.KeyOrigin = providerOrigin(provider)
		}
		list := append(providers, provider)
		values := map[string]json.RawMessage{providersKey: mustJSON(list)}
		if len(providers) == 0 {
			model := s.kvGetString(modelKey)
			values[activeKey] = mustJSON(provider.ID)
			values[modelKey] = mustJSON(activeModel(provider, model))
		}
		secrets := map[string]string{}
		if key := stringOf(update["api_key"]); key != "" {
			secrets[providerKeyName(provider.ID)] = key
		}
		created = provider
		return s.kvSet(ctx, values, secrets)
	}()
	created.HasKey = created.ID != "" && s.hasProviderKey(created)
	return created, err
}

// UpdateProvider 更新供应商（密钥缺省不改、空值清空、空地址不覆盖已生效地址）。
func (s *Service) UpdateProvider(ctx context.Context, pid string, update map[string]any) (Provider, error) {
	var updated Provider
	err := func() error {
		providers := s.LoadProviders()
		idx := -1
		for i := range providers {
			if providers[i].ID == pid {
				idx = i
				break
			}
		}
		if idx < 0 {
			return fmt.Errorf("provider not found: %s", pid)
		}
		target := providers[idx]
		merged := mergeProvider(target, update)
		if strings.TrimSpace(stringOf(update["base_url"])) == "" && target.BaseURL != "" {
			merged.BaseURL = target.BaseURL
		}
		secrets := map[string]string{}
		if key, ok := update["api_key"]; ok {
			keyStr := stringOf(key)
			if keyStr != "" {
				merged.KeyOrigin = providerOrigin(merged)
			} else {
				merged.KeyOrigin = ""
			}
			secrets[providerKeyName(pid)] = keyStr
		} else if providerOrigin(merged) != target.KeyOrigin {
			merged.KeyOrigin = ""
			secrets[providerKeyName(pid)] = ""
		}
		providers[idx] = merged
		values := map[string]json.RawMessage{providersKey: mustJSON(providers)}
		activeID := s.kvGetString(activeKey)
		if pid == activeID || activeID == "" {
			values[modelKey] = mustJSON(activeModel(merged, s.kvGetString(modelKey)))
		}
		updated = merged
		return s.kvSet(ctx, values, secrets)
	}()
	updated.HasKey = s.hasProviderKey(updated)
	return updated, err
}

func (s *Service) DeleteProvider(ctx context.Context, pid string) error {
	{
		providers := s.LoadProviders()
		remaining := []Provider{}
		for _, p := range providers {
			if p.ID != pid {
				remaining = append(remaining, p)
			}
		}
		values := map[string]json.RawMessage{
			providersKey: mustJSON(remaining),
		}
		if s.kvGetString(activeKey) == pid {
			nextActive := ""
			if len(remaining) > 0 {
				nextActive = remaining[0].ID
			} else {
				values[modelKey] = mustJSON("")
			}
			values[activeKey] = mustJSON(nextActive)
		}
		return s.kvSet(ctx, values, map[string]string{providerKeyName(pid): ""})
	}
}

func mergeProvider(target Provider, update map[string]any) Provider {
	raw := map[string]any{
		"id": target.ID, "name": target.Name, "protocol": target.Protocol,
		"base_url": target.BaseURL, "models": target.Models,
		"models_disabled": target.ModelsDisabled, "key_origin": target.KeyOrigin,
	}
	if target.ContextWindow > 0 {
		raw["context_window"] = target.ContextWindow
	}
	if v, ok := update["name"]; ok {
		raw["name"] = v
	}
	if v, ok := update["protocol"]; ok {
		raw["protocol"] = v
	}
	if v, ok := update["base_url"]; ok {
		raw["base_url"] = v
	}
	if v, ok := update["models"]; ok {
		raw["models"] = v
	}
	if v, ok := update["models_disabled"]; ok {
		raw["models_disabled"] = v
	}
	if v, ok := update["context_window"]; ok {
		raw["context_window"] = v
	}
	return cleanProvider(raw)
}

func activeModel(provider Provider, model string) string {
	disabled := map[string]bool{}
	for _, m := range provider.ModelsDisabled {
		disabled[m] = true
	}
	enabled := []string{}
	for _, m := range provider.Models {
		if !disabled[m] {
			enabled = append(enabled, m)
		}
	}
	if len(enabled) > 0 && !containsAny(model, enabled) {
		return enabled[0]
	}
	return model
}

func truthy(v any) bool {
	switch value := v.(type) {
	case bool:
		return value
	case string:
		return value == "true"
	}
	return false
}

func mustJSON(v any) json.RawMessage {
	payload, _ := json.Marshal(v)
	return payload
}

func jsonNumber0() json.RawMessage { return json.RawMessage("0") }

func (s *Service) kvGet(key string) (json.RawMessage, bool) {
	if s.deps.KVGet == nil {
		return nil, false
	}
	return s.deps.KVGet(key)
}

func (s *Service) kvGetSecret(key string) (string, bool) {
	if s.deps.KVGetSecret == nil {
		return "", false
	}
	return s.deps.KVGetSecret(key)
}

func (s *Service) kvSet(ctx context.Context, values map[string]json.RawMessage, secrets map[string]string) error {
	if s.deps.KVSet == nil {
		return fmt.Errorf("kv 未接线")
	}
	return s.deps.KVSet(ctx, values, secrets)
}

func (s *Service) kvSetRaw(key string, payload []byte) {
	if s.deps.KVSet == nil {
		return
	}
	_ = s.deps.KVSet(context.Background(), map[string]json.RawMessage{key: payload}, nil)
}

func (s *Service) kvGetString(key string) string {
	raw, ok := s.kvGet(key)
	if !ok {
		return ""
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return strings.TrimSpace(text)
	}
	return ""
}
