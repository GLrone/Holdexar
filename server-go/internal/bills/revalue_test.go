package bills

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestRevaluePreservesCurrencyAndFifteenDayCausalWindow(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	if e = store.InitializeFreshDatabase(ctx, db); e != nil {
		t.Fatal(e)
	}
	if _, e = db.Exec(`INSERT INTO bill_imports(id,steam_id,nickname,avatar_base64,source_file,warnings_json) VALUES(1,'account-a','','','','[]'),(2,'account-b','','','','[]');
INSERT INTO bill_game_txs(id,import_id,date,tx_type,items_json,currency,amount,sign,cny_fen,fx_rate,fx_note,payment,discount_pct,wallet_balance,base_price,is_gift,is_refund,orig_is_gift,raw_idx) VALUES
(1,1,'2026-01-01','','[]','USD',1,1,99,99,'','','','','',0,0,0,0),
(2,2,'2026-01-16','','[]','USD',2,-1,99,99,'','','','','',0,1,0,0),
(3,1,'2026-01-17','','[]','USD',1,1,99,99,'','','','','',0,0,0,0),
(4,2,'2026-01-02','','[]','EUR',1,1,99,99,'','','','','',0,0,0,0);
INSERT INTO bill_topup_txs(id,import_id,date,"desc",currency,amount,sign,fx_note,payment,tx_type,is_refund) VALUES(1,2,'2026-01-10','','USD',0.5,1,'','','',0);
INSERT INTO fx_rate_history(currency_code,rate_date,rate_to_cny,source_kind) VALUES('USD','2026-01-01',7,'derived'),('USD','2026-01-05',8,'observed'),('USD','2026-01-10',100,'carried');`); e != nil {
		t.Fatal(e)
	}
	run := func() (map[string]int, error) {
		var result map[string]int
		e := store.NewWriteGate(db, path).WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			var e error
			result, e = RevalueAffected(ctx, conn, map[string][]string{"USD": {"2026-01-01"}})
			return e
		})
		return result, e
	}
	result, e := run()
	if e != nil {
		t.Fatal(e)
	}
	if result["bills"] != 2 || result["transactions"] != 3 {
		t.Fatal(result)
	}
	for id, want := range map[int]int{1: 700, 2: -1600, 3: 99, 4: 99} {
		var got int
		if e = db.QueryRow("SELECT cny_fen FROM bill_game_txs WHERE id=?", id).Scan(&got); e != nil || got != want {
			t.Fatalf("id=%d got=%d want=%d err=%v", id, got, want, e)
		}
	}
	var topup int
	_ = db.QueryRow("SELECT cny_fen FROM bill_topup_txs WHERE id=1").Scan(&topup)
	if topup != 400 {
		t.Fatal(topup)
	}
	result, e = run()
	if e != nil || result["transactions"] != 0 {
		t.Fatalf("replay=%v err=%v", result, e)
	}
}
