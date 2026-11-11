package monitoring

import (
	"context"
	"database/sql"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestAccountSourcesReconcileBatchUnionWithoutChangingExclusionsOrDirectSources(t *testing.T) {
	s, db := fixture(t)
	ctx := context.Background()
	ids := make([]int64, 650)
	for i := range ids {
		ids[i] = int64(i + 1)
	}
	if _, e := db.Exec(`INSERT INTO wishlist_items(steamid,appid,active,owned,manual,wishlisted,manual_pool,excluded,board_pool)
 SELECT 'a',value,1,0,0,1,0,0,0 FROM json_each(?)`, encode(ids)); e != nil {
		t.Fatal(e)
	}
	if _, e := db.Exec(`INSERT INTO wishlist_items VALUES('b',1,NULL,1,1,0,0,0,0,0),('inactive',2,NULL,0,1,0,0,0,0,0);`); e != nil {
		t.Fatal(e)
	}
	on := true
	if _, e := s.Apply(ctx, "game", []int64{1}, Change{Sources: map[int64][]string{1: {"favorite", "board"}}, AddOnly: true, Excluded: &on, Reason: "user"}); e != nil {
		t.Fatal(e)
	}
	e := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		states, e := s.SyncGameSourcesTx(ctx, conn, ids)
		if e == nil && (len(states) != 650 || states[1] != "excluded" || states[650] != "active") {
			t.Errorf("batch states=%d first=%s last=%s", len(states), states[1], states[650])
		}
		return e
	})
	if e != nil {
		t.Fatal(e)
	}
	assertState(t, s, 1, "excluded", 4)
	assertState(t, s, 2, "active", 1)
	if _, e = db.Exec(`DELETE FROM wishlist_items WHERE steamid='a'`); e != nil {
		t.Fatal(e)
	}
	if e = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error { _, e := s.SyncGameSourcesTx(ctx, conn, ids); return e }); e != nil {
		t.Fatal(e)
	}
	assertState(t, s, 1, "excluded", 3)
	assertState(t, s, 2, "released", 0)
	assertState(t, s, 650, "released", 0)
}
