package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
	"time"
)

func (r *Runtime) Jobs(ctx context.Context, limit int) ([]map[string]any, error) {
	rows, err := r.db.QueryContext(ctx, `SELECT id,kind,status,mode,cycle_id AS cycleId,regions_json AS regions,stats_json AS stats,CAST(started_at AS TEXT) AS startedAt,CAST(finished_at AS TEXT) AS finishedAt,error FROM crawl_jobs ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	out, err := readRows(rows)
	if err != nil {
		return nil, err
	}
	for _, item := range out {
		if err = decodeFields(item, "regions", "stats"); err != nil {
			return nil, err
		}
		if err = isoFields(item, "startedAt", "finishedAt"); err != nil {
			return nil, err
		}
	}
	return out, nil
}

const cycleReadColumns = `id,kind,status,scope,expected_json,specs_json,batches_expected,batches_done,CAST(started_at AS TEXT) AS started_at,CAST(running_at AS TEXT) AS running_at,CAST(repairing_at AS TEXT) AS repairing_at,CAST(finalizing_at AS TEXT) AS finalizing_at,CAST(finished_at AS TEXT) AS finished_at,error,targets_total,targets_done,units_expected,units_ok,units_locked,units_failed,units_missing,units_blocked,units_unobserved,stale_count`

func (r *Runtime) Cycles(ctx context.Context, limit int) ([]map[string]any, error) {
	rows, err := r.db.QueryContext(ctx, `SELECT `+cycleReadColumns+` FROM price_cycles ORDER BY id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	cycles, err := readRows(rows)
	if err != nil {
		return nil, err
	}
	ids := []int64{}
	indexed := map[int64]map[string]any{}
	out := []map[string]any{}
	for _, row := range cycles {
		item, e := cyclePayload(row)
		if e != nil {
			return nil, e
		}
		id := readInt(row["id"])
		ids = append(ids, id)
		item["jobs"] = []map[string]any{}
		indexed[id] = item
		out = append(out, item)
	}
	if len(ids) == 0 {
		return out, nil
	}
	encoded, _ := json.Marshal(ids)
	rows, err = r.db.QueryContext(ctx, `SELECT cycle_id,id,kind,status FROM crawl_jobs WHERE cycle_id IN(SELECT value FROM json_each(?)) ORDER BY id`, string(encoded))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var cid, id int64
		var kind, status string
		if err = rows.Scan(&cid, &id, &kind, &status); err != nil {
			return nil, err
		}
		item := indexed[cid]
		item["jobs"] = append(item["jobs"].([]map[string]any), map[string]any{"id": id, "kind": kind, "status": status})
	}
	return out, rows.Err()
}

func cyclePayload(row map[string]any) (map[string]any, error) {
	if err := decodeFields(row, "expected_json", "specs_json"); err != nil {
		return nil, err
	}
	expected := map[string]any{}
	if v, ok := row["expected_json"].(map[string]any); ok {
		expected = v
	}
	count := func(v any) int {
		if list, ok := v.([]any); ok {
			return len(list)
		}
		return 0
	}
	deferred := []any{}
	if specs, ok := row["specs_json"].([]any); ok {
		for _, value := range specs {
			spec, ok := value.(map[string]any)
			if !ok {
				continue
			}
			execution, _ := spec["execution"].(map[string]any)
			switch execution["state"] {
			case "pending", "deferred", "recovering":
				deferred = append(deferred, spec)
			}
		}
	}
	item := map[string]any{"id": row["id"], "kind": row["kind"], "status": row["status"], "scope": row["scope"], "expectedUnits": count(expected["appids"]) * count(expected["regions"]), "batchesExpected": row["batches_expected"], "batchesDone": row["batches_done"], "enteredRepairing": row["repairing_at"] != nil, "error": row["error"], "deferredSpecs": deferred, "stats": nil}
	for _, pair := range [][2]string{{"started_at", "startedAt"}, {"running_at", "runningAt"}, {"repairing_at", "repairingAt"}, {"finalizing_at", "finalizingAt"}, {"finished_at", "finishedAt"}} {
		item[pair[1]] = row[pair[0]]
		if err := isoFields(item, pair[1]); err != nil {
			return nil, err
		}
	}
	if row["units_expected"] != nil {
		coverage, confirmed := readRatios(row["units_expected"], row["units_ok"], row["units_locked"])
		stats := map[string]any{"targetsTotal": row["targets_total"], "targetsDone": row["targets_done"], "unitsExpected": row["units_expected"], "unitsOk": row["units_ok"], "unitsLocked": row["units_locked"], "unitsFailed": row["units_failed"], "unitsUnobserved": row["units_unobserved"], "coverage": coverage, "coverageConfirmed": confirmed, "staleCount": row["stale_count"]}
		times := map[string]*time.Time{}
		for _, key := range []string{"started_at", "running_at", "repairing_at", "finalizing_at", "finished_at"} {
			if row[key] != nil {
				at, err := readTime(row[key].(string))
				if err != nil {
					return nil, err
				}
				times[key] = &at
			}
		}
		var duration any
		if times["started_at"] != nil && times["finished_at"] != nil {
			duration = roundRead(times["finished_at"].Sub(*times["started_at"]).Seconds(), 1)
		}
		stats["durationSeconds"] = duration
		runningEnd := times["repairing_at"]
		if runningEnd == nil {
			runningEnd = times["finalizing_at"]
		}
		if runningEnd == nil {
			runningEnd = times["finished_at"]
		}
		stats["stageMs"] = map[string]any{"planning": elapsedRead(times["started_at"], times["running_at"]), "running": elapsedRead(times["running_at"], runningEnd), "repairing": elapsedRead(times["repairing_at"], times["finalizing_at"]), "finalizing": elapsedRead(times["finalizing_at"], times["finished_at"])}
		item["stats"] = stats
	}
	return item, nil
}

