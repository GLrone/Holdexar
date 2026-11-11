package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
	_ "modernc.org/sqlite"
)

func plannerFixture(t *testing.T) *Planner {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "planning.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err = db.Exec(store.CurrentSchemaSQL()); err != nil {
		t.Fatal(err)
	}
	return &Planner{DB: db, Now: func() time.Time { return time.Date(2026, 11, 6, 12, 0, 0, 0, time.FixedZone("CST", 8*3600)) }}
}

func planExec(t *testing.T, db *sql.DB, q string, args ...any) {
	t.Helper()
	if _, err := db.Exec(q, args...); err != nil {
		t.Fatal(err)
	}
}

func planGame(t *testing.T, db *sql.DB, id int) {
	planExec(t, db, `INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count)
 VALUES (?,'Game',0,0,0,0,0,0,0,0,0,0,0,0,0)`, id)
}

func TestPlanningRegionsAndScope(t *testing.T) {
	p := plannerFixture(t)
	ctx := context.Background()
	planExec(t, p.DB, `INSERT INTO crawl_regions(code,name,currency,enabled,sort) VALUES ('cn','China','CNY',1,1),('us','US','USD',0,2)`)
	r, err := p.Regions(ctx, nil)
	if err != nil || !reflect.DeepEqual(r, []Region{{"cn", "CNY"}}) {
		t.Fatalf("regions=%v err=%v", r, err)
	}
	r, err = p.Regions(ctx, []string{" US ", "us"})
	if err != nil || !reflect.DeepEqual(r, []Region{{"us", "USD"}}) {
		t.Fatalf("explicit=%v err=%v", r, err)
	}
	if _, err = p.Regions(ctx, []string{"zz"}); err == nil {
		t.Fatal("unknown region accepted")
	}
	planExec(t, p.DB, `UPDATE crawl_regions SET enabled=0`)
	if _, err = p.Regions(ctx, nil); err == nil {
		t.Fatal("disabled all became all regions")
	}
	for _, id := range []int{1, 2, 3, 4, 5, 6} {
		planGame(t, p.DB, id)
	}
	planExec(t, p.DB, `UPDATE games SET free_kind='f2p' WHERE appid=3`)
	planExec(t, p.DB, `INSERT INTO monitor_targets(target_type,target_id) VALUES ('game',1),('game',2),('game',3),('game',4),('game',5)`)
	planExec(t, p.DB, `INSERT INTO monitor_sources(target_type,target_id,source,priority,active) VALUES ('game',1,'favorite',100,1),('game',2,'favorite',100,1),('game',3,'favorite',100,1),('game',4,'owned',40,0),('game',5,'favorite',100,1)`)
	planExec(t, p.DB, `INSERT INTO monitor_exclusions(target_type,target_id,active) VALUES ('game',5,1)`)
	planExec(t, p.DB, `UPDATE games SET hl_flag=1 WHERE appid=2`)
	got, err := p.ResolveScope(ctx, "pool", nil)
	if err != nil || !reflect.DeepEqual(got, []int64{2, 1}) {
		t.Fatalf("pool=%v err=%v", got, err)
	}
	got, err = p.ResolveScope(ctx, "catalog", nil)
	if err != nil || !reflect.DeepEqual(got, []int64{6}) {
		t.Fatalf("catalog=%v err=%v", got, err)
	}
	p.Board = func(context.Context, string) ([]int64, error) { return []int64{1, 4, 6, 7, 7}, nil }
	got, err = p.ResolveScope(ctx, "discounts", nil)
	if err != nil || !reflect.DeepEqual(got, []int64{6, 7}) {
		t.Fatalf("discounts=%v err=%v", got, err)
	}
	got, err = p.ResolveScope(ctx, "specials", nil)
	if err != nil || !reflect.DeepEqual(got, []int64{7}) {
		t.Fatalf("specials=%v err=%v", got, err)
	}
	got, err = p.ResolveScope(ctx, "appids", []int64{3})
	if err != nil || !reflect.DeepEqual(got, []int64{3}) {
		t.Fatalf("explicit=%v err=%v", got, err)
	}
}

func TestPlanningDefaultQueueAndDeferredOrder(t *testing.T) {
	p := plannerFixture(t)
	ctx := context.Background()
	got, err := p.DefaultSpecs(ctx, false)
	want := []Spec{{Kind: "missing"}, {Scope: "pool"}, {Scope: "discounts", Kind: "discounts"}}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("defaults=%#v err=%v", got, err)
	}
	planExec(t, p.DB, `INSERT INTO price_cycles(kind,status,scope,specs_json) VALUES ('manual','partial','all','[{"scope":"pool","kind":"manual","execution":{"state":"deferred"}}]')`)
	got, err = p.DefaultSpecs(ctx, true)
	want = []Spec{{Scope: "pool"}, {Kind: "missing"}, {Scope: "catalog"}}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("prioritized=%#v err=%v", got, err)
	}
	planExec(t, p.DB, `INSERT INTO price_cycles(kind,status,scope,specs_json) VALUES ('scheduled','completed','pool','[{"scope":"pool","execution":{"state":"done"}}]')`)
	planExec(t, p.DB, `INSERT INTO app_settings(key,value_json) VALUES ('proxy.strategy','"proxy_first"')`)
	got, err = p.DefaultSpecs(ctx, false)
	want = []Spec{{Kind: "missing"}, {Scope: "pool"}, {Scope: "catalog"}, {Scope: "specials", Kind: "specials_backfill"}}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("proxy=%#v err=%v", got, err)
	}
}

