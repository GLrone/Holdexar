package migration

import (
	"context"
	"database/sql"
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/store"
)

func middleTestConn(t *testing.T, statements ...string) (*sql.DB, *sql.Conn) {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "test.db"))
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
	return db, conn
}

func middleTestStep(t *testing.T, conn *sql.Conn, version int) error {
	t.Helper()
	ctx := context.Background()
	if _, err := conn.ExecContext(ctx, "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	err := middleMigrationSteps[version](ctx, conn)
	if err != nil {
		_, _ = conn.ExecContext(ctx, "ROLLBACK")
		return err
	}
	_, err = conn.ExecContext(ctx, "COMMIT")
	return err
}

func TestMiddleMigrationRegistryAndDeclarations(t *testing.T) {
	for version := 14; version <= 24; version++ {
		if middleMigrationSteps[version] == nil {
			t.Fatalf("missing migration %d", version)
		}
	}
	ddl := "CREATE TABLE x (`id` INTEGER, note TEXT DEFAULT 'a,(' /* , ) */, [a,b] TEXT, n INTEGER GENERATED ALWAYS AS (length(note)+coalesce(1,2)), PRIMARY KEY (`id`)) STRICT"
	parts, suffix, err := migrationTableDeclarations(ddl, "x")
	if err != nil || len(parts) != 5 || suffix != ") STRICT" {
		t.Fatalf("declarations = %#v, %q, %v", parts, suffix, err)
	}
	for _, invalid := range []string{"CREATE TABLE x(id INTEGER) WITHOUT ROWID", "CREATE TABLE x(id INTEGER, x TEXT DEFAULT 'broken)", "CREATE TABLE x(id INTEGER, CHECK((1))"} {
		if _, _, err := migrationTableDeclarations(invalid, "x"); err == nil {
			t.Fatalf("accepted unsafe DDL %q", invalid)
		}
	}
}

func middleIdentityFixture(t *testing.T, extra string) (*sql.DB, *sql.Conn) {
	return middleTestConn(t,
		`CREATE TABLE price_events("id" INTEGER NOT NULL, cycle_id INTEGER, value TEXT DEFAULT 'PRIMARY KEY, DESC', extra TEXT GENERATED ALWAYS AS (value || ',extra') VIRTUAL, CHECK(length(value)>0), PRIMARY KEY("id"))`,
		`CREATE TABLE price_cycles(id INTEGER PRIMARY KEY, label TEXT)`,
		`CREATE TABLE crawl_jobs(id INTEGER PRIMARY KEY, cycle_id INTEGER)`,
		`CREATE TABLE notification_candidates(event_id INTEGER, cycle_id INTEGER)`,
		`CREATE TABLE game_price_history(appid INTEGER,region_code TEXT,snapshot_at TEXT,sub_id INTEGER,price INTEGER,is_gold INTEGER)`,
		`CREATE TABLE audit(event_id INTEGER)`,
		`CREATE INDEX extra_events ON price_events(value)`,
		`CREATE TRIGGER extra_event_insert AFTER INSERT ON price_events BEGIN INSERT INTO audit VALUES(new.id); END`,
		`INSERT INTO price_events(id,cycle_id,value) VALUES(1,20,'preserved'),(2,-30,'other')`,
		`INSERT INTO price_cycles VALUES(4,'keep')`,
		`INSERT INTO crawl_jobs VALUES(3,60)`,
		`INSERT INTO notification_candidates VALUES(99,40),(7,-80)`, extra)
}

func TestMiddleProxySubscriptionIdentityMergesAndSeparatesSubscriptions(t *testing.T) {
	_, conn := middleTestConn(t,
		`CREATE TABLE proxy_subscriptions(id INTEGER PRIMARY KEY,url TEXT)`,
		`INSERT INTO proxy_subscriptions VALUES(1,'https://one.example.com/sub'),(2,'https://two.example.com/sub'),(3,'enc1:unreadable')`,
		`CREATE TABLE proxy_nodes(id INTEGER PRIMARY KEY,node_id TEXT UNIQUE,fingerprint TEXT UNIQUE,normalized_config TEXT,state TEXT,runtime_name TEXT)`,
		`CREATE TABLE proxy_node_sources(node_id TEXT,subscription_id INTEGER,original_name TEXT)`,
		`CREATE TABLE health_observations(node_id TEXT,value TEXT)`,
		`INSERT INTO proxy_nodes VALUES(1,'old-one','old-one','{"type":"ss","server":"host","port":1,"password":"key","name":"ignored"}','ACTIVE','unrelated'),(2,'old-two','old-two','{"type":"ss","server":"host","port":2,"password":"key"}','NEW','1|fallback'),(3,'old-three','old-three','{"type":"ss","server":"host","port":1,"password":"key"}','ACTIVE','2|other'),(4,'old-four','old-four','{"type":"ss","server":"host","port":3,"password":"key"}','ACTIVE','1|newest'),(5,'old-five','old-five','broken json','NEW','3|unreadable')`,
		`INSERT INTO proxy_node_sources VALUES('old-one',1,'original')`,
		`INSERT INTO health_observations VALUES('old-one','discarded'),('old-two','discarded'),('old-three','kept'),('old-four','keeper')`)
	if err := middleTestStep(t, conn, 17); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM proxy_nodes").Scan(&count); err != nil || count != 3 {
		t.Fatalf("node count=%d, %v", count, err)
	}
	config := map[string]any{"type": "ss", "server": "host", "port": 3, "password": "key"}
	expected, err := proxypool.NodeFingerprint(config, proxypool.SubscriptionTag("https://one.example.com/sub"))
	if err != nil {
		t.Fatal(err)
	}
	var nodeID, fingerprint, name string
	var sid int
	if err := conn.QueryRowContext(context.Background(), "SELECT node_id,fingerprint,subscription_id,original_name FROM proxy_nodes WHERE id=4").Scan(&nodeID, &fingerprint, &sid, &name); err != nil {
		t.Fatal(err)
	}
	if nodeID != expected || fingerprint != expected || sid != 1 || name != "newest" {
		t.Fatalf("keeper identity/name=%q/%q/%d/%q", nodeID, fingerprint, sid, name)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM health_observations").Scan(&count); err != nil || count != 2 {
		t.Fatalf("health count=%d, %v", count, err)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM sqlite_schema WHERE name='proxy_node_sources'").Scan(&count); err != nil || count != 0 {
		t.Fatalf("source table count=%d, %v", count, err)
	}
	unreadable, err := proxypool.NodeFingerprint(map[string]any{}, "sub3")
	if err != nil {
		t.Fatal(err)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT node_id FROM proxy_nodes WHERE id=5").Scan(&nodeID); err != nil || nodeID != unreadable {
		t.Fatalf("unreadable link fallback=%q, %v", nodeID, err)
	}
}

