package clashcfg

// 运行配置生成：机场整份配置 → 不消费地理数据的纯节点配置；节点连接依赖
// （dialer-proxy / 组）按闭包解析，缺失、歧义、循环一律拒绝生成。

import (
	"fmt"
	"strings"
)

// 内核自己解析的内置逻辑节点：dialer-proxy 指向它们是合法用法（直连/拒绝），
// 不经过订阅节点表，不做映射也不判「目标不存在」。
var builtinProxyNames = map[string]bool{
	"DIRECT": true, "REJECT": true, "REJECT-DROP": true, "GLOBAL": true,
	"COMPATIBLE": true, "PASS": true, "PASS-RULE": true,
}

var deterministicDowngradeTypes = map[string]bool{"url-test": true, "fallback": true}
var executionGroupTypes = map[string]bool{"select": true, "relay": true}

var groupAutonomyKeys = []string{
	"url", "interval", "lazy", "timeout", "max-failed-times", "expected-status",
	"tolerance", "strategy", "filter", "exclude-filter", "exclude-type", "use",
	"include-all", "include-all-proxies", "include-all-providers",
}

var groupMemberFilterKeys = []string{"filter", "exclude-filter", "exclude-type"}

var includeAllFlags = []string{"include-all", "include-all-proxies", "include-all-providers"}

// Bundle render_node_bundle 的产物：节点与组（均按依赖闭包重排）。
type Bundle struct {
	Proxies []map[string]any
	Groups  []map[string]any
}

// BundleOptions render_node_bundle 可选参数；零值等价 Python 默认。
// RootsSet/SourceNamesSet 区分「未提供」与「显式空」（Python None vs ()）。
type BundleOptions struct {
	Groups          []map[string]any
	Namespace       string
	Roots           []string
	RootsSet        bool
	ProviderNames   []string
	ProviderConfigs map[string]map[string]any
	SourceNames     []string
	SourceNamesSet  bool
}

// strOr 对齐 str(v or "")：None/空串归空，非字符串按展示文本。
func strOr(v any, _ string) string {
	return pyStr(v)
}

