// 对象级价格新鲜度：仅由 last_success_at 计算，无成功观察时三值全空。
package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"time"
)

const (
	freshHours   = 6.0
	laggingHours = 12.0
	freshChunk   = 500
)

type freshPriceRow struct {
	appid   int64
	region  string
	status  sql.NullString
	outcome sql.NullString
	answer  sql.NullString
	success *time.Time
	attempt *time.Time
}

// pendingUnitsSQL 失败/中断任务中尚未被观察或 skipUnits 结算的挂账单元。
const pendingUnitsSQL = `
WITH target_jobs AS MATERIALIZED (
    SELECT id, json_extract(stats_json, '$.targets.appids') AS appids,
           json_extract(stats_json, '$.targets.regions') AS regions,
           json_extract(stats_json, '$.targets.tasks') AS tasks,
           replace(json_extract(stats_json, '$.targetStartedAt'), 'T', ' ') AS threshold
    FROM crawl_jobs
    WHERE status IN ('failed','interrupted')
      AND NOT EXISTS (SELECT 1 FROM price_cycles c WHERE c.id = crawl_jobs.cycle_id AND c.status = 'cancelled')
      AND json_type(stats_json, '$.targets') = 'object'
      AND json_extract(stats_json, '$.targetStartedAt') IS NOT NULL
), units AS (
    SELECT j.id AS job_id, CAST(a.value AS INTEGER) AS appid,
           upper(r.value) AS region, j.threshold
    FROM target_jobs j, json_each(j.appids) a, json_each(j.regions) r
    UNION ALL
    SELECT j.id, CAST(a.value AS INTEGER),
           upper(json_extract(t.value, '$.region')), j.threshold
    FROM target_jobs j, json_each(j.tasks) t,
         json_each(t.value, '$.appids') a
), skipped AS (
    SELECT s.value AS unit,
           MAX(replace(json_extract(j.stats_json, '$.targetStartedAt'), 'T', ' ')) AS threshold
    FROM crawl_jobs j, json_each(j.stats_json, '$.skipUnits') s
    WHERE replace(json_extract(j.stats_json, '$.targetStartedAt'), 'T', ' ')
          >= (SELECT MIN(threshold) FROM target_jobs)
    GROUP BY s.value
), unit_results AS (
    SELECT u.*, CASE WHEN (
        p.price_status IN ('ok','locked','missing','blocked')
        AND (p.updated_at >= u.threshold OR p.last_success_at >= u.threshold)
    ) OR s.threshold >= u.threshold THEN 1 ELSE 0 END AS settled
    FROM units u
    LEFT JOIN game_current_prices p ON p.appid = u.appid AND p.region_code = u.region
    LEFT JOIN skipped s ON s.unit = CAST(u.appid AS TEXT) || ':' || u.region
), unresolved AS (
    SELECT DISTINCT appid, region, threshold FROM unit_results WHERE settled = 0
)
SELECT DISTINCT appid, region FROM unresolved
WHERE threshold < ?
  AND appid IN (SELECT CAST(value AS INTEGER) FROM json_each(?))
LIMIT -1
`

// AppIDFreshness 批量读取各地区的成功观察与更新状态：入参序即出参序（不去重）。
func (r *Runtime) AppIDFreshness(ctx context.Context, appids []int64) ([]map[string]any, error) {
	regions, err := r.enabledRegions(ctx)
	if err != nil {
		return nil, err
	}
	byAppid := map[int64][]freshPriceRow{}
	if len(appids) > 0 {
		for start := 0; start < len(appids); start += freshChunk {
			end := start + freshChunk
			if end > len(appids) {
				end = len(appids)
			}
			chunk, _ := json.Marshal(appids[start:end])
			rows, err := r.db.QueryContext(ctx,
				`SELECT appid, region_code, price_status, attempt_outcome, steam_answer,
				        last_success_at, updated_at
				 FROM game_current_prices WHERE appid IN (SELECT CAST(value AS INTEGER) FROM json_each(?))`,
				string(chunk))
			if err != nil {
				return nil, err
			}
			if err := collectFreshRows(rows, byAppid); err != nil {
				return nil, err
			}
		}
	}
	pending := map[freshUnitKey]bool{}
	if len(appids) > 0 {
		ids, _ := json.Marshal(appids)
		rows, err := r.db.QueryContext(ctx, pendingUnitsSQL, timestamp(r.now()), string(ids))
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var appid int64
			var region string
			if err := rows.Scan(&appid, &region); err != nil {
				rows.Close()
				return nil, err
			}
			pending[freshUnitKey{appid, region}] = true
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return nil, err
		}
		rows.Close()
	}
	out := make([]map[string]any, 0, len(appids))
	now := naiveWall(r.now())
	for _, appid := range appids {
		item := regionalFreshness(byAppid[appid], regions, pending, appid, now)
		item["appid"] = appid
		out = append(out, item)
	}
	return out, nil
}

