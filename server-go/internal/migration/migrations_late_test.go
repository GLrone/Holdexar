package migration

import (
	"context"
	"database/sql"
	"encoding/json"
	"testing"
)

func lateTestStep(t *testing.T, conn *sql.Conn, version int) error {
	t.Helper()
	ctx := context.Background()
	if err := migrationExec(ctx, conn, "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	if err := lateMigrationSteps[version](ctx, conn); err != nil {
		_ = migrationExec(ctx, conn, "ROLLBACK")
		return err
	}
	return migrationExec(ctx, conn, "COMMIT")
}

func TestLateProviderMigrationPreservesStoredKey(t *testing.T) {
	_, conn := middleTestConn(t,
		"CREATE TABLE app_settings(key TEXT PRIMARY KEY,value_json JSON,updated_at DATETIME)",
		`INSERT INTO app_settings VALUES ('pilot.llm.protocol','"anthropic"',NULL),('pilot.llm.base_url','" https://example.invalid "',NULL),('pilot.llm.models','[" a ","",3,true]',NULL),('pilot.llm.api_key','"test-encrypted-value"',NULL),('pilot.llm.context_window','"8192"',NULL)`)
	if err := lateTestStep(t, conn, 25); err != nil {
		t.Fatal(err)
	}
	var raw string
	if err := conn.QueryRowContext(context.Background(), "SELECT value_json FROM app_settings WHERE key='pilot.llm.providers'").Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var providers []map[string]any
	if err := json.Unmarshal([]byte(raw), &providers); err != nil {
		t.Fatal(err)
	}
	p := providers[0]
	if p["protocol"] != "anthropic" || p["base_url"] != "https://example.invalid" || p["context_window"] != float64(8192) {
		t.Fatalf("provider = %#v", p)
	}
	models := p["models"].([]any)
	if len(models) != 3 || models[0] != " a " || models[1] != "3" || models[2] != "True" {
		t.Fatalf("models = %#v", models)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT value_json FROM app_settings WHERE key='pilot.llm.provider_key.legacy-v1'").Scan(&raw); err != nil || raw != `"test-encrypted-value"` {
		t.Fatalf("stored key = %s, %v", raw, err)
	}
	var old int
	if err := conn.QueryRowContext(context.Background(), "SELECT count(*) FROM app_settings WHERE key='pilot.llm.api_key'").Scan(&old); err != nil || old != 0 {
		t.Fatalf("old key count = %d, %v", old, err)
	}
}

func TestLateProjectionMigrationPreservesExtensions(t *testing.T) {
	_, conn := middleTestConn(t,
		"CREATE TABLE bill_imports(id INTEGER PRIMARY KEY,game_net_fen INTEGER)",
		"INSERT INTO bill_imports VALUES(1,500)",
		"CREATE VIEW kept_view AS SELECT game_net_fen FROM bill_imports",
		"CREATE TABLE tracked_accounts(id INTEGER PRIMARY KEY,item_count INTEGER)")
	if err := lateTestStep(t, conn, 26); err == nil {
		t.Fatal("retired column still referenced by extension")
	}
	var value int
	if err := conn.QueryRowContext(context.Background(), "SELECT game_net_fen FROM bill_imports").Scan(&value); err != nil || value != 500 {
		t.Fatalf("projection changed: %d, %v", value, err)
	}
}

func TestLateSessionUnknownContextIsNotDiscarded(t *testing.T) {
	_, conn := middleTestConn(t,
		"CREATE TABLE agent_sessions(id INTEGER PRIMARY KEY,title TEXT,context_snapshot JSON,context_version INTEGER,context_updated_at DATETIME)",
		`INSERT INTO agent_sessions VALUES(1,'kept',NULL,0,NULL)`,
		"CREATE TABLE app_settings(key TEXT PRIMARY KEY,value_json JSON)")
	if err := lateTestStep(t, conn, 30); err == nil {
		t.Fatal("unknown context discarded")
	}
	if err := migrationExec(context.Background(), conn, "UPDATE agent_sessions SET title=''"); err != nil {
		t.Fatal(err)
	}
	if err := lateTestStep(t, conn, 30); err != nil {
		t.Fatal(err)
	}
	columns, err := migrationColumns(context.Background(), conn, "agent_sessions")
	if err != nil || columns["title"] || columns["context_snapshot"] {
		t.Fatalf("old columns remain: %v, %v", columns, err)
	}
}