// RenderNodeBundle 按指定节点取依赖闭包；缺失、歧义和循环依赖均拒绝生成。
func RenderNodeBundle(payloads []map[string]any, names map[string]string, opts BundleOptions) (Bundle, error) {
	originals := map[string]string{}
	for original, runtime := range names {
		originals[runtime] = original
	}
	aliases := map[string][]int{}
	runtimeNames := make([]string, len(payloads))
	for index, payload := range payloads {
		payloadName := strOr(payload["name"], "")
		rawName := payloadName
		if opts.SourceNamesSet {
			rawName = ""
			if index < len(opts.SourceNames) {
				rawName = opts.SourceNames[index]
			}
		}
		rn := payloadName
		if mapped, ok := names[rawName]; ok {
			rn = mapped
		}
		runtimeNames[index] = rn
		aliases[rawName] = append(aliases[rawName], index)
	}

	groupIndex := map[string][]int{}
	for index, group := range opts.Groups {
		if name := strOr(group["name"], ""); name != "" {
			groupIndex[name] = append(groupIndex[name], index)
		}
	}
	groupNames := map[string]string{}
	for name := range groupIndex {
		if opts.Namespace != "" {
			groupNames[name] = "HlRelay|" + opts.Namespace + "|" + name
		} else {
			groupNames[name] = name
		}
	}
	var visiting []string
	visited := map[string]bool{}
	renderedNodes := map[int]map[string]any{}
	renderedGroups := map[int]map[string]any{}

	providers := map[string]map[string]any{}
	for name, cfg := range opts.ProviderConfigs {
		providers[name] = cfg
	}
	for _, name := range opts.ProviderNames {
		if _, ok := providers[name]; !ok {
			providers[name] = map[string]any{}
		}
	}
	providerOrder := providerKeys(providers)

	var visit func(token string) (string, error)
	var visitEntry func(isGroup bool, index int) (string, error)

	visitProvider := func(name string) error {
		cfg, ok := providers[name]
		if !ok {
			return fmt.Errorf("连接依赖的节点来源不存在：%s", name)
		}
		key := "provider|" + name
		for _, v := range visiting {
			if v == key {
				return fmt.Errorf("连接依赖存在循环：%s", name)
			}
		}
		if visited[key] {
			return nil
		}
		visiting = append(visiting, key)
		override, _ := cfg["override"].(map[string]any)
		targets := []any{cfg["proxy"], override["dialer-proxy"]}
		for _, target := range targets {
			if s, ok := target.(string); ok && s != "" {
				if _, err := visit(s); err != nil {
					return err
				}
			}
		}
		visiting = visiting[:len(visiting)-1]
		visited[key] = true
		return nil
	}

	visit = func(token string) (string, error) {
		nodeIDs := aliases[token]
		groupIDs := groupIndex[token]
		if len(nodeIDs)+len(groupIDs) > 1 {
			return "", fmt.Errorf("连接依赖目标重名：%s", token)
		}
		if len(nodeIDs) == 0 && len(groupIDs) == 0 {
			if builtinProxyNames[token] {
				return token, nil
			}
			return "", fmt.Errorf("连接依赖目标不存在：%s", token)
		}
		return visitEntry(len(groupIDs) > 0, firstOf(groupIDs, nodeIDs))
	}

	visitEntry = func(isGroup bool, index int) (string, error) {
		var token string
		if isGroup {
			token = strOr(opts.Groups[index]["name"], "")
		} else if opts.SourceNamesSet && index < len(opts.SourceNames) {
			token = opts.SourceNames[index]
		} else {
			token = strOr(payloads[index]["name"], "")
		}
		kind := "node"
		if isGroup {
			kind = "group"
		}
		key := kind + "|" + fmt.Sprint(index)
		runtimeName := ""
		if !isGroup {
			runtimeName = runtimeNames[index]
		} else {
			runtimeName = groupNames[token]
		}
		for _, v := range visiting {
			if v == key {
				return "", fmt.Errorf("连接依赖存在循环：%s", token)
			}
		}
		if visited[key] {
			return runtimeName, nil
		}
		visiting = append(visiting, key)
		var item map[string]any
		if isGroup {
			item = deepCopyMap(opts.Groups[index])
		} else {
			item = deepCopyMap(payloads[index])
		}
		item["name"] = runtimeName
		if dp, ok := item["dialer-proxy"].(string); ok && dp != "" {
			resolved, err := visit(dp)
			if err != nil {
				return "", err
			}
			item["dialer-proxy"] = resolved
		}
		if isGroup {
			members := toStringSlice(item["proxies"])
			groupType := strings.ToLower(strOr(item["type"], "select"))
			if deterministicDowngradeTypes[groupType] {
				groupType = "select"
			} else if !executionGroupTypes[groupType] {
				return "", fmt.Errorf("连接依赖组 %s 的类型 %s 无法确定性执行", token, groupType)
			}
			hasInclude := false
			for _, flag := range includeAllFlags {
				if truthy(item[flag]) {
					hasInclude = true
				}
			}
			if truthy(item["use"]) {
				hasInclude = true
			}
			hasFilter := false
			for _, key := range groupMemberFilterKeys {
				if truthy(item[key]) {
					hasFilter = true
				}
			}
			if hasInclude && hasFilter {
				return "", fmt.Errorf("连接依赖组 %s 使用节点过滤，无法确定性展开", token)
			}
			if truthy(item["include-all"]) || truthy(item["include-all-proxies"]) {
				for _, p := range payloads {
					members = append(members, strOr(p["name"], ""))
				}
			}
			uses := toStringSlice(item["use"])
			if truthy(item["include-all"]) || truthy(item["include-all-providers"]) {
				uses = append(uses, providerOrder...)
			}
			for _, source := range uses {
				if err := visitProvider(source); err != nil {
					return "", err
				}
			}
			if len(members) == 0 && len(uses) == 0 {
				return "", fmt.Errorf("连接依赖组没有可用成员：%s", token)
			}
			if len(members) > 0 {
				resolved := make([]string, 0, len(members))
				seen := map[string]bool{}
				for _, member := range members {
					rn, err := visit(member)
					if err != nil {
						return "", err
					}
					if !seen[rn] {
						seen[rn] = true
						resolved = append(resolved, rn)
					}
				}
				item["proxies"] = toAnySlice(resolved)
			}
			if len(uses) > 0 {
				item["use"] = toAnySlice(dedupStrings(uses))
			}
			for _, flag := range includeAllFlags {
				delete(item, flag)
			}
			item["type"] = groupType
			for _, autonomyKey := range []string{
				"url", "interval", "lazy", "timeout", "max-failed-times",
				"expected-status", "tolerance", "strategy",
				"filter", "exclude-filter", "exclude-type",
			} {
				delete(item, autonomyKey)
			}
			if _, ok := item["empty-fallback"]; !ok {
				item["empty-fallback"] = "REJECT"
			}
			renderedGroups[index] = item
		} else {
			renderedNodes[index] = item
		}
		visiting = visiting[:len(visiting)-1]
		visited[key] = true
		return runtimeName, nil
	}

	for _, name := range providerOrder {
		if err := visitProvider(name); err != nil {
			return Bundle{}, err
		}
	}
	if !opts.RootsSet {
		for index := range payloads {
			if _, err := visitEntry(false, index); err != nil {
				return Bundle{}, err
			}
		}
	} else {
		for _, root := range opts.Roots {
			if _, err := visit(root); err != nil {
				return Bundle{}, err
			}
		}
	}
	proxies := make([]map[string]any, 0, len(payloads))
	for i := range payloads {
		if node, ok := renderedNodes[i]; ok {
			proxies = append(proxies, node)
		}
	}
	groups := make([]map[string]any, 0, len(opts.Groups))
	for i := range opts.Groups {
		if group, ok := renderedGroups[i]; ok {
			groups = append(groups, group)
		}
	}
	outputNames := []string{}
	for _, item := range proxies {
		outputNames = append(outputNames, strOr(item["name"], ""))
	}
	for _, item := range groups {
		outputNames = append(outputNames, strOr(item["name"], ""))
	}
	seen := map[string]bool{}
	for _, name := range outputNames {
		if seen[name] {
			return Bundle{}, fmt.Errorf("连接依赖的运行名称重复")
		}
		seen[name] = true
	}
	return Bundle{Proxies: proxies, Groups: groups}, nil
}

