package notifications

func CurrencyForRegion(region string) string { return regionToCurrency[region] }
func KnownCurrency(currency string) bool {
	for _, v := range regionToCurrency {
		if v == currency {
			return true
		}
	}
	return false
}
func CategoryOf(eventType string) string {
	for _, v := range typeLabels {
		if v.key == eventType {
			return v.category
		}
	}
	return ""
}

func TypeEnabled(prefs map[string]any, eventType string) bool {
	category := CategoryOf(eventType)
	if !truthy(prefs["enabled"]) || category == "" {
		return false
	}
	categories, _ := prefs["categories"].(map[string]any)
	if v, ok := categories[category]; ok && !truthy(v) {
		return false
	}
	types, _ := prefs["types"].(map[string]any)
	config, _ := types[eventType].(map[string]any)
	if v, ok := config["enabled"]; ok && !truthy(v) {
		return false
	}
	return config["frequency"] != "off"
}
