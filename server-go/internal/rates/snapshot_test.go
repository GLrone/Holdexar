package rates

import (
	"context"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestRecomputeCNYPricesUsesTransactionAndRoundEven(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "rates.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, sql := range []string{
		`CREATE TABLE fx_rates(currency_code TEXT,rate_to_cny REAL)`,
		`INSERT INTO fx_rates VALUES('USD',0.5)`,
		`CREATE TABLE game_current_prices(id INTEGER,currency TEXT,price INTEGER,cny_fen INTEGER)`,
		`CREATE TABLE bundle_region_prices(id INTEGER,currency TEXT,price INTEGER,cny_fen INTEGER)`,
		`CREATE TABLE game_price_history(cny_fen INTEGER)`,
		`INSERT INTO game_price_history VALUES(12345)`,
		`INSERT INTO game_current_prices VALUES(1,'usd',1,9),(2,'USD',3,9),(3,'USD',0,9),(4,'UNKNOWN',3,9),(5,'USD',-2,9),(6,'USD',NULL,9)`,
		`INSERT INTO bundle_region_prices VALUES(1,'USD',5,9)`,
	} {
		if _, err := db.Exec(sql); err != nil {
			t.Fatal(err)
		}
	}
	conn, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	if err := RecomputeCNYPrices(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	for id, want := range map[int]int{1: 0, 2: 2, 3: 9, 4: 9, 5: 9, 6: 9} {
		var got int
		if err := conn.QueryRowContext(context.Background(), "SELECT cny_fen FROM game_current_prices WHERE id=?", id).Scan(&got); err != nil || got != want {
			t.Fatalf("id%d = %d want %d, %v", id, got, want, err)
		}
	}
	var got int
	if err := conn.QueryRowContext(context.Background(), "SELECT cny_fen FROM bundle_region_prices").Scan(&got); err != nil || got != 2 {
		t.Fatalf("bundle cents=%d, %v", got, err)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT cny_fen FROM game_price_history").Scan(&got); err != nil || got != 12345 {
		t.Fatalf("history changed=%d, %v", got, err)
	}
	if _, err := conn.ExecContext(context.Background(), "ROLLBACK"); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT cny_fen FROM game_current_prices WHERE id=1").Scan(&got); err != nil || got != 9 {
		t.Fatalf("caller rollback ignored=%d, %v", got, err)
	}
}
