package notifications

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
)

func preferenceDefaults() map[string]any {
	categories, types, featured := map[string]any{}, map[string]any{}, map[string]any{}
	for _, v := range categoryLabels {
		categories[v.key] = true
	}
	for _, v := range typeLabels {
		types[v.key] = map[string]any{"enabled": true, "frequency": "immediate"}
	}
	for _, v := range featuredDefaultKeys {
		featured[v.key] = v.def
	}
	return map[string]any{"enabled": false, "categories": categories, "quietEnabled": false, "quietStart": "23:00", "quietEnd": "08:00", "includeDetails": true, "manualRegion": nil, "digestTime": "08:00", "digestWeekday": json.Number("0"), "types": types, "featured": featured}
}
func mergePreferences(base, patch map[string]any) map[string]any {
	result := map[string]any{}
	for k, v := range base {
		if m, ok := v.(map[string]any); ok {
			p, _ := patch[k].(map[string]any)
			result[k] = mergePreferences(m, p)
		} else {
			if p, ok := patch[k]; ok {
				v = p
			}
			result[k] = v
		}
	}
	return result
}
func ValidateFeatured(patch map[string]any) (map[string]any, error) {
	defaults := map[string]any{}
	for _, v := range featuredDefaultKeys {
		defaults[v.key] = v.def
	}
	p := mergePreferences(defaults, patch)
	list, ok := p["scope"].([]any)
	if !ok || len(list) == 0 {
		return nil, fmt.Errorf("请选择有效的精选来源范围")
	}
	for _, v := range list {
		if v != "followed" && v != "wishlist" && v != "owned" && v != "manual" && v != "catalog" {
			return nil, fmt.Errorf("请选择有效的精选来源范围")
		}
	}
	if p["windowDays"] != nil {
		n, e := strconv.ParseFloat(fmt.Sprint(p["windowDays"]), 64)
		if e != nil || (n != 90 && n != 180 && n != 365) {
			return nil, fmt.Errorf("无效的历史比较范围")
		}
	}
	for _, name := range []string{"minDiscount", "minPositiveRate", "minReviews", "minSavings", "maxPrice"} {
		if p[name] == nil {
			continue
		}
		n, e := strconv.ParseFloat(fmt.Sprint(p[name]), 64)
		if e != nil {
			return nil, fmt.Errorf("%s必须是整数", name)
		}
		if math.IsNaN(n) || math.IsInf(n, 0) || n < 0 || math.Trunc(n) != n {
			return nil, fmt.Errorf("%s必须是非负整数", name)
		}
		if (name == "minDiscount" || name == "minPositiveRate") && n > 100 {
			return nil, fmt.Errorf("%s超过允许范围", name)
		}
		p[name] = n
	}
	if p["lowMode"] != "any" && p["lowMode"] != "low" && p["lowMode"] != "new_low" {
		return nil, fmt.Errorf("无效的低价条件")
	}
	if p["missingReviews"] != "include" && p["missingReviews"] != "exclude" {
		return nil, fmt.Errorf("无效的缺失评价处理方式")
	}
	sort := p["sort"]
	if sort != "discount" && sort != "price" && sort != "savings" && sort != "quality" && sort != "followed" && sort != "wishlist" {
		return nil, fmt.Errorf("无效的精选排序方式")
	}
	for _, name := range []string{"allowFullPrice", "includeOwned", "includeIgnored"} {
		if _, ok := p[name].(bool); !ok {
			return nil, fmt.Errorf("%s必须为开关值", name)
		}
	}
	return p, nil
}
func validatePreferences(p map[string]any) error {
	for _, key := range []string{"quietStart", "quietEnd", "digestTime"} {
		if _, ok := hhmm(p[key]); !ok {
			return fmt.Errorf("通知时间须为有效的小时和分钟")
		}
	}
	weekday, ok := p["digestWeekday"].(json.Number)
	if !ok {
		return fmt.Errorf("每周发送日须为0至6")
	}
	n, e := weekday.Int64()
	if e != nil || n < 0 || n > 6 {
		return fmt.Errorf("每周发送日须为0至6")
	}
	types := p["types"].(map[string]any)
	for _, v := range types {
		cfg := v.(map[string]any)
		f := cfg["frequency"]
		if f != "immediate" && f != "daily" && f != "weekly" && f != "off" {
			return fmt.Errorf("不支持的通知频率")
		}
	}
	if p["manualRegion"] != nil {
		region := strings.ToUpper(strings.TrimSpace(fmt.Sprint(p["manualRegion"])))
		if CurrencyForRegion(region) == "" {
			return fmt.Errorf("请选择已支持的通知地区")
		}
		p["manualRegion"] = region
	}
	p["featured"], e = ValidateFeatured(p["featured"].(map[string]any))
	return e
}
func (h *handler) putPrefs(w http.ResponseWriter, r *http.Request) {
	var patch map[string]any
	decoder := json.NewDecoder(r.Body)
	decoder.UseNumber()
	if e := decoder.Decode(&patch); e != nil {
		writeJSON(w, 422, map[string]any{"detail": "请求内容必须是对象"})
		return
	}
	var prefs map[string]any
	var invalid error
	e := h.options.Gate.WithWrite(r.Context(), store.PriorityInteractive, func(conn *sql.Conn) error {
		old, e := LoadPrefs(r.Context(), conn)
		if e != nil {
			return e
		}
		prefs = mergePreferences(mergePreferences(preferenceDefaults(), old), patch)
		if e = validatePreferences(prefs); e != nil {
			invalid = e
			return e
		}
		raw, e := json.Marshal(prefs)
		if e != nil {
			return e
		}
		_, e = conn.ExecContext(r.Context(), `INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, prefsKey, string(raw), time.Now().Format("2006-01-02 15:04:05.000000"))
		return e
	})
	if e != nil {
		status := 500
		if invalid != nil {
			status = 400
		}
		writeJSON(w, status, map[string]any{"detail": e.Error()})
		return
	}
	if h.options.Bus != nil {
		h.options.Bus.Publish("settings.changed", events.KV{Key: "keys", Val: []string{prefsKey}})
	}
	region, e := EffectiveRegion(r.Context(), h.db, "", prefs, nil)
	if e != nil {
		writeJSON(w, 500, map[string]any{"detail": e.Error()})
		return
	}
	view := prefsView(prefs, nil, nil, region)
	result := newOrderedObj()
	result.set("ok", true)
	for _, key := range view.keys {
		if key != "smtp" && key != "lastDelivery" {
			result.set(key, view.vals[key])
		}
	}
	writeJSON(w, 200, result)
}
