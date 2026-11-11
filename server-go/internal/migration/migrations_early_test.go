package migration

import (
	"context"
	"database/sql"
	"fmt"
	"math"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func earlyTestConnection(t *testing.T, statements ...string) *sql.Conn {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "early.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	conn, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	if err := migrationExec(context.Background(), conn, statements...); err != nil {
		t.Fatal(err)
	}
	return conn
}

func earlyTestStep(t *testing.T, conn *sql.Conn, version int) error {
	t.Helper()
	ctx := context.Background()
	if _, err := conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	err := earlyMigrationSteps[version](ctx, conn)
	if err != nil {
		if _, rollbackErr := conn.ExecContext(ctx, "ROLLBACK"); rollbackErr != nil {
			t.Fatalf("migration error %v; rollback error %v", err, rollbackErr)
		}
		return err
	}
	if _, err := conn.ExecContext(ctx, "COMMIT"); err != nil {
		t.Fatal(err)
	}
	return nil
}

func earlyTestInt(t *testing.T, conn *sql.Conn, query string, want int64, args ...any) {
	t.Helper()
	var got int64
	if err := conn.QueryRowContext(context.Background(), query, args...).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("%s = %d, want %d", query, got, want)
	}
}

func TestEarlyMigrationStepsPresent(t *testing.T) {
	if len(earlyMigrationSteps) != 12 {
		t.Fatalf("early steps count = %d, want 12", len(earlyMigrationSteps))
	}
	for version := 2; version <= 13; version++ {
		if earlyMigrationSteps[version] == nil {
			t.Errorf("missing version %d", version)
		}
	}
}

func TestEarlyMigrationMoneyBoundaries(t *testing.T) {
	if got := migrationAmountDistance(math.MinInt64, math.MaxInt64); got != math.MaxUint64 {
		t.Fatalf("integer distance overflowed: %d", got)
	}
	for _, value := range []float64{math.Inf(1), math.NaN(), 9223372036854775808.0} {
		if _, err := migrationRoundedAmount(value); err == nil {
			t.Fatalf("accepted invalid rounded amount %v", value)
		}
	}
	for _, check := range []struct {
		input float64
		want  int64
	}{{2.5, 2}, {3.5, 4}, {-2.5, -2}, {-3.5, -4}} {
		if got, err := migrationRoundedAmount(check.input); err != nil || got != check.want {
			t.Fatalf("round(%v) = %d, %v", check.input, got, err)
		}
	}
}

const earlyBundleFixture = `CREATE TABLE fx_rates(currency_code TEXT PRIMARY KEY,rate_to_cny REAL);
	CREATE TABLE bundles(bundle_id INTEGER PRIMARY KEY,must_purchase_as_set INTEGER,item_kind INTEGER);
	CREATE TABLE bundle_region_prices(id INTEGER PRIMARY KEY,bundle_id INTEGER,currency TEXT,price INTEGER,original_price INTEGER,cny_fen INTEGER);
	INSERT INTO fx_rates VALUES('JPY',0.05),('COP',0.5);
	INSERT INTO bundles VALUES(1,0,NULL),(2,1,-1),(3,0,1),(4,0,NULL),(5,1,NULL),(6,0,NULL),(7,NULL,NULL),(8,0,NULL),(9,1,NULL);
	INSERT INTO bundle_region_prices VALUES
	(1,1,'USD',5000,6000,5000),(2,1,'JPY',1000,1500,5000),
	(3,2,'USD',5000,6000,5000),(4,2,'JPY',100000,150000,500000),
	(5,3,'USD',4000,5000,4000),(6,3,'USD',5000,6000,5000),(7,3,'JPY',1000,NULL,50),
	(8,4,'JPY',1000,0,50),(9,5,'JPY',1000,1500,5000),(10,6,'JPY',1000,1500,5000),
	(11,7,'JPY',1000,1500,5000),(12,8,'HUF',1000,1500,5000),
	(13,9,'COP',5,NULL,250),(14,9,'COP',7,0,350),(15,9,'JPY',0,1500,5000),(16,9,'JPY',1000,1500,NULL);`

