package bundles

import (
	"context"
	"database/sql"
	"math"
	"path/filepath"
	"testing"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

func TestRefreshSortCacheUsesProductFamilyAndTrackedRegions(t *testing.T) {
	db, err := store.Open(filepath.Join(t.TempDir(), "bundles.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	for _, q := range []string{
		`CREATE TABLE bundles(bundle_id INTEGER PRIMARY KEY,name TEXT,must_purchase_as_set INTEGER,min_cny_fen INTEGER,diff_fen INTEGER,is_lowest INTEGER,smart_score REAL)`,
		`CREATE TABLE bundle_region_prices(id INTEGER PRIMARY KEY,bundle_id INTEGER,cny_fen INTEGER,discount_percent INTEGER,bundle_base_discount INTEGER,region_code TEXT,app_ids JSON)`,
		`CREATE TABLE games(appid INTEGER PRIMARY KEY,positive_rate INTEGER,review_count INTEGER,release_date TEXT)`,
		`INSERT INTO games VALUES(1,9000,1000,NULL),(2,NULL,0,NULL)`,
		`INSERT INTO bundles VALUES(10,'sample',0,NULL,0,0,0),(20,'empty',0,NULL,99,0,99),(30,'malformed',0,NULL,0,0,0)`,
		`INSERT INTO bundle_region_prices VALUES(1,10,10000,50,10,'cn','[1,2]'),(2,10,NULL,50,10,'us','[1]'),(3,10,6000,30,10,'US','[1]'),(4,10,1,90,0,'jp','[99]'),(5,10,4000,0,0,'bd','[1]'),(6,10,100,0,0,'ru','[1]'),(7,30,1000,0,0,'cn','invalid'),(8,30,500,0,0,'us','{}')`,
	} {
		if _, err := db.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
	conn, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if n, err := RefreshSortCache(context.Background(), conn, []int64{}, []string{"US", "PK"}); err != nil || n != 0 {
		t.Fatalf("empty update=%d,%v", n, err)
	}
	if n, err := RefreshSortCache(context.Background(), conn, nil, []string{"US", "PK"}); err != nil || n != 3 {
		t.Fatalf("all update=%d,%v", n, err)
	}
	var minimum sql.NullInt64
	var difference, lowest int64
	var score float64
	if err := conn.QueryRowContext(context.Background(), "SELECT min_cny_fen,diff_fen,is_lowest,smart_score FROM bundles WHERE bundle_id=10").Scan(&minimum, &difference, &lowest, &score); err != nil {
		t.Fatal(err)
	}
	if !minimum.Valid || minimum.Int64 != 4000 || difference != 6000 || lowest != 0 {
		t.Fatalf("bundle projection=%v/%d/%d", minimum, difference, lowest)
	}
	positive, reviews := int64(9000), int64(1000)
	quality := (derived.QualityScore(&positive, &reviews, nil, derived.Clock()) + 0.5) / 2
	familiarity := derived.FamiliarityScore(&reviews) / 2
	want := derived.BlendScore(derived.SaveScore(&difference), quality, 1, familiarity)
	if math.Abs(score-want) > 1e-12 {
		t.Fatalf("score=%v want %v", score, want)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT min_cny_fen,diff_fen,is_lowest,smart_score FROM bundles WHERE bundle_id=20").Scan(&minimum, &difference, &lowest, &score); err != nil || minimum.Valid || difference != 0 || lowest != 0 || math.Abs(score-0.14) > 1e-12 {
		t.Fatalf("empty projection=%v/%d/%d/%v, %v", minimum, difference, lowest, score, err)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT min_cny_fen FROM bundles WHERE bundle_id=30").Scan(&minimum); err != nil || !minimum.Valid || minimum.Int64 != 500 {
		t.Fatalf("bad arrays rejected projection=%v, %v", minimum, err)
	}
	if _, err := RefreshSortCache(context.Background(), conn, []int64{10}, []string{"US"}); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT min_cny_fen FROM bundles WHERE bundle_id=10").Scan(&minimum); err != nil || minimum.Int64 != 6000 {
		t.Fatalf("untracked BD retained=%v, %v", minimum, err)
	}
	if _, err := conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		t.Fatal(err)
	}
	if _, err := RefreshSortCache(context.Background(), conn, []int64{10}, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := conn.ExecContext(context.Background(), "ROLLBACK"); err != nil {
		t.Fatal(err)
	}
	if err := conn.QueryRowContext(context.Background(), "SELECT min_cny_fen FROM bundles WHERE bundle_id=10").Scan(&minimum); err != nil || minimum.Int64 != 6000 {
		t.Fatalf("caller rollback ignored=%v, %v", minimum, err)
	}
}