// ResolveNodeDependencies 按同一依赖闭包解析节点运行名。
func ResolveNodeDependencies(payloads []map[string]any, names map[string]string) ([]map[string]any, error) {
	originals := map[string]string{}
	for original, runtime := range names {
		originals[runtime] = original
	}
	sourceNames := make([]string, len(payloads))
	for i, payload := range payloads {
		name := strOr(payload["name"], "")
		if original, ok := originals[name]; ok {
			sourceNames[i] = original
		} else {
			sourceNames[i] = name
		}
	}
	bundle, err := RenderNodeBundle(payloads, names, BundleOptions{
		SourceNames: sourceNames, SourceNamesSet: true,
	})
	if err != nil {
		return nil, err
	}
	return bundle.Proxies, nil
}

var dnsBooleanKeys = []string{"enable", "ipv6", "prefer-h3", "use-hosts", "use-system-hosts"}
var dnsResolverKeys = []string{"nameserver", "default-nameserver", "proxy-server-nameserver", "direct-nameserver"}

// RenderDNS 仅保留基础解析白名单，策略与未知字段一律不进入运行配置。
func RenderDNS(raw any) *Doc {
	out := NewDoc()
	if m, ok := raw.(map[string]any); ok {
		for _, key := range dnsBooleanKeys {
			if b, ok := m[key].(bool); ok {
				out.Set(key, b)
			}
		}
		for _, key := range dnsResolverKeys {
			values, ok := m[key].([]any)
			if !ok {
				continue
			}
			var resolvers []any
			for _, value := range values {
				if s, ok := value.(string); ok {
					resolvers = append(resolvers, s)
				}
			}
			if len(resolvers) > 0 {
				out.Set(key, resolvers)
			}
		}
	}
	out.Set("enhanced-mode", "redir-host")
	return out
}

// RenderSubscriptionConfig 机场整份配置 → 项目运行配置（YAML 文本）。
// 保留：节点、中转组（自动组降为 select，成员顺序即默认选择）、基础解析。
// 剥离：规则/规则集/子规则、tun 接管、控制器凭据、地区 DNS 过滤。
func RenderSubscriptionConfig(text string) (string, error) {
	doc, err := LoadYAML(text)
	if err != nil {
		return "", fmt.Errorf("订阅配置不是合法 YAML：%w", err)
	}
	rawProxies, _ := doc["proxies"].([]any)
	var proxies []map[string]any
	for _, p := range rawProxies {
		if m, ok := p.(map[string]any); ok {
			proxies = append(proxies, m)
		}
	}
	if truthy(doc["proxy-providers"]) {
		return "", fmt.Errorf("订阅配置含未展开的节点来源，须先由程序获取并展开为具体节点")
	}
	if len(proxies) == 0 {
		return "", fmt.Errorf("订阅配置里没有节点")
	}
	rawGroups, _ := doc["proxy-groups"].([]any)
	var groups []map[string]any
	for _, g := range rawGroups {
		if m, ok := g.(map[string]any); ok {
			groups = append(groups, m)
		}
	}
	bundle, err := RenderNodeBundle(proxies, nil, BundleOptions{Groups: groups})
	if err != nil {
		return "", err
	}
	out := NewDoc()
	out.Set("mode", "global")
	out.Set("allow-lan", false)
	out.Set("bind-address", "127.0.0.1")
	out.Set("dns", RenderDNS(doc["dns"]))
	out.Set("proxies", toAnySliceMaps(bundle.Proxies))
	if len(bundle.Groups) > 0 {
		out.Set("proxy-groups", toAnySliceMaps(bundle.Groups))
	}
	if err := ValidateExecutionConfigDoc(docToMap(out)); err != nil {
		return "", err
	}
	return DumpYAML(out), nil
}
