//go:build windows

// config.go 领航员 LLM 配置：多供应商账本（settings KV）+ 月度用量台账。
// 语义对齐 pilot/config.py；写路径全部走写闸。
package pilot

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
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

// Provider 供应商记录（账本口径）。
type Provider struct {
	ID             string   `json:"id"`
	Name           string   `json:"name"`
	Protocol       string   `json:"protocol"`
	BaseURL        string   `json:"base_url"`
	Models         []string `json:"models"`
	ModelsDisabled []string `json:"models_disabled"`
	ContextWindow  int      `json:"context_window"`
	// HasKey 密钥在位布尔（不含原文）。
	HasKey bool `json:"has_key"`
}

// ConfigPayload 平面配置（消费方形态）。
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

// kvGet / kvSet / kvGetSecret / kvSetSecret 由 serverd 装配注入
// （settings_service 语义：KV JSON 值 + secretbox 密文）。
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
	}
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

// resolveWindow 手填优先 → 模型名识别 → 宽裕兜底（source 供前端提示）。
func resolveWindow(manual, auto int) (int, string) {
	if manual > 0 {
		return manual, "manual"
	}
	if auto > 0 {
		return auto, "auto"
	}
	return FallbackContextWindow, "fallback"
}

// LoadProviders 供应商清单投影（has_key 布尔，不含密钥原文）。
func (s *Service) LoadProviders() []Provider {
	raw, _ := s.kvGet(providersKey)
	var list []map[string]any
	if raw != nil {
		_ = json.Unmarshal(raw, &list)
	}
	out := make([]Provider, 0, len(list))
	for _, item := range list {
		provider := cleanProvider(item)
		provider.HasKey = s.hasProviderKey(provider.ID)
		out = append(out, provider)
	}
	return out
}

func (s *Service) hasProviderKey(pid string) bool {
	// has_key 布尔只关心「密钥在不在」，不需要解密成功——
	// 解密失败（机器换/钥文件丢）时 Python 侧 default="" 降级但 has_key 仍 true
	_, ok := s.kvGet(providerKeyName(pid))
	return ok
}

// LoadConfig 平面配置（解析自活跃供应商 + 全局开关/上限/台账）。
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
		hasKey = s.hasProviderKey(active.ID)
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
		// 旧默认值不是用户的选择：一次性迁为不限
		s.kvSetRaw(capKey, jsonNumber0())
		return 0
	}
	return cap
}

// UsageMonth 当月用量台账（存量整数旧口径只记合计）。
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

// AddUsage 用量累加：单语句原子自增（不得经写闸——闸内 fn 再开池连接写入
// 会被本事务的 RESERVED 锁挡住，busy_timeout 60s 自锁）。
func (s *Service) AddUsage(ctx context.Context, inp, outN int) {
	inp = maxInt(inp, 0)
	outN = maxInt(outN, 0)
	if inp == 0 && outN == 0 {
		return
	}
	_, _ = s.deps.DB.ExecContext(ctx, `
INSERT INTO app_settings(key,value_json,updated_at)
VALUES(?1, json('{"inp":?2,"out":?3,"calls":1}'), ?4)
ON CONFLICT(key) DO UPDATE SET
  updated_at=excluded.updated_at,
  value_json = CASE WHEN json_type(app_settings.value_json)='object'
    THEN json_set(app_settings.value_json,
      '$.inp',   COALESCE(json_extract(app_settings.value_json,'$.inp'),0)+?2,
      '$.out',   COALESCE(json_extract(app_settings.value_json,'$.out'),0)+?3,
      '$.calls', COALESCE(json_extract(app_settings.value_json,'$.calls'),0)+1)
    ELSE json('{"inp":'||(COALESCE(CAST(app_settings.value_json AS INTEGER),0)+?2)||',"out":'||?3||',"calls":1}')
  END`,
		usageKey(), inp, outN, usageStamp())
}

// usageStamp 与 serverd KVSet 同口径的 updated_at 文本（东八区）。
func usageStamp() string {
	return time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// LlmReady 开关、地址、模型齐备即可用（密钥可选）。
func (c *ConfigPayload) LlmReady() bool {
	return c.Enabled && c.BaseURL != "" && c.Model != ""
}

// OverCap 月度用量超限（0=不限）。
func (s *Service) OverCap() bool {
	cfg := s.LoadConfig()
	return cfg.MonthlyCap > 0 && cfg.UsageTotal >= cfg.MonthlyCap
}

// SaveConfig 平面 API 的一次提交（首次保存创建默认接入）。
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
			merged := mergeProvider(providers[targetIdx], update)
			providers[targetIdx] = merged
			model := s.kvGetString(modelKey)
			values[providersKey] = mustJSON(providers)
			values[activeKey] = mustJSON(merged.ID)
			values[modelKey] = mustJSON(activeModel(merged, model))
			if hasAPIKey {
				secrets[providerKeyName(merged.ID)] = stringOf(update["api_key"])
			}
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

// CreateProvider 新增供应商（清单/密钥/首次激活同一写边界）。
func (s *Service) CreateProvider(ctx context.Context, update map[string]any) (Provider, error) {
	var created Provider
	err := func() error {
		raw := map[string]any{}
		for key, value := range update {
			raw[key] = value
		}
		providers := s.LoadProviders()
		provider := cleanProvider(raw)
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
	created.HasKey = created.ID != "" && s.hasProviderKey(created.ID)
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
		providers[idx] = merged
		values := map[string]json.RawMessage{providersKey: mustJSON(providers)}
		activeID := s.kvGetString(activeKey)
		if pid == activeID || activeID == "" {
			values[modelKey] = mustJSON(activeModel(merged, s.kvGetString(modelKey)))
		}
		secrets := map[string]string{}
		if key, ok := update["api_key"]; ok {
			secrets[providerKeyName(pid)] = stringOf(key)
		}
		updated = merged
		return s.kvSet(ctx, values, secrets)
	}()
	updated.HasKey = s.hasProviderKey(pid)
	return updated, err
}

// DeleteProvider 删除供应商及密钥（删活跃家时交接首家）。
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
			providersKey:         mustJSON(remaining),
			providerKeyName(pid): mustJSON(""),
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
		return s.kvSet(ctx, values, map[string]string{})
	}
}

func mergeProvider(target Provider, update map[string]any) Provider {
	raw := map[string]any{
		"id": target.ID, "name": target.Name, "protocol": target.Protocol,
		"base_url": target.BaseURL, "models": target.Models,
		"models_disabled": target.ModelsDisabled,
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

// activeModel 供应商启用清单内回落首个模型。
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

// kv 桥（serverd 注入 settings_service 语义）。
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