func TestMiddleProxySubscriptionIdentityAcceptsEmptyModernTable(t *testing.T) {
	_, conn := middleTestConn(t,
		`CREATE TABLE proxy_nodes(id INTEGER PRIMARY KEY,node_id TEXT UNIQUE,normalized_config TEXT,state TEXT,runtime_name TEXT,subscription_id INTEGER,original_name TEXT)`,
		`CREATE TABLE proxy_subscriptions(id INTEGER PRIMARY KEY,url TEXT)`,
		`CREATE TABLE health_observations(node_id TEXT)`,
		`CREATE TABLE proxy_node_sources(node_id TEXT,subscription_id INTEGER,original_name TEXT)`)
	if err := middleTestStep(t, conn, 17); err != nil {
		t.Fatal(err)
	}
	columns, err := migrationColumns(context.Background(), conn, "proxy_nodes")
	if err != nil || columns["fingerprint"] {
		t.Fatalf("empty modern schema changed: %#v, %v", columns, err)
	}
	var count int
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM proxy_nodes").Scan(&count); err != nil || count != 0 {
		t.Fatalf("empty table acquired rows: %d, %v", count, err)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM sqlite_schema WHERE name='proxy_node_sources'").Scan(&count); err != nil || count != 0 {
		t.Fatalf("legacy source table retained: %d, %v", count, err)
	}
}