func (r *Runtime) Coverage(ctx context.Context, cycleID int64) (map[string]any, error) {
	rows, err := r.db.QueryContext(ctx, `SELECT `+cycleReadColumns+` FROM price_cycles WHERE id=?`, cycleID)
	if err != nil {
		return nil, err
	}
	cycles, err := readRows(rows)
	if err != nil {
		return nil, err
	}
	if len(cycles) == 0 {
		return nil, nil
	}
	row := cycles[0]
	if row["units_expected"] != nil {
		missing, blocked := row["units_missing"], row["units_blocked"]
		if missing == nil && blocked == nil {
			missing = row["units_failed"]
			blocked = int64(0)
		}
		return coveragePayload(cycleID, row["status"], row["units_expected"], row["units_ok"], row["units_locked"], missing, blocked, row["units_unobserved"], row["targets_total"], row["targets_done"]), nil
	}
	if err = decodeFields(row, "expected_json"); err != nil {
		return nil, err
	}
	expected, _ := row["expected_json"].(map[string]any)
	ids, _ := expected["appids"].([]any)
	regions, _ := expected["regions"].([]any)
	set := map[string]bool{}
	for _, v := range regions {
		region := strings.ToLower(strings.TrimSpace(fmt.Sprint(v)))
		set[region] = true
	}
	regions = []any{}
	for region := range set {
		regions = append(regions, region)
	}
	units := int64(len(ids) * len(regions))
	counts := map[string]int64{"ok": 0, "locked": 0, "missing": 0, "blocked": 0, "unobserved": units}
	if units == 0 {
		return coveragePayload(cycleID, row["status"], int64(0), int64(0), int64(0), int64(0), int64(0), int64(0), int64(0), int64(0)), nil
	}
	apps, _ := json.Marshal(ids)
	regs, _ := json.Marshal(regions)
	end := timestamp(r.now())
	if row["finished_at"] != nil {
		end = row["finished_at"].(string)
	}
	query := `SELECT appid,price_status,attempt_outcome,steam_answer,COUNT(*) FROM game_current_prices WHERE appid IN(SELECT value FROM json_each(?)) AND lower(region_code) IN(SELECT value FROM json_each(?)) AND updated_at IS NOT NULL AND updated_at<=?`
	args := []any{string(apps), string(regs), end}
	if row["started_at"] != nil {
		query += ` AND updated_at>=?`
		args = append(args, row["started_at"])
	}
	query += ` GROUP BY appid,price_status,attempt_outcome,steam_answer`
	rows, err = r.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	observed := map[int64]int64{}
	for rows.Next() {
		var id, n int64
		var status, outcome, answer sql.NullString
		if err = rows.Scan(&id, &status, &outcome, &answer, &n); err != nil {
			return nil, err
		}
		bucket := strings.ToLower(strings.TrimSpace(status.String))
		switch {
		case outcome.String == "failed":
			if bucket != "blocked" {
				bucket = "missing"
			}
		case outcome.String == "success" && (answer.String == "locked" || answer.String == "no_options"):
			bucket = "locked"
		default:
			if bucket != "ok" && bucket != "locked" && bucket != "missing" && bucket != "blocked" {
				bucket = "missing"
			}
		}
		counts[bucket] += n
		counts["unobserved"] -= n
		observed[id] += n
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	done := int64(0)
	for _, id := range ids {
		if observed[readInt(id)] >= int64(len(regions)) {
			done++
		}
	}
	return coveragePayload(cycleID, row["status"], units, counts["ok"], counts["locked"], counts["missing"], counts["blocked"], counts["unobserved"], int64(len(ids)), done), nil
}

func coveragePayload(id int64, status, expected, ok, locked, missing, blocked, unobserved, total, done any) map[string]any {
	coverage, confirmed := readRatios(expected, ok, locked)
	return map[string]any{"cycleId": id, "status": status, "targetsTotal": total, "targetsDone": done, "expectedUnits": expected, "ok": ok, "locked": locked, "missing": missing, "blocked": blocked, "unobserved": unobserved, "coverage": coverage, "coverageConfirmed": confirmed}
}

func (r *Runtime) PriceEvents(ctx context.Context, cycleID int64, limit int) ([]map[string]any, error) {
	return r.PriceEventsFiltered(ctx, cycleID, 0, "", limit)
}

func (r *Runtime) PriceEventsFiltered(ctx context.Context, cycleID, appid int64, eventType string, limit int) ([]map[string]any, error) {
	query := `SELECT id,cycle_id AS cycleId,appid,region_code AS region,event_type AS eventType,previous_json AS previous,current_json AS current,CAST(occurred_at AS TEXT) AS occurredAt FROM price_events WHERE 1=1`
	args := []any{}
	if cycleID != 0 {
		query += ` AND cycle_id=?`
		args = append(args, cycleID)
	}
	if appid != 0 {
		query += ` AND appid=?`
		args = append(args, appid)
	}
	if eventType != "" {
		query += ` AND event_type=?`
		args = append(args, eventType)
	}
	query += ` ORDER BY occurred_at DESC,id DESC LIMIT ?`
	args = append(args, limit)
	rows, err := r.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	out, err := readRows(rows)
	if err != nil {
		return nil, err
	}
	for _, item := range out {
		if err = decodeFields(item, "previous", "current"); err != nil {
			return nil, err
		}
		if err = isoFields(item, "occurredAt"); err != nil {
			return nil, err
		}
	}
	return out, nil
}

func (r *Runtime) LatestFreshness(ctx context.Context) (map[string]any, error) {
	var latest sql.NullString
	if err := r.db.QueryRowContext(ctx, `SELECT CAST(MAX(last_success_at) AS TEXT) FROM game_current_prices`).Scan(&latest); err != nil {
		return nil, err
	}
	now := r.now()
	item := map[string]any{"lastSuccessAt": nil, "serverNow": readTimeISO(now) + now.Format("-07:00")}
	if latest.Valid {
		item["lastSuccessAt"] = latest.String
		if err := isoFields(item, "lastSuccessAt"); err != nil {
			return nil, err
		}
	}
	return item, nil
}

func readRows(rows *sql.Rows) ([]map[string]any, error) {
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		return nil, err
	}
	out := []map[string]any{}
	for rows.Next() {
		values := make([]any, len(columns))
		pointers := make([]any, len(columns))
		for i := range values {
			pointers[i] = &values[i]
		}
		if err = rows.Scan(pointers...); err != nil {
			return nil, err
		}
		item := map[string]any{}
		for i, key := range columns {
			if b, ok := values[i].([]byte); ok {
				values[i] = string(b)
			}
			item[key] = values[i]
		}
		out = append(out, item)
	}
	return out, rows.Err()
}
func decodeFields(row map[string]any, fields ...string) error {
	for _, key := range fields {
		if row[key] == nil {
			continue
		}
		text, ok := row[key].(string)
		if !ok {
			continue
		}
		var value any
		if err := json.Unmarshal([]byte(text), &value); err != nil {
			return fmt.Errorf("invalid %s JSON: %w", key, err)
		}
		row[key] = value
	}
	return nil
}
func isoFields(row map[string]any, fields ...string) error {
	for _, key := range fields {
		if row[key] == nil {
			continue
		}
		at, err := readTime(row[key].(string))
		if err != nil {
			return err
		}
		row[key] = readTimeISO(at)
	}
	return nil
}
func readTime(s string) (time.Time, error) {
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999", time.RFC3339Nano} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, nil
		}
	}
	return time.Time{}, fmt.Errorf("invalid crawl timestamp %q", s)
}
func readTimeISO(t time.Time) string {
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05")
	}
	return t.Format("2006-01-02T15:04:05.000000")
}
func readInt(v any) int64 {
	switch n := v.(type) {
	case int64:
		return n
	case int:
		return int64(n)
	case float64:
		return int64(n)
	}
	return 0
}
func roundRead(n float64, places int) float64 {
	value, _ := strconv.ParseFloat(strconv.FormatFloat(n, 'f', places, 64), 64)
	return value
}
func readRatios(expected, ok, locked any) (any, any) {
	if expected == nil || readInt(expected) == 0 || ok == nil || locked == nil {
		return nil, nil
	}
	n := float64(readInt(expected))
	return roundRead(float64(readInt(ok))/n, 4), roundRead(float64(readInt(ok)+readInt(locked))/n, 4)
}
func elapsedRead(start, end *time.Time) int64 {
	if start == nil || end == nil || end.Before(*start) {
		return 0
	}
	return end.Sub(*start).Milliseconds()
}
