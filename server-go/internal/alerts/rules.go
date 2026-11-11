package alerts

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"

	"holdexar/server-go/internal/notifications"
)

func number(v any) (float64, bool) {
	switch n := v.(type) {
	case json.Number:
		f, e := n.Float64()
		return f, e == nil
	case float64:
		return n, true
	case int:
		return float64(n), true
	case int64:
		return float64(n), true
	case string:
		f, e := strconv.ParseFloat(n, 64)
		return f, e == nil
	}
	return 0, false
}
func obj(v any) map[string]any {
	m, _ := v.(map[string]any)
	if m == nil {
		return map[string]any{}
	}
	return m
}
func copyMap(m map[string]any) map[string]any {
	o := map[string]any{}
	for k, v := range m {
		o[k] = v
	}
	return o
}
func text(v any) string {
	if v == nil {
		return ""
	}
	return fmt.Sprint(v)
}
func integer(v any, name string, maximum *float64) (float64, error) {
	n, ok := number(v)
	if !ok {
		return 0, fmt.Errorf("%s必须是整数", name)
	}
	if math.IsNaN(n) || math.IsInf(n, 0) || n < 0 || math.Trunc(n) != n {
		return 0, fmt.Errorf("%s必须是非负整数", name)
	}
	if maximum != nil && n > *maximum {
		return 0, fmt.Errorf("%s超过允许范围", name)
	}
	return n, nil
}
func ValidateRule(raw map[string]any) (map[string]any, error) {
	r := copyMap(raw)
	id, _ := number(r["appid"])
	if id <= 0 {
		return nil, fmt.Errorf("请选择游戏")
	}
	region := strings.ToUpper(strings.TrimSpace(text(r["region"])))
	if notifications.CurrencyForRegion(region) == "" {
		return nil, fmt.Errorf("请选择有效的提醒地区")
	}
	r["region"] = region
	kind, ok := r["targetType"]
	if !ok {
		kind = "price"
		r["targetType"] = kind
	}
	if kind != "price" && kind != "pct" && kind != "historic_low" && kind != "new_low" {
		return nil, fmt.Errorf("未知提醒类型")
	}
	if r["config"] != nil {
		if _, ok := r["config"].(map[string]any); !ok {
			return nil, fmt.Errorf("提醒配置必须是对象")
		}
	}
	c := copyMap(obj(r["config"]))
	if c["windowDays"] != nil {
		n, ok := number(c["windowDays"])
		if !ok || (n != 90 && n != 180 && n != 365) {
			return nil, fmt.Errorf("历史比较范围仅支持全部本地历史或90、180、365天")
		}
	}
	if currency := text(c["currency"]); currency != "" {
		currency = strings.ToUpper(currency)
		if !notifications.KnownCurrency(currency) {
			return nil, fmt.Errorf("未知价格币种")
		}
		c["currency"] = currency
	}
	if kind == "price" || kind == "pct" {
		var maximum *float64
		if kind == "pct" {
			n := float64(100)
			maximum = &n
		}
		n, err := integer(r["targetValue"], "目标值", maximum)
		if err != nil {
			return nil, err
		}
		r["targetValue"] = n
	}
	mode, ok := r["repeatMode"]
	if !ok {
		mode = "once"
		r["repeatMode"] = mode
	}
	if mode != "once" && mode != "cooldown" && mode != "always" {
		return nil, fmt.Errorf("未知重复策略")
	}
	if r["repeatHours"] != nil {
		n, e := integer(r["repeatHours"], "重复间隔", nil)
		if e != nil {
			return nil, e
		}
		r["repeatHours"] = max(1, n)
	}
	if len(c) == 0 {
		r["config"] = nil
	} else {
		r["config"] = c
	}
	return r, nil
}

