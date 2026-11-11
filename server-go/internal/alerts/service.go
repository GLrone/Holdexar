package alerts

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/store"
)

type Options struct {
	Now      func() time.Time
	Log      func(string, ...any)
	Dispatch func(context.Context) error
}
type Service struct {
	db   *sql.DB
	gate *store.WriteGate
	opts Options
}

func New(db *sql.DB, gate *store.WriteGate, opts Options) *Service {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	return &Service{db, gate, opts}
}

var beijing = func() *time.Location { loc, _ := time.LoadLocation("Asia/Shanghai"); return loc }()

func (s *Service) now() time.Time { return s.opts.Now().In(beijing) }
func stamp(t time.Time) string    { return t.Format("2006-01-02 15:04:05.000000") }
func iso(v any) any {
	if v == nil {
		return nil
	}
	if t, e := parseTime(text(v)); e == nil {
		return t.Format("2006-01-02T15:04:05.999999")
	}
	return v
}
func parseTime(raw string) (time.Time, error) {
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999", time.RFC3339Nano} {
		if t, e := time.ParseInLocation(layout, raw, beijing); e == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("invalid reminder timestamp")
}
func jsonText(v any) (string, error) { b, e := json.Marshal(v); return string(b), e }

type query interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}

func maps(ctx context.Context, q query, sqlText string, args ...any) ([]map[string]any, error) {
	rows, e := q.QueryContext(ctx, sqlText, args...)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	cols, e := rows.Columns()
	if e != nil {
		return nil, e
	}
	out := []map[string]any{}
	for rows.Next() {
		v := make([]any, len(cols))
		p := make([]any, len(cols))
		for i := range v {
			p[i] = &v[i]
		}
		if e = rows.Scan(p...); e != nil {
			return nil, e
		}
		m := map[string]any{}
		for i, k := range cols {
			if b, ok := v[i].([]byte); ok {
				v[i] = string(b)
			}
			m[k] = v[i]
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

const ruleSelect = `SELECT id,appid,region,target_type AS targetType,target_value AS targetValue,config_json AS config,active,repeat_mode AS repeatMode,repeat_hours AS repeatHours,CAST(created_at AS TEXT) AS createdAt,CAST(last_triggered_at AS TEXT) AS lastTriggeredAt FROM price_alerts`

func normalizeRule(r map[string]any) map[string]any {
	var config any
	_ = json.Unmarshal([]byte(text(r["config"])), &config)
	r["config"] = config
	r["active"] = r["active"] == true || r["active"] == int64(1)
	r["createdAt"] = iso(r["createdAt"])
	r["lastTriggeredAt"] = iso(r["lastTriggeredAt"])
	return r
}
func (s *Service) get(ctx context.Context, id int64) (map[string]any, error) {
	rows, e := maps(ctx, s.db, ruleSelect+` WHERE id=?`, id)
	if e != nil {
		return nil, e
	}
	if len(rows) == 0 {
		return nil, fmt.Errorf("提醒不存在")
	}
	return normalizeRule(rows[0]), nil
}
func (s *Service) List(ctx context.Context) ([]map[string]any, error) {
	rows, e := maps(ctx, s.db, `SELECT a.id,a.appid,a.region,a.target_type AS targetType,a.target_value AS targetValue,a.config_json AS config,a.active,a.repeat_mode AS repeatMode,a.repeat_hours AS repeatHours,CAST(a.created_at AS TEXT) AS createdAt,CAST(a.last_triggered_at AS TEXT) AS lastTriggeredAt,coalesce(g.name,'') AS gameName,coalesce(nullif(g.header_image,''),'https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/'||a.appid||'/header.jpg') AS gameHeader FROM price_alerts a LEFT JOIN games g ON g.appid=a.appid ORDER BY a.id DESC`)
	for _, r := range rows {
		normalizeRule(r)
	}
	return rows, e
}
func (s *Service) after(ctx context.Context, matched int) {
	if matched == 0 {
		return
	}
	s.opts.Log("价格提醒已触发 │ 条数=%d", matched)
	if s.opts.Dispatch != nil {
		if e := s.opts.Dispatch(ctx); e != nil {
			s.opts.Log("价格提醒已保存，本次发送未完成：%v", e)
		}
	}
}
func (s *Service) Add(ctx context.Context, raw map[string]any) (map[string]any, error) {
	data := copyMap(raw)
	if data["repeatHours"] == nil {
		data["repeatHours"] = 24
	}
	data, e := ValidateRule(data)
	if e != nil {
		return nil, e
	}
	appid, _ := number(data["appid"])
	now := s.now()
	var id int64
	matched := 0
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		config, e := jsonText(data["config"])
		if e != nil {
			return e
		}
		r, e := conn.ExecContext(ctx, `INSERT INTO price_alerts(appid,region,target_type,target_value,active,created_at,repeat_mode,repeat_hours,config_json) VALUES(?,?,?,?,1,?,?,?,?)`, int64(appid), data["region"], data["targetType"], data["targetValue"], stamp(now), data["repeatMode"], data["repeatHours"], config)
		if e != nil {
			return e
		}
		id, e = r.LastInsertId()
		if e != nil {
			return e
		}
		matched, e = s.checkTx(ctx, conn, []int64{int64(appid)}, []int64{id}, now)
		return e
	})
	if e != nil {
		return nil, e
	}
	s.after(ctx, matched)
	return s.get(ctx, id)
}
func (s *Service) Update(ctx context.Context, id int64, patch map[string]any) (map[string]any, error) {
	now := s.now()
	matched := 0
	e := s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		rows, e := maps(ctx, conn, ruleSelect+` WHERE id=?`, id)
		if e != nil {
			return e
		}
		if len(rows) == 0 {
			return fmt.Errorf("提醒不存在")
		}
		r := normalizeRule(rows[0])
		for _, k := range []string{"targetType", "targetValue", "region", "repeatMode", "repeatHours", "config"} {
			if patch[k] != nil {
				r[k] = patch[k]
			}
		}
		r, e = ValidateRule(r)
		if e != nil {
			return e
		}
		if patch["active"] != nil {
			r["active"] = patch["active"]
		}
		config, e := jsonText(r["config"])
		if e != nil {
			return e
		}
		_, e = conn.ExecContext(ctx, `UPDATE price_alerts SET region=?,target_type=?,target_value=?,active=?,repeat_mode=?,repeat_hours=?,config_json=? WHERE id=?`, r["region"], r["targetType"], r["targetValue"], r["active"], r["repeatMode"], r["repeatHours"], config, id)
		if e != nil {
			return e
		}
		if patch["active"] == false {
			return notifications.PauseRuleTx(ctx, conn, id, now)
		}
		appid, _ := number(r["appid"])
		matched, e = s.checkTx(ctx, conn, []int64{int64(appid)}, []int64{id}, now)
		return e
	})
	if e != nil {
		return nil, e
	}
	s.after(ctx, matched)
	return s.get(ctx, id)
}
func (s *Service) Delete(ctx context.Context, id int64) (bool, error) {
	removed := false
	e := s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if e := notifications.PauseRuleTx(ctx, conn, id, s.now()); e != nil {
			return e
		}
		r, e := conn.ExecContext(ctx, `DELETE FROM price_alerts WHERE id=?`, id)
		if e != nil {
			return e
		}
		n, e := r.RowsAffected()
		removed = n > 0
		return e
	})
	return removed, e
}

