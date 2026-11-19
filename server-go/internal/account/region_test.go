package account

import (
	"context"
	"fmt"
	"net/http"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestPrimaryWalletRegionUsesBoundOrderAndNeverCurrencyInference(t *testing.T) {
	db := newAccountDB(t)
	seedAccount(t, db, "active", "secondary", 1, "2026-02-01")
	seedAccount(t, db, "primary", "primary", 0, nil)
	mustExec(t, db, `UPDATE steam_accounts SET wallet_json=CASE steam_id WHEN 'active' THEN '{"region_code":"US","currency":"USD"}' ELSE '{"region_code":"CN","currency":"CNY"}' END`)
	got, e := PrimaryWalletRegion(context.Background(), db)
	if e != nil || got != "CN" {
		t.Fatalf("%s %v", got, e)
	}
	mustExec(t, db, `UPDATE steam_accounts SET wallet_json='{"currency":"USD"}' WHERE steam_id='primary'`)
	got, e = PrimaryWalletRegion(context.Background(), db)
	if e != nil || got != "" {
		t.Fatalf("inferred or used secondary country: %s %v", got, e)
	}
}

func TestDeriveRegionMatchesUniqueMultiRegionAndPrimaryFallback(t *testing.T) {
	db, e := store.Open(t.TempDir() + "/region.db")
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	ctx := context.Background()
	if e = store.InitializeFreshDatabase(ctx, db); e != nil {
		t.Fatal(e)
	}
	cases := []struct {
		currency, name, want string
	}{
		{"CNY", "", "cn"},
		{"inr", "印度", "in"},
		{"USD", "", "us"},
		{"USD", "US", "us"},
		{"USD", "阿根廷", "ar"},
		{"USD", "南亚", "pk"},
		{"USD", "印度", "us"},
		{"ZZZ", "中国", ""},
		{"", "中国", ""},
	}
	for _, tc := range cases {
		if got := deriveRegion(ctx, db, tc.currency, tc.name); got != tc.want {
			t.Fatalf("deriveRegion(%q, %q) = %q, want %q", tc.currency, tc.name, got, tc.want)
		}
	}
}

func TestSyncWalletPopulatesDerivedRegionAndHistoryCountry(t *testing.T) {
	const sid = "76561198000000011"
	s, db := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/account/":
			fmt.Fprint(w, walletPage(sid, "$10.00", "$10.00", 1))
		case "/account/history/":
			fmt.Fprint(w, `<div>货币转换至 USD（阿根廷、12）</div>`)
		default:
			fmt.Fprint(w, `{"persona_name":"fixture"}`)
		}
	})
	ctx := context.Background()
	status, e := s.BindCookies(ctx, "steamCountry=HK%7Cabc; steamLoginSecure="+sid+"||opaque")
	if e != nil {
		t.Fatal(e)
	}
	wallet := status["wallet"].(map[string]any)
	if wallet["region_code"] != "ar" || wallet["country_code"] != "阿根廷" {
		t.Fatalf("unexpected wallet region/country: %+v", wallet)
	}
	region, e := PrimaryWalletRegion(ctx, db)
	if e != nil || region != "ar" {
		t.Fatalf("PrimaryWalletRegion = %q, err=%v", region, e)
	}
}
