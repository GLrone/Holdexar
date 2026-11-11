package notifications

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"time"
)

type SettingsQuery interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func LoadPrefs(ctx context.Context, q SettingsQuery) (map[string]any, error) {
	var raw sql.NullString
	e := q.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, prefsKey).Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) || e == nil && (!raw.Valid || raw.String == "null" || raw.String == "") {
		return map[string]any{}, nil
	}
	if e != nil {
		return nil, e
	}
	var prefs map[string]any
	d := json.NewDecoder(strings.NewReader(raw.String))
	d.UseNumber()
	e = d.Decode(&prefs)
	return prefs, e
}
func CategoryFrequency(p map[string]any, eventType string) string {
	types, _ := p["types"].(map[string]any)
	config, _ := types[eventType].(map[string]any)
	v, _ := config["frequency"].(string)
	if v == "" {
		return "immediate"
	}
	return v
}
func hhmm(v any) (int, bool) {
	s, ok := v.(string)
	if !ok {
		return 0, false
	}
	h, m, found := strings.Cut(s, ":")
	if !found {
		return 0, false
	}
	a, e := strconv.Atoi(h)
	b, f := strconv.Atoi(m)
	return a*60 + b, e == nil && f == nil && a >= 0 && a < 24 && b >= 0 && b < 60
}
func InQuietWindow(now time.Time, p map[string]any) bool {
	if !truthy(p["quietEnabled"]) {
		return false
	}
	start := p["quietStart"]
	if start == nil {
		start = "23:00"
	}
	end := p["quietEnd"]
	if end == nil {
		end = "08:00"
	}
	a, ok := hhmm(start)
	b, bok := hhmm(end)
	if !ok || !bok || a == b {
		return false
	}
	n := now.Hour()*60 + now.Minute()
	if a < b {
		return n >= a && n < b
	}
	return n >= a || n < b
}
func DeliveryDue(at time.Time, frequency string, p map[string]any) time.Time {
	if frequency == "immediate" {
		return at
	}
	raw := p["digestTime"]
	if raw == nil {
		raw = "08:00"
	}
	minutes, ok := hhmm(raw)
	if !ok {
		minutes = 480
	}
	due := time.Date(at.Year(), at.Month(), at.Day(), minutes/60, minutes%60, 0, 0, at.Location())
	if frequency == "weekly" {
		weekday := int(pyInt(p["digestWeekday"], 0))
		current := (int(at.Weekday()) + 6) % 7
		due = due.AddDate(0, 0, (weekday-current+7)%7)
	}
	if !due.After(at) {
		days := 1
		if frequency == "weekly" {
			days = 7
		}
		due = due.AddDate(0, 0, days)
	}
	return due
}

type Fact struct {
	ID, CycleID, AppID int64
	Region             *string
	EventType          string
	Payload            map[string]any
	OccurredAt         time.Time
}