func inputs(ctx context.Context, q query, ids []int64, region string, scopes ...[]string) ([]map[string]any, error) {
	sqlText := `SELECT g.appid,g.name AS gameName,g.header_image AS headerImage,g.positive_rate AS positiveRate,g.review_count AS reviewCount,p.appid AS priceAppid,p.currency,p.sub_id AS subId,p.price,p.original_price AS originalPrice,p.discount_percent AS discount,p.price_status AS status,p.cny_fen AS cnyFen,CAST(p.updated_at AS TEXT) AS observedAt FROM games g LEFT JOIN game_current_prices p ON p.appid=g.appid AND p.region_code=?`
	args := []any{region}
	filters := []string{}
	if ids != nil {
		encoded, e := jsonText(ids)
		if e != nil {
			return nil, e
		}
		filters = append(filters, `g.appid IN(SELECT value FROM json_each(?))`)
		args = append(args, encoded)
	}
	if len(scopes) > 0 {
		scope := map[string]bool{}
		for _, name := range scopes[0] {
			scope[name] = true
		}
		if !scope["catalog"] {
			sources := []string{}
			for name, source := range map[string]string{"followed": "favorite", "wishlist": "family_wishlist", "owned": "owned", "manual": "manual"} {
				if scope[name] {
					sources = append(sources, source)
				}
			}
			encoded, e := jsonText(sources)
			if e != nil {
				return nil, e
			}
			alternatives := []string{`g.appid IN(SELECT target_id FROM monitor_sources WHERE target_type='game' AND active=1 AND source IN(SELECT value FROM json_each(?)))`}
			args = append(args, encoded)
			flags := []string{}
			if scope["wishlist"] {
				flags = append(flags, "wishlisted=1")
			}
			if scope["owned"] {
				flags = append(flags, "owned=1")
			}
			if len(flags) > 0 {
				alternatives = append(alternatives, `g.appid IN(SELECT appid FROM wishlist_items WHERE active=1 AND (`+strings.Join(flags, " OR ")+`))`)
			}
			filters = append(filters, "("+strings.Join(alternatives, " OR ")+")")
		}
	}
	if len(filters) > 0 {
		sqlText += " WHERE " + strings.Join(filters, " AND ")
	}
	rows, e := maps(ctx, q, sqlText, args...)
	for _, r := range rows {
		r["region"] = region
		if r["priceAppid"] == nil {
			for _, key := range []string{"currency", "subId", "price", "originalPrice", "discount", "status", "cnyFen", "observedAt"} {
				delete(r, key)
			}
		} else {
			if r["currency"] == nil {
				r["currency"] = ""
			}
			r["observedAt"] = iso(r["observedAt"])
		}
		delete(r, "priceAppid")
	}
	return rows, e
}

