package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"reflect"
	"testing"
	"time"
)

func recoveryFixture(t *testing.T) (*Runtime, *sql.DB) {
	t.Helper()
	db, gate := runtimeFixture(t)
	now := time.Date(2026, 11, 6, 12, 0, 0, 0, time.FixedZone("CST", 8*3600))
	return New(db, gate, nil, Options{Now: func() time.Time { return now }}), db
}
func recoveryExec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.Exec(query, args...); err != nil {
		t.Fatal(err)
	}
}
func recoveryPlan(t *testing.T, db *sql.DB, id int64, status string, specs any) {
	t.Helper()
	encoded, err := json.Marshal(specs)
	if err != nil {
		t.Fatal(err)
	}
	recoveryExec(t, db, `INSERT INTO price_cycles(id,kind,status,scope,started_at,specs_json) VALUES(?,'scheduled',?,'pool','2026-11-06 10:00:00.000000',?)`, id, status, string(encoded))
}
func recoverySpecs(t *testing.T, db *sql.DB, id int64) []map[string]any {
	t.Helper()
	var raw string
	if err := db.QueryRow(`SELECT specs_json FROM price_cycles WHERE id=?`, id).Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var specs []map[string]any
	if err := json.Unmarshal([]byte(raw), &specs); err != nil {
		t.Fatal(err)
	}
	return specs
}

func TestRecoveryFrozenUnstartedStageAndIdempotency(t *testing.T) {
	r, db := recoveryFixture(t)
	ctx := context.Background()
	recoveryPlan(t, db, 1, "running", []any{map[string]any{"scope": "pool", "resolved_appids": []int64{620}, "resolved_regions": []string{"CN", "US"}, "custom": "preserve"}})
	recoveryExec(t, db, `UPDATE crawl_regions SET enabled=(code='jp')`)
	if err := r.Recover(ctx); err != nil {
		t.Fatal(err)
	}
	spec := recoverySpecs(t, db, 1)[0]
	execution := spec["execution"].(map[string]any)
	if execution["state"] != "recovering" || readInt(execution["jobId"]) <= 0 || spec["custom"] != "preserve" {
		t.Fatal(spec)
	}
	var jobID, cycleID int64
	var kind, status, mode, regions, raw, start, finish, reason string
	if err := db.QueryRow(`SELECT id,cycle_id,kind,status,mode,regions_json,stats_json,CAST(started_at AS TEXT),CAST(finished_at AS TEXT),error FROM crawl_jobs`).Scan(&jobID, &cycleID, &kind, &status, &mode, &regions, &raw, &start, &finish, &reason); err != nil {
		t.Fatal(err)
	}
	if jobID != readInt(execution["jobId"]) || cycleID != 1 || kind != "scheduled" || status != "interrupted" || mode != "app" || regions != `["CN","US"]` || start != "2026-11-06 10:00:00.000000" || finish != "2026-11-06 12:00:00.000000" || reason != "进程中断，未完成地区将自动补全" {
		t.Fatalf("recovered job id=%d cycle=%d kind=%s status=%s mode=%s regions=%s start=%s finish=%s reason=%s", jobID, cycleID, kind, status, mode, regions, start, finish, reason)
	}
	var stats map[string]any
	if err := json.Unmarshal([]byte(raw), &stats); err != nil {
		t.Fatal(err)
	}
	if stats["targetStartedAt"] != "2026-11-06T10:00:00" {
		t.Fatal(stats)
	}
	want := []TargetBatch{{AppIDs: []int64{620}, Region: "cn"}, {AppIDs: []int64{620}, Region: "us"}}
	got, err := r.planner.Missing(ctx, 0)
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("frozen tasks=%+v err=%v", got, err)
	}
	recoveryExec(t, db, `INSERT INTO game_current_prices(appid,region_code,discount_percent,fail_count,price_status,updated_at,last_success_at) VALUES(620,'CN',0,0,'ok','2026-11-06 10:05:00.000000','2026-11-06 10:05:00.000000')`)
	got, err = r.planner.Missing(ctx, 0)
	if err != nil || !reflect.DeepEqual(got, want[1:]) {
		t.Fatalf("remaining tasks=%+v err=%v", got, err)
	}
	before := jsonString(recoverySpecs(t, db, 1))
	if err = r.Recover(ctx); err != nil {
		t.Fatal(err)
	}
	if after := jsonString(recoverySpecs(t, db, 1)); before != after {
		t.Fatalf("second recovery changed specs: %s / %s", before, after)
	}
	var count int
	if err = db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs`).Scan(&count); err != nil || count != 1 {
		t.Fatalf("duplicate count=%d err=%v", count, err)
	}
}

func TestRecoveryExistingJobsAndTerminalStageDecisions(t *testing.T) {
	r, db := recoveryFixture(t)
	specs := []any{}
	for _, tc := range []struct {
		id            int64
		status, stats string
	}{{1, "running", `{"targets":{"appids":[1],"regions":["CN"]},"targetStartedAt":"2026-11-06T10:00:00"}`}, {2, "done", `{"targets":{"appids":[2]}}`}, {3, "stopped", `{"targets":{"appids":[3]}}`}, {4, "failed", `{"targets":{"appids":[4]}}`}, {5, "failed", `{"other":1}`}} {
		recoveryExec(t, db, `INSERT INTO crawl_jobs(id,kind,status,cycle_id,stats_json,started_at) VALUES(?,'scheduled',?,1,?,'2026-11-06 10:00:00.000000')`, tc.id, tc.status, tc.stats)
		specs = append(specs, map[string]any{"scope": "appids", "resolved_appids": []int64{tc.id}, "resolved_regions": []string{"CN"}, "execution": map[string]any{"state": "pending", "jobId": tc.id, "reason": "keep reason"}})
	}
	specs = append(specs, map[string]any{"scope": "appids", "resolved_appids": []int64{9}, "execution": map[string]any{"state": "deferred", "jobId": 999}}, map[string]any{"scope": "pool", "resolved_appids": []int64{}, "execution": map[string]any{"state": "pending"}}, map[string]any{"kind": "missing", "execution": map[string]any{"state": "deferred"}})
	recoveryPlan(t, db, 1, "running", specs)
	if err := r.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	got := recoverySpecs(t, db, 1)
	want := []string{"recovering", "done", "cancelled", "recovering", "done", "done", "done", "deferred"}
	for i, spec := range got {
		execution := spec["execution"].(map[string]any)
		if execution["state"] != want[i] {
			t.Fatalf("stage %d execution=%+v want=%s", i, execution, want[i])
		}
		if i < 5 && (execution["jobId"] != float64(i+1) || execution["reason"] != "keep reason") {
			t.Fatal(execution)
		}
	}
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs`).Scan(&count); err != nil || count != 5 {
		t.Fatalf("created duplicate jobs count=%d err=%v", count, err)
	}
}