func TestEarlyMigrationBundlePriceUnits(t *testing.T) {
	conn := earlyTestConnection(t, earlyBundleFixture)
	if err := earlyTestStep(t, conn, 2); err != nil {
		t.Fatal(err)
	}
	checks := []struct {
		id, price, fen int64
		original       any
	}{
		{2, 100000, 5000, int64(150000)}, {4, 100000, 5000, int64(150000)},
		{7, 100000, 5000, nil}, {8, 1000, 50, int64(0)},
		{9, 1000, 50, int64(1500)}, {10, 100000, 5000, int64(150000)},
		{11, 1000, 5000, int64(1500)}, {12, 1000, 5000, int64(1500)},
		{13, 5, 2, nil}, {14, 7, 4, int64(0)}, {15, 0, 5000, int64(1500)},
	}
	for _, check := range checks {
		var price, fen int64
		var original any
		if err := conn.QueryRowContext(context.Background(), "SELECT price,original_price,cny_fen FROM bundle_region_prices WHERE id=?", check.id).Scan(&price, &original, &fen); err != nil {
			t.Fatal(err)
		}
		if price != check.price || fen != check.fen || original != check.original {
			t.Errorf("row %d = (%d,%v,%d), want (%d,%v,%d)", check.id, price, original, fen, check.price, check.original, check.fen)
		}
	}
	if err := earlyTestStep(t, conn, 2); err != nil {
		t.Fatal(err)
	}
	earlyTestInt(t, conn, "SELECT price FROM bundle_region_prices WHERE id=2", 100000)
	earlyTestInt(t, conn, "SELECT cny_fen IS NULL FROM bundle_region_prices WHERE id=16", 1)
}

func TestEarlyMigrationBundleOverflowPreservesRows(t *testing.T) {
	conn := earlyTestConnection(t, earlyBundleFixture,
		"INSERT INTO bundle_region_prices VALUES(20,6,'JPY',100000000000000000,1,500000000000000000)")
	if err := earlyTestStep(t, conn, 2); err == nil || !strings.Contains(err.Error(), "integer range") {
		t.Fatalf("expected safe overflow failure, got %v", err)
	}
	earlyTestInt(t, conn, "SELECT price FROM bundle_region_prices WHERE id=2", 1000)
	earlyTestInt(t, conn, "SELECT price FROM bundle_region_prices WHERE id=20", 100000000000000000)
}

