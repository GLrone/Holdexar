package proxypool

import (
	"database/sql"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func TestRetentionKeepsRecentRunsAndReferencedSnapshots(t *testing.T) {
	ctx := t.Context()
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	now := time.Date(2026, 11, 9, 4, 35, 0, 0, time.Local)
	if _, err = db.Exec(`WITH RECURSIVE n(i) AS (VALUES(1) UNION ALL SELECT i+1 FROM n WHERE i<203)
		INSERT INTO proxy_job_runs(id,status,kind,started_at) SELECT i,'done','test','2020-01-01' FROM n;
		INSERT INTO proxy_events(ts,kind) VALUES ('2020-01-01','old'),('2026-11-08','recent');
		INSERT INTO health_observations(node_id,level,ok,observed_at) VALUES
		('node','L2',1,'2026-09-01'),('node','L2',1,'2026-11-08');
		INSERT INTO proxy_nodes(node_id,runtime_name,proxy_type,state,consecutive_failures)
		VALUES ('node','test','http','ACTIVE',0)`); err != nil {
		t.Fatal(err)
	}
	sharedSHA, orphanSHA := strings.Repeat("a", 64), strings.Repeat("b", 64)
	shared, err := writeRaw(dir, 1, sharedSHA, []byte("shared"))
	if err != nil {
		t.Fatal(err)
	}
	orphan, err := writeRaw(dir, 1, orphanSHA, []byte("orphan"))
	if err != nil {
		t.Fatal(err)
	}
	out := filepath.Join(dir, "unrelated.bin")
	if err := os.WriteFile(out, []byte("keep"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err = db.Exec(`INSERT INTO subscription_snapshots(id,subscription_id,sha256,format,raw_path,node_count,status,fetched_at) VALUES
		(1,1,?,'clash',?,0,'OK','2020-01-01'),(2,1,?,'clash',?,0,'OK','2020-01-01'),
		(3,1,?,'clash',?,0,'OK','2020-01-01'),(4,1,?,'clash',?,0,'OK','2020-01-01'),
		(5,1,'c','clash',NULL,0,'OK','2020-01-01'),(6,1,'d','clash',NULL,0,'OK','2020-01-01'),
		(7,1,'','unknown',NULL,0,'FAILED','2020-01-01')`,
		sharedSHA, shared, orphanSHA, orphan, strings.Repeat("e", 64), out, sharedSHA, shared); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, path)
	first, err := pruneTelemetry(ctx, db, gate, dir, now, 2, 1)
	if err != nil || first["job_runs"] != 2 || first["snapshots"] != 2 || first["truncated"] != true {
		t.Fatalf("first=%v err=%v", first, err)
	}
	if _, err := os.Stat(orphan); !os.IsNotExist(err) {
		t.Fatalf("orphan still exists: %v", err)
	}
	for _, file := range []string{shared, out} {
		if _, err := os.Stat(file); err != nil {
			t.Fatal(err)
		}
	}
	second, err := PruneTelemetry(ctx, db, gate, dir, now)
	if err != nil || second["job_runs"] != 1 || second["snapshots"] != 2 || second["truncated"] != false {
		t.Fatalf("second=%v err=%v", second, err)
	}
	for table, want := range map[string]int{"proxy_job_runs": 200, "proxy_events": 1, "health_observations": 1, "proxy_nodes": 1, "subscription_snapshots": 3} {
		var count int
		if err := db.QueryRow(`SELECT count(*) FROM ` + table).Scan(&count); err != nil || count != want {
			t.Fatalf("%s count=%d want=%d err=%v", table, count, want, err)
		}
	}
	if _, err := os.Stat(out); err != nil {
		t.Fatal(err)
	}
	if err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `INSERT INTO subscription_snapshots(subscription_id,sha256,format,raw_path,node_count,status,fetched_at)
			VALUES (2,?,'clash',?,0,'OK','2026-11-09')`, orphanSHA, orphan)
		if err == nil {
			_, err = writeRaw(dir, 1, orphanSHA, []byte("referenced by a later commit"))
		}
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if err := reclaimSnapshotFiles(ctx, db, gate, dir, []retainedSnapshotFile{{subID: 1, sha: orphanSHA, path: orphan}}); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(orphan); err != nil {
		t.Fatalf("later reference was not protected: %v", err)
	}
}
