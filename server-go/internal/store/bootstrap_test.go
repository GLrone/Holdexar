package store

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"
)

func bootstrapFingerprint(t *testing.T, db *sql.DB, query string) string {
	t.Helper()
	rows, err := db.Query(query)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	hash := sha256.New()
	for rows.Next() {
		var row string
		if err := rows.Scan(&row); err != nil {
			t.Fatal(err)
		}
		fmt.Fprintln(hash, row)
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return fmt.Sprintf("%x", hash.Sum(nil))
}

func TestInitializeFreshDatabaseReferenceParity(t *testing.T) {
	db, _ := openTestDB(t)
	before := time.Now().In(time.FixedZone("Asia/Shanghai", 8*60*60))
	if err := InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	var version int
	if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if version != 34 {
		t.Fatalf("schema version = %d, want 34", version)
	}

	// Fingerprints come from a pristine reference initialization, excluding FTS-generated tables.
	schemaQuery := `SELECT type || char(0) || name || char(0) || tbl_name || char(0) || replace(sql,char(13),'')
        FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL
        AND name NOT IN ('achievement_notes','challenge_progress')
        AND name NOT IN (SELECT name FROM pragma_table_list WHERE type='shadow') ORDER BY type,name`
	if got := bootstrapFingerprint(t, db, schemaQuery); got != "9f34aaa27823edfe3721d06fa720d53c694c848ffa37cbd9bc879d6ee4a9fa44" {
		t.Fatalf("schema differs from pristine reference: %s", got)
	}
	fullAppTables := `SELECT type || char(0) || name || char(0) || tbl_name || char(0) ||
        replace(replace(sql,char(13),''),' '||char(10),char(10)) FROM sqlite_schema
        WHERE name IN ('achievement_notes','challenge_progress') ORDER BY type,name`
	if got := bootstrapFingerprint(t, db, fullAppTables); got != "428a1699881b83bba37952261cd0f969d3e1c59385c5cd751c28e1739414e125" {
		t.Fatalf("achievement tables differ from full application schema: %s", got)
	}
	checks := []struct {
		table, cols, order, hash string
		count                    int
	}{
		{"crawl_regions", `quote(code)||'|'||quote(name)||'|'||quote(currency)||'|'||quote(enabled)||'|'||quote(sort)`, "code", "12812adb44a456d101790334115b3c2c357a8e4e1781bc9bab5543f3b85b911c", 41},
		{"fx_rates", `quote(currency_code)||'|'||quote(rate_to_cny)||'|'||quote(fetched_at)`, "currency_code", "712b197537b66b7f0e70a812f68e484a082e4514f124e69e68a8616dd03e52ca", 1},
		{"tags", `quote(tagid)||'|'||quote(name_zh)||'|'||quote(name_en)`, "tagid", "b887ebdae15983c78a1b4e4bad238505208caa10cbed2c0ed6e31ff2b1288504", 429},
	}
	for _, check := range checks {
		got := bootstrapFingerprint(t, db, "SELECT "+check.cols+" FROM "+check.table+" ORDER BY "+check.order)
		if got != check.hash {
			t.Errorf("%s seed fingerprint = %s", check.table, got)
		}
		var count int
		if err := db.QueryRow("SELECT count(*) FROM " + check.table).Scan(&count); err != nil {
			t.Fatal(err)
		}
		if count != check.count {
			t.Errorf("%s rows = %d, want %d", check.table, count, check.count)
		}
	}
	var raw string
	if err := db.QueryRow("SELECT value_json FROM app_settings WHERE key='games.derived_inputs'").Scan(&raw); err != nil {
		t.Fatal(err)
	}
	var inputs struct {
		Day    string
		Series map[string]any
	}
	if err := json.Unmarshal([]byte(raw), &inputs); err != nil {
		t.Fatal(err)
	}
	after := time.Now().In(time.FixedZone("Asia/Shanghai", 8*60*60))
	if inputs.Day != before.Format("2006-01-02") && inputs.Day != after.Format("2006-01-02") {
		t.Fatalf("derived input day = %q", inputs.Day)
	}
	if inputs.Series == nil || len(inputs.Series) != 0 {
		t.Fatalf("derived series = %v", inputs.Series)
	}
	tables, err := db.Query(`SELECT name FROM pragma_table_list WHERE schema='main'
        AND type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name`)
	if err != nil {
		t.Fatal(err)
	}
	var emptyTables []string
	for tables.Next() {
		var name string
		if err := tables.Scan(&name); err != nil {
			t.Fatal(err)
		}
		switch name {
		case "crawl_regions", "fx_rates", "tags":
		default:
			emptyTables = append(emptyTables, name)
		}
	}
	if err := tables.Err(); err != nil {
		t.Fatal(err)
	}
	tables.Close()
	for _, name := range emptyTables {
		var count int
		if err := db.QueryRow(`SELECT count(*) FROM "` + name + `"`).Scan(&count); err != nil {
			t.Fatal(err)
		}
		want := 0
		if name == "app_settings" {
			want = 1
		}
		if count != want {
			t.Errorf("%s contains %d rows, want %d", name, count, want)
		}
	}
	for _, table := range []string{"crawl_regions", "tags"} {
		var oldest, newest string
		if err := db.QueryRow("SELECT min(updated_at),max(updated_at) FROM "+table).Scan(&oldest, &newest); err != nil {
			t.Fatal(err)
		}
		lower, upper := before.Format("2006-01-02 15:04:05.000000"), after.Format("2006-01-02 15:04:05.000000")
		if oldest < lower || newest > upper {
			t.Errorf("%s seed timestamps outside initialization: %s..%s", table, oldest, newest)
		}
	}
	rows, err := db.Query("PRAGMA foreign_key_check")
	if err != nil {
		t.Fatal(err)
	}
	if rows.Next() {
		t.Error("foreign key violation after initialization")
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	rows.Close()
	var integrity string
	if err := db.QueryRow("PRAGMA integrity_check").Scan(&integrity); err != nil {
		t.Fatal(err)
	}
	if integrity != "ok" {
		t.Fatalf("integrity check: %s", integrity)
	}
}

func TestInitializeFreshDatabaseRejectsExisting(t *testing.T) {
	for _, setup := range []string{
		"CREATE TABLE preserved(value TEXT); INSERT INTO preserved VALUES ('keep')",
		"PRAGMA user_version=1",
		"PRAGMA user_version=34",
		"CREATE VIEW existing AS SELECT 1",
	} {
		t.Run(setup, func(t *testing.T) {
			db, _ := openTestDB(t)
			if _, err := db.Exec(setup); err != nil {
				t.Fatal(err)
			}
			before := bootstrapFingerprint(t, db, "SELECT type || name || coalesce(sql,'') FROM sqlite_schema ORDER BY name")
			var version int
			if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
				t.Fatal(err)
			}
			if err := InitializeFreshDatabase(context.Background(), db); err == nil {
				t.Fatal("accepted existing database")
			}
			after := bootstrapFingerprint(t, db, "SELECT type || name || coalesce(sql,'') FROM sqlite_schema ORDER BY name")
			if before != after {
				t.Fatal("existing schema changed")
			}
			var got int
			if err := db.QueryRow("PRAGMA user_version").Scan(&got); err != nil {
				t.Fatal(err)
			}
			if got != version {
				t.Fatalf("version changed %d -> %d", version, got)
			}
			if strings.Contains(setup, "preserved") {
				var value string
				if err := db.QueryRow("SELECT value FROM preserved").Scan(&value); err != nil {
					t.Fatal(err)
				}
				if value != "keep" {
					t.Fatal("existing row changed")
				}
			}
		})
	}
}

func TestInitializeFreshDatabaseRollbackOnFailure(t *testing.T) {
	db, _ := openTestDB(t)
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA max_page_count=3"); err != nil {
		t.Fatal(err)
	}
	if err := InitializeFreshDatabase(context.Background(), db); err == nil {
		t.Fatal("initialization unexpectedly fit within 3 pages")
	}
	var version, objects int
	if err := db.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT count(*) FROM sqlite_schema").Scan(&objects); err != nil {
		t.Fatal(err)
	}
	if version != 0 || objects != 0 {
		t.Fatalf("failed initialization left version=%d, objects=%d", version, objects)
	}
	if _, err := db.Exec("PRAGMA max_page_count=10000"); err != nil {
		t.Fatal(err)
	}
	if err := InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatalf("retry failed: %v", err)
	}
	if err := InitializeFreshDatabase(context.Background(), db); err == nil {
		t.Fatal("accepted initialized database on second call")
	}
}

