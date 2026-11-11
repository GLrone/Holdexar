package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"reflect"
	"testing"
	"time"
)

func readFixture(t *testing.T) (*Runtime, *sql.DB) {
	t.Helper()
	db, gate := runtimeFixture(t)
	now := time.Date(2026, 11, 6, 12, 0, 0, 123456000, time.FixedZone("CST", 8*3600))
	return New(db, gate, nil, Options{Now: func() time.Time { return now }}), db
}
func readExec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.Exec(query, args...); err != nil {
		t.Fatal(err)
	}
}
func readCycle(t *testing.T, db *sql.DB, id int64, expected string) {
	readExec(t, db, `INSERT INTO price_cycles(id,kind,status,scope,expected_json,started_at,running_at,finished_at) VALUES(?,'scheduled','completed','pool',?,'2026-11-06 10:00:00.000000','2026-11-06 10:01:00.000000','2026-11-06 11:00:00.000000')`, id, expected)
}
func readCurrent(t *testing.T, db *sql.DB, id int64, region, status, outcome, answer, at string) {
	readExec(t, db, `INSERT INTO game_current_prices(appid,region_code,discount_percent,fail_count,price_status,attempt_outcome,steam_answer,updated_at) VALUES(?,?,0,0,?,?,?,?)`, id, region, status, outcome, answer, at)
}

