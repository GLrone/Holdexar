package notifications

import "testing"

func TestRegionResolutionUsesExplicitManualThenKnownAccountFact(t *testing.T) {
	selected, e := ResolveRegion("", nil, "", map[string]any{"currency": "USD"})
	if e != nil || selected.Region != nil {
		t.Fatalf("inferred country from currency: %v %v", selected, e)
	}
	selected, e = ResolveRegion("", nil, "us", nil)
	if e != nil || selected.Region == nil || *selected.Region != "US" || selected.Source != "account" {
		t.Fatalf("%v %v", selected, e)
	}
	selected, e = ResolveRegion("", map[string]any{"manualRegion": "cn"}, "us", map[string]any{"region": "US", "currency": "USD"})
	if e != nil || *selected.Region != "CN" || *selected.Currency != "CNY" || selected.CurrencySource != "region_mapping" {
		t.Fatalf("%v %v", selected, e)
	}
	selected, e = ResolveRegion("hk", map[string]any{"manualRegion": "CN"}, "US", map[string]any{"region": "hk", "currency": "USD"})
	if e != nil || *selected.Region != "HK" || *selected.Currency != "USD" || selected.Source != "rule" || selected.CurrencySource != "observation" {
		t.Fatalf("%v %v", selected, e)
	}
	if _, e = ResolveRegion("invalid", nil, "US", nil); e == nil {
		t.Fatal("invalid explicit region accepted")
	}
}