func newEvaluation(current, history map[string]any, window any) map[string]any {
	observed := copyMap(current)
	comparison := copyMap(history)
	comparison["windowDays"] = window
	comparison["complete"] = false
	return map[string]any{"matched": true, "conditions": []any{}, "exclusions": []any{}, "comparison": comparison, "observed": observed, "labels": []any{}}
}
func addCondition(result map[string]any, code string, passed bool, actual, expected any) {
	item := map[string]any{"code": code, "passed": passed, "actual": actual, "expected": expected}
	result["conditions"] = append(result["conditions"].([]any), item)
	if !passed {
		result["matched"] = false
		result["exclusions"] = append(result["exclusions"].([]any), item)
	}
}
func currentConditions(result map[string]any) {
	current := obj(result["observed"])
	condition := func(code string, passed bool, actual, expected any) {
		addCondition(result, code, passed, actual, expected)
	}
	condition("price_available", current["status"] == "ok" && current["price"] != nil, current["status"], "ok")
	condition("known_currency", text(current["currency"]) != "", current["currency"], "observed currency")
	sub, _ := number(current["subId"])
	known := true
	if v, ok := current["currentKnown"]; ok {
		known = v == true
	}
	condition("known_product", sub != 0 && known, current["subId"], "identified standard Steam observation")
	condition("observation_time", text(current["observedAt"]) != "", current["observedAt"], "observation timestamp")
}
func lowCondition(result map[string]any, mode string) {
	current, comparison := obj(result["observed"]), obj(result["comparison"])
	low, ok := number(comparison["baselineLow"])
	addCondition(result, "no_baseline", ok, comparison["baselineLow"], "prior comparable positive price")
	if ok {
		price, pok := number(current["price"])
		matched := pok && price > 0 && price <= low
		if mode == "new_low" {
			matched = pok && price > 0 && price < low
		}
		addCondition(result, mode, matched, current["price"], comparison["baselineLow"])
	}
}
func EvaluateRule(rule, current, history map[string]any) map[string]any {
	result := newEvaluation(current, history, obj(rule["config"])["windowDays"])
	currentConditions(result)
	observed := obj(result["observed"])
	condition := func(code string, passed bool, actual, expected any) {
		addCondition(result, code, passed, actual, expected)
	}
	kind := text(rule["targetType"])
	if kind == "" {
		kind = "price"
	}
	switch kind {
	case "price":
		currency := text(obj(rule["config"])["currency"])
		field := "cnyFen"
		basis := "legacy_cny"
		if currency != "" {
			field = "price"
			basis = "native"
			condition("currency_mismatch", currency == text(current["currency"]), current["currency"], currency)
		}
		observed["thresholdBasis"] = basis
		amount, aok := number(current[field])
		target, tok := number(rule["targetValue"])
		condition("target_price", aok && tok && amount <= target, current[field], rule["targetValue"])
	case "pct":
		amount, aok := number(current["discount"])
		target, tok := number(rule["targetValue"])
		condition("target_discount", aok && tok && amount >= target, current["discount"], rule["targetValue"])
	case "historic_low", "new_low":
		mode := "low"
		if kind == "new_low" {
			mode = kind
		}
		lowCondition(result, mode)
	default:
		condition("rule_type", false, kind, []string{"price", "pct", "historic_low", "new_low"})
	}
	if result["matched"] == true {
		result["labels"] = []any{kind}
	}
	return result
}

func sourceList(value any) []string {
	values := []string{}
	switch list := value.(type) {
	case []any:
		for _, v := range list {
			values = append(values, text(v))
		}
	case []string:
		values = append(values, list...)
	}
	return values
}
func EvaluateFeatured(config, current, history, membership map[string]any) (map[string]any, error) {
	config, e := notifications.ValidateFeatured(config)
	if e != nil {
		return nil, e
	}
	result := newEvaluation(current, history, config["windowDays"])
	currentConditions(result)
	condition := func(code string, passed bool, actual, expected any) {
		addCondition(result, code, passed, actual, expected)
	}
	sources := map[string]bool{}
	for _, source := range sourceList(membership["sources"]) {
		sources[source] = true
	}
	available := []struct {
		key string
		ok  bool
	}{{"followed", sources["favorite"]}, {"manual", sources["manual"]}, {"wishlist", sources["family_wishlist"] || membership["wishlisted"] == true}, {"owned", membership["owned"] == true}, {"catalog", true}}
	scope := sourceList(config["scope"])
	actual := []string{}
	scopeOK := false
	for _, v := range available {
		if v.ok {
			actual = append(actual, v.key)
			for _, wanted := range scope {
				if wanted == v.key {
					scopeOK = true
				}
			}
		}
	}
	condition("scope", scopeOK, actual, config["scope"])
	owned, ignored := membership["owned"] == true, membership["ignored"] == true
	condition("owned", config["includeOwned"] == true || !owned, owned, config["includeOwned"])
	condition("ignored", config["includeIgnored"] == true || !ignored, ignored, config["includeIgnored"])
	discount, dok := number(current["discount"])
	condition("full_price", config["allowFullPrice"] == true || dok && discount > 0, current["discount"], config["allowFullPrice"])
	if threshold, ok := number(config["minDiscount"]); ok {
		condition("min_discount", dok && discount >= threshold, current["discount"], config["minDiscount"])
	}
	price, pok := number(current["price"])
	original, ook := number(current["originalPrice"])
	var savings any
	if pok && ook {
		savings = original - price
	}
	if threshold, ok := number(config["minSavings"]); ok {
		condition("min_savings", pok && ook && original-price >= threshold, savings, config["minSavings"])
	}
	if threshold, ok := number(config["maxPrice"]); ok {
		condition("max_price", pok && price <= threshold, current["price"], config["maxPrice"])
	}
	rate, rok := number(current["positiveRate"])
	count, cok := number(current["reviewCount"])
	missing := !rok || !cok || count == 0
	condition("review_data_missing", !missing || config["missingReviews"] == "include", missing, config["missingReviews"])
	if threshold, ok := number(config["minPositiveRate"]); ok {
		var value any
		if rok {
			value = rate / 100
		}
		condition("min_positive_rate", rok && rate/100 >= threshold, value, config["minPositiveRate"])
	}
	if threshold, ok := number(config["minReviews"]); ok {
		condition("min_reviews", cok && count >= threshold, current["reviewCount"], config["minReviews"])
	}
	if config["lowMode"] != "any" {
		lowCondition(result, text(config["lowMode"]))
	}
	if result["matched"] == true {
		result["labels"] = []any{"featured"}
	}
	result["config"] = config
	member := copyMap(membership)
	sorted := []string{}
	for source := range sources {
		sorted = append(sorted, source)
	}
	sort.Strings(sorted)
	member["sources"] = sorted
	result["membership"] = member
	return result, nil
}