func TestReadJobsPreservesCurrentFactsAndNulls(t *testing.T) {
	r, db := readFixture(t)
	readExec(t, db, `INSERT INTO crawl_jobs(id,kind,status,mode,cycle_id,regions_json,stats_json,started_at) VALUES(1,'manual','running','app',NULL,'["cn"]','{"total":5,"processed":2,"targets":{"appids":[620],"regions":["CN"]}}','2026-11-06 10:00:00.000123')`)
	readExec(t, db, `INSERT INTO crawl_jobs(id,kind,status,finished_at,error) VALUES(2,'scheduled','failed','2026-11-06 11:00:00.000000','network failed')`)
	rows, err := r.Jobs(context.Background(), 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 2 || rows[0]["id"] != int64(2) || rows[1]["id"] != int64(1) {
		t.Fatal(rows)
	}
	if rows[0]["stats"] != nil || rows[0]["regions"] != nil || rows[0]["startedAt"] != nil || rows[1]["cycleId"] != nil || rows[1]["finishedAt"] != nil {
		t.Fatalf("nulls lost: %+v", rows)
	}
	if rows[1]["startedAt"] != "2026-11-06T10:00:00.000123" || rows[0]["finishedAt"] != "2026-11-06T11:00:00" {
		t.Fatalf("timestamps=%+v", rows)
	}
	stats := rows[1]["stats"].(map[string]any)
	if stats["processed"] != float64(2) || stats["targets"] == nil {
		t.Fatal(stats)
	}
	readExec(t, db, `UPDATE crawl_jobs SET stats_json=json_set(stats_json,'$.processed',4) WHERE id=1`)
	rows, err = r.Jobs(context.Background(), 1)
	if err != nil || len(rows) != 1 || rows[0]["id"] != int64(2) {
		t.Fatalf("limit=%+v err=%v", rows, err)
	}
}

func TestReadCyclesFrozenStatsAndDeferredSpecs(t *testing.T) {
	r, db := readFixture(t)
	readCycle(t, db, 1, `{"appids":[620,621],"regions":["cn","us"]}`)
	readExec(t, db, `UPDATE price_cycles SET repairing_at='2026-11-06 10:30:00.000000',finalizing_at='2026-11-06 10:59:00.000000',batches_expected=4,batches_done=3,targets_total=2,targets_done=1,units_expected=4,units_ok=1,units_locked=1,units_failed=1,units_unobserved=1,stale_count=0,specs_json='[{"scope":"pool","execution":{"state":"done"}},{"scope":"discounting","resolved_appids":[620],"execution":{"state":"deferred","reason":"transport"}},{"kind":"missing","execution":{"state":"recovering"}},{"scope":"rank","execution":{"state":"pending"}}]' WHERE id=1`)
	readCycle(t, db, 2, `{"appids":[],"regions":["cn"]}`)
	readExec(t, db, `UPDATE price_cycles SET units_expected=0,units_ok=0,units_locked=0,units_failed=0,units_unobserved=0,targets_total=0,targets_done=0,stale_count=0 WHERE id=2`)
	readCycle(t, db, 3, `{"appids":[620],"regions":["cn"]}`)
	readExec(t, db, `INSERT INTO crawl_jobs(id,kind,status,cycle_id) VALUES(4,'scheduled','done',1),(5,'missing','failed',1),(6,'manual','running',NULL)`)
	rows, err := r.Cycles(context.Background(), 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 3 || rows[0]["id"] != int64(3) || rows[0]["stats"] != nil {
		t.Fatalf("unwritten stats fabricated: %+v", rows)
	}
	if rows[0]["batchesExpected"] != nil || rows[0]["batchesDone"] != nil {
		t.Fatal("null batch count changed")
	}
	empty := rows[1]["stats"].(map[string]any)
	if empty["unitsExpected"] != int64(0) || empty["coverage"] != nil || empty["coverageConfirmed"] != nil {
		t.Fatal(empty)
	}
	cycle := rows[2]
	stats := cycle["stats"].(map[string]any)
	if cycle["expectedUnits"] != 4 || cycle["enteredRepairing"] != true || cycle["batchesExpected"] != int64(4) || stats["coverage"] != 0.25 || stats["coverageConfirmed"] != 0.5 || stats["durationSeconds"] != float64(3600) {
		t.Fatalf("cycle=%+v", cycle)
	}
	wantStages := map[string]any{"planning": int64(60000), "running": int64(1740000), "repairing": int64(1740000), "finalizing": int64(60000)}
	if !reflect.DeepEqual(stats["stageMs"], wantStages) {
		t.Fatalf("stages=%+v", stats["stageMs"])
	}
	deferred := cycle["deferredSpecs"].([]any)
	if len(deferred) != 3 || deferred[0].(map[string]any)["resolved_appids"] == nil {
		t.Fatal(deferred)
	}
	jobs := cycle["jobs"].([]map[string]any)
	if len(jobs) != 2 || jobs[0]["id"] != int64(4) || jobs[1]["id"] != int64(5) {
		t.Fatal(jobs)
	}
}

func TestReadCoverageObservedWindowAndFrozenLegacyFallback(t *testing.T) {
	r, db := readFixture(t)
	ctx := context.Background()
	readCycle(t, db, 1, `{"appids":[1,2,3],"regions":["cn","US","cn"]}`)
	readCurrent(t, db, 1, "CN", "ok", "success", "ok", "2026-11-06 10:10:00.000000")
	readCurrent(t, db, 1, "US", "ok", "success", "no_options", "2026-11-06 10:10:00.000000")
	readCurrent(t, db, 2, "CN", "ok", "failed", "", "2026-11-06 10:10:00.000000")
	readCurrent(t, db, 2, "US", "blocked", "failed", "", "2026-11-06 10:10:00.000000")
	readCurrent(t, db, 3, "CN", "ok", "success", "ok", "2026-11-06 09:10:00.000000")
	readCurrent(t, db, 3, "US", "ok", "success", "ok", "2026-11-06 11:10:00.000000")
	readCurrent(t, db, 1, "JP", "ok", "success", "ok", "2026-11-06 10:10:00.000000")
	got, err := r.Coverage(ctx, 1)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]any{"cycleId": int64(1), "status": "completed", "targetsTotal": int64(3), "targetsDone": int64(2), "expectedUnits": int64(6), "ok": int64(1), "locked": int64(1), "missing": int64(1), "blocked": int64(1), "unobserved": int64(2), "coverage": 0.1667, "coverageConfirmed": 0.3333}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("coverage=%+v want=%+v", got, want)
	}
	readExec(t, db, `UPDATE price_cycles SET units_expected=6,units_ok=1,units_locked=1,units_failed=2,units_unobserved=2,targets_total=3,targets_done=2 WHERE id=1`)
	readExec(t, db, `UPDATE game_current_prices SET updated_at='2026-11-06 14:00:00.000000'`)
	got, err = r.Coverage(ctx, 1)
	if err != nil {
		t.Fatal(err)
	}
	want["missing"] = int64(2)
	want["blocked"] = int64(0)
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("frozen coverage drifted=%+v", got)
	}
	readExec(t, db, `UPDATE price_cycles SET units_missing=1,units_blocked=1 WHERE id=1`)
	got, err = r.Coverage(ctx, 1)
	if err != nil || got["missing"] != int64(1) || got["blocked"] != int64(1) {
		t.Fatalf("split coverage=%+v err=%v", got, err)
	}
	if got, err = r.Coverage(ctx, 999); err != nil || got != nil {
		t.Fatalf("unknown=%+v err=%v", got, err)
	}
	readCycle(t, db, 2, `{"appids":[],"regions":[]}`)
	got, err = r.Coverage(ctx, 2)
	if err != nil || got["coverage"] != nil || got["coverageConfirmed"] != nil || got["expectedUnits"] != int64(0) {
		t.Fatalf("empty coverage=%+v err=%v", got, err)
	}
}

