package alerts

import "testing"

func TestFeaturedUsesMembershipQualityAndComparableLow(t *testing.T) {
	current := map[string]any{"appid": 620, "region": "US", "currency": "USD", "subId": 400, "price": 999, "originalPrice": 1999, "discount": 50, "status": "ok", "observedAt": "2026-11-06T10:00:00", "currentKnown": true, "positiveRate": 9800, "reviewCount": 100}
	membership := map[string]any{"sources": []any{"favorite"}, "owned": false, "wishlisted": false, "ignored": false, "priority": 100}
	config := map[string]any{}
	check := func(want bool) {
		t.Helper()
		got, e := EvaluateFeatured(config, current, map[string]any{"baselineLow": 999}, membership)
		if e != nil || got["matched"] != want {
			t.Fatalf("wanted %v, got %v %v", want, got, e)
		}
	}
	check(true)
	membership["owned"] = true
	check(false)
	config["includeOwned"] = true
	check(true)
	membership["ignored"] = true
	check(false)
	config["includeIgnored"] = true
	check(true)
	config["minPositiveRate"] = 99
	check(false)
	config["minPositiveRate"] = 95
	check(true)
	config["lowMode"] = "new_low"
	check(false)
	config["lowMode"] = "low"
	check(true)
	config["scope"] = []any{"manual"}
	check(false)
	config["scope"] = []any{"followed"}
	check(true)
	current["currentKnown"] = false
	check(false)
}

func TestFeaturedMissingReviewsAndFullPriceAreExplicitConditions(t *testing.T) {
	current := map[string]any{"appid": 620, "currency": "CNY", "subId": 400, "price": 3500, "originalPrice": 3500, "discount": 0, "status": "ok", "observedAt": "now", "currentKnown": true, "reviewCount": 0}
	config := map[string]any{"scope": []any{"catalog"}, "minDiscount": 0, "allowFullPrice": true, "missingReviews": "exclude"}
	result, e := EvaluateFeatured(config, current, nil, nil)
	if e != nil || result["matched"] != false {
		t.Fatalf("%v %v", result, e)
	}
	config["missingReviews"] = "include"
	result, e = EvaluateFeatured(config, current, nil, nil)
	if e != nil || result["matched"] != true {
		t.Fatalf("%v %v", result, e)
	}
	config["allowFullPrice"] = false
	result, e = EvaluateFeatured(config, current, nil, nil)
	if e != nil || result["matched"] != false {
		t.Fatalf("%v %v", result, e)
	}
}
