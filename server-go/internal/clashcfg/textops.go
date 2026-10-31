package clashcfg

// 内核配置文本操作：混合口钉定、tun 剥除、控制器端点解析、探测 lane 注入、
// 订阅内容比较视图。全部以 YAML 文本为出入，不可解析时保守返回原文。

import (
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"net"
	"regexp"
	"strings"
)

// 探测 lane 常量：与池内核 lane 同形态（select 组 + mixed listener），组用
// include-all 随内核当前节点集自动变化，配置比较时剥掉即可。
const (
	ProbeLaneCount     = 20
	ProbeLanePortBase  = 20000
	ProbeLanePortSpan  = 1000
	ControllerPort     = 19090
	ConfigTestTimeoutS = 15.0

	LaneGroupPrefix       = "lane-"
	MaxCrawlWorkers       = 60
	MaxLanes              = MaxCrawlWorkers * 2
	PoolProbeLanePortBase = 21100
	PoolProbeLanePortSpan = 900

	RuntimeMode           = "global"
	RuntimeBindAddress    = "127.0.0.1"
	RuntimeControllerHost = "127.0.0.1"
	RuntimeConfigFilename = "crawl-runtime.yaml"
)

// FreeLocalPort 让系统分配一个空闲的本机 TCP 端口（不做端口租约）。
func FreeLocalPort() (int, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, fmt.Errorf("本机空闲端口分配失败：%w", err)
	}
	defer listener.Close()
	addr, ok := listener.Addr().(*net.TCPAddr)
	if !ok {
		return 0, fmt.Errorf("本机空闲端口分配失败：地址形态异常")
	}
	return addr.Port, nil
}

// PortFreeBind 端口空闲探测（bind 判定）。
func PortFreeBind(port int) bool {
	listener, err := net.Listen("tcp", fmt.Sprintf("127.0.0.1:%d", port))
	if err != nil {
		return false
	}
	_ = listener.Close()
	return true
}

