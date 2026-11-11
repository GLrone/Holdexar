package miniseed

import (
	"database/sql"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/store"
)

const testVersion = "2026-10-25T04:51:19.313341+00:00"

func TestImportRollsBackAndPreservesLocalFacts(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, Filename)
	source, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer source.Close()
	exec(t, source, `CREATE TABLE seed_meta(key TEXT PRIMARY KEY,value TEXT);
		CREATE TABLE catalog(appid INTEGER PRIMARY KEY,name TEXT,name_en TEXT,type TEXT);
		CREATE TABLE curated(appid INTEGER PRIMARY KEY,xgp_tier TEXT,epic_date TEXT,is_epic INTEGER,is_hb INTEGER,hb_data TEXT,series_id TEXT);
		CREATE TABLE bundle_index(bundle_id INTEGER PRIMARY KEY,item_kind INTEGER,name TEXT,app_ids TEXT,must_purchase_as_set INTEGER);
		INSERT INTO seed_meta VALUES('schema_version','1'),('price_history_included','false'),('current_prices_included','false'),('exported_at_utc',?);`, testVersion)
	exec(t, source, `INSERT INTO catalog VALUES (101,'Alpha Story 1','Alpha Story 1','GAME'),(102,'Alpha Story 2','Alpha Story 2','GAME'),(103,'Seed rename',NULL,'GAME');
		INSERT INTO curated VALUES (101,'core',NULL,0,0,NULL,NULL),(103,'ultimate','2020-01-01',0,1,NULL,'seed-series');
		INSERT INTO bundle_index VALUES (201,-1,'New bundle','[101,102]',0),(202,1,'Seed bundle','[101]',1)`)
	live := filepath.Join(dir, "live.db")
	db, err := store.Open(live)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	exec(t, db, `INSERT INTO games(appid,name,type,family_sharing,trading_cards,is_epic,epic_date,is_hb,hb_data,series_id,
		hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count)
		VALUES(103,'Local Game','GAME',0,0,1,'2026-10-01',0,'','local-series',0,0,0,0,0,0,0,0,0);
		INSERT INTO bundles(bundle_id,name,item_kind,must_purchase_as_set,is_lowest,view_count,app_ids)
		VALUES(202,'Local Bundle',0,0,0,0,'[103]');
		CREATE TRIGGER fail_seed BEFORE INSERT ON bundles WHEN NEW.bundle_id=201 BEGIN SELECT RAISE(ABORT,'controlled failure'); END`)
	gate := store.NewWriteGate(db, live)
	if _, err := Import(t.Context(), db, gate, path, dir); err == nil {
		t.Fatal("expected the final merge to fail")
	}
	assertValue(t, db, `SELECT count(*) FROM games`, 1)
	assertValue(t, db, `SELECT count(*) FROM app_settings WHERE key='miniseed.imported_version'`, 0)
	assertValue(t, db, `SELECT is_hb FROM games WHERE appid=103`, 0)
	exec(t, db, `DROP TRIGGER fail_seed`)
	result, err := Import(t.Context(), db, gate, path, dir)
	if err != nil || result.Games != 2 || result.Curated != 2 || result.Bundles != 1 || result.Version != testVersion {
		t.Fatalf("import=%+v err=%v", result, err)
	}
	assertValue(t, db, `SELECT count(*) FROM games WHERE appid IN (101,102) AND smart_score IS NOT NULL AND series_id=(SELECT series_id FROM games WHERE appid=101) AND series_id<>''`, 2)
	assertValue(t, db, `SELECT CAST(updated_at AS TEXT) FROM games WHERE appid=101`, "2026-10-25 04:51:19.313341")
	assertValue(t, db, `SELECT name||':'||epic_date||':'||series_id||':'||xgp_tier||':'||is_hb FROM games WHERE appid=103`, "Local Game:2026-10-01:local-series:ultimate:1")
	assertValue(t, db, `SELECT name||':'||item_kind||':'||json_type(app_ids)||':'||json_extract(app_ids,'$[1]') FROM bundles WHERE bundle_id=201`, "New bundle:-1:array:102")
	assertValue(t, db, `SELECT name FROM bundles WHERE bundle_id=202`, "Local Bundle")
	assertValue(t, db, `SELECT (SELECT count(*) FROM game_current_prices)+(SELECT count(*) FROM game_price_history)+(SELECT count(*) FROM monitor_targets)`, 0)
	if result, err := Import(t.Context(), db, gate, path, dir); err != nil || result.Skipped == "" {
		t.Fatalf("repeat=%+v err=%v", result, err)
	}
	exec(t, source, `UPDATE seed_meta SET value='2026-11-01T00:00:00Z' WHERE key='exported_at_utc'; INSERT INTO catalog VALUES (104,'New Game',NULL,'GAME')`)
	if result, err := Import(t.Context(), db, gate, path, dir); err != nil || result.Games != 1 || result.Bundles != 0 {
		t.Fatalf("new release=%+v err=%v", result, err)
	}
	exec(t, source, `UPDATE seed_meta SET value='true' WHERE key='current_prices_included'; INSERT INTO catalog VALUES (105,'Forbidden',NULL,'GAME')`)
	if _, err := Import(t.Context(), db, gate, path, dir); err == nil {
		t.Fatal("seed declaring price contents was accepted")
	}
	assertValue(t, db, `SELECT count(*) FROM games WHERE appid=105`, 0)
}

func exec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.Exec(query, args...); err != nil {
		t.Fatal(err)
	}
}

func assertValue[T comparable](t *testing.T, db *sql.DB, query string, want T) {
	t.Helper()
	var got T
	if err := db.QueryRow(query).Scan(&got); err != nil || got != want {
		t.Fatalf("got %v, want %v, err=%v (%s)", got, want, err, query)
	}
}