func TestRecoveryCancellationAndExistingCompletionExcluded(t *testing.T) {
	r, db := recoveryFixture(t)
	specs := []any{map[string]any{"scope": "pool", "resolved_appids": []int64{620}, "resolved_regions": []string{"CN"}, "execution": map[string]any{"state": "pending"}}}
	recoveryPlan(t, db, 1, "cancelled", specs)
	recoveryPlan(t, db, 2, "completed", specs)
	recoveryPlan(t, db, 3, "partial", []any{map[string]any{"scope": "pool", "resolved_appids": []int64{620}, "execution": map[string]any{"state": "cancelled"}}, map[string]any{"scope": "discounts", "resolved_appids": []int64{621}, "execution": map[string]any{"state": "done"}}})
	if err := r.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("cancelled/completed jobs created=%d error=%v", count, err)
	}
	if got := recoverySpecs(t, db, 1)[0]["execution"].(map[string]any)["state"]; got != "pending" {
		t.Fatalf("cancelled plan rewritten: %v", got)
	}
}

func TestRecoveryCanonicalPlannerRetainsExclusionAndFreeFilters(t *testing.T) {
	r, db := recoveryFixture(t)
	for id := 1; id <= 4; id++ {
		planGame(t, db, id)
	}
	recoveryExec(t, db, `UPDATE games SET free_kind='f2p' WHERE appid=3`)
	recoveryExec(t, db, `INSERT INTO monitor_targets(target_type,target_id) VALUES('game',2),('game',4)`)
	recoveryExec(t, db, `INSERT INTO monitor_sources(target_type,target_id,source,priority,active) VALUES('game',2,'manual',1,1)`)
	recoveryExec(t, db, `INSERT INTO monitor_exclusions(target_type,target_id,active) VALUES('game',2,1)`)
	recoveryPlan(t, db, 1, "running", []any{map[string]any{"scope": "catalog", "resolved_appids": []int64{1, 2, 3, 4}, "resolved_regions": []string{"US"}}})
	if err := r.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	got, err := r.planner.Missing(context.Background(), 0)
	want := []TargetBatch{{AppIDs: []int64{1}, Region: "us"}}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("planner filters bypassed=%+v err=%v", got, err)
	}
}

func TestRecoverySameTransactionRollbackAndRetry(t *testing.T) {
	r, db := recoveryFixture(t)
	recoveryPlan(t, db, 1, "running", []any{map[string]any{"scope": "pool", "resolved_appids": []int64{620}, "resolved_regions": []string{"CN"}}})
	recoveryExec(t, db, `CREATE TRIGGER fail_recovery BEFORE UPDATE OF specs_json ON price_cycles BEGIN SELECT RAISE(ABORT,'recovery failure'); END`)
	if err := r.Recover(context.Background()); err == nil {
		t.Fatal("injected recovery error swallowed")
	}
	var status string
	var count int
	if err := db.QueryRow(`SELECT status FROM price_cycles WHERE id=1`).Scan(&status); err != nil || status != "running" {
		t.Fatalf("cycle state partially committed=%s err=%v", status, err)
	}
	if err := db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs`).Scan(&count); err != nil || count != 0 {
		t.Fatalf("recovery jobs partially committed=%d err=%v", count, err)
	}
	recoveryExec(t, db, `DROP TRIGGER fail_recovery`)
	if err := r.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs`).Scan(&count); err != nil || count != 1 {
		t.Fatalf("retry jobs=%d err=%v", count, err)
	}
}

func TestRecoveryBatchesCorrelateDuplicateAndDistinctFrozenStages(t *testing.T) {
	r, db := recoveryFixture(t)
	specs := make([]any, 502)
	for i := range specs {
		specs[i] = map[string]any{"scope": "appids", "kind": "manual", "resolved_appids": []int64{int64(i + 1)}, "resolved_regions": []string{"US"}}
	}
	specs[501] = specs[500]
	recoveryPlan(t, db, 1, "running", specs)
	if err := r.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	got := recoverySpecs(t, db, 1)
	seen := map[int64]bool{}
	for _, spec := range got {
		execution := spec["execution"].(map[string]any)
		id := readInt(execution["jobId"])
		if id <= 0 || seen[id] || execution["state"] != "recovering" {
			t.Fatalf("invalid stage association=%+v", spec)
		}
		seen[id] = true
	}
	if len(seen) != 502 {
		t.Fatalf("associated jobs=%d", len(seen))
	}
	if err := r.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM crawl_jobs`).Scan(&count); err != nil || count != 502 {
		t.Fatalf("batch duplicates=%d err=%v", count, err)
	}
}
