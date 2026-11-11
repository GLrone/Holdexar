package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

type Spec struct {
	Scope           string         `json:"scope,omitempty"`
	Kind            string         `json:"kind,omitempty"`
	AppIDs          []int64        `json:"appids,omitempty"`
	ResolvedAppIDs  []int64        `json:"resolved_appids,omitempty"`
	ResolvedRegions []string       `json:"resolved_regions,omitempty"`
	Execution       map[string]any `json:"execution,omitempty"`
}

func (s Spec) MarshalJSON() ([]byte, error) {
	fields := map[string]any{}
	if s.Scope != "" {
		fields["scope"] = s.Scope
	}
	if s.Kind != "" {
		fields["kind"] = s.Kind
	}
	if len(s.AppIDs) > 0 {
		fields["appids"] = s.AppIDs
	}
	if len(s.Execution) > 0 {
		fields["execution"] = s.Execution
	}
	if s.ResolvedAppIDs != nil {
		fields["resolved_appids"] = s.ResolvedAppIDs
	}
	if s.ResolvedRegions != nil {
		fields["resolved_regions"] = s.ResolvedRegions
	}
	return json.Marshal(fields)
}

type Region struct{ Code, Currency string }
type TargetBatch struct {
	AppIDs []int64
	Region string
}

type Planner struct {
	DB    *sql.DB
	Gate  *store.WriteGate
	Now   func() time.Time
	Board func(context.Context, string) ([]int64, error)
}

func (p *Planner) planNow() time.Time {
	if p.Now != nil {
		return p.Now()
	}
	return time.Now()
}

func (p *Planner) setting(ctx context.Context, key string, fallback any) (any, error) {
	var raw sql.NullString
	err := p.DB.QueryRowContext(ctx, "SELECT value_json FROM app_settings WHERE key=?", key).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return fallback, nil
	}
	if err != nil {
		return nil, err
	}
	if !raw.Valid {
		return nil, nil
	}
	var v any
	if err = json.Unmarshal([]byte(raw.String), &v); err != nil {
		return nil, err
	}
	return v, nil
}

func planTruthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case float64:
		return x != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

var errNoRegions = errors.New("未启用任何区服：请先在「我」页勾选要爬取的地区")

