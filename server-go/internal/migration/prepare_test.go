package migration

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestPrepareRestoresExistingAchievementTables(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := PrepareDatabase(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec(`DROP TABLE achievement_notes; DROP TABLE challenge_progress`); err != nil {
		db.Close()
		t.Fatal(err)
	}
	db.Close()
	db, err = PrepareDatabase(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec(`INSERT INTO achievement_notes(steamid,appid,image_name,content,created_at,updated_at)
		VALUES('76561198000000001',220,'fixture','saved',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);
		INSERT INTO challenge_progress(steamid,challenge_id,progress,status,updated_at)
		VALUES('76561198000000001','first_trophy',1,'claimed',CURRENT_TIMESTAMP)`); err != nil {
		db.Close()
		t.Fatal(err)
	}
	db.Close()
	db, err = PrepareDatabase(ctx, path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var content, status string
	if err = db.QueryRow(`SELECT n.content,c.status FROM achievement_notes n JOIN challenge_progress c USING(steamid)`).Scan(&content, &status); err != nil || content != "saved" || status != "claimed" {
		t.Fatalf("existing notes and challenges changed: %q %q %v", content, status, err)
	}
}

func prepareFixture(t *testing.T, version int, duplicates bool) (string, *sql.DB) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	statements := []string{
		"CREATE TABLE price_alerts(id INTEGER PRIMARY KEY)",
		"CREATE TABLE alert_events(id INTEGER PRIMARY KEY)",
		"CREATE TABLE notification_candidates(source_kind TEXT NOT NULL,event_id INTEGER NOT NULL,recipient TEXT NOT NULL,cycle_id INTEGER)",
		"INSERT INTO notification_candidates(source_kind,event_id,recipient) VALUES ('price',7,'one')",
		"CREATE TABLE sample(value TEXT)",
		"INSERT INTO sample VALUES ('uncheckpointed')",
	}
	if duplicates {
		statements = append(statements,
			"INSERT INTO notification_candidates(source_kind,event_id,recipient) VALUES ('price',7,'one')",
			"CREATE INDEX ux_notification_candidate ON notification_candidates(event_id,recipient)")
	} else {
		statements = append(statements,
			"CREATE UNIQUE INDEX ux_notification_candidate ON notification_candidates(event_id,recipient)")
	}
	for _, statement := range statements {
		if _, err := db.Exec(statement); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := db.Exec("PRAGMA user_version = " + strconv.Itoa(version)); err != nil {
		t.Fatal(err)
	}
	return path, db
}

func TestPrepareDatabaseMigratesActual33Columns(t *testing.T) {
	path, original := prepareFixture(t, 33, false)
	if _, err := original.Exec("ALTER TABLE notification_candidates DROP COLUMN source_kind"); err != nil {
		t.Fatal(err)
	}
	db, err := PrepareDatabase(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	var kind string
	if err := db.QueryRow("SELECT source_kind FROM notification_candidates").Scan(&kind); err != nil || kind != "price_event" {
		t.Fatalf("legacy source = %q, %v", kind, err)
	}
	for _, query := range []string{
		"SELECT payload_json,due_at FROM notification_candidates",
		"SELECT config_json FROM price_alerts",
		"SELECT details_json FROM alert_events",
	} {
		rows, err := db.Query(query)
		if err != nil {
			t.Fatal(err)
		}
		rows.Close()
	}
	backup, err := store.OpenReadOnly(path + ".pre-migration.bak")
	if err != nil {
		t.Fatal(err)
	}
	defer backup.Close()
	if rows, err := backup.Query("SELECT source_kind FROM notification_candidates"); err == nil {
		rows.Close()
		t.Fatal("rollback snapshot contains migrated columns")
	}
}

func TestPrepareDatabaseRespectsMaintenanceProtection(t *testing.T) {
	for _, marker := range []string{`{"purpose":"restore"}`, `{"purpose":"update"}`, "damaged"} {
		t.Run(marker, func(t *testing.T) {
			path, _ := prepareFixture(t, 33, false)
			dir := filepath.Join(filepath.Dir(path), "backups")
			if err := os.Mkdir(dir, 0o700); err != nil {
				t.Fatal(err)
			}
			lock := filepath.Join(dir, ".restore.lock")
			if err := os.WriteFile(lock, []byte(marker), 0o600); err != nil {
				t.Fatal(err)
			}
			if db, err := PrepareDatabase(context.Background(), path); err == nil {
				db.Close()
				t.Fatal("startup ignored maintenance protection")
			}
			if version, err := store.ReadSchemaVersion(path); err != nil || version != 33 {
				t.Fatalf("protected database changed: %d, %v", version, err)
			}
			if _, err := os.Stat(path + ".pre-migration.bak"); !os.IsNotExist(err) {
				t.Fatalf("protected database created backup: %v", err)
			}
			if actual, err := os.ReadFile(lock); err != nil || string(actual) != marker {
				t.Fatalf("protection record changed: %v", err)
			}
		})
	}
}

func TestPrepareDatabaseMigrates33AndSnapshotsWAL(t *testing.T) {
	path, _ := prepareFixture(t, 33, false)
	db, err := PrepareDatabase(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if version, err := store.ReadSchemaVersion(path); err != nil || version != 34 {
		t.Fatalf("version = %d, %v", version, err)
	}
	if _, err := db.Exec("INSERT INTO notification_candidates(source_kind,event_id,recipient) VALUES ('rule',7,'one')"); err != nil {
		t.Fatalf("different sources must coexist: %v", err)
	}
	if _, err := db.Exec("INSERT INTO notification_candidates(source_kind,event_id,recipient) VALUES ('price',7,'one')"); err == nil {
		t.Fatal("same-source duplicate accepted")
	}
	backup, err := store.OpenReadOnly(path + ".pre-migration.bak")
	if err != nil {
		t.Fatal(err)
	}
	defer backup.Close()
	var version int
	var value string
	if err := backup.QueryRow("PRAGMA user_version").Scan(&version); err != nil || version != 33 {
		t.Fatalf("backup version = %d, %v", version, err)
	}
	if err := backup.QueryRow("SELECT value FROM sample").Scan(&value); err != nil || value != "uncheckpointed" {
		t.Fatalf("backup lost WAL data: %q, %v", value, err)
	}
	before, err := os.ReadFile(path + ".pre-migration.bak")
	if err != nil {
		t.Fatal(err)
	}
	reopened, err := PrepareDatabase(context.Background(), path)
	if err != nil {
		t.Fatal(err)
	}
	reopened.Close()
	after, err := os.ReadFile(path + ".pre-migration.bak")
	if err != nil || string(before) != string(after) {
		t.Fatalf("steady startup replaced rollback snapshot: %v", err)
	}
}

func TestPrepareDatabaseMigrationFailureRollsBack(t *testing.T) {
	path, original := prepareFixture(t, 33, true)
	if db, err := PrepareDatabase(context.Background(), path); err == nil {
		db.Close()
		t.Fatal("invalid unique index migration succeeded")
	}
	var version, count int
	var definition string
	if err := original.QueryRow("PRAGMA user_version").Scan(&version); err != nil || version != 33 {
		t.Fatalf("failure advanced version: %d, %v", version, err)
	}
	if err := original.QueryRow("SELECT count(*) FROM notification_candidates").Scan(&count); err != nil || count != 2 {
		t.Fatalf("failure changed rows: %d, %v", count, err)
	}
	if err := original.QueryRow("SELECT sql FROM sqlite_master WHERE name='ux_notification_candidate'").Scan(&definition); err != nil || !strings.HasPrefix(definition, "CREATE INDEX") {
		t.Fatalf("failure lost original index: %q, %v", definition, err)
	}
}

func TestPrepareDatabaseSnapshotFailurePreventsMigration(t *testing.T) {
	path, _ := prepareFixture(t, 33, false)
	if err := os.Mkdir(path+".pre-migration.bak", 0o700); err != nil {
		t.Fatal(err)
	}
	if db, err := PrepareDatabase(context.Background(), path); err == nil {
		db.Close()
		t.Fatal("migration ignored snapshot failure")
	}
	if version, err := store.ReadSchemaVersion(path); err != nil || version != 33 {
		t.Fatalf("snapshot failure changed version: %d, %v", version, err)
	}
}

func TestPrepareDatabaseRejectsNewerVersions(t *testing.T) {
	for _, version := range []int{35} {
		t.Run(strconv.Itoa(version), func(t *testing.T) {
			path, _ := prepareFixture(t, version, false)
			if db, err := PrepareDatabase(context.Background(), path); err == nil {
				db.Close()
				t.Fatal("unsupported schema accepted")
			}
			if actual, err := store.ReadSchemaVersion(path); err != nil || actual != version {
				t.Fatalf("version changed: %d, %v", actual, err)
			}
			if _, err := os.Stat(path + ".pre-migration.bak"); !os.IsNotExist(err) {
				t.Fatalf("unsupported schema created backup: %v", err)
			}
		})
	}
}

func TestPrepareDatabaseCancelledDoesNotCreateDatabase(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if db, err := PrepareDatabase(ctx, path); !errors.Is(err, context.Canceled) {
		if db != nil {
			db.Close()
		}
		t.Fatalf("cancelled prepare = %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("cancelled startup created database: %v", err)
	}
}