func TestMiddleHistoryIndexAndActivation(t *testing.T) {
	_, conn := middleTestConn(t, `CREATE TABLE game_price_history(appid INTEGER,region_code TEXT,snapshot_at TEXT,sub_id INTEGER,price INTEGER,is_gold INTEGER)`, `CREATE INDEX ix_gph_appid_region_snapshot ON game_price_history(appid,region_code,snapshot_at)`, `CREATE INDEX ix_gph_sub_id ON game_price_history(sub_id)`)
	if err := middleTestStep(t, conn, 14); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM sqlite_schema WHERE name IN ('ix_gph_appid_region_snapshot','ix_gph_sub_id')").Scan(&count); err != nil || count != 2 {
		t.Fatalf("removed fallback indexes without unique protection %d, %v", count, err)
	}
	for _, version := range []int{18, 14, 15} {
		if err := middleTestStep(t, conn, version); err != nil {
			t.Fatal(err)
		}
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM sqlite_schema WHERE name IN ('ix_gph_appid_region_snapshot','ix_gph_sub_id')").Scan(&count); err != nil || count != 0 {
		t.Fatalf("redundant indexes retained %d, %v", count, err)
	}
	if _, err := conn.ExecContext(context.Background(), `INSERT INTO redeem_activations(created_at,kind,status,product_key,subid,sub_name,detail,raw,steamid) VALUES('stamp','cdk','ok','','','','','','account')`); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(context.Background(), `INSERT INTO game_price_history VALUES(1,'cn','stamp',NULL,NULL,NULL),(1,'cn','stamp',NULL,NULL,NULL)`); err == nil {
		t.Fatal("unique history index allowed null-key duplicate")
	}
}

func TestMiddleFTSUpdateGate(t *testing.T) {
	_, conn := middleTestConn(t, `CREATE TABLE games(appid INTEGER PRIMARY KEY,name TEXT,name_en TEXT)`, `CREATE TABLE games_name_fts(appid INTEGER,name_bi TEXT)`, `INSERT INTO games VALUES(1,'old',NULL)`, `INSERT INTO games_name_fts(rowid,appid,name_bi) VALUES(20,1,'old')`)
	if err := middleTestStep(t, conn, 16); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(context.Background(), "UPDATE games SET name=name,name_en=name_en"); err != nil {
		t.Fatal(err)
	}
	var rowid int
	if err := conn.QueryRowContext(context.Background(), "SELECT rowid FROM games_name_fts").Scan(&rowid); err != nil || rowid != 20 {
		t.Fatalf("unchanged update rewrote row %d, %v", rowid, err)
	}
	if _, err := conn.ExecContext(context.Background(), "UPDATE games SET name_en='English'"); err != nil {
		t.Fatal(err)
	}
	var tokens string
	if err := conn.QueryRowContext(context.Background(), "SELECT name_bi FROM games_name_fts").Scan(&tokens); err != nil || !strings.Contains(tokens, "English") {
		t.Fatalf("null-safe rename not indexed %q, %v", tokens, err)
	}
}

func TestMiddleIdentityPreservesExistingDeletedSequence(t *testing.T) {
	_, conn := middleIdentityFixture(t, "SELECT 1")
	if err := migrationExec(context.Background(), conn, "DROP TRIGGER extra_event_insert", "DROP TABLE price_events", `CREATE TABLE price_events(id INTEGER PRIMARY KEY AUTOINCREMENT,cycle_id INTEGER)`, `INSERT INTO price_events VALUES(500,1)`, `DELETE FROM price_events`); err != nil {
		t.Fatal(err)
	}
	if err := middleTestStep(t, conn, 19); err != nil {
		t.Fatal(err)
	}
	result, err := conn.ExecContext(context.Background(), "INSERT INTO price_events DEFAULT VALUES")
	if err != nil {
		t.Fatal(err)
	}
	if id, _ := result.LastInsertId(); id != 501 {
		t.Fatalf("existing sequence reset to %d", id)
	}
}

func TestMiddleCallerRollbackRestoresRebuiltTables(t *testing.T) {
	_, conn := middleIdentityFixture(t, "SELECT 1")
	ctx := context.Background()
	if err := migrationExec(ctx, conn, "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	if err := middleMigrationSteps[19](ctx, conn); err != nil {
		t.Fatal(err)
	}
	if err := migrationExec(ctx, conn, "ROLLBACK"); err != nil {
		t.Fatal(err)
	}
	var ddl string
	if err := conn.QueryRowContext(ctx, "SELECT sql FROM sqlite_schema WHERE name='price_events'").Scan(&ddl); err != nil || strings.Contains(ddl, "AUTOINCREMENT") {
		t.Fatalf("caller rollback failed %q, %v", ddl, err)
	}
	var count int
	if err := conn.QueryRowContext(ctx, "SELECT count(*) FROM price_events").Scan(&count); err != nil || count != 2 {
		t.Fatalf("caller rollback lost rows %d, %v", count, err)
	}
}