func window(r map[string]any) *int {
	if n, ok := number(obj(r["config"])["windowDays"]); ok {
		v := int(n)
		return &v
	}
	return nil
}
func (s *Service) Preview(ctx context.Context, raw map[string]any) (map[string]any, error) {
	r, e := ValidateRule(raw)
	if e != nil {
		return nil, e
	}
	id, _ := number(r["appid"])
	currents, e := inputs(ctx, s.db, []int64{int64(id)}, text(r["region"]))
	if e != nil {
		return nil, e
	}
	histories, e := derived.HistoryContext(ctx, s.db, currents, s.now(), window(r))
	if e != nil {
		return nil, e
	}
	current := map[string]any{"appid": r["appid"], "region": r["region"]}
	if len(currents) > 0 {
		current = currents[0]
	}
	h := histories[derived.ComparisonKey(current)]
	current["currentKnown"] = h["currentKnown"] == true
	result := EvaluateRule(r, current, h)
	result["rule"] = r
	return result, nil
}
func eventType(kind string) string {
	switch kind {
	case "price":
		return "RULE_PRICE"
	case "pct":
		return "RULE_PCT"
	default:
		return "RULE_LOW"
	}
}
func observationKey(c map[string]any) string {
	v, _ := jsonText([]any{c["appid"], c["region"], c["subId"], c["currency"], c["observedAt"], c["price"]})
	return v
}
func (s *Service) checkTx(ctx context.Context, conn *sql.Conn, ids, ruleIDs []int64, now time.Time) (int, error) {
	prefs, e := notifications.LoadPrefs(ctx, conn)
	if e != nil {
		return 0, e
	}
	encoded, e := jsonText(ids)
	if e != nil {
		return 0, e
	}
	sqlText := ruleSelect + ` WHERE active=1 AND appid IN(SELECT value FROM json_each(?))`
	args := []any{encoded}
	if ruleIDs != nil {
		raw, e := jsonText(ruleIDs)
		if e != nil {
			return 0, e
		}
		sqlText += ` AND id IN(SELECT value FROM json_each(?))`
		args = append(args, raw)
	}
	rows, e := maps(ctx, conn, sqlText, args...)
	if e != nil {
		return 0, e
	}
	grouped := map[string][]map[string]any{}
	for _, r := range rows {
		normalizeRule(r)
		if notifications.TypeEnabled(prefs, eventType(text(r["targetType"]))) {
			grouped[text(r["region"])] = append(grouped[text(r["region"])], r)
		}
	}
	matches := []evaluationMatch{}
	matchedRules := map[int64]map[string]any{}
	for region, rules := range grouped {
		appids := []int64{}
		for _, r := range rules {
			n, _ := number(r["appid"])
			appids = append(appids, int64(n))
		}
		currents, e := inputs(ctx, conn, appids, region)
		if e != nil {
			return 0, e
		}
		currentMap := map[string]map[string]any{}
		for _, c := range currents {
			currentMap[text(c["appid"])] = c
		}
		contexts := map[int]map[string]map[string]any{}
		for _, r := range rules {
			w := window(r)
			key := 0
			if w != nil {
				key = *w
			}
			if contexts[key] == nil {
				h, e := derived.HistoryContext(ctx, conn, currents, now, w)
				if e != nil {
					return 0, e
				}
				contexts[key] = h
			}
		}
		for _, r := range rules {
			if r["repeatMode"] == "cooldown" && r["lastTriggeredAt"] != nil {
				last, e := parseTime(text(r["lastTriggeredAt"]))
				if e != nil {
					return 0, e
				}
				hours, _ := number(r["repeatHours"])
				if hours == 0 {
					hours = 24
				}
				if now.Sub(last).Hours() < hours {
					continue
				}
			}
			c := copyMap(currentMap[text(r["appid"])])
			key := 0
			if w := window(r); w != nil {
				key = *w
			}
			h := contexts[key][derived.ComparisonKey(c)]
			c["currentKnown"] = h["currentKnown"] == true
			result := EvaluateRule(r, c, h)
			if result["matched"] == true {
				id, _ := number(r["id"])
				matches = append(matches, evaluationMatch{RuleID: int64(id), Result: result, EventType: eventType(text(r["targetType"])), Metadata: map[string]any{"rule": r}})
				matchedRules[int64(id)] = r
			}
		}
	}

	records, e := appendMatchesTx(ctx, conn, matches, now)
	if e != nil {
		return 0, e
	}
	if len(records) == 0 {
		return 0, nil
	}
	updates := []map[string]any{}
	for _, record := range records {
		rule := matchedRules[record.RuleID]
		updates = append(updates, map[string]any{"id": record.RuleID, "once": rule["repeatMode"] == "once"})
	}
	raw, e := jsonText(updates)
	if e != nil {
		return 0, e
	}
	_, e = conn.ExecContext(ctx, `UPDATE price_alerts SET last_triggered_at=?,active=CASE WHEN id IN(SELECT value->>'id' FROM json_each(?) WHERE value->>'once'=1) THEN 0 ELSE active END WHERE id IN(SELECT value->>'id' FROM json_each(?))`, stamp(now), raw, raw)
	return len(records), e
}

