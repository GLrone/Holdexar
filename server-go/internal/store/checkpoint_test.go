package store

import (
	"context"
	"path/filepath"
	"testing"
	"time"
)

func TestTruncateWALYieldsToReaderThenPreservesWrites(t *testing.T) {
	ctx := t.Context()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	gate := NewWriteGate(db, path)
	if _, err := db.Exec(`CREATE TABLE checkpoint_fixture (v INTEGER); INSERT INTO checkpoint_fixture VALUES (1)`); err != nil {
		t.Fatal(err)
	}
	lease, err := gate.Acquire(ctx, PriorityInteractive)
	if err != nil {
		t.Fatal(err)
	}
	skipped, err := gate.TruncateWAL(ctx, path)
	lease.Done()
	if err != nil || skipped["skipped"] != "数据库正在写入" {
		t.Fatalf("writer-held checkpoint=%v err=%v", skipped, err)
	}
	reader, err := db.BeginTx(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Rollback()
	var value int
	if err := reader.QueryRow(`SELECT sum(v) FROM checkpoint_fixture`).Scan(&value); err != nil || value != 1 {
		t.Fatalf("reader value=%d err=%v", value, err)
	}
	if _, err := db.Exec(`INSERT INTO checkpoint_fixture VALUES (2)`); err != nil {
		t.Fatal(err)
	}
	short, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	busy, err := gate.TruncateWAL(short, path)
	if err != nil || busy["busy"] != true || busy["afterBytes"].(int64) == 0 {
		t.Fatalf("reader-held checkpoint=%v err=%v", busy, err)
	}
	if err := reader.Rollback(); err != nil {
		t.Fatal(err)
	}
	clean, err := gate.TruncateWAL(ctx, path)
	if err != nil || clean["busy"] != false || clean["beforeBytes"].(int64) == 0 || clean["afterBytes"] != int64(0) {
		t.Fatalf("idle checkpoint=%v err=%v", clean, err)
	}
	var timeout int
	if err := db.QueryRow(`PRAGMA busy_timeout`).Scan(&timeout); err != nil || timeout != 60000 {
		t.Fatalf("pooled timeout=%d err=%v", timeout, err)
	}
	if err := db.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	if err := reopened.QueryRow(`SELECT sum(v) FROM checkpoint_fixture`).Scan(&value); err != nil || value != 3 {
		t.Fatalf("reopened value=%d err=%v", value, err)
	}
}