// NewControllerSecret 随机控制密钥（token_urlsafe(32) 同形）。
func NewControllerSecret() string {
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		panic("随机密钥生成失败：" + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(buf)
}

var mixedPortRe = regexp.MustCompile(`(?m)^\s*mixed-port\s*:\s*(\d+)`)
var plainPortRe = regexp.MustCompile(`(?m)^\s*port\s*:\s*(\d+)`)
var controllerAddrRe = regexp.MustCompile(`(?m)^external-controller\s*:\s*['"]?([^'"\n#]+)`)
var controllerPortTailRe = regexp.MustCompile(`:(\d+)\s*$`)
var secretLineRe = regexp.MustCompile(`(?m)^secret\s*:.*$`)
var hasSecretRe = regexp.MustCompile(`(?m)^secret\s*:`)

// ParseMixedPort 读配置里的混合监听口（无则 0）。
func ParseMixedPort(configText string) int {
	if m := mixedPortRe.FindStringSubmatch(configText); m != nil {
		return atoiSafe(m[1])
	}
	if m := plainPortRe.FindStringSubmatch(configText); m != nil {
		return atoiSafe(m[1])
	}
	return 0
}

func atoiSafe(s string) int {
	n := 0
	for _, c := range s {
		if c < '0' || c > '9' {
			return 0
		}
		n = n*10 + int(c-'0')
	}
	return n
}

// PinMixedPort 钉定受管混合监听口并收在回环上；已是目标形态则原文返回。
func PinMixedPort(configText string, port int) string {
	doc, err := LoadYAML(configText)
	if err != nil {
		return configText
	}
	if current, ok := toInt(doc["mixed-port"]); ok && current == port {
		if allowLan, isBool := doc["allow-lan"].(bool); isBool && !allowLan {
			if bind, _ := doc["bind-address"].(string); bind == "127.0.0.1" {
				return configText
			}
		}
	}
	merged := map[string]any{}
	for key, value := range doc {
		merged[key] = value
	}
	merged["mixed-port"] = port
	merged["allow-lan"] = false
	merged["bind-address"] = "127.0.0.1"
	return DumpYAML(docFromMap(merged))
}

// StripTunSection 剥掉订阅自带的虚拟网卡段（tun），幂等；不可解析原文返回。
func StripTunSection(configText string) string {
	doc, err := LoadYAML(configText)
	if err != nil {
		return configText
	}
	if _, has := doc["tun"]; !has {
		return configText
	}
	merged := map[string]any{}
	for key, value := range doc {
		if key == "tun" {
			continue
		}
		merged[key] = value
	}
	return DumpYAML(docFromMap(merged))
}

// WithControllerSecret 写入控制密钥：已有 secret 行原位替换，否则文末追加。
func WithControllerSecret(configText string, secret string) string {
	line := "secret: " + secret
	if hasSecretRe.MatchString(configText) {
		return secretLineRe.ReplaceAllStringFunc(configText, func(string) string { return line })
	}
	return strings.TrimRight(configText, "\n") + "\n" + line + "\n"
}

// ResolveController 分配自有回环控制端点和随机密钥；端口占用时从基址避让。
func ResolveController(configText string) (string, string) {
	secret := NewControllerSecret()
	var port int
	if m := controllerAddrRe.FindStringSubmatch(configText); m != nil {
		addr := strings.TrimSpace(m[1])
		if pm := controllerPortTailRe.FindStringSubmatch(addr); pm != nil {
			candidate := atoiSafe(pm[1])
			if candidate > 0 && PortFreeBind(candidate) {
				port = candidate
			}
		}
	}
	if port == 0 {
		port = ControllerPort
		for !PortFreeBind(port) {
			port++
		}
	}
	return fmt.Sprintf("http://127.0.0.1:%d", port), secret
}

// ProbeLane 探测 lane（组名, listener 端口）。
type ProbeLane struct {
	Group string
	Port  int
}

// ProbeLaneOptions InjectProbeLanes 的可调参数；零值 = 旧链路默认段。
type ProbeLaneOptions struct {
	PortBase  int
	PortSpan  int
	PinPorts  []int
	PortFree  func(int) bool
	LaneCount int
}

// InjectProbeLanes 把探测 lane 注入内核配置（幂等）：剔除本前缀旧条目后追加
// LaneCount 组 select(include-all) + 同数 mixed listener。端口避让本配置自己的
// 混合口与控制器口；PinPorts 钉住沿用（热重载通道，绕过空闲检查）。不可解析的
// 配置原样返回；一个端口都分不到时不动配置。
func InjectProbeLanes(configText string, opts ProbeLaneOptions) string {
	portBase, portSpan := opts.PortBase, opts.PortSpan
	if portBase == 0 {
		portBase = ProbeLanePortBase
	}
	if portSpan == 0 {
		portSpan = ProbeLanePortSpan
	}
	count := opts.LaneCount
	if count == 0 {
		count = ProbeLaneCount
	}
	portFree := opts.PortFree
	if portFree == nil {
		portFree = PortFreeBind
	}
	doc, err := LoadYAML(configText)
	if err != nil {
		return configText
	}
	avoid := map[int]bool{}
	if mixed, ok := toInt(doc["mixed-port"]); ok {
		avoid[mixed] = true
	}
	controller := pyStr(doc["external-controller"])
	if m := controllerPortTailRe.FindStringSubmatch(controller); m != nil {
		avoid[atoiSafe(m[1])] = true
	}
	var pinned []int
	for _, p := range opts.PinPorts {
		if p > 0 && !avoid[p] {
			pinned = append(pinned, p)
			if len(pinned) >= count {
				break
			}
		}
	}
	ports := append([]int{}, pinned...)
	avoidAll := map[int]bool{}
	for p := range avoid {
		avoidAll[p] = true
	}
	for _, p := range pinned {
		avoidAll[p] = true
	}
	for candidate := portBase; len(ports) < count && candidate < portBase+portSpan; candidate++ {
		if !avoidAll[candidate] && portFree(candidate) {
			ports = append(ports, candidate)
		}
	}
	if len(ports) == 0 {
		return configText
	}
	rawGroups, _ := doc["proxy-groups"].([]any)
	groups := make([]any, 0, len(rawGroups)+count)
	for _, g := range rawGroups {
		gm, ok := g.(map[string]any)
		if ok && strings.HasPrefix(pyStr(gm["name"]), ProbeLaneGroupPrefix) {
			continue
		}
		groups = append(groups, g)
	}
	rawListeners, _ := doc["listeners"].([]any)
	listeners := make([]any, 0, len(rawListeners)+count)
	for _, entry := range rawListeners {
		em, ok := entry.(map[string]any)
		if ok && strings.HasPrefix(pyStr(em["name"]), ProbeLaneGroupPrefix) {
			continue
		}
		listeners = append(listeners, entry)
	}
	for i, port := range ports {
		groupName := fmt.Sprintf("%s-%d", ProbeLaneGroupPrefix, i+1)
		groups = append(groups, map[string]any{
			"name": groupName, "type": "select", "include-all": true,
		})
		listeners = append(listeners, map[string]any{
			"name": groupName + "-in", "type": "mixed", "port": port,
			"listen": "127.0.0.1", "proxy": groupName,
		})
	}
	merged := map[string]any{}
	for key, value := range doc {
		merged[key] = value
	}
	merged["proxy-groups"] = groups
	merged["listeners"] = listeners
	return DumpYAML(docFromMap(merged))
}

// ParseProbeLanes 从内核配置读出探测 lane（组名, listener 端口），按组名升序。
func ParseProbeLanes(configText string) ([]ProbeLane, error) {
	doc, err := LoadYAML(configText)
	if err != nil {
		return nil, nil
	}
	listeners, ok := doc["listeners"].([]any)
	if !ok {
		return nil, nil
	}
	var lanes []ProbeLane
	for _, entry := range listeners {
		em, ok := entry.(map[string]any)
		if !ok {
			continue
		}
		name := pyStr(em["name"])
		proxy := pyStr(em["proxy"])
		port, isInt := toInt(em["port"])
		if strings.HasPrefix(name, ProbeLaneGroupPrefix) &&
			strings.HasPrefix(proxy, ProbeLaneGroupPrefix) && isInt {
			lanes = append(lanes, ProbeLane{Group: proxy, Port: port})
		}
	}
	sortProbes(lanes)
	return lanes, nil
}

func sortProbes(lanes []ProbeLane) {
	for i := 1; i < len(lanes); i++ {
		for j := i; j > 0 && lanes[j].Group < lanes[j-1].Group; j-- {
			lanes[j], lanes[j-1] = lanes[j-1], lanes[j]
		}
	}
}

// ParseNodeNames 从配置的 proxies 段解析节点名列表（轻量扫描，不做完整解析）。
// 只认块式 `- name: xx` 与行内 `- { name: xx, ... }` 首键形态。
func ParseNodeNames(configText string) []string {
	var names []string
	inProxies := false
	for _, line := range strings.Split(configText, "\n") {
		stripped := strings.TrimSpace(line)
		if matched, _ := regexp.MatchString(`^proxies\s*:`, line); matched {
			inProxies = true
			continue
		}
		if inProxies {
			if matched, _ := regexp.MatchString(`^[A-Za-z-]+\s*:`, line); matched {
				break
			}
			m := nameItemRe.FindStringSubmatch(stripped)
			if m != nil {
				name := m[1]
				if name == "" {
					name = m[2]
				}
				if name == "" {
					name = strings.TrimSpace(m[3])
				}
				if name != "" {
					names = append(names, name)
				}
			}
		}
	}
	return names
}

var nameItemRe = regexp.MustCompile(`-\s*(?:\{\s*)?name\s*:\s*(?:'([^']*)'|"([^"]*)"|([^,}]+))`)

// SubscriptionView 配置的订阅内容视图：剥掉探测 lane、混合口与控制密钥后的
// yaml 结构。mixed-port 一并剥掉：监听口归程序管，不属于订阅内容——端口漂移
// 不触发重启的语义由这里兑现。不可解析返回 nil。
func SubscriptionView(configText string) map[string]any {
	doc, err := LoadYAML(configText)
	if err != nil {
		return nil
	}
	view := map[string]any{}
	for key, value := range doc {
		view[key] = value
	}
	delete(view, "mixed-port")
	delete(view, "secret")
	for _, key := range []string{"proxy-groups", "listeners"} {
		entries, ok := view[key].([]any)
		if !ok {
			continue
		}
		kept := make([]any, 0, len(entries))
		for _, entry := range entries {
			em, isMap := entry.(map[string]any)
			if isMap && strings.HasPrefix(pyStr(em["name"]), ProbeLaneGroupPrefix) {
				continue
			}
			kept = append(kept, entry)
		}
		if len(kept) > 0 {
			view[key] = kept
		} else {
			delete(view, key)
		}
	}
	return view
}

// ConfigUnchanged 内核启动文本与磁盘配置是否等价（比较对象是订阅内容视图）。
// 语义是「订阅内容真的变了才重启」：格式变化与端口漂移都不触发重启。
func ConfigUnchanged(startupText, diskText string) bool {
	if startupText == diskText {
		return true
	}
	a := SubscriptionView(startupText)
	b := SubscriptionView(diskText)
	if a == nil || b == nil {
		return false
	}
	return deepEqualAny(a, b)
}

func deepEqualAny(a, b any) bool {
	switch av := a.(type) {
	case nil:
		return b == nil
	case bool:
		bv, ok := b.(bool)
		return ok && av == bv
	case string:
		bv, ok := b.(string)
		return ok && av == bv
	case int:
		return numEqual(float64(av), b)
	case int64:
		return numEqual(float64(av), b)
	case float64:
		return numEqual(av, b)
	case []any:
		bv, ok := b.([]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for i := range av {
			if !deepEqualAny(av[i], bv[i]) {
				return false
			}
		}
		return true
	case map[string]any:
		bv, ok := b.(map[string]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for key, value := range av {
			other, exists := bv[key]
			if !exists || !deepEqualAny(value, other) {
				return false
			}
		}
		return true
	default:
		return false
	}
}

func numEqual(a float64, b any) bool {
	switch bv := b.(type) {
	case int:
		return a == float64(bv)
	case int64:
		return a == float64(bv)
	case float64:
		return a == bv
	default:
		return false
	}
}

func toInt(v any) (int, bool) {
	switch n := v.(type) {
	case int:
		return n, true
	case int64:
		return int(n), true
	case float64:
		return int(n), true
	default:
		return 0, false
	}
}