func TestLateCooldownMovesOnlyUniquelyOwnedDeadline(t *testing.T) {
	_, conn := middleTestConn(t,
		"CREATE TABLE proxy_nodes(node_id TEXT PRIMARY KEY,runtime_name TEXT,subscription_id INTEGER,original_name TEXT,cooldown_until DATETIME)",
		"INSERT INTO proxy_nodes VALUES('node','1|节点',1,'节点','2998-01-01 00:00:00')",
		"CREATE TABLE clash_nodes(id INTEGER PRIMARY KEY,subscription_id INTEGER,name TEXT,cooldown_until DATETIME)",
		"CREATE INDEX ix_clash_nodes_sub_name ON clash_nodes(subscription_id,name)",
		"INSERT INTO clash_nodes VALUES(1,1,'节点 #2','2999-01-01 00:00:00')")
	if err := lateTestStep(t, conn, 31); err != nil {
		t.Fatal(err)
	}
	var deadline string
	if err := conn.QueryRowContext(context.Background(), "SELECT CAST(cooldown_until AS TEXT) FROM proxy_nodes").Scan(&deadline); err != nil || deadline != "2999-01-01 00:00:00.000000" {
		t.Fatalf("deadline = %q, %v", deadline, err)
	}
	if exists, err := migrationTableExists(context.Background(), conn, "clash_nodes"); err != nil || exists {
		t.Fatalf("old cooldown table remains: %v, %v", exists, err)
	}
}

func TestLateCoverageMismatchKeepsFrozenValue(t *testing.T) {
	_, conn := middleTestConn(t,
		"CREATE TABLE price_cycles(id INTEGER PRIMARY KEY,units_expected INTEGER,units_ok INTEGER,units_locked INTEGER,coverage FLOAT,coverage_confirmed FLOAT)",
		"INSERT INTO price_cycles VALUES(1,64,15,1,0.9,0.25)")
	if err := lateTestStep(t, conn, 32); err == nil {
		t.Fatal("inconsistent coverage was discarded")
	}
	if err := migrationExec(context.Background(), conn, "UPDATE price_cycles SET coverage=0.2344"); err != nil {
		t.Fatal(err)
	}
	if err := lateTestStep(t, conn, 32); err != nil {
		t.Fatal(err)
	}
}

func TestLateCooldownCanMatchRuntimeWithoutOriginalName(t *testing.T) {
	_, conn := middleTestConn(t,
		"CREATE TABLE proxy_nodes(node_id TEXT PRIMARY KEY,runtime_name TEXT,subscription_id INTEGER,original_name TEXT,cooldown_until DATETIME)",
		"INSERT INTO proxy_nodes VALUES('node','1|节点',1,NULL,NULL)",
		"CREATE TABLE clash_nodes(id INTEGER PRIMARY KEY,subscription_id INTEGER,name TEXT,cooldown_until DATETIME)",
		"INSERT INTO clash_nodes VALUES(1,1,'节点','2999-01-01 00:00:00')")
	if err := lateTestStep(t, conn, 31); err != nil {
		t.Fatal(err)
	}
	var deadline string
	if err := conn.QueryRowContext(context.Background(), "SELECT CAST(cooldown_until AS TEXT) FROM proxy_nodes").Scan(&deadline); err != nil || deadline != "2999-01-01 00:00:00.000000" {
		t.Fatalf("deadline=%q, %v", deadline, err)
	}
}
