// 对象级价格新鲜度：单游戏各地区成功观察与更新状态（等价 Python
// crawl/freshness.py appid_freshness）。新鲜度只来自成功观察（last_success_at），
// 「没有数据」不是「数据很旧」——无观察时刻三值全空，不塞进 stale。
package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"time"
)

// 刷新网格步长与陈旧阈值（小时）：6h 起 lagging，12h 起 stale（边界归下一档）。
const (
	freshHours   = 6.0
	laggingHours = 12.0
	// freshChunk appid 分批宽度：避开 SQLite 变量上限。
	freshChunk = 500
)

type freshPriceRow struct {
	appid   int64
	region  string
	status  sql.NullString // price_status
	outcome sql.NullString // attempt_outcome
	answer  sql.NullString // steam_answer
	success *time.Time     // last_success_at
	attempt *time.Time     // updated_at
}

// pendingUnitsSQL 失败/中断任务挂账中未观察目标单元（等价 crawl/service.py
// _JOB_TARGET_UNITS_SQL）：targets（appids×regions 或 tasks 分桶）减去观察已
// 推进（updated_at/last_success_at ≥ targetStartedAt）与 skipUnits 已跳过者。
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

// naiveWall 时刻 → 墙钟裸时刻（UTC 标签）：DB 时间串（无时区）经 readTime
// 解析成 UTC 标签的墙钟值，年龄比较两侧必须同口径——等价 Python 对 naive
// datetime 的直接相减。
func naiveWall(t time.Time) time.Time {
	return time.Date(t.Year(), t.Month(), t.Day(), t.Hour(), t.Minute(), t.Second(), t.Nanosecond(), time.UTC)
}

type freshUnitKey struct {
	appid  int64
	region string
}

// enabledRegions 启用区 code（小写，按 sort 排；Python enabled_regions 同口径）。
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

// regionalFreshness 同一份价格行生成地区明细和对象新鲜度：regions 为启用区
// （大写化后作明细键），空则按现有价格行 region_code 展开。
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
		row, has := indexed[upper] // 明细键与查找同用大写（Python codes 先 upper 再 indexed.get）
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

// freshnessBucket 年龄 → 档位。边界归下一档：6h 起 lagging，12h 起 stale。
func freshnessBucket(ageHours float64) string {
	if ageHours < freshHours {
		return "fresh"
	}
	if ageHours < laggingHours {
		return "lagging"
	}
	return "stale"
}

// freshnessOf 观察时刻 → {observedAt, ageHours, freshness}；无观察（nil）三值
// 全空——「没有数据」不是「数据很旧」。
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