func TestInitializeFreshDatabaseSearchTriggers(t *testing.T) {
	db, _ := openTestDB(t)
	if err := InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	_, err := db.Exec(`INSERT INTO games(appid,name,name_en,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count)
        VALUES(42,'黑神话悟空','Black Myth',0,0,0,0,0,0,0,0,0,0,0,0,0)`)
	if err != nil {
		t.Fatal(err)
	}
	assertMatches := func(term string, want int) {
		t.Helper()
		var got int
		if err := db.QueryRow("SELECT count(*) FROM games_name_fts WHERE games_name_fts MATCH ?", term).Scan(&got); err != nil {
			t.Fatal(err)
		}
		if got != want {
			t.Fatalf("search %q matches %d, want %d", term, got, want)
		}
	}
	assertMatches("神话", 1)
	assertMatches("Black", 1)
	var rowid int64
	if err := db.QueryRow("SELECT rowid FROM games_name_fts WHERE appid=42").Scan(&rowid); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("UPDATE games SET name=name,name_en=name_en WHERE appid=42"); err != nil {
		t.Fatal(err)
	}
	var unchanged int64
	if err := db.QueryRow("SELECT rowid FROM games_name_fts WHERE appid=42").Scan(&unchanged); err != nil {
		t.Fatal(err)
	}
	if rowid != unchanged {
		t.Fatal("unchanged name rewrote search row")
	}
	if _, err := db.Exec("UPDATE games SET name='只狼',name_en='Sekiro' WHERE appid=42"); err != nil {
		t.Fatal(err)
	}
	assertMatches("神话", 0)
	assertMatches("只狼", 1)
	assertMatches("Sekiro", 1)
	if _, err := db.Exec("DELETE FROM games WHERE appid=42"); err != nil {
		t.Fatal(err)
	}
	assertMatches("Sekiro", 0)
}

