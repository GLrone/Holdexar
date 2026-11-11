package migration

import (
	"context"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

func TestPriceSnapshotMigrationRebuildsBothDomainsWithoutHistoryRewrite(t *testing.T) {
	db, conn := middleTestConn(t)
	ctx := context.Background()
	if err := store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	for _, q := range []string{
		`INSERT INTO games(appid,name,name_en,type,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,positive_rate) VALUES(1,'Alpha','Alpha','GAME',0,0,0,0,0,0,0,0,0,0,900,1000,0,9000),(2,'Separate','Separate','GAME',0,0,0,0,0,0,0,0,0,0,0,0,0,NULL)`,
		`INSERT INTO fx_rates(currency_code,rate_to_cny) VALUES('USD',0.5)`,
		`INSERT INTO game_current_prices(appid,region_code,currency,price,original_price,discount_percent,price_status,fail_count,cny_fen,attempt_outcome,updated_at) VALUES(1,'CN','CNY',1000,2000,50,'ok',0,77,'success','2026-11-05 12:00:00'),(1,'US','usd',3,3,0,'ok',0,99,'success','2026-11-05 12:00:00')`,
		`INSERT INTO game_price_history(appid,region_code,currency,price,original_price,discount_percent,is_gold,is_bundle,price_status,cny_fen,product_unknown,snapshot_at) VALUES(1,'CN','CNY',1000,2000,50,0,0,'ok',555,0,'2026-11-01 12:00:00')`,
		`INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind,is_lowest,diff_fen,view_count) VALUES(100,'Bundle',0,0,0,0,0)`,
		`INSERT INTO bundle_region_prices(bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids) VALUES(100,'CN','CNY',1000,50,10,'ok',55,'[1,2]'),(100,'US','USD',3,0,0,'ok',99,'[1,2]')`,
		`UPDATE crawl_regions SET enabled=CASE WHEN code='us' THEN 1 ELSE 0 END`,
	} {
		if err := migrationExec(ctx, conn, q); err != nil {
			t.Fatal(err)
		}
	}
	dataDir, err := migrationDataDir(ctx, conn)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "series_overrides.json"), []byte(`{"Z":[1,2],"A":[1]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := middleTestStep(t, conn, 21); err != nil {
		t.Fatal(err)
	}
	for _, table := range []string{"games", "bundles"} {
		var minimum, difference int
		if err := conn.QueryRowContext(ctx, "SELECT min_cny_fen,diff_fen FROM "+table).Scan(&minimum, &difference); err != nil || minimum != 2 || difference != 998 {
			t.Fatalf("%s projection=%d/%d, %v", table, minimum, difference, err)
		}
	}
	for _, item := range []struct {
		query string
		score float64
	}{{"SELECT smart_score FROM games WHERE appid=1", 0.41941809990414886}, {"SELECT smart_score FROM games WHERE appid=2", 0.14}, {"SELECT smart_score FROM bundles WHERE bundle_id=100", 0.43741254229309495}} {
		var score float64
		if err := conn.QueryRowContext(ctx, item.query).Scan(&score); err != nil || math.Abs(score-item.score) > 1e-12 {
			t.Fatalf("reference score=%v want %v, %v", score, item.score, err)
		}
	}
	var hl, pp int
	if err := conn.QueryRowContext(ctx, "SELECT hl_flag,pp_flag FROM games WHERE appid=1").Scan(&hl, &pp); err != nil || hl != 3 || pp != 0 {
		t.Fatalf("reference history flags=%d/%d, %v", hl, pp, err)
	}
	var historical int
	if err := conn.QueryRowContext(ctx, "SELECT cny_fen FROM game_price_history").Scan(&historical); err != nil || historical != 555 {
		t.Fatalf("historical amount=%d, %v", historical, err)
	}
	var series string
	for _, id := range []int{1, 2} {
		if err := conn.QueryRowContext(ctx, "SELECT series_id FROM games WHERE appid=?", id).Scan(&series); err != nil || series != "Z" {
			t.Fatalf("series for %d=%q, %v", id, series, err)
		}
	}
	var input string
	if err := conn.QueryRowContext(ctx, "SELECT value_json FROM app_settings WHERE key='games.derived_inputs'").Scan(&input); err != nil {
		t.Fatal(err)
	}
	var version struct {
		Day    string
		Series map[string][]int64
	}
	if err := json.Unmarshal([]byte(input), &version); err != nil || version.Day == "" || !reflect.DeepEqual(version.Series, map[string][]int64{"Z": {1, 2}, "A": {1}}) {
		t.Fatalf("input version=%q, %v", input, err)
	}
}

func TestMigrationSeriesOverridesKeepsFileOrderAndFiltersInvalidEntries(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "series_overrides.json")
	if err := os.WriteFile(path, []byte(`{" Z ":[3,"2",2,1.9,true,null,-1,"bad",{}],"A":[5],"bad":{}," ":[9],"Z":[8,4]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	got, order := derived.LoadSeriesOverrides(dir)
	if !reflect.DeepEqual(order, []string{"Z", "A"}) || !reflect.DeepEqual(got, map[string][]int64{"Z": {4, 8}, "A": {5}}) {
		t.Fatalf("overrides=%#v, order=%#v", got, order)
	}
	for _, bad := range []string{"not json", `{"Z":[1]} trailing`, "[]"} {
		if err := os.WriteFile(path, []byte(bad), 0o600); err != nil {
			t.Fatal(err)
		}
		if got, _ := derived.LoadSeriesOverrides(dir); len(got) != 0 {
			t.Fatalf("accepted malformed file %q: %#v", bad, got)
		}
	}
	if err := os.WriteFile(path, []byte(`{"Z":[1],"Z":false}`), 0o600); err != nil {
		t.Fatal(err)
	}
	if got, _ := derived.LoadSeriesOverrides(dir); len(got) != 0 {
		t.Fatalf("ignored final duplicate key value: %#v", got)
	}
}