func TestEarlyMigrationSQLSteps(t *testing.T) {
	conn := earlyTestConnection(t, earlyBundleFixture,
		`CREATE TABLE wishlist_items(id INTEGER PRIMARY KEY,owned INTEGER,active INTEGER,manual INTEGER,wishlisted INTEGER);
		INSERT INTO wishlist_items VALUES(1,0,1,0,0),(2,1,1,0,0),(3,0,0,0,0),(4,0,1,1,0);
		CREATE TABLE game_achievements(id INTEGER);CREATE TABLE player_achievements(id INTEGER);
		CREATE TABLE achievement_defs(id INTEGER);INSERT INTO achievement_defs VALUES(42)`)
	for _, version := range []int{3, 5, 6, 6} {
		if err := earlyTestStep(t, conn, version); err != nil {
			t.Fatalf("v%d: %v", version, err)
		}
	}
	earlyTestInt(t, conn, "SELECT item_kind FROM bundles WHERE bundle_id=1", 0)
	earlyTestInt(t, conn, "SELECT item_kind FROM bundles WHERE bundle_id=2", 1)
	earlyTestInt(t, conn, "SELECT item_kind FROM bundles WHERE bundle_id=3", 1)
	earlyTestInt(t, conn, "SELECT sum(wishlisted) FROM wishlist_items", 1)
	earlyTestInt(t, conn, "SELECT wishlisted FROM wishlist_items WHERE id=1", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM sqlite_schema WHERE name IN ('game_achievements','player_achievements')", 0)
	earlyTestInt(t, conn, "SELECT id FROM achievement_defs", 42)
}

const earlyHistoryFixture = `CREATE TABLE game_price_history(id INTEGER PRIMARY KEY,appid INTEGER,region_code TEXT,snapshot_at TEXT,sub_id INTEGER,price INTEGER,is_gold INTEGER);
	PRAGMA user_version=3;`

func TestEarlyMigrationSnapshotUnique(t *testing.T) {
	conn := earlyTestConnection(t, earlyHistoryFixture,
		`INSERT INTO game_price_history VALUES(1,42,'cn','2026-01-01',NULL,NULL,NULL),(2,42,'cn','2026-01-02',NULL,0,0);
		CREATE INDEX ux_gph_snapshot ON game_price_history(id)`)
	if err := earlyTestStep(t, conn, 4); err != nil {
		t.Fatal(err)
	}
	if err := earlyTestStep(t, conn, 4); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(context.Background(), "INSERT INTO game_price_history VALUES(3,42,'cn','2026-01-01',-1,0,0)"); err == nil {
		t.Fatal("accepted equivalent null identity")
	}
	earlyTestInt(t, conn, "SELECT count(*) FROM game_price_history", 2)
	earlyTestInt(t, conn, "PRAGMA user_version", 3)
}

func TestEarlyMigrationSnapshotDuplicatesRollback(t *testing.T) {
	conn := earlyTestConnection(t, earlyHistoryFixture,
		`INSERT INTO game_price_history VALUES(1,42,'cn','2026-01-01',NULL,NULL,NULL),(2,42,'cn','2026-01-01',-1,0,0);
		CREATE INDEX ux_gph_snapshot ON game_price_history(id)`)
	if err := earlyTestStep(t, conn, 4); err == nil || !strings.Contains(err.Error(), "duplicate") {
		t.Fatalf("expected duplicates failure, got %v", err)
	}
	earlyTestInt(t, conn, "SELECT count(*) FROM game_price_history", 2)
	earlyTestInt(t, conn, "PRAGMA user_version", 3)
	var definition string
	if err := conn.QueryRowContext(context.Background(), "SELECT sql FROM sqlite_schema WHERE name='ux_gph_snapshot'").Scan(&definition); err != nil {
		t.Fatal(err)
	}
	if !strings.HasSuffix(definition, "(id)") {
		t.Fatalf("failed migration replaced original index: %q", definition)
	}
}

func TestEarlyMigrationAlertTargets(t *testing.T) {
	conn := earlyTestConnection(t, `CREATE TABLE fx_rates(currency_code TEXT,rate_to_cny REAL);
		INSERT INTO fx_rates VALUES('USD',7.5),('JPY',0);
		CREATE TABLE price_alerts(id INTEGER PRIMARY KEY,region TEXT,target_type TEXT,target_value REAL);
		INSERT INTO price_alerts VALUES(1,'us','price',3),(2,'US','price',5),(3,'cn','price',2.5),(4,'CN','price',3.5),
		(5,'br','price',100),(6,'jp','price',100),(7,'ar','price',100),(8,'XX','price',100),(9,NULL,'price',100),
		(10,'us','pct',50),(11,'us','historic_low',1),(12,'us','price',NULL);`)
	if err := earlyTestStep(t, conn, 7); err != nil {
		t.Fatal(err)
	}
	for id, want := range map[int]int64{1: 22, 2: 38, 3: 2, 4: 4, 5: 142, 6: 5, 7: 750, 8: 100, 9: 100, 10: 50, 11: 1} {
		earlyTestInt(t, conn, "SELECT target_value FROM price_alerts WHERE id=?", want, id)
	}
	earlyTestInt(t, conn, "SELECT target_value IS NULL FROM price_alerts WHERE id=12", 1)
}

func TestEarlyMigrationFXCanonical(t *testing.T) {
	conn := earlyTestConnection(t, `CREATE TABLE fx_rate_history(id INTEGER PRIMARY KEY,currency_code TEXT,rate_to_cny REAL,fetched_at TEXT,source TEXT,rate_date TEXT,source_kind TEXT);
		INSERT INTO fx_rate_history VALUES
		(1,'USD',7,'2026-01-01 08:00:00','archive',NULL,NULL),
		(2,'USD',8,'2026-01-01 09:00:00','live',NULL,NULL),
		(3,'USD',9,'2026-01-01 09:00:00','backfill',NULL,NULL),
		(4,'USD',10,NULL,'archive',NULL,NULL),(5,'USD',11,NULL,'backfill',NULL,NULL),
		(6,'EUR',12,'2026-01-01 10:00:00','other','2025-12-31','observed'),
		(7,'GBP',13,'2026-01-02 10:00:00','backfill',NULL,'custom');`)
	for range 2 {
		if err := earlyTestStep(t, conn, 8); err != nil {
			t.Fatal(err)
		}
	}
	earlyTestInt(t, conn, "SELECT count(*) FROM fx_rate_history", 5)
	earlyTestInt(t, conn, "SELECT id FROM fx_rate_history WHERE currency_code='USD' AND rate_date='2026-01-01'", 3)
	earlyTestInt(t, conn, "SELECT count(*) FROM fx_rate_history WHERE rate_date IS NULL", 2)
	earlyTestInt(t, conn, "SELECT count(*) FROM fx_rate_history WHERE id=3 AND source_kind='carried'", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM fx_rate_history WHERE id=4 AND source_kind='observed'", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM fx_rate_history WHERE id=6 AND rate_date='2025-12-31' AND source_kind='observed'", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM fx_rate_history WHERE id=7 AND source_kind='custom'", 1)
	if _, err := conn.ExecContext(context.Background(), "INSERT INTO fx_rate_history(id,currency_code,rate_date) VALUES(8,'USD','2026-01-01')"); err == nil {
		t.Fatal("accepted duplicate canonical date")
	}
}

const earlyMonitoringFixture = `CREATE TABLE games(appid INTEGER PRIMARY KEY,free_kind TEXT);
	INSERT INTO games VALUES(201,'f2p'),(999,NULL);
	CREATE TABLE tracked_accounts(steamid TEXT PRIMARY KEY,item_count INTEGER);
	INSERT INTO tracked_accounts VALUES('a',999),('b',999);
	CREATE TABLE wishlist_items(steamid TEXT,appid INTEGER,active INTEGER,manual INTEGER,wishlisted INTEGER,manual_pool INTEGER,owned INTEGER,board_pool INTEGER,excluded INTEGER);
	INSERT INTO wishlist_items VALUES
	('a',100,1,1,0,0,0,0,0),('b',100,1,0,0,0,1,0,0),('b',100,0,0,0,0,0,0,1),
	('a',101,1,0,1,1,0,0,0),('a',102,1,0,0,0,0,1,0),('a',103,1,0,0,1,1,0,0),
	('a',104,1,0,0,0,0,0,0),('a',105,1,1,0,1,0,0,0),
	('a',200,0,0,0,0,0,0,1),('a',201,0,0,0,1,0,0,1),('a',202,0,0,0,0,0,1,1);
	CREATE TABLE monitor_targets(id INTEGER PRIMARY KEY,target_type TEXT,target_id INTEGER,state TEXT,priority INTEGER,created_at TEXT,updated_at TEXT,activated_at TEXT,excluded_at TEXT,UNIQUE(target_type,target_id));
	INSERT INTO monitor_targets(target_type,target_id,state,priority) VALUES('game',105,'paused',3);
	CREATE TABLE monitor_sources(id INTEGER PRIMARY KEY,target_type TEXT,target_id INTEGER,source TEXT,priority INTEGER,active INTEGER,created_at TEXT,updated_at TEXT,UNIQUE(target_type,target_id,source));
	INSERT INTO monitor_sources(target_type,target_id,source,priority,active) VALUES('game',100,'favorite',1,0);
	CREATE TABLE monitor_exclusions(id INTEGER PRIMARY KEY,target_type TEXT,target_id INTEGER,reason TEXT,active INTEGER,created_at TEXT);
	CREATE UNIQUE INDEX ux_monitor_exclusion_active ON monitor_exclusions(target_type,target_id) WHERE active=1;`

func TestEarlyMigrationMonitoringBootstrap(t *testing.T) {
	conn := earlyTestConnection(t, earlyMonitoringFixture)
	for range 2 {
		if err := earlyTestStep(t, conn, 9); err != nil {
			t.Fatal(err)
		}
	}
	for appid, want := range map[int]int64{100: 100, 101: 95, 102: 20, 103: 60, 104: 0, 105: 3} {
		earlyTestInt(t, conn, "SELECT priority FROM monitor_targets WHERE target_id=?", want, appid)
	}
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_targets WHERE target_id=105 AND state='paused'", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_targets WHERE target_id IN (201,999)", 0)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_exclusions WHERE target_id IN (200,202)", 2)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_exclusions WHERE target_id IN (100,201)", 0)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_sources WHERE target_id=101 AND source IN ('family_wishlist','manual')", 2)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_sources WHERE target_id=100 AND source='favorite' AND priority=100 AND active=1", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_sources WHERE target_id=104", 0)
	earlyTestInt(t, conn, "SELECT count(*) FROM wishlist_items", 11)
}

