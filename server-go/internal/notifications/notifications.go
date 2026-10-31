// Package notifications：通知域只读路径（偏好投影 / 候选账本 / 投递指标 / 事实增量）。
// 写端点（PUT prefs / POST test）与投递链（dispatch / 认领 / 结算）未迁移，见 deferred 清单。
// 候选状态是投递状态不是事实状态：pending/suppressed 未投递，sending/unknown
// 结果未确认（禁止自动重发），delivered 已投递，failed 失败（可重试判定见 stats）。
package notifications

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"strings"
)

// prefsKey 用户通知偏好的 KV 键（app_settings.value_json，结构对齐 Python DEFAULT_PREFS）。
const prefsKey = "notifications.prefs"

// maxAttempts 单条候选投递次数上限；超过即永久失败。
const maxAttempts = 3

// statusUnknown 投递结果未知（进程中断时可能已发出，不自动重发）。
const statusUnknown = "unknown"

// Register 挂载 notifications 只读路由；PUT prefs / POST test 未迁移。
func Register(mux *http.ServeMux, db *sql.DB) {
	h := &handler{db: db}
	mux.HandleFunc("GET /api/v1/notifications/prefs", h.getPrefs)
	mux.HandleFunc("GET /api/v1/notifications/candidates", h.listCandidates)
	mux.HandleFunc("GET /api/v1/notifications/stats", h.getStats)
	mux.HandleFunc("GET /api/v1/notifications/facts", h.listFacts)
	for _, p := range []string{
		"/api/v1/notifications/prefs",
		"/api/v1/notifications/candidates",
		"/api/v1/notifications/stats",
		"/api/v1/notifications/facts",
	} {
		mux.HandleFunc(p, methodNotAllowed("GET"))
	}
}

type handler struct {
	db *sql.DB
}

// getPrefs 通知偏好投影 + SMTP 出口状态 + 最近一次投递结果（设置页一屏）。
func (h *handler) getPrefs(w http.ResponseWriter, r *http.Request) {
	prefs, err := h.readPrefs(r)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	smtp, err := h.smtpView(r)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	last, err := h.lastDelivery(r)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	writeJSON(w, http.StatusOK, prefsView(prefs, smtp, last))
}

// prefsView 偏好投影；字段值 = 库内补丁值优先，缺省用 Python 默认（逐字段覆盖语义）。
func prefsView(p map[string]any, smtp *orderedObj, last *orderedObj) *orderedObj {
	v := newOrderedObj()
	v.set("enabled", patched(p, "enabled", false))
	v.set("quietEnabled", patched(p, "quietEnabled", false))
	v.set("quietStart", patched(p, "quietStart", "23:00"))
	v.set("quietEnd", patched(p, "quietEnd", "08:00"))
	v.set("includeDetails", patched(p, "includeDetails", true))
	v.set("maxAttempts", maxAttempts)
	v.set("manualRegion", patched(p, "manualRegion", nil))
	v.set("digestTime", patched(p, "digestTime", "08:00"))
	v.set("digestWeekday", patched(p, "digestWeekday", json.Number("0")))
	v.set("featured", featuredView(p))
	v.set("region", regionView(p))
	v.set("types", typeEntries(p))
	v.set("categories", categoryEntries(p))
	v.set("smtp", smtp)
	v.set("lastDelivery", last)
	return v
}

// patched 补丁值优先的标量取值（Python _merge_dict 的标量分支）。
func patched(p map[string]any, key string, def any) any {
	if raw, ok := p[key]; ok {
		return raw
	}
	return def
}

// featuredDefaultKeys 精选配置的键序（对齐 Python DEFAULT_FEATURED 的插入序）。
var featuredDefaultKeys = []struct {
	key string
	def any
}{
	{"scope", []any{"followed", "wishlist"}}, {"minDiscount", json.Number("50")},
	{"minSavings", nil}, {"maxPrice", nil}, {"lowMode", "any"},
	{"allowFullPrice", false}, {"minPositiveRate", nil}, {"minReviews", nil},
	{"missingReviews", "include"}, {"includeOwned", false}, {"includeIgnored", false},
	{"sort", "discount"}, {"windowDays", nil},
}

// featuredView 精选配置 = 默认骨架 + 库内补丁逐键覆盖。
func featuredView(p map[string]any) *orderedObj {
	patch, _ := p["featured"].(map[string]any)
	v := newOrderedObj()
	for _, kv := range featuredDefaultKeys {
		val := any(kv.def)
		if raw, ok := patch[kv.key]; ok {
			val = raw
		}
		v.set(kv.key, val)
	}
	return v
}