// naiveWall 将时刻转为 UTC 标签的裸墙钟，与 readTime 解析出的 DB 时刻同口径比较。
func naiveWall(t time.Time) time.Time {
	return time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), t.Minute(), t.Second(), t.Nanosecond(), time.UTC)
}

type freshUnitKey struct {
	appid  int64
	region string
}

func (r *Runtime) enabledRegions(ctx context.Context) ([]string, error) {
	rows, err := r.db.QueryContext(ctx, `SELECT code FROM crawl_regions WHERE enabled=1 ORDER BY sort, code`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []string{}
	for rows.Next() {
		var code string
		if err := rows.Scan(&code); err != nil {
			return nil, err
		}
		out = append(out, code)
	}
	return out, rows.Err()
}

func collectFreshRows(rows *sql.Rows, byAppid map[int64][]freshPriceRow) error {
	defer rows.Close()
	for rows.Next() {
		var row freshPriceRow
		var success, attempt sql.NullString
		if err := rows.Scan(&row.appid, &row.region, &row.status, &row.outcome, &row.answer, &success, &attempt); err != nil {
			return err
		}
		if success.Valid && success.String != "" {
			if at, err := readTime(success.String); err == nil {
				row.success = &at
			}
		}
		if attempt.Valid && attempt.String != "" {
			if at, err := readTime(attempt.String); err == nil {
				row.attempt = &at
			}
		}
		byAppid[row.appid] = append(byAppid[row.appid], row)
	}
	return rows.Err()
}

func regionalFreshness(rows []freshPriceRow, regions []string, pending map[freshUnitKey]bool, appid int64, now time.Time) map[string]any {
	indexed := map[string]freshPriceRow{}
	for _, row := range rows {
		if row.region != "" {
			indexed[row.region] = row
		}
	}
	codes := regions
	if len(codes) == 0 {
		codes = make([]string, 0, len(indexed))
		for code := range indexed {
			codes = append(codes, code)
		}
	}
	units := map[string]any{}
	var successes []time.Time
	for _, code := range codes {
		upper := strings.ToUpper(code)
		row, has := indexed[upper]
		outcome := "notAttempted"
		if has {
			outcome = row.outcome.String
			if outcome == "" {
				outcome = "failed"
			}
		}
		status := any("notAttempted") // 无行 ≠ NULL：缺行显式 notAttempted
		if has {
			if row.status.Valid {
				status = row.status.String
			} else {
				status = nil
			}
		}
		answer := any(nil)
		if has && row.answer.Valid {
			answer = row.answer.String
		}
		lastSuccess := any(nil)
		ageHours := any(nil)
		freshness := any(nil)
		if row.success != nil {
			age := now.Sub(*row.success).Seconds() / 3600.0
			lastSuccess = readTimeISO(*row.success)
			ageHours = roundRead(age, 3)
			freshness = freshnessBucket(age)
			successes = append(successes, *row.success)
		}
		lastAttempt := any(nil)
		if row.attempt != nil {
			lastAttempt = readTimeISO(*row.attempt)
		}
		pendingFlag := has && pending[freshUnitKey{appid, upper}]
		pendingFlag = pendingFlag || (outcome != "success" && (!has || row.status.String != "blocked"))
		needsUpdate := pendingFlag || outcome != "success" || ageHours == nil || ageHours.(float64) >= freshHours
		units[upper] = map[string]any{
			"status":        status,
			"outcome":       outcome,
			"answer":        answer,
			"lastSuccessAt": lastSuccess,
			"lastAttemptAt": lastAttempt,
			"ageHours":      ageHours,
			"freshness":     freshness,
			"pending":       pendingFlag,
			"needsUpdate":   needsUpdate,
		}
	}
	item := freshnessOf(nil, now)
	if len(successes) > 0 {
		earliest := successes[0]
		for _, at := range successes[1:] {
			if at.Before(earliest) {
				earliest = at
			}
		}
		item = freshnessOf(&earliest, now)
	}
	item["regions"] = units
	return item
}

func freshnessBucket(ageHours float64) string {
	if ageHours < freshHours {
		return "fresh"
	}
	if ageHours < laggingHours {
		return "lagging"
	}
	return "stale"
}

func freshnessOf(observedAt *time.Time, now time.Time) map[string]any {
	if observedAt == nil {
		return map[string]any{"observedAt": nil, "ageHours": nil, "freshness": nil}
	}
	age := now.Sub(*observedAt).Seconds() / 3600.0
	return map[string]any{
		"observedAt": readTimeISO(*observedAt),
		"ageHours":   roundRead(age, 3),
		"freshness":  freshnessBucket(age),
	}
}