func TestEarlyMigrationPoolFlagsRetire(t *testing.T) {
	conn := earlyTestConnection(t, earlyMonitoringFixture)
	for range 2 {
		if err := earlyTestStep(t, conn, 10); err != nil {
			t.Fatal(err)
		}
	}
	earlyTestInt(t, conn, "SELECT count(*) FROM wishlist_items WHERE appid IN (102,201,202)", 0)
	earlyTestInt(t, conn, "SELECT count(*) FROM wishlist_items WHERE appid IN (101,103,105) AND manual_pool=0", 3)
	earlyTestInt(t, conn, "SELECT count(*) FROM wishlist_items WHERE appid=101 AND wishlisted=1", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM wishlist_items WHERE appid=103 AND owned=1", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM wishlist_items WHERE appid=105 AND manual=1", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_targets WHERE target_id IN (100,104,200,201,999)", 0)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_targets WHERE target_id=102 AND state='active'", 1)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_sources WHERE source IN ('favorite','family_wishlist','owned') AND active=1", 0)
	earlyTestInt(t, conn, "SELECT count(*) FROM monitor_exclusions WHERE target_id=202 AND reason='wishlist_removed'", 1)
	earlyTestInt(t, conn, "SELECT item_count FROM tracked_accounts WHERE steamid='a'", 5)
	earlyTestInt(t, conn, "SELECT item_count FROM tracked_accounts WHERE steamid='b'", 1)
}

