package migration

import (
	"context"
	"path/filepath"
	"strconv"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestPrepareDatabaseHistoricalVersionMatrix(t *testing.T) {
	for version := 1; version <= store.CurrentSchemaVersion; version++ {
		t.Run(strconv.Itoa(version), func(t *testing.T) {
			path := filepath.Join(t.TempDir(), "holdexar.db")
			db, err := store.Open(path)
			if err != nil {
				t.Fatal(err)
			}
			if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
				db.Close()
				t.Fatal(err)
			}
			for _, sql := range []string{
				"CREATE TABLE kept_user_data(value TEXT)",
				"INSERT INTO kept_user_data VALUES('preserve')",
				"PRAGMA user_version=" + strconv.Itoa(version),
			} {
				if _, err := db.Exec(sql); err != nil {
					db.Close()
					t.Fatal(err)
				}
			}
			db.Close()
			db, err = PrepareDatabase(context.Background(), path)
			if err != nil {
				t.Fatal(err)
			}
			defer db.Close()
			var actual int
			if err := db.QueryRow("PRAGMA user_version").Scan(&actual); err != nil || actual != store.CurrentSchemaVersion {
				t.Fatalf("version=%d, %v", actual, err)
			}
			var value string
			if err := db.QueryRow("SELECT value FROM kept_user_data").Scan(&value); err != nil || value != "preserve" {
				t.Fatalf("user value=%q, %v", value, err)
			}
			if result, err := store.QuickCheck(path); err != nil || result != "ok" {
				t.Fatalf("integrity=%q, %v", result, err)
			}
			if version < store.CurrentSchemaVersion {
				if actual, err := store.ReadSchemaVersion(path + ".pre-migration.bak"); err != nil || actual != version {
					t.Fatalf("snapshot version=%d, %v", actual, err)
				}
			}
		})
	}
}

func TestPrepareDatabaseSeedsAndMigratesLegacyRegionChoice(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE app_settings(key TEXT PRIMARY KEY,value_json JSON,updated_at DATETIME);
INSERT INTO app_settings VALUES('crawl.enabled_regions','["cn","us"]',NULL);
PRAGMA user_version=32;`); err != nil {
		db.Close()
		t.Fatal(err)
	}
	db.Close()
	db, err = PrepareDatabase(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var count int
	if err := db.QueryRow("SELECT count(*) FROM crawl_regions WHERE enabled=1").Scan(&count); err != nil || count != 2 {
		t.Fatalf("enabled=%d, %v", count, err)
	}
	if err := db.QueryRow("SELECT count(*) FROM tags").Scan(&count); err != nil || count != 429 {
		t.Fatalf("tags=%d, %v", count, err)
	}
	if err := db.QueryRow("SELECT count(*) FROM app_settings WHERE key='crawl.enabled_regions'").Scan(&count); err != nil || count != 0 {
		t.Fatalf("legacy setting=%d, %v", count, err)
	}
}