func (p *Planner) Regions(ctx context.Context, requested []string) ([]Region, error) {
	rows, err := p.DB.QueryContext(ctx, "SELECT code,currency,enabled FROM crawl_regions ORDER BY sort,code")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	all := map[string]Region{}
	enabled := []Region{}
	for rows.Next() {
		var r Region
		var on bool
		if err = rows.Scan(&r.Code, &r.Currency, &on); err != nil {
			return nil, err
		}
		r.Code = strings.ToLower(r.Code)
		all[r.Code] = r
		if on {
			enabled = append(enabled, r)
		}
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	if len(requested) == 0 {
		if len(enabled) == 0 {
			return nil, errNoRegions
		}
		return enabled, nil
	}
	out := []Region{}
	seen := map[string]bool{}
	for _, code := range requested {
		code = strings.ToLower(strings.TrimSpace(code))
		if code == "" {
			continue
		}
		r, ok := all[code]
		if !ok {
			return nil, fmt.Errorf("未知区服代码: %s", code)
		}
		if !seen[code] {
			seen[code] = true
			out = append(out, r)
		}
	}
	if len(out) == 0 {
		return nil, fmt.Errorf("区服参数为空")
	}
	return out, nil
}

const planActiveMonitor = `SELECT s.target_id,MAX(s.priority) AS priority FROM monitor_sources s
 JOIN monitor_targets t ON t.target_type=s.target_type AND t.target_id=s.target_id
 WHERE s.target_type='game' AND s.active=1 AND NOT EXISTS
 (SELECT 1 FROM monitor_exclusions e WHERE e.target_type=s.target_type AND e.target_id=s.target_id AND e.active=1)
 GROUP BY s.target_id`

func (p *Planner) ResolveScope(ctx context.Context, scope string, ids []int64) ([]int64, error) {
	if scope == "appids" {
		return append([]int64{}, ids...), nil
	}
	var query string
	switch scope {
	case "pool":
		query = `WITH active AS (` + planActiveMonitor + `) SELECT a.target_id FROM active a LEFT JOIN games g ON g.appid=a.target_id
 WHERE COALESCE(g.free_kind,'')!='f2p' ORDER BY a.priority DESC,
 (COALESCE(g.hl_flag,0)>0 OR EXISTS(SELECT 1 FROM game_current_prices p WHERE p.appid=a.target_id AND p.discount_percent>0)) DESC,a.target_id`
	case "catalog":
		query = `SELECT g.appid FROM games g WHERE g.removed_at IS NULL AND g.free_kind IS NULL
 AND NOT EXISTS(SELECT 1 FROM monitor_targets t WHERE t.target_type='game' AND t.target_id=g.appid) ORDER BY g.appid`
	case "wishlist", "wishlist_only", "owned":
		filter := ""
		if scope == "wishlist_only" {
			filter = " AND w.owned=0"
		}
		if scope == "owned" {
			filter = " AND w.owned=1"
		}
		query = `SELECT w.appid FROM wishlist_items w LEFT JOIN games g ON g.appid=w.appid WHERE w.active=1` + filter + `
 AND COALESCE(g.free_kind,'')!='f2p' GROUP BY w.appid ORDER BY MAX(w.manual) DESC,
 MAX(w.manual OR w.wishlisted) DESC,(COALESCE(g.hl_flag,0)>0 OR EXISTS
 (SELECT 1 FROM game_current_prices p WHERE p.appid=w.appid AND p.discount_percent>0)) DESC,w.appid`
	case "discounts", "specials":
		enabled, err := p.setting(ctx, "fetch.boards", true)
		if err != nil {
			return nil, err
		}
		if !planTruthy(enabled) {
			return []int64{}, nil
		}
		if p.Board == nil {
			return nil, fmt.Errorf("榜单获取尚未接入")
		}
		board, err := p.Board(ctx, "specials")
		if err != nil {
			return nil, err
		}
		query = `SELECT target_id FROM monitor_targets WHERE target_type='game' UNION SELECT appid FROM games WHERE free_kind='f2p'`
		if scope == "specials" {
			query += ` UNION SELECT appid FROM games WHERE removed_at IS NULL AND free_kind IS NULL`
		}
		excluded, err := p.planIDs(ctx, query)
		if err != nil {
			return nil, err
		}
		blocked := map[int64]bool{}
		for _, id := range excluded {
			blocked[id] = true
		}
		out := []int64{}
		for _, id := range board {
			if !blocked[id] {
				blocked[id] = true
				out = append(out, id)
			}
		}
		return out, nil
	default:
		return nil, fmt.Errorf("未知 scope: %s", scope)
	}
	return p.planIDs(ctx, query)
}

func (p *Planner) planIDs(ctx context.Context, query string, args ...any) ([]int64, error) {
	rows, err := p.DB.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	ids := []int64{}
	for rows.Next() {
		var id int64
		if err = rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func planSpecKey(s Spec) string {
	kind := s.Kind
	if kind == "manual" || kind == "scheduled" {
		kind = ""
	}
	v := map[string]any{}
	if s.Scope != "" {
		v["scope"] = s.Scope
	}
	if kind != "" {
		v["kind"] = kind
	}
	if len(s.AppIDs) > 0 {
		v["appids"] = s.AppIDs
	}
	raw, _ := json.Marshal(v)
	return string(raw)
}

func (p *Planner) DefaultSpecs(ctx context.Context, full bool) ([]Spec, error) {
	keys := []string{"crawl.queue_missing", "crawl.queue_pool", "proxy.strategy", "crawl.queue_discounts"}
	fallbacks := []any{true, true, "direct_only", true}
	v := make([]any, len(keys))
	for i, key := range keys {
		var err error
		v[i], err = p.setting(ctx, key, fallbacks[i])
		if err != nil {
			return nil, err
		}
	}
	direct := v[2] == "direct_only"
	catalog, err := p.setting(ctx, "crawl.catalog_refresh", !direct)
	if err != nil {
		return nil, err
	}
	out := []Spec{}
	if planTruthy(v[0]) {
		out = append(out, Spec{Kind: "missing"})
	}
	if planTruthy(v[1]) {
		out = append(out, Spec{Scope: "pool"})
	}
	if direct && !full && planTruthy(v[3]) {
		out = append(out, Spec{Scope: "discounts", Kind: "discounts"})
	}
	if full || planTruthy(catalog) {
		out = append(out, Spec{Scope: "catalog"})
	}
	if planTruthy(catalog) {
		out = append(out, Spec{Scope: "specials", Kind: "specials_backfill"})
	}
	history, err := readCycleHistory(ctx, p.DB, `SELECT id,specs_json,0 FROM price_cycles WHERE specs_json IS NOT NULL ORDER BY id`)
	if err != nil {
		return nil, err
	}
	order := resolveCycleStages(history).order
	rank := map[string]int{}
	for i, key := range order {
		rank[key] = i
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, ok := rank[planSpecKey(out[i])]
		if !ok {
			a = len(rank)
		}
		b, ok := rank[planSpecKey(out[j])]
		if !ok {
			b = len(rank)
		}
		return a < b
	})
	return out, nil
}

const jobTargetResultsSQL = `WITH target_jobs AS MATERIALIZED (
 SELECT id,json_extract(stats_json,'$.targets.appids') appids,json_extract(stats_json,'$.targets.regions') regions,
 json_extract(stats_json,'$.targets.tasks') tasks,replace(json_extract(stats_json,'$.targetStartedAt'),'T',' ') threshold
 FROM crawl_jobs WHERE status IN ('failed','interrupted')
 AND NOT EXISTS(SELECT 1 FROM price_cycles c WHERE c.id=crawl_jobs.cycle_id AND c.status='cancelled')
 AND CASE WHEN json_valid(stats_json) THEN json_type(stats_json,'$.targets')='object'
     AND json_extract(stats_json,'$.targetStartedAt') IS NOT NULL END
 AND (?1 IS NULL OR id IN (SELECT value FROM json_each(?1)))
), units AS (
 SELECT j.id job_id,CAST(a.value AS INTEGER) appid,upper(r.value) region,j.threshold FROM target_jobs j,json_each(j.appids) a,json_each(j.regions) r
 UNION ALL SELECT j.id,CAST(a.value AS INTEGER),upper(json_extract(t.value,'$.region')),j.threshold FROM target_jobs j,json_each(j.tasks) t,json_each(t.value,'$.appids') a
), skipped AS (
 SELECT s.value unit,MAX(replace(json_extract(j.stats_json,'$.targetStartedAt'),'T',' ')) threshold
 FROM crawl_jobs j,json_each(CASE WHEN json_valid(j.stats_json) THEN j.stats_json END,'$.skipUnits') s
 WHERE CASE WHEN json_valid(j.stats_json) THEN replace(json_extract(j.stats_json,'$.targetStartedAt'),'T',' ')
     >= (SELECT MIN(threshold) FROM target_jobs) END GROUP BY s.value
), unit_results AS (
 SELECT u.*,CASE WHEN (p.price_status IN ('ok','locked','missing','blocked')
     AND (p.updated_at>=u.threshold OR p.last_success_at>=u.threshold)) OR s.threshold>=u.threshold THEN 1 ELSE 0 END settled
 FROM units u
 LEFT JOIN game_current_prices p ON p.appid=u.appid AND p.region_code=u.region
 LEFT JOIN skipped s ON s.unit=CAST(u.appid AS TEXT)||':'||u.region
)`

const planUnobservedSQL = jobTargetResultsSQL + `
SELECT DISTINCT appid,region FROM unit_results WHERE settled=0 AND threshold<?2`

func (p *Planner) Missing(ctx context.Context, cooldown time.Duration) ([]TargetBatch, error) {
	now := p.planNow().In(time.FixedZone("Asia/Shanghai", 8*3600))
	format := "2006-01-02 15:04:05.000000"
	excluded, err := p.planIDs(ctx, `SELECT t.target_id FROM monitor_targets t WHERE t.target_type='game' AND
 (NOT EXISTS(SELECT 1 FROM monitor_sources s WHERE s.target_type=t.target_type AND s.target_id=t.target_id AND s.active=1)
 OR EXISTS(SELECT 1 FROM monitor_exclusions e WHERE e.target_type=t.target_type AND e.target_id=t.target_id AND e.active=1))
 UNION SELECT appid FROM games WHERE free_kind='f2p'`)
	if err != nil {
		return nil, err
	}
	blocked := map[int64]bool{}
	for _, id := range excluded {
		blocked[id] = true
	}
	type unit struct {
		id int64
		cc string
	}
	picked := map[unit]bool{}
	read := func(query string, args ...any) error {
		rows, e := p.DB.QueryContext(ctx, query, args...)
		if e != nil {
			return e
		}
		defer rows.Close()
		for rows.Next() {
			var u unit
			if e = rows.Scan(&u.id, &u.cc); e != nil {
				return e
			}
			u.cc = strings.ToLower(u.cc)
			if !blocked[u.id] && u.cc != "" && len(picked) < 8000 {
				picked[u] = true
			}
		}
		return rows.Err()
	}
	if err = read(`SELECT appid,region_code FROM game_current_prices WHERE region_code!='' AND
 ((price_status='missing' AND updated_at<?) OR (price_status='blocked' AND updated_at<?)) ORDER BY updated_at`, now.Add(-cooldown).Format(format), now.Add(-7*24*time.Hour).Format(format)); err != nil {
		return nil, err
	}
	if err = read(planUnobservedSQL, nil, now.Add(-cooldown).Format(format)); err != nil {
		return nil, err
	}
	units := make([]unit, 0, len(picked))
	for u := range picked {
		units = append(units, u)
	}
	sort.Slice(units, func(i, j int) bool {
		if units[i].id == units[j].id {
			return units[i].cc < units[j].cc
		}
		return units[i].id < units[j].id
	})
	byRegion := map[string][]int64{}
	for _, u := range units {
		byRegion[u.cc] = append(byRegion[u.cc], u.id)
	}
	codes := []string{}
	for cc := range byRegion {
		codes = append(codes, cc)
	}
	sort.Strings(codes)
	out := []TargetBatch{}
	for _, cc := range codes {
		ids := byRegion[cc]
		for i := 0; i < len(ids); i += 300 {
			end := i + 300
			if end > len(ids) {
				end = len(ids)
			}
			out = append(out, TargetBatch{AppIDs: ids[i:end], Region: cc})
		}
	}
	return out, nil
}