func EnqueueFactsTx(ctx context.Context, conn *sql.Conn, sourceKind string, facts []Fact, now time.Time) (int64, error) {
	if len(facts) == 0 {
		return 0, nil
	}
	if sourceKind != "alert_event" && sourceKind != "price_event" && sourceKind != "fact_notice" {
		return 0, fmt.Errorf("不支持的通知事实来源")
	}
	prefs, e := LoadPrefs(ctx, conn)
	if e != nil || !truthy(prefs["enabled"]) {
		return 0, e
	}
	recipient, e := savedRecipient(ctx, conn)
	if e != nil {
		return 0, e
	}
	ids := []int64{}
	for _, f := range facts {
		ids = append(ids, f.ID)
	}
	idsJSON, _ := json.Marshal(ids)
	table, app, region, eventType, at := "alert_events", "appid", "region", "json_extract(details_json,'$.eventType')", "triggered_at"
	if sourceKind == "price_event" {
		table, region, eventType, at = "price_events", "region_code", "event_type", "occurred_at"
	}
	if sourceKind == "fact_notice" {
		table, app, region, at = "fact_notices", "coalesce(json_extract(data,'$.appid'),0)", "NULL", "occurred_at"
		eventType = `CASE source||':'||kind WHEN 'hb_choice:bundle_changed' THEN 'HB_CHOICE' WHEN 'epic_free:free_rotation' THEN 'EPIC_FREE' WHEN 'bundles:discount_pick' THEN 'BUNDLE_DEAL' WHEN 'alerts:proxy_health' THEN 'PROXY_ALERT' WHEN 'alerts:system_alert' THEN 'SYSTEM_ALERT' END`
	}
	rows, e := conn.QueryContext(ctx, `SELECT id,`+app+`,`+region+`,`+eventType+`,CAST(`+at+` AS TEXT) FROM `+table+` WHERE id IN(SELECT value FROM json_each(?))`, string(idsJSON))
	if e != nil {
		return 0, e
	}
	type identity struct {
		appid             int64
		region, event, at sql.NullString
	}
	sources := map[int64]identity{}
	for rows.Next() {
		var id int64
		var v identity
		if e = rows.Scan(&id, &v.appid, &v.region, &v.event, &v.at); e != nil {
			rows.Close()
			return 0, e
		}
		sources[id] = v
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return 0, e
	}
	payloads := []map[string]any{}
	seen := map[int64]bool{}
	for _, f := range facts {
		if seen[f.ID] || !TypeEnabled(prefs, f.EventType) {
			continue
		}
		v, exists := sources[f.ID]
		sameRegion := f.Region == nil && !v.region.Valid || f.Region != nil && v.region.Valid && *f.Region == v.region.String
		if !exists {
			return 0, fmt.Errorf("提醒依据不存在")
		}
		if f.AppID != v.appid || !sameRegion || f.EventType != v.event.String {
			return 0, fmt.Errorf("提醒依据与来源不一致")
		}
		status := "pending"
		if InQuietWindow(now, prefs) {
			status = "suppressed"
		}
		occurred := now
		if v.at.Valid {
			if t, err := parseFactTime(v.at.String); err == nil {
				occurred = t
			}
		}
		payloads = append(payloads, map[string]any{"id": f.ID, "cycle": f.CycleID, "appid": f.AppID, "region": f.Region, "event": f.EventType, "category": CategoryOf(f.EventType), "recipient": recipient, "status": status, "payload": f.Payload, "due": DeliveryDue(occurred, CategoryFrequency(prefs, f.EventType), prefs).Format("2006-01-02 15:04:05.000000")})
		seen[f.ID] = true
	}
	if len(payloads) == 0 {
		return 0, nil
	}
	encoded, e := json.Marshal(payloads)
	if e != nil {
		return 0, e
	}
	result, e := conn.ExecContext(ctx, `INSERT INTO notification_candidates(source_kind,event_id,cycle_id,appid,region_code,event_type,category,recipient,status,attempts,payload_json,due_at,created_at,updated_at)
 SELECT ?,value->>'id',value->>'cycle',value->>'appid',value->>'region',value->>'event',value->>'category',value->>'recipient',value->>'status',0,value->'payload',value->>'due',?,? FROM json_each(?)
 WHERE NOT EXISTS(SELECT 1 FROM notification_candidates c WHERE c.source_kind=? AND c.event_id=value->>'id')
 ON CONFLICT(source_kind,event_id,recipient) DO NOTHING`, sourceKind, now.Format("2006-01-02 15:04:05.000000"), now.Format("2006-01-02 15:04:05.000000"), string(encoded), sourceKind)
	if e != nil {
		return 0, e
	}
	return result.RowsAffected()
}
func parseFactTime(raw string) (time.Time, error) {
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999", time.RFC3339Nano} {
		if t, e := time.Parse(layout, raw); e == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("invalid fact timestamp")
}

func PauseRuleTx(ctx context.Context, conn *sql.Conn, ruleID int64, now time.Time) error {
	_, e := conn.ExecContext(ctx, `UPDATE notification_candidates SET status='suppressed',reason='rule_paused',updated_at=? WHERE source_kind='alert_event' AND json_extract(payload_json,'$.ruleId')=? AND status IN('pending','suppressed','failed')`, now.Format("2006-01-02 15:04:05.000000"), ruleID)
	return e
}
