package backup

import (
	"compress/gzip"
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func TestAutomaticArchiveIncludesWALAndUsesExistingBackupWindow(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`PRAGMA wal_autocheckpoint=0; CREATE TABLE games(id INTEGER PRIMARY KEY, name TEXT); INSERT INTO games VALUES(620,'WAL committed')`); err != nil {
		t.Fatal(err)
	}
	if wal, err := os.Stat(path + "-wal"); err != nil || wal.Size() == 0 {
		t.Fatalf("expected committed WAL: %v", err)
	}
	now := time.Now().Truncate(time.Second)
	s := New(db, path, func() time.Time { return now }, store.NewWriteGate(db, path))
	result, skipped, err := s.Automatic(ctx)
	if err != nil || skipped || result.Games != 1 || !result.IntegrityOK {
		t.Fatalf("backup=%+v skipped=%v err=%v", result, skipped, err)
	}
	if _, skipped, err := s.Automatic(ctx); err != nil || !skipped {
		t.Fatalf("fresh archive did not skip: %v %v", skipped, err)
	}
	if _, err := os.Stat(result.Path + ".gz.json"); err != nil {
		t.Fatalf("original archive sidecar missing: %v", err)
	}
	for _, deep := range []bool{false, true} {
		v, err := s.Verify(ctx, result.Name, deep)
		if err != nil || !v.IntegrityOK || v.Games != 1 || v.SHA256 != result.SHA256 {
			t.Fatalf("verify(deep=%v)=%+v err=%v", deep, v, err)
		}
	}
	file, err := os.Open(result.Path)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	reader, err := gzip.NewReader(file)
	if err != nil {
		t.Fatal(err)
	}
	defer reader.Close()
	restored := filepath.Join(t.TempDir(), "restored.db")
	out, err := os.Create(restored)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := io.Copy(out, reader); err != nil {
		out.Close()
		t.Fatal(err)
	}
	if err := out.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, err := store.OpenReadOnly(restored)
	if err != nil {
		t.Fatal(err)
	}
	defer reopened.Close()
	var value string
	if err := reopened.QueryRow(`SELECT name FROM games WHERE id=620`).Scan(&value); err != nil || value != "WAL committed" {
		t.Fatalf("archived value=%q err=%v", value, err)
	}
	if err := os.Chtimes(result.Path, now.Add(-25*time.Hour), now.Add(-25*time.Hour)); err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Second)
	marker := filepath.Join(filepath.Dir(result.Path), ".restore.lock")
	if err := os.WriteFile(marker, []byte(`{"pid":2147483647,"purpose":"restore"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if _, skipped, err := s.Automatic(ctx); skipped || !errors.Is(err, store.ErrMaintenanceProtected) {
		t.Fatalf("restore protection skipped=%v err=%v", skipped, err)
	}
	if _, err := os.Stat(marker); err != nil {
		t.Fatalf("restore protection was removed: %v", err)
	}
}

func TestRotationKeepsManualSeparateAndRejectsTampering(t *testing.T) {
	ctx := t.Context()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`CREATE TABLE games(id INTEGER PRIMARY KEY); INSERT INTO games VALUES(620); PRAGMA wal_checkpoint(TRUNCATE)`); err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	s := New(db, path, func() time.Time { return now }, store.NewWriteGate(db, path))
	oldManual, err := s.Create(ctx, "first")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"old1.db", "old2.db", "old3.db", "old4.db"} {
		backupPath := filepath.Join(s.dir, name)
		copy, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(backupPath, copy, 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Chtimes(backupPath, now.Add(-48*time.Hour), now.Add(-48*time.Hour)); err != nil {
			t.Fatal(err)
		}
	}
	legacy, err := s.Verify(ctx, "old1.db", false)
	if err != nil || !legacy.IntegrityOK || legacy.Games != 1 || legacy.Checks.Hash != nil {
		t.Fatalf("legacy backup=%+v err=%v", legacy, err)
	}
	if err := os.Chtimes(oldManual.Path, now.Add(-25*time.Hour), now.Add(-25*time.Hour)); err != nil {
		t.Fatal(err)
	}
	now = now.Add(time.Second)
	auto, skipped, err := s.Automatic(ctx)
	if err != nil || skipped {
		t.Fatalf("automatic skipped=%v err=%v", skipped, err)
	}
	manual, err := s.Create(ctx, "second")
	if err != nil {
		t.Fatal(err)
	}
	items, err := s.List()
	if err != nil || len(items) != 3 || items[2].Name != "old4.db" {
		t.Fatalf("retained items=%+v err=%v", items, err)
	}
	for _, suffix := range []string{"", metadataSuffix} {
		if _, err := os.Stat(oldManual.Path + suffix); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("old manual %s not removed: %v", suffix, err)
		}
	}
	if err := os.WriteFile(manual.Path, []byte("invalid archive"), 0600); err != nil {
		t.Fatal(err)
	}
	v, err := s.Verify(ctx, manual.Name, true)
	if err != nil || v.IntegrityOK || v.Checks.Hash == nil || *v.Checks.Hash || v.Checks.Deep != nil {
		t.Fatalf("tampered backup=%+v err=%v", v, err)
	}
	if _, err := os.Stat(auto.Path); err != nil {
		t.Fatalf("automatic backup was removed by manual rotation: %v", err)
	}
}