func TestInitializeFreshDatabaseConstraints(t *testing.T) {
	db, _ := openTestDB(t)
	if err := InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("INSERT INTO agent_events(run_id,seq,event_type) VALUES('missing',1,'test')"); err == nil {
		t.Fatal("accepted event without its parent run")
	}
	price := `INSERT INTO game_price_history(appid,region_code,discount_percent,is_gold,is_bundle,price_status,snapshot_at)
        VALUES(42,'cn',0,0,0,'ok','2026-01-01 00:00:00')`
	if _, err := db.Exec(price); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(price); err == nil {
		t.Fatal("accepted duplicate price snapshot with null price/subscription identity")
	}
	candidate := `INSERT INTO notification_candidates(source_kind,event_id,cycle_id,appid,event_type,category,recipient,status,attempts)
        VALUES(?,1,1,42,'discount','price','local-test','pending',0)`
	for _, kind := range []string{"price_event", "fact_notice"} {
		if _, err := db.Exec(candidate, kind); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec(candidate, "price_event"); err == nil {
		t.Fatal("accepted duplicate notification identity")
	}
}

func TestInitializeFreshDatabaseConcurrent(t *testing.T) {
	db, _ := openTestDB(t)
	start := make(chan struct{})
	results := make(chan error, 2)
	for range 2 {
		go func() {
			<-start
			results <- InitializeFreshDatabase(context.Background(), db)
		}()
	}
	close(start)
	accepted := 0
	for range 2 {
		if <-results == nil {
			accepted++
		}
	}
	if accepted != 1 {
		t.Fatalf("accepted %d concurrent initializers, want 1", accepted)
	}
	var count int
	if err := db.QueryRow("SELECT count(*) FROM tags").Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 429 {
		t.Fatalf("concurrent seed count = %d, want 429", count)
	}
}
