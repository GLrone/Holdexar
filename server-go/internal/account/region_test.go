package account

import (
	"context"
	"testing"
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