func TestMiddleIdentityPreservesExtensionsAndReferencedHighwater(t *testing.T) {
	_, conn := middleIdentityFixture(t, "SELECT 1")
	if err := middleTestStep(t, conn, 19); err != nil {
		t.Fatal(err)
	}
	for _, item := range []struct {
		table string
		want  int64
	}{{"price_events", 100}, {"price_cycles", 61}, {"crawl_jobs", 81}} {
		result, err := conn.ExecContext(context.Background(), "INSERT INTO "+item.table+" DEFAULT VALUES")
		if err != nil {
			t.Fatal(err)
		}
		id, _ := result.LastInsertId()
		if id != item.want {
			t.Errorf("%s next id = %d, want %d", item.table, id, item.want)
		}
	}
	var extra string
	if err := conn.QueryRowContext(context.Background(), "SELECT extra FROM price_events WHERE id=1").Scan(&extra); err != nil || extra != "preserved,extra" {
		t.Fatalf("generated extension %q, %v", extra, err)
	}
	var audit int
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM audit WHERE event_id=100").Scan(&audit); err != nil || audit != 1 {
		t.Fatalf("trigger not preserved %d, %v", audit, err)
	}
	if _, err := conn.ExecContext(context.Background(), "INSERT INTO price_events(value) VALUES('')"); err == nil {
		t.Fatal("lost CHECK constraint")
	}
}

func TestMiddleIdentityRejectsUnknownFKAndRollsBack(t *testing.T) {
	_, conn := middleIdentityFixture(t, `CREATE TABLE extension(owner INTEGER REFERENCES price_events(id))`)
	if err := middleTestStep(t, conn, 19); err == nil {
		t.Fatal("accepted inbound foreign key")
	}
	var ddl string
	if err := conn.QueryRowContext(context.Background(), "SELECT sql FROM sqlite_schema WHERE name='price_events'").Scan(&ddl); err != nil || strings.Contains(ddl, "AUTOINCREMENT") {
		t.Fatalf("unsafe migration changed original: %s, %v", ddl, err)
	}
}

func TestMiddleIdentityRejectsUnsupportedPrimaryKeys(t *testing.T) {
	for _, id := range []string{"id INTEGER PRIMARY KEY DESC", "id BIGINT PRIMARY KEY", "id INTEGER, PRIMARY KEY(id DESC)"} {
		t.Run(id, func(t *testing.T) {
			_, conn := middleIdentityFixture(t, "SELECT 1")
			if err := migrationExec(context.Background(), conn, "DROP TRIGGER extra_event_insert", "DROP TABLE price_events", "CREATE TABLE price_events(cycle_id INTEGER,"+id+")"); err != nil {
				t.Fatal(err)
			}
			if err := middleTestStep(t, conn, 19); err == nil {
				t.Fatal("accepted unsupported identity")
			}
		})
	}
}

func TestMiddleProxyRetirementPreservesExtensionsAndSequence(t *testing.T) {
	_, conn := middleTestConn(t,
		`CREATE TABLE proxy_nodes(id INTEGER PRIMARY KEY AUTOINCREMENT,node_id TEXT NOT NULL UNIQUE,fingerprint TEXT NOT NULL UNIQUE,payload TEXT CHECK(length(payload)>0),extension TEXT GENERATED ALWAYS AS (payload || ',x') VIRTUAL)`,
		`CREATE TABLE audit(node_id TEXT)`,
		`CREATE INDEX extra_payload ON proxy_nodes(payload)`,
		`CREATE UNIQUE INDEX ux_proxy_nodes_fingerprint ON proxy_nodes(fingerprint)`,
		`CREATE TRIGGER extra_node AFTER INSERT ON proxy_nodes BEGIN INSERT INTO audit VALUES(new.node_id); END`,
		`INSERT INTO proxy_nodes(id,node_id,fingerprint,payload) VALUES(1,'keep','keep','data'),(900,'deleted','deleted','gone')`,
		`DELETE FROM proxy_nodes WHERE id=900`,
		`CREATE TABLE clash_nodes(id INTEGER PRIMARY KEY,exit_ip TEXT,status TEXT,latency_ms INTEGER,fail_count INTEGER,last_checked_at TEXT,created_at TEXT,revive_passes INTEGER,cooldown_until TEXT)`,
		`INSERT INTO clash_nodes(id,cooldown_until) VALUES(1,'retained')`)
	if err := middleTestStep(t, conn, 24); err != nil {
		t.Fatal(err)
	}
	result, err := conn.ExecContext(context.Background(), "INSERT INTO proxy_nodes(node_id,payload) VALUES('new','row')")
	if err != nil {
		t.Fatal(err)
	}
	if id, _ := result.LastInsertId(); id != 901 {
		t.Fatalf("sequence reset: %d", id)
	}
	var value string
	if err := conn.QueryRowContext(context.Background(), "SELECT extension FROM proxy_nodes WHERE id=1").Scan(&value); err != nil || value != "data,x" {
		t.Fatalf("extension lost: %q, %v", value, err)
	}
	var count int
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM audit WHERE node_id='new'").Scan(&count); err != nil || count != 1 {
		t.Fatalf("trigger lost: %d, %v", count, err)
	}
	cols, err := migrationColumns(context.Background(), conn, "clash_nodes")
	if err != nil || cols["exit_ip"] || !cols["cooldown_until"] {
		t.Fatalf("retired columns %#v, %v", cols, err)
	}
}

