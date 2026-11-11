package store

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"testing"
	"time"
)

func TestWriteGatePauseRejectsAndResumes(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("CREATE TABLE t(x INTEGER)"); err != nil {
		t.Fatal(err)
	}
	gate := NewWriteGate(db, path)

	ctx := context.Background()
	if err := gate.WithWrite(ctx, PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, "INSERT INTO t VALUES(1)")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if err := gate.PauseWrites(ctx); err != nil {
		t.Fatalf("空闲闸必须可暂停: %v", err)
	}
	err = gate.WithWrite(ctx, PriorityInteractive, func(conn *sql.Conn) error { return nil })
	if !errors.Is(err, ErrWritesPaused) {
		t.Fatalf("暂停窗口内新写必须被拒绝：%v", err)
	}
	gate.ResumeWrites()
	if err := gate.WithWrite(ctx, PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, "INSERT INTO t VALUES(2)")
		return err
	}); err != nil {
		t.Fatalf("恢复后写入必须可用：%v", err)
	}
}

func TestWriteGatePauseBusyRefused(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
	db, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("CREATE TABLE t(x INTEGER)"); err != nil {
		t.Fatal(err)
	}
	gate := NewWriteGate(db, path)
	lease, err := gate.Acquire(context.Background(), PriorityBackground)
	if err != nil {
		t.Fatal(err)
	}
	defer lease.Done()
	if err := gate.PauseWrites(context.Background()); !errors.Is(err, ErrWritesPaused) {
		t.Fatalf("持闸期间暂停必须被拒：%v", err)
	}
	lease.Done()
	deadline := time.Now().After(time.Now()) // 占位：释放后应可暂停
	_ = deadline
	if err := gate.PauseWrites(context.Background()); err != nil {
		t.Fatalf("写者位排空后必须可暂停: %v", err)
	}
	gate.ResumeWrites()
}
