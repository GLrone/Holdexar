package alerts

import "testing"

func TestRuleRequiresReliableObservationAndUsesNativeThreshold(t *testing.T) {
	rule := map[string]any{"appid": 620, "region": "US", "targetType": "price", "targetValue": 999, "config": map[string]any{"currency": "USD"}}
	cur := map[string]any{"appid": 620, "region": "US", "currency": "USD", "subId": 400, "price": 999, "cnyFen": 7000, "status": "ok", "observedAt": "2026-11-06T10:00:00", "currentKnown": true}
	validated, err := ValidateRule(rule)
	if err != nil {
		t.Fatal(err)
	}
	if !EvaluateRule(validated, cur, nil)["matched"].(bool) {
		t.Fatal("native USD threshold did not match")
	}
	cur["currentKnown"] = false
	if EvaluateRule(validated, cur, nil)["matched"].(bool) {
		t.Fatal("unreliable observation matched")
	}
	cur["currentKnown"] = true
	cur["currency"] = "CNY"
	if EvaluateRule(validated, cur, nil)["matched"].(bool) {
		t.Fatal("cross-currency threshold matched")
	}
	rule["targetValue"] = 1.5
	if _, err = ValidateRule(rule); err == nil {
		t.Fatal("fractional minor units accepted")
	}
}

func TestLowRuleNeedsPriorComparablePrice(t *testing.T) {
	cur := map[string]any{"currency": "CNY", "subId": 400, "price": 999, "status": "ok", "observedAt": "now", "currentKnown": true}
	for _, kind := range []string{"historic_low", "new_low"} {
		rule := map[string]any{"targetType": kind}
		if EvaluateRule(rule, cur, nil)["matched"].(bool) {
			t.Fatal("invented baseline")
		}
		result := EvaluateRule(rule, cur, map[string]any{"baselineLow": 999})
		if result["matched"].(bool) != (kind == "historic_low") {
			t.Fatalf("equal baseline %s: %v", kind, result)
		}
	}
}

func TestRuleRejectsMalformedConfiguration(t *testing.T) {
	_, e := ValidateRule(map[string]any{"appid": 620, "region": "US", "targetType": "price", "targetValue": 999, "config": []any{"USD"}})
	if e == nil {
		t.Fatal("malformed config silently discarded")
	}
}