func (s *Service) CheckAppIDs(ctx context.Context, ids []int64) (int, error) {
	if len(ids) == 0 {
		return 0, nil
	}
	matched := 0
	e := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var e error
		matched, e = s.checkTx(ctx, conn, ids, nil, s.now())
		return e
	})
	if e == nil {
		s.after(ctx, matched)
	}
	return matched, e
}
func (s *Service) DeleteEvent(ctx context.Context, id int64) (bool, error) {
	removed := false
	e := s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var found int
		e := conn.QueryRowContext(ctx, `SELECT 1 FROM alert_events WHERE id=?`, id).Scan(&found)
		if errors.Is(e, sql.ErrNoRows) {
			return nil
		}
		if e != nil {
			return e
		}
		e = conn.QueryRowContext(ctx, `SELECT 1 FROM notification_candidates WHERE source_kind='alert_event' AND event_id=? LIMIT 1`, id).Scan(&found)
		if e == nil {
			return fmt.Errorf("该记录是通知结果的依据，需要保留")
		}
		if !errors.Is(e, sql.ErrNoRows) {
			return e
		}
		r, e := conn.ExecContext(ctx, `DELETE FROM alert_events WHERE id=?`, id)
		if e != nil {
			return e
		}
		n, e := r.RowsAffected()
		removed = n > 0
		return e
	})
	return removed, e
}
