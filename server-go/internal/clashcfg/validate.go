package clashcfg

// 最终运行配置统一准入：禁地理数据消费、禁内核自取外部资源、禁内核自主选路组。

import (
	"fmt"
	"regexp"
	"sort"
	"strings"
)

var geoConfigKeys = map[string]bool{
	"geo-auto-update": true, "geo-update-interval": true, "geodata-mode": true,
	"geodata-loader": true, "geosite-matcher": true, "geox-url": true,
}

var geoRuleTypes = map[string]bool{
	"GEOIP": true, "GEOSITE": true, "SRC-GEOIP": true,
	"IP-ASN": true, "SRC-IP-ASN": true, "RULE-SET": true,
}

var logicRuleTypes = map[string]bool{"AND": true, "OR": true, "NOT": true}

var geoSelectorPrefixes = []string{"geosite:", "geoip:", "ip-asn:", "rule-set:"}

// ProbeLaneGroupPrefix 注入的探测 lane 组前缀：唯一允许使用 include-all 的执行组。
const ProbeLaneGroupPrefix = "HlProbeLane"

var logicGeoRe = regexp.MustCompile(`\(([A-Za-z][A-Za-z0-9-]*),`)

func geoSelector(v any) bool {
	s, ok := v.(string)
	if !ok {
		return false
	}
	s = strings.ToLower(strings.TrimSpace(s))
	for _, prefix := range geoSelectorPrefixes {
		if strings.HasPrefix(s, prefix) {
			return true
		}
	}
	return false
}

func containsGeoSelector(v any) bool {
	if geoSelector(v) {
		return true
	}
	if list, ok := v.([]any); ok {
		for _, item := range list {
			if containsGeoSelector(item) {
				return true
			}
		}
	}
	return false
}

func containsGeoRule(v any) bool {
	switch val := v.(type) {
	case string:
		head := strings.ToUpper(strings.TrimSpace(val))
		if idx := strings.Index(head, ","); idx >= 0 {
			head = strings.TrimSpace(head[:idx])
		}
		if geoRuleTypes[head] {
			return true
		}
		for _, m := range logicGeoRe.FindAllStringSubmatch(val, -1) {
			if geoRuleTypes[strings.ToUpper(m[1])] {
				return true
			}
		}
		return false
	case map[string]any:
		for key, item := range val {
			ruleType := strings.ToUpper(strings.TrimSpace(key))
			if geoRuleTypes[ruleType] {
				return true
			}
			if logicRuleTypes[ruleType] || strings.EqualFold(key, "type") ||
				strings.EqualFold(key, "rule") || strings.EqualFold(key, "rules") {
				if containsGeoRule(item) {
					return true
				}
			} else if isMapOrList(item) && containsGeoRule(item) {
				return true
			}
		}
		return false
	case []any:
		for _, item := range val {
			if containsGeoRule(item) {
				return true
			}
		}
		return false
	default:
		return false
	}
}

func isMapOrList(v any) bool {
	switch val := v.(type) {
	case map[string]any:
		return true
	case []any:
		return len(val) >= 0
	default:
		return false
	}
}

func geoDatabasePath(v any) bool {
	s, ok := v.(string)
	if !ok {
		return false
	}
	name := strings.ToLower(basenamePosix(strings.ReplaceAll(s, "\\", "/")))
	if name == "geoip.db" || name == "geoip.metadb" {
		return true
	}
	if strings.HasSuffix(name, ".mmdb") {
		return true
	}
	stem := strings.TrimSuffix(name, ".dat")
	return strings.HasSuffix(name, ".dat") && (stem == "geoip" || stem == "geosite" ||
		stem == "asn" || stem == "country" ||
		strings.HasPrefix(stem, "geoip-") || strings.HasPrefix(stem, "geosite-") ||
		strings.HasPrefix(stem, "asn-") || strings.HasPrefix(stem, "country-"))
}

func basenamePosix(p string) string {
	if idx := strings.LastIndex(p, "/"); idx >= 0 {
		return p[idx+1:]
	}
	return p
}

func geoDatabaseReference(v any) bool {
	s, ok := v.(string)
	if !ok {
		return false
	}
	path := s
	if idx := strings.Index(s, "://"); idx >= 0 {
		rest := s[idx+3:]
		if slash := strings.Index(rest, "/"); slash >= 0 {
			path = rest[slash:]
		} else {
			path = ""
		}
	}
	return geoDatabasePath(path)
}

func parseConfigDoc(config any) (map[string]any, error) {
	switch v := config.(type) {
	case map[string]any:
		return v, nil
	case *Doc:
		return docToMap(v), nil
	case string:
		doc, err := LoadYAML(v)
		if err != nil {
			return nil, fmt.Errorf("运行配置不是合法 YAML：%w", err)
		}
		return doc, nil
	default:
		return nil, fmt.Errorf("运行配置不是 YAML 映射")
	}
}

