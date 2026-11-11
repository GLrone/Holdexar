package notifications

import (
	"context"
	"fmt"
	"strings"

	"holdexar/server-go/internal/account"
)

type RegionSelection struct {
	Region         *string `json:"region"`
	Source         string  `json:"source"`
	Currency       *string `json:"currency"`
	CurrencySource string  `json:"currencySource"`
}

func ResolveRegion(explicit string, prefs map[string]any, fallback string, observed map[string]any) (RegionSelection, error) {
	raw := explicit
	source := "rule"
	if raw == "" {
		raw = stringValue(prefs["manualRegion"])
		source = "manual"
	}
	region := strings.ToUpper(strings.TrimSpace(raw))
	if region == "" {
		code := strings.ToUpper(strings.TrimSpace(fallback))
		if CurrencyForRegion(code) != "" {
			region, source = code, "account"
		}
	}
	if region == "" {
		return RegionSelection{Source: "unknown", CurrencySource: "unknown"}, nil
	}
	currency := CurrencyForRegion(region)
	if currency == "" {
		return RegionSelection{}, fmt.Errorf("unsupported notification region")
	}
	currencySource := "region_mapping"
	observedCurrency := strings.ToUpper(strings.TrimSpace(stringValue(observed["currency"])))
	observedRegion := stringValue(observed["region"])
	if observedRegion == "" {
		observedRegion = stringValue(observed["region_code"])
	}
	observedRegion = strings.ToUpper(strings.TrimSpace(observedRegion))
	if observedCurrency != "" && observedRegion == region {
		currency, currencySource = observedCurrency, "observation"
	}
	return RegionSelection{Region: &region, Source: source, Currency: &currency, CurrencySource: currencySource}, nil
}
func EffectiveRegion(ctx context.Context, q SettingsQuery, explicit string, prefs map[string]any, observed map[string]any) (RegionSelection, error) {
	fallback, _ := account.PrimaryWalletRegion(ctx, q)
	return ResolveRegion(explicit, prefs, fallback, observed)
}
