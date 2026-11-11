package monitoring

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func fixture(t *testing.T) (*Service, *sql.DB) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	return New(db, store.NewWriteGate(db, path)), db
}

func TestSourcesExclusionAndManagedReconciliationShareOneState(t *testing.T) {
	s, db := fixture(t)
	ctx := context.Background()
	off, on := false, true
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Sources: map[int64][]string{1: {"owned", "favorite"}}, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	assertState(t, s, 1, "active", 2)
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Sources: map[int64][]string{1: {"family_wishlist"}}, Managed: []string{"owned", "family_wishlist"}}); e != nil {
		t.Fatal(e)
	}
	assertState(t, s, 1, "active", 2)
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Excluded: &on, Reason: "用户移除"}); e != nil {
		t.Fatal(e)
	}
	assertState(t, s, 1, "excluded", 2)
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Sources: map[int64][]string{1: {"owned"}}, Managed: []string{"owned", "family_wishlist"}}); e != nil {
		t.Fatal(e)
	}
	assertState(t, s, 1, "excluded", 2)
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Excluded: &off}); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Sources: map[int64][]string{}, Managed: []string{"favorite"}}); e != nil {
		t.Fatal(e)
	}
	assertState(t, s, 1, "active", 1)
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Sources: map[int64][]string{}}); e != nil {
		t.Fatal(e)
	}
	assertState(t, s, 1, "released", 0)
	var targets int
	_ = db.QueryRow("SELECT count(*) FROM monitor_targets").Scan(&targets)
	if targets != 1 {
		t.Fatalf("target records=%d", targets)
	}
}

func assertState(t *testing.T, s *Service, id int64, want string, count int) {
	t.Helper()
	snapshot, e := s.Snapshot(context.Background(), "game", id)
	if e != nil {
		t.Fatal(e)
	}
	if snapshot["state"] != want || len(snapshot["sources"].([]string)) != count {
		t.Fatalf("state=%v", snapshot)
	}
}

func TestBatchLifecyclePriorityAndIdempotentTimestamp(t *testing.T) {
	s, db := fixture(t)
	ctx := context.Background()
	at := time.Date(2026, 11, 6, 10, 0, 0, 0, time.UTC)
	s.Now = func() time.Time { return at }
	ids := make([]int64, 650)
	desired := map[int64][]string{}
	for i := range ids {
		ids[i] = int64(i + 1)
		desired[ids[i]] = []string{"owned"}
	}
	if _, e := s.Apply(ctx, "game", ids, Change{Sources: desired, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	var stamp string
	_ = db.QueryRow("SELECT CAST(activated_at AS TEXT) FROM monitor_targets WHERE target_id=1").Scan(&stamp)
	at = at.Add(time.Hour)
	if _, e := s.Apply(ctx, "game", ids, Change{Sources: desired, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	var unchanged string
	_ = db.QueryRow("SELECT CAST(activated_at AS TEXT) FROM monitor_targets WHERE target_id=1").Scan(&unchanged)
	if stamp != unchanged {
		t.Fatal("idempotent source changed lifecycle")
	}
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Sources: map[int64][]string{1: {"favorite"}}, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	var changed string
	_ = db.QueryRow("SELECT CAST(activated_at AS TEXT) FROM monitor_targets WHERE target_id=1").Scan(&changed)
	if stamp == changed {
		t.Fatal("priority transition did not update active timestamp")
	}
	stats, e := s.Stats(ctx)
	if e != nil {
		t.Fatal(e)
	}
	if stats["monitoring"].(map[string]int)["active"] != 650 {
		t.Fatalf("stats=%v", stats)
	}
}

func TestFailureRollsBackAllSourcesAndLifecycle(t *testing.T) {
	s, db := fixture(t)
	ctx := context.Background()
	if _, e := db.Exec(`CREATE TRIGGER reject_source BEFORE INSERT ON monitor_sources WHEN NEW.target_id=2 BEGIN SELECT RAISE(ABORT,'controlled failure'); END`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Apply(ctx, "game", []int64{1, 2}, Change{Sources: map[int64][]string{1: {"manual"}, 2: {"manual"}}, AddOnly: true}); e == nil {
		t.Fatal("failure swallowed")
	}
	var count int
	_ = db.QueryRow("SELECT count(*) FROM monitor_targets").Scan(&count)
	if count != 0 {
		t.Fatalf("partial targets committed=%d", count)
	}
}
