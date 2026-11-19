package monitoring

import (
	"context"
	"database/sql"
	"reflect"
	"testing"
)

func TestExclusionReason(t *testing.T) {
	s, _ := fixture(t)
	ctx := context.Background()
	on, off := true, false
	if v, e := s.ExclusionReason(ctx, "bundle", 1); e != nil || v != "" {
		t.Fatalf("无排除 reason=%q err=%v", v, e)
	}
	if _, e := s.Apply(ctx, "bundle", []int64{1}, Change{Excluded: &on, Reason: "bundle_removed"}); e != nil {
		t.Fatal(e)
	}
	if v, e := s.ExclusionReason(ctx, "bundle", 1); e != nil || v != "bundle_removed" {
		t.Fatalf("active 排除 reason=%q err=%v", v, e)
	}
	if _, e := s.Apply(ctx, "bundle", []int64{2}, Change{Excluded: &on}); e != nil {
		t.Fatal(e)
	}
	if v, e := s.ExclusionReason(ctx, "bundle", 2); e != nil || v != "" {
		t.Fatalf("reason 为 NULL 时应返回空串 reason=%q err=%v", v, e)
	}
	if _, e := s.Apply(ctx, "bundle", []int64{1}, Change{Excluded: &off}); e != nil {
		t.Fatal(e)
	}
	if v, e := s.ExclusionReason(ctx, "bundle", 1); e != nil || v != "" {
		t.Fatalf("排除解除后 reason=%q err=%v", v, e)
	}
	if _, e := s.Apply(ctx, "game", []int64{7}, Change{Excluded: &on, Reason: "用户移除"}); e != nil {
		t.Fatal(e)
	}
	if v, e := s.ExclusionReason(ctx, "game", 7); e != nil || v != "用户移除" {
		t.Fatalf("game 类型 reason=%q err=%v", v, e)
	}
}

func assertCounts(t *testing.T, db *sql.DB, targets, sources, exclusions int) {
	t.Helper()
	var nT, nS, nE int
	if e := db.QueryRow("SELECT count(*) FROM monitor_targets").Scan(&nT); e != nil {
		t.Fatal(e)
	}
	if e := db.QueryRow("SELECT count(*) FROM monitor_sources").Scan(&nS); e != nil {
		t.Fatal(e)
	}
	if e := db.QueryRow("SELECT count(*) FROM monitor_exclusions").Scan(&nE); e != nil {
		t.Fatal(e)
	}
	if nT != targets || nS != sources || nE != exclusions {
		t.Fatalf("行数 targets=%d sources=%d exclusions=%d，期望 %d/%d/%d", nT, nS, nE, targets, sources, exclusions)
	}
}

func TestForgetLifecycleRefusalAndErasure(t *testing.T) {
	s, db := fixture(t)
	ctx := context.Background()
	on, off := true, false

	if _, e := s.Apply(ctx, "bundle", []int64{1}, Change{Sources: map[int64][]string{1: {"favorite"}}, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	if applied, e := s.Forget(ctx, "bundle", 1); e != nil || applied {
		t.Fatalf("激活来源时 forget=%v err=%v", applied, e)
	}
	assertCounts(t, db, 1, 1, 0)

	if _, e := s.Apply(ctx, "bundle", []int64{2}, Change{Sources: map[int64][]string{2: {"favorite", "import"}}, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Apply(ctx, "bundle", []int64{2}, Change{Sources: map[int64][]string{2: {"import"}}}); e != nil {
		t.Fatal(e)
	}
	if applied, e := s.Forget(ctx, "bundle", 2); e != nil || applied {
		t.Fatalf("import 仍激活时 forget=%v err=%v", applied, e)
	}
	if _, e := s.Apply(ctx, "bundle", []int64{2}, Change{Sources: map[int64][]string{}}); e != nil {
		t.Fatal(e)
	}
	if applied, e := s.Forget(ctx, "bundle", 2); e != nil || !applied {
		t.Fatalf("released 后 forget=%v err=%v", applied, e)
	}
	assertCounts(t, db, 1, 1, 0)

	if _, e := s.Apply(ctx, "bundle", []int64{3}, Change{Excluded: &on, Reason: "bundle_removed"}); e != nil {
		t.Fatal(e)
	}
	if applied, e := s.Forget(ctx, "bundle", 3); e != nil || applied {
		t.Fatalf("激活排除时 forget=%v err=%v", applied, e)
	}
	assertCounts(t, db, 2, 1, 1)

	if applied, e := s.Forget(ctx, "bundle", 99); e != nil || !applied {
		t.Fatalf("无记录 forget=%v err=%v", applied, e)
	}
	assertCounts(t, db, 2, 1, 1)

	if _, e := s.Apply(ctx, "bundle", []int64{4}, Change{Sources: map[int64][]string{4: {"favorite"}}, Excluded: &on, Reason: "用户移除"}); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Apply(ctx, "bundle", []int64{4}, Change{Excluded: &off, Sources: map[int64][]string{}}); e != nil {
		t.Fatal(e)
	}
	if applied, e := s.Forget(ctx, "bundle", 4); e != nil || !applied {
		t.Fatalf("排除历史态 forget=%v err=%v", applied, e)
	}
	assertCounts(t, db, 2, 1, 2)
}

func TestActiveBundleIDsOrderingAndExclusionFilter(t *testing.T) {
	s, db := fixture(t)
	ctx := context.Background()
	on := true
	feed := map[int64][]string{
		3:  {"favorite"},
		10: {"favorite"},
		5:  {"import"},
		7:  {"board", "manual"},
		9:  {"favorite"},
		8:  {"favorite"},
	}
	if _, e := s.Apply(ctx, "bundle", []int64{3, 5, 7, 8, 9, 10}, Change{Sources: feed, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Apply(ctx, "bundle", []int64{9}, Change{Excluded: &on, Reason: "bundle_removed"}); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Apply(ctx, "bundle", []int64{8}, Change{Sources: map[int64][]string{}}); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`INSERT INTO monitor_sources(target_type,target_id,source,priority,active,created_at,updated_at) VALUES('bundle',11,'import',30,1,'2026-01-01 00:00:00','2026-01-01 00:00:00')`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.Apply(ctx, "game", []int64{20}, Change{Sources: map[int64][]string{20: {"favorite"}}, AddOnly: true}); e != nil {
		t.Fatal(e)
	}
	ids, e := s.ActiveBundleIDs(ctx)
	if e != nil {
		t.Fatal(e)
	}
	want := []int64{3, 10, 7, 5}
	if !reflect.DeepEqual(ids, want) {
		t.Fatalf("ids=%v want=%v", ids, want)
	}
}