func TestPlanningRecoveryOnlyUnobservedRegions(t *testing.T) {
	p := plannerFixture(t)
	ctx := context.Background()
	for id := 1; id <= 4; id++ {
		planGame(t, p.DB, id)
	}
	planExec(t, p.DB, `UPDATE games SET free_kind='f2p' WHERE appid=3`)
	planExec(t, p.DB, `INSERT INTO monitor_targets(target_type,target_id) VALUES ('game',4)`)
	planExec(t, p.DB, `INSERT INTO monitor_exclusions(target_type,target_id,active) VALUES ('game',4,1)`)
	planExec(t, p.DB, `INSERT INTO price_cycles(id,kind,status,scope) VALUES (1,'manual','cancelled','all')`)
	insertJob := func(status string, cycle any, targets any, skips []string, at string) {
		v := map[string]any{"targetStartedAt": at, "targets": targets}
		if skips != nil {
			v["skipUnits"] = skips
		}
		raw, _ := json.Marshal(v)
		planExec(t, p.DB, `INSERT INTO crawl_jobs(kind,status,cycle_id,stats_json) VALUES ('scheduled',?,?,?)`, status, cycle, string(raw))
	}
	insertJob("failed", nil, map[string]any{"appids": []int{1, 2, 3, 4}, "regions": []string{"CN", "US"}}, nil, "2026-11-06T10:00:00.000000")
	insertJob("stopped", nil, map[string]any{"appids": []int{100}, "regions": []string{"US"}}, nil, "2026-11-06T10:00:00.000000")
	insertJob("interrupted", 1, map[string]any{"appids": []int{101}, "regions": []string{"US"}}, nil, "2026-11-06T10:00:00.000000")
	insertJob("done", nil, map[string]any{}, []string{"2:CN", "2:US"}, "2026-11-06T11:00:00.000000")
	planExec(t, p.DB, `INSERT INTO game_current_prices(appid,region_code,price_status,updated_at,last_success_at,discount_percent,fail_count) VALUES
 (1,'CN','ok','2026-11-06 10:05:00.000000','2026-11-06 10:05:00.000000',0,0),
 (2,'US','missing','2026-11-06 11:59:00.000000',NULL,0,0),
 (5,'CN','blocked','2026-10-29 10:00:00.000000',NULL,0,0),
 (6,'CN','locked','2026-10-01 10:00:00.000000',NULL,0,0),
 (7,'','missing','2026-10-01 10:00:00.000000',NULL,0,0)`)
	got, err := p.Missing(ctx, 4*time.Minute)
	want := []TargetBatch{{AppIDs: []int64{5}, Region: "cn"}, {AppIDs: []int64{1}, Region: "us"}}
	if err != nil || !reflect.DeepEqual(got, want) {
		t.Fatalf("missing=%#v err=%v", got, err)
	}
	var at string
	if err = p.DB.QueryRow(`SELECT CAST(last_success_at AS TEXT) FROM game_current_prices WHERE appid=1 AND region_code='CN'`).Scan(&at); err != nil || at != "2026-11-06 10:05:00.000000" {
		t.Fatalf("success changed: %q %v", at, err)
	}
}

func TestPlanningRecoveryBatchLimit(t *testing.T) {
	p := plannerFixture(t)
	ids := make([]int, 601)
	for i := range ids {
		ids[i] = i + 1
	}
	raw, _ := json.Marshal(map[string]any{"targetStartedAt": "2026-11-06T10:00:00.000000", "targets": map[string]any{"tasks": []any{map[string]any{"region": "US", "appids": ids}}}})
	planExec(t, p.DB, `INSERT INTO crawl_jobs(kind,status,stats_json) VALUES ('repair','interrupted',?)`, string(raw))
	got, err := p.Missing(context.Background(), 4*time.Minute)
	if err != nil || len(got) != 3 || len(got[0].AppIDs) != 300 || len(got[1].AppIDs) != 300 || len(got[2].AppIDs) != 1 {
		t.Fatalf("batches=%#v err=%v", got, err)
	}
}

func TestPlanningFrozenEmptyScopeSurvivesJSON(t *testing.T) {
	s := Spec{Scope: "pool", ResolvedAppIDs: []int64{}, ResolvedRegions: []string{"cn"}}
	raw, err := json.Marshal(s)
	if err != nil {
		t.Fatal(err)
	}
	var got Spec
	if err = json.Unmarshal(raw, &got); err != nil {
		t.Fatal(err)
	}
	if got.ResolvedAppIDs == nil {
		t.Fatalf("frozen empty scope lost: %s", raw)
	}
	var unfrozen map[string]any
	raw, _ = json.Marshal(Spec{Scope: "pool"})
	if err = json.Unmarshal(raw, &unfrozen); err != nil {
		t.Fatal(err)
	}
	if _, exists := unfrozen["resolved_appids"]; exists {
		t.Fatalf("unfrozen scope marked frozen: %s", raw)
	}
}