func TestMiddleProxyRetirementRejectsUnsafeReferences(t *testing.T) {
	for _, extra := range []string{
		`UPDATE proxy_nodes SET fingerprint='different'`,
		`CREATE TABLE extension(node TEXT REFERENCES proxy_nodes(node_id))`,
		`CREATE TRIGGER extension AFTER UPDATE ON proxy_nodes BEGIN SELECT new.fingerprint; END`,
	} {
		t.Run(fmt.Sprint(extra), func(t *testing.T) {
			_, conn := middleTestConn(t, `CREATE TABLE proxy_nodes(id INTEGER PRIMARY KEY AUTOINCREMENT,node_id TEXT UNIQUE,fingerprint TEXT UNIQUE)`, `INSERT INTO proxy_nodes VALUES(1,'one','one')`, extra)
			if err := middleTestStep(t, conn, 24); err == nil {
				t.Fatal("accepted unsafe fingerprint retirement")
			}
			cols, err := migrationColumns(context.Background(), conn, "proxy_nodes")
			if err != nil || !cols["fingerprint"] {
				t.Fatalf("unsafe retirement changed schema: %#v, %v", cols, err)
			}
		})
	}
}

func TestMiddleRetirementAndObservations(t *testing.T) {
	_, conn := middleTestConn(t,
		`CREATE TABLE orchestration_events(id INTEGER PRIMARY KEY)`,
		`CREATE VIEW extension AS SELECT * FROM orchestration_events`,
		`CREATE TABLE price_cycles(id INTEGER PRIMARY KEY,coverage_json TEXT,keep TEXT)`,
		`INSERT INTO price_cycles VALUES(1,'retired','preserved')`,
		`CREATE TABLE game_current_prices(price_status TEXT,attempt_outcome TEXT,steam_answer TEXT,last_success_at TEXT,updated_at TEXT)`,
		`INSERT INTO game_current_prices VALUES('ok',NULL,NULL,NULL,'stamp'),('locked',NULL,NULL,NULL,'lockstamp'),('blocked',NULL,NULL,NULL,'badstamp'),('ok','existing','existing','existing','new')`)
	if err := middleTestStep(t, conn, 22); err == nil {
		t.Fatal("dropped extension's source")
	}
	if err := migrationExec(context.Background(), conn, "DROP VIEW extension"); err != nil {
		t.Fatal(err)
	}
	for _, version := range []int{20, 22, 23} {
		if err := middleTestStep(t, conn, version); err != nil {
			t.Fatal(err)
		}
	}
	var success, failed, preserved int
	for i, q := range []string{`SELECT count(*) FROM game_current_prices WHERE attempt_outcome='success' AND last_success_at IN ('stamp','lockstamp')`, `SELECT count(*) FROM game_current_prices WHERE attempt_outcome='failed' AND steam_answer IS NULL AND last_success_at IS NULL`, `SELECT count(*) FROM game_current_prices WHERE attempt_outcome='existing' AND last_success_at='existing'`} {
		p := []*int{&success, &failed, &preserved}[i]
		if err := conn.QueryRowContext(context.Background(), q).Scan(p); err != nil {
			t.Fatal(err)
		}
	}
	if success != 2 || failed != 1 || preserved != 1 {
		t.Fatalf("observation backfill %d/%d/%d", success, failed, preserved)
	}
	var keep string
	if err := conn.QueryRowContext(context.Background(), "SELECT keep FROM price_cycles").Scan(&keep); err != nil || keep != "preserved" {
		t.Fatalf("coverage retirement lost data: %q %v", keep, err)
	}
}