func validateGeoFreeConfig(parsed map[string]any) error {
	var configured []string
	for key := range parsed {
		lower := strings.ToLower(strings.TrimSpace(key))
		if geoConfigKeys[lower] {
			configured = append(configured, lower)
		}
	}
	if len(configured) > 0 {
		sort.Strings(configured)
		return fmt.Errorf("运行配置包含地理数据加载设置：%s", configured[0])
	}
	if truthy(parsed["rule-providers"]) {
		return fmt.Errorf("运行配置包含可能读取地理数据的规则集")
	}
	if truthy(parsed["sub-rules"]) {
		return fmt.Errorf("运行配置包含可能读取地理数据的子规则")
	}
	if containsGeoRule(parsed["rules"]) {
		return fmt.Errorf("运行配置包含依赖地理数据的规则")
	}
	if providers, ok := parsed["proxy-providers"].(map[string]any); ok {
		for _, providerVal := range providers {
			provider, ok := providerVal.(map[string]any)
			if !ok {
				continue
			}
			if geoDatabaseReference(provider["path"]) || geoDatabaseReference(provider["url"]) {
				return fmt.Errorf("节点来源指向地理数据库文件")
			}
		}
	}
	if err := validateSniffer(parsed); err != nil {
		return err
	}
	dns, ok := parsed["dns"].(map[string]any)
	if !ok {
		return nil
	}
	for _, key := range []string{"nameserver", "default-nameserver", "proxy-server-nameserver", "direct-nameserver", "fallback"} {
		if geoSelector(dns[key]) {
			return fmt.Errorf("DNS %s 指向地理数据规则", key)
		}
		if list, ok := dns[key].([]any); ok {
			for _, value := range list {
				if geoSelector(value) {
					return fmt.Errorf("DNS %s 指向地理数据规则", key)
				}
			}
		}
	}
	for _, key := range []string{"nameserver-policy", "proxy-server-nameserver-policy"} {
		policy, ok := dns[key].(map[string]any)
		if !ok {
			continue
		}
		for selector, resolvers := range policy {
			if geoSelector(selector) || containsGeoSelector(resolvers) {
				return fmt.Errorf("DNS %s 使用地理数据规则", key)
			}
		}
	}
	fallback, isList := dns["fallback"].([]any)
	fallbackFilter, filterIsMap := dns["fallback-filter"].(map[string]any)
	if isList && len(fallback) > 0 {
		if !filterIsMap {
			return fmt.Errorf("DNS fallback 默认启用地理数据库筛选")
		}
		if b, isBool := fallbackFilter["geoip"].(bool); !isBool || b {
			return fmt.Errorf("DNS fallback 默认启用地理数据库筛选")
		}
	}
	if filterIsMap {
		if b, ok := fallbackFilter["geoip"].(bool); ok && b {
			return fmt.Errorf("DNS fallback-filter 使用地理数据")
		}
		if truthy(fallbackFilter["geosite"]) {
			return fmt.Errorf("DNS fallback-filter 使用地理数据")
		}
		if _, exists := fallbackFilter["geoip-code"]; exists {
			return fmt.Errorf("DNS fallback-filter 指定地理 IP 区域")
		}
	}
	if containsGeoRule(dns["fake-ip-filter"]) || containsGeoSelector(dns["fake-ip-filter"]) {
		return fmt.Errorf("DNS fake-ip-filter 使用地理数据规则")
	}
	return nil
}

func validateSniffer(parsed map[string]any) error {
	sniffer, ok := parsed["sniffer"].(map[string]any)
	if !ok {
		return nil
	}
	for _, field := range []string{"force-domain", "skip-domain", "skip-src-address", "skip-dst-address"} {
		if containsGeoSelector(sniffer[field]) {
			return fmt.Errorf("sniffer.%s 使用地理数据规则", field)
		}
	}
	return nil
}

func validateExternalResourceConfig(parsed map[string]any) error {
	if truthy(parsed["proxy-providers"]) {
		return fmt.Errorf("运行配置包含未展开的节点来源，节点必须由程序获取后写成具体节点")
	}
	for _, key := range []string{"external-ui", "external-ui-url", "external-ui-name"} {
		if truthy(parsed[key]) {
			return fmt.Errorf("运行配置包含界面资源设置：%s", key)
		}
	}
	return nil
}

func validateGroupAutonomy(parsed map[string]any) error {
	groupsRaw, exists := parsed["proxy-groups"]
	if !exists {
		return nil
	}
	groups, ok := groupsRaw.([]any)
	if !ok {
		return fmt.Errorf("运行配置的代理组形态不对")
	}
	for _, groupVal := range groups {
		group, ok := groupVal.(map[string]any)
		if !ok {
			return fmt.Errorf("运行配置的代理组形态不对")
		}
		name := pyStr(group["name"])
		groupType := strings.ToLower(pyStr(group["type"]))
		if !executionGroupTypes[groupType] {
			shown := groupType
			if shown == "" {
				shown = "未知"
			}
			return fmt.Errorf("代理组 %s 的类型 %s 会让内核自行决定出口", name, shown)
		}
		probeLane := groupType == "select" && strings.HasPrefix(name, ProbeLaneGroupPrefix)
		for _, key := range groupAutonomyKeys {
			if !truthy(group[key]) {
				continue
			}
			if probeLane && key == "include-all" {
				if b, ok := group[key].(bool); ok && b {
					continue
				}
			}
			return fmt.Errorf("代理组 %s 含内核自主选路或探活设置：%s", name, key)
		}
	}
	return nil
}

// ValidateExecutionConfigDoc 最终运行配置准入（映射形态）。
func ValidateExecutionConfigDoc(doc map[string]any) error {
	if err := validateGeoFreeConfig(doc); err != nil {
		return err
	}
	if err := validateExternalResourceConfig(doc); err != nil {
		return err
	}
	return validateGroupAutonomy(doc)
}

// ValidateExecutionConfigText 最终运行配置准入（文本形态）。
func ValidateExecutionConfigText(text string) error {
	doc, err := parseConfigDoc(text)
	if err != nil {
		return err
	}
	return ValidateExecutionConfigDoc(doc)
}