func TestReadPriceEventsFiltersOccurrenceAndPayload(t *testing.T) {
	r, db := readFixture(t)
	ctx := context.Background()
	readExec(t, db, `INSERT INTO price_events(id,cycle_id,appid,region_code,event_type,previous_json,current_json,occurred_at) VALUES(1,2,620,'CN','PRICE_DROP','{"price":200}','{"price":100,"currency":"CNY"}','2026-11-06 11:00:00.000123'),(2,2,621,NULL,'FREE_PROMO',NULL,'{"freeKind":"promo"}','2026-11-06 10:00:00.000000'),(3,-4,620,'US','PRICE_INCREASE','null','{"price":300}','2026-11-06 09:00:00.000000')`)
	rows, err := r.PriceEvents(ctx, 0, 10)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 3 || rows[0]["id"] != int64(1) || rows[1]["region"] != nil || rows[1]["previous"] != nil || rows[2]["previous"] != nil || rows[0]["occurredAt"] != "2026-11-06T11:00:00.000123" {
		t.Fatal(rows)
	}
	rows, err = r.PriceEvents(ctx, 2, 1)
	if err != nil || len(rows) != 1 || rows[0]["id"] != int64(1) {
		t.Fatalf("cycle+limit=%+v err=%v", rows, err)
	}
	rows, err = r.PriceEventsFiltered(ctx, 0, 620, "PRICE_INCREASE", 10)
	if err != nil || len(rows) != 1 || rows[0]["cycleId"] != int64(-4) {
		t.Fatalf("filters=%+v err=%v", rows, err)
	}
	rows, err = r.PriceEvents(ctx, 999, 10)
	if err != nil || len(rows) != 0 {
		t.Fatalf("unknown=%+v err=%v", rows, err)
	}
}

func TestReadLatestFreshnessUsesSuccessAcrossAllTriggers(t *testing.T) {
	r, db := readFixture(t)
	ctx := context.Background()
	got, err := r.LatestFreshness(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if got["lastSuccessAt"] != nil || got["serverNow"] != "2026-11-06T12:00:00.123456+08:00" {
		t.Fatal(got)
	}
	readCurrent(t, db, 620, "CN", "ok", "failed", "", "2026-11-06 11:59:00.000000")
	readCurrent(t, db, 620, "US", "locked", "success", "locked", "2026-11-06 10:00:00.000000")
	readExec(t, db, `UPDATE game_current_prices SET last_success_at=CASE region_code WHEN 'CN' THEN '2026-11-06 09:00:00.000000' ELSE '2026-11-06 10:00:00.000000' END`)
	readCycle(t, db, 99, `{"appids":[620],"regions":["cn"]}`)
	readExec(t, db, `UPDATE price_cycles SET finished_at='2026-11-06 11:59:59.000000' WHERE id=99`)
	got, err = r.LatestFreshness(ctx)
	if err != nil || got["lastSuccessAt"] != "2026-11-06T10:00:00" {
		t.Fatalf("success clock=%+v err=%v", got, err)
	}
	encoded, err := json.Marshal(got)
	if err != nil || len(encoded) == 0 {
		t.Fatalf("JSON=%s err=%v", encoded, err)
	}
}

func TestReadCyclesTimingSkipsAbsentAndClampsReversedStages(t *testing.T) {
	r, db := readFixture(t)
	readCycle(t, db, 1, `{"appids":[],"regions":[]}`)
	readExec(t, db, `UPDATE price_cycles SET units_expected=0,units_ok=0,units_locked=0,finished_at=NULL,running_at='2026-11-06 09:00:00.000000' WHERE id=1`)
	rows, err := r.Cycles(context.Background(), 10)
	if err != nil {
		t.Fatal(err)
	}
	stats := rows[0]["stats"].(map[string]any)
	if stats["durationSeconds"] != nil {
		t.Fatal(stats)
	}
	for _, v := range stats["stageMs"].(map[string]any) {
		if v != int64(0) {
			t.Fatal(stats)
		}
	}
}

func TestReadRoundingPreservesBinaryFloatBeforeDecimalRounding(t *testing.T) {
	for _, tc := range []struct {
		value  float64
		places int
		want   float64
	}{{0.15, 1, 0.1}, {0.25, 1, 0.2}, {2.675, 2, 2.67}, {0.00005, 4, 0.0001}, {-0.15, 1, -0.1}} {
		if got := roundRead(tc.value, tc.places); got != tc.want {
			t.Fatalf("round(%v,%d)=%v want %v", tc.value, tc.places, got, tc.want)
		}
	}
	r, db := readFixture(t)
	readCycle(t, db, 1, `{"appids":[],"regions":[]}`)
	readExec(t, db, `UPDATE price_cycles SET units_expected=20000,units_ok=1,units_locked=0,finished_at='2026-11-06 10:00:00.150000' WHERE id=1`)
	rows, err := r.Cycles(context.Background(), 1)
	if err != nil {
		t.Fatal(err)
	}
	stats := rows[0]["stats"].(map[string]any)
	if stats["durationSeconds"] != 0.1 || stats["coverage"] != 0.0001 {
		t.Fatalf("decimal boundary projection=%+v", stats)
	}
}
