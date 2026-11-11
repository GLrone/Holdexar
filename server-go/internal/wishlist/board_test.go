package wishlist

import (
	"context"
	"testing"
)

func TestBoardPoolPreservesExclusionsAndInactiveSources(t *testing.T) {
	p, db := poolFixture(t)
	ctx := context.Background()
	if _, e := p.Add(ctx, []int64{1}, ""); e != nil {
		t.Fatal(e)
	}
	if _, e := p.EnsureBoard(ctx, []int64{2, 3}, "board"); e != nil {
		t.Fatal(e)
	}
	if _, e := p.Remove(ctx, []int64{2}); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`UPDATE monitor_sources SET active=0 WHERE target_id=3`); e != nil {
		t.Fatal(e)
	}
	result, e := p.EnsureBoard(ctx, []int64{1, 2, 3, 4, 4, -1}, "board")
	if e != nil || result["added"] != 1 || result["exists"] != 1 || result["skipped"] != 2 {
		t.Fatalf("%v %v", result, e)
	}
	var n int
	if e = db.QueryRow(`SELECT count(*) FROM wishlist_items`).Scan(&n); e != nil || n != 0 {
		t.Fatalf("account rows %d %v", n, e)
	}
	if e = db.QueryRow(`SELECT count(*) FROM monitor_sources WHERE source='board' AND active=1`).Scan(&n); e != nil || n != 1 {
		t.Fatalf("sources %d %v", n, e)
	}
}

func TestRecordBoardReplacesPublicBatchAndPreservesOtherImports(t *testing.T) {
	p, db := poolFixture(t)
	ctx := context.Background()
	if _, e := p.Add(ctx, []int64{1, 2}, "local.txt"); e != nil {
		t.Fatal(e)
	}
	if n, e := p.RecordBoard(ctx, []int64{2, 3, 3}); e != nil || n != 2 {
		t.Fatalf("%d %v", n, e)
	}
	if n, e := p.RecordBoard(ctx, []int64{4}); e != nil || n != 1 {
		t.Fatalf("%d %v", n, e)
	}
	var n int
	if e := db.QueryRow(`SELECT count(*) FROM preset_games WHERE source='topsellers' AND appid=4`).Scan(&n); e != nil || n != 1 {
		t.Fatalf("%d %v", n, e)
	}
	if e := db.QueryRow(`SELECT count(*) FROM preset_games`).Scan(&n); e != nil || n != 2 {
		t.Fatalf("%d %v", n, e)
	}
	if _, e := p.RecordBoard(ctx, nil); e != nil {
		t.Fatal(e)
	}
	if e := db.QueryRow(`SELECT count(*) FROM preset_games`).Scan(&n); e != nil || n != 2 {
		t.Fatalf("empty deleted batch %d %v", n, e)
	}
}