func TestEarlyMigrationColumnChanges(t *testing.T) {
	conn := earlyTestConnection(t, "CREATE TABLE price_cycles(id INTEGER);CREATE TABLE games(appid INTEGER,genres TEXT,name TEXT);INSERT INTO games VALUES(42,'old','keep')")
	for _, version := range []int{11, 11, 12, 12} {
		if err := earlyTestStep(t, conn, version); err != nil {
			t.Fatalf("v%d: %v", version, err)
		}
	}
	columns, err := migrationColumns(context.Background(), conn, "games")
	if err != nil || columns["genres"] || !columns["name"] {
		t.Fatalf("games columns = %v, %v", columns, err)
	}
	columns, err = migrationColumns(context.Background(), conn, "price_cycles")
	if err != nil || !columns["coverage_json"] {
		t.Fatalf("price cycle columns = %v, %v", columns, err)
	}
	earlyTestInt(t, conn, "SELECT count(*) FROM games WHERE appid=42 AND name='keep'", 1)
}

func TestEarlyMigrationFTS(t *testing.T) {
	conn := earlyTestConnection(t, "CREATE TABLE games(appid INTEGER PRIMARY KEY,name TEXT,name_en TEXT);INSERT INTO games VALUES(42,'黑神话悟空','Black Myth'),(43,'只狼',NULL)")
	for range 2 {
		if err := earlyTestStep(t, conn, 13); err != nil {
			t.Fatal(err)
		}
	}
	for _, term := range []string{"神话", "Black", "只狼"} {
		earlyTestInt(t, conn, "SELECT count(*) FROM games_name_fts WHERE games_name_fts MATCH ?", 1, term)
	}
	if err := migrationExec(context.Background(), conn, "UPDATE games SET name='群星',name_en='Stellaris' WHERE appid=42", "INSERT INTO games VALUES(44,'双人成行','It Takes Two')", "DELETE FROM games WHERE appid=43"); err != nil {
		t.Fatal(err)
	}
	for term, want := range map[string]int64{"Black": 0, "神话": 0, "只狼": 0, "Stellaris": 1, "双人": 1} {
		earlyTestInt(t, conn, "SELECT count(*) FROM games_name_fts WHERE games_name_fts MATCH ?", want, term)
	}
	earlyTestInt(t, conn, "SELECT count(*) FROM games_name_fts", 2)
}

func TestEarlyMigrationMissingTables(t *testing.T) {
	for _, version := range []int{2, 3, 4, 5, 7, 8, 9, 10, 11, 13} {
		t.Run(fmt.Sprint(version), func(t *testing.T) {
			conn := earlyTestConnection(t)
			if err := earlyTestStep(t, conn, version); err == nil {
				t.Fatal("missing required historical table did not fail")
			}
		})
	}
	for _, version := range []int{6, 12} {
		conn := earlyTestConnection(t)
		if err := earlyTestStep(t, conn, version); err != nil {
			t.Fatalf("v%d must tolerate missing retired tables: %v", version, err)
		}
	}
}