// regionView 手动通知地区解析：大写区码 + 币种来源（观测缺席恒走地区映射）。
func regionView(p map[string]any) *orderedObj {
	region := ""
	if s, ok := p["manualRegion"].(string); ok {
		region = strings.ToUpper(strings.TrimSpace(s))
	}
	if region == "" {
		return kvObj("region", nil, "source", "unknown", "currency", nil, "currencySource", "unknown")
	}
	currency, ok := regionToCurrency[region]
	if !ok {
		// 写入侧已把 manualRegion 限定在地区清单内，正常库不可达；异常库按 500 出口
		return nil
	}
	return kvObj("region", region, "source", "manual", "currency", currency, "currencySource", "region_mapping")
}

// typeEntry 单个通知类型的偏好投影。
type typeEntry struct {
	Key       string `json:"key"`
	Label     string `json:"label"`
	Category  string `json:"category"`
	Enabled   any    `json:"enabled"`
	Frequency any    `json:"frequency"`
}

// typeEntries 全部通知类型（键序对齐 Python TYPE_LABELS 的插入序）。
func typeEntries(p map[string]any) []typeEntry {
	patch, _ := p["types"].(map[string]any)
	out := make([]typeEntry, 0, len(typeLabels))
	for _, t := range typeLabels {
		cfg, _ := patch[t.key].(map[string]any)
		enabled, frequency := any(true), any("immediate")
		if raw, ok := cfg["enabled"]; ok {
			enabled = raw
		}
		if raw, ok := cfg["frequency"]; ok {
			frequency = raw
		}
		out = append(out, typeEntry{Key: t.key, Label: t.label, Category: t.category,
			Enabled: enabled, Frequency: frequency})
	}
	return out
}

// categoryEntry 单个用户类别的偏好投影。
type categoryEntry struct {
	Key        string `json:"key"`
	Label      string `json:"label"`
	Enabled    any    `json:"enabled"`
	EventTypes int    `json:"eventTypes"`
}

// categoryEntries 全部用户类别（键序对齐 Python CATEGORIES 元组序）。
func categoryEntries(p map[string]any) []categoryEntry {
	patch, _ := p["categories"].(map[string]any)
	out := make([]categoryEntry, 0, len(categoryLabels))
	for _, c := range categoryLabels {
		enabled := any(true)
		if raw, ok := patch[c.key]; ok {
			enabled = raw
		}
		out = append(out, categoryEntry{Key: c.key, Label: c.label, Enabled: enabled,
			EventTypes: c.eventTypes})
	}
	return out
}

// lastDelivery 最近一次投递结果：三种已终态候选中 updated_at 最新的一行；
// 无终态行回 nil（字段整体缺省为 null）。
func (h *handler) lastDelivery(r *http.Request) (*orderedObj, error) {
	var status, deliveredAt, updatedAt, reason, lastError sql.NullString
	var attempts sql.NullInt64
	err := h.db.QueryRowContext(r.Context(),
		"SELECT status, CAST(delivered_at AS TEXT), CAST(updated_at AS TEXT),"+
			" attempts, reason, last_error FROM notification_candidates"+
			" WHERE status IN ('delivered', 'failed', 'unknown')"+
			" ORDER BY updated_at DESC LIMIT 1").Scan(&status, &deliveredAt, &updatedAt,
		&attempts, &reason, &lastError)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	at := isoformatPtr(deliveredAt)
	if at == nil {
		at = isoformatPtr(updatedAt)
	}
	v := newOrderedObj()
	v.set("status", nullTextPtr(status))
	v.set("at", at)
	v.set("attempts", nullIntPtr(attempts))
	v.set("reason", nullTextPtr(reason))
	v.set("retryable", isRetryable(lastError.String))
	return v, nil
}

// readPrefs 读偏好补丁：value_json 解析为无序映射（仅作键值查表，不再输出）。
func (h *handler) readPrefs(r *http.Request) (map[string]any, error) {
	var raw sql.NullString
	err := h.db.QueryRowContext(r.Context(),
		"SELECT value_json FROM app_settings WHERE key = ?", prefsKey).Scan(&raw)
	if err == sql.ErrNoRows {
		return map[string]any{}, nil
	}
	if err != nil {
		return nil, err
	}
	if !raw.Valid || raw.String == "" {
		return map[string]any{}, nil
	}
	dec := json.NewDecoder(strings.NewReader(raw.String))
	dec.UseNumber()
	var value map[string]any
	if err := dec.Decode(&value); err != nil {
		return nil, err
	}
	return value, nil
}

// settingValue 读单个设置的 value_json；行缺席回 (def, false)，NULL 回 (nil, true)。
func (h *handler) settingValue(ctx context.Context, key string) (any, bool, error) {
	var raw sql.NullString
	err := h.db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key = ?", key).Scan(&raw)
	if err == sql.ErrNoRows {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	if !raw.Valid || raw.String == "" {
		return nil, true, nil
	}
	dec := json.NewDecoder(strings.NewReader(raw.String))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		return nil, true, err
	}
	return value, true, nil
}
