package clashcfg

import (
	"strings"
	"testing"
)

func mustLoad(t *testing.T, text string) map[string]any {
	t.Helper()
	doc, err := LoadYAML(text)
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	return doc
}

func TestYAMLRoundTrip(t *testing.T) {
	text := "mode: global\n" +
		"allow-lan: false\n" +
		"mixed-port: 17890\n" +
		"ratio: 0.25\n" +
		"flag: yes\n" +
		"empty: null\n" +
		"quoted: 'true 1'\n" +
		"chinese: 香港 01\n" +
		"nested:\n" +
		"  path: /ws\n" +
		"  headers:\n" +
		"    Host: h.example.com\n" +
		"list:\n" +
		"- name: a\n" +
		"  port: 1\n" +
		"- name: b\n" +
		"  opts:\n" +
		"  - x\n" +
		"  - y\n"
	doc := mustLoad(t, text)
	out := DumpYAML(doc)
	again := mustLoad(t, out)
	if !deepEqualAny(doc, again) {
		t.Fatalf("往返不一致\n原文：%s\n重排：%s", text, out)
	}
	if doc["mixed-port"] != int64(17890) || doc["flag"] != true || doc["empty"] != nil {
		t.Fatalf("标量解析形态不对：%#v", doc)
	}
}

func TestYAMLFlowAndComments(t *testing.T) {
	text := "a: [1, 2, x]  # 行内注释\nb: {k: v, n: '2'}\nc: 'it''s'\n"
	doc := mustLoad(t, text)
	list, _ := doc["a"].([]any)
	if len(list) != 3 || list[0] != int64(1) || list[2] != "x" {
		t.Fatalf("流式序列解析不对：%#v", doc["a"])
	}
	m, _ := doc["b"].(map[string]any)
	if m["n"] != "2" || m["k"] != "v" {
		t.Fatalf("流式映射解析不对：%#v", doc["b"])
	}
	if doc["c"] != "it's" {
		t.Fatalf("单引号转义不对：%#v", doc["c"])
	}
}

func TestInjectProbeLanesDeterministicPorts(t *testing.T) {
	base := "mixed-port: 7890\nexternal-controller: 127.0.0.1:19090\nproxies:\n- name: n1\ntype: ss\n"
	free := func(int) bool { return true }
	text := InjectProbeLanes(base, ProbeLaneOptions{PortFree: free})
	doc := mustLoad(t, text)
	listeners, _ := doc["listeners"].([]any)
	if len(listeners) != ProbeLaneCount {
		t.Fatalf("listener 数不对：%d", len(listeners))
	}
	for i, entry := range listeners {
		em := entry.(map[string]any)
		port, _ := toInt(em["port"])
		if want := ProbeLanePortBase + i; port != want {
			t.Fatalf("第 %d 个端口 %d ≠ 确定性基址 %d", i, port, want)
		}
		if em["listen"] != "127.0.0.1" || em["type"] != "mixed" {
			t.Fatalf("listener 形态不对：%#v", em)
		}
	}
	// 占用一个基址段端口时确定性避让
	text = InjectProbeLanes(base, ProbeLaneOptions{PortFree: func(p int) bool { return p != 20000 }})
	doc = mustLoad(t, text)
	listeners, _ = doc["listeners"].([]any)
	first := listeners[0].(map[string]any)
	port, _ := toInt(first["port"])
	if port != ProbeLanePortBase+1 {
		t.Fatalf("占用基址端口后首个端口应为 %d，实际 %d", ProbeLanePortBase+1, port)
	}
}

func TestInjectProbeLanesIdempotent(t *testing.T) {
	base := "mixed-port: 7890\nproxies:\n- name: n1\n"
	free := func(int) bool { return true }
	once := InjectProbeLanes(base, ProbeLaneOptions{PortFree: free})
	twice := InjectProbeLanes(once, ProbeLaneOptions{PortFree: free})
	if once != twice {
		t.Fatalf("注入不幂等：\n一次：%s\n两次：%s", once, twice)
	}
	// 钉住端口：全部沿用，不再分配
	pinned := make([]int, ProbeLaneCount)
	for i := range pinned {
		pinned[i] = 30000 + i
	}
	text := InjectProbeLanes(base, ProbeLaneOptions{PinPorts: pinned, PortFree: free})
	doc := mustLoad(t, text)
	listeners, _ := doc["listeners"].([]any)
	for i, entry := range listeners {
		em := entry.(map[string]any)
		port, _ := toInt(em["port"])
		if port != pinned[i] {
			t.Fatalf("钉住端口被改：%d ≠ %d", port, pinned[i])
		}
	}
}

func TestSubscriptionViewStripsProbeLanes(t *testing.T) {
	base := "mode: global\nsecret: s3cret\nmixed-port: 7890\nproxies:\n- name: n1\ntype: ss\n"
	text := InjectProbeLanes(base, ProbeLaneOptions{PortFree: func(int) bool { return true }})
	view := SubscriptionView(text)
	if view == nil {
		t.Fatal("视图解析失败")
	}
	if _, has := view["mixed-port"]; has {
		t.Fatal("视图应剥掉 mixed-port")
	}
	if _, has := view["secret"]; has {
		t.Fatal("视图应剥掉 secret")
	}
	if _, has := view["listeners"]; has {
		t.Fatal("剥空后的 listeners 键应与「本就没有」等价")
	}
	if _, has := view["proxy-groups"]; has {
		t.Fatal("剥空后的 proxy-groups 键应与「本就没有」等价")
	}
	if _, has := view["proxies"]; !has {
		t.Fatal("业务键必须保留")
	}
	if !ConfigUnchanged(text, base) {
		t.Fatal("只差探测 lane/端口/密钥的配置应判 unchanged")
	}
	changed := strings.Replace(base, "name: n1", "name: n2", 1)
	if ConfigUnchanged(text, changed) {
		t.Fatal("节点内容变了必须判 changed")
	}
	if ConfigUnchanged(text, "::::") {
		t.Fatal("不可解析配置不能判 unchanged")
	}
}

func TestEmptyFallbackReject(t *testing.T) {
	source := "proxies:\n" +
		"- name: n-a\n  type: ss\n  server: a.example.com\n  port: 8388\n  cipher: aes-256-gcm\n  password: p1\n" +
		"- name: n-b\n  type: ss\n  server: b.example.com\n  port: 8388\n  cipher: aes-256-gcm\n  password: p2\n" +
		"  dialer-proxy: 中转\n" +
		"proxy-groups:\n" +
		"- name: 中转\n  type: url-test\n  proxies: [n-a]\n  url: http://x/204\n  interval: 300\n"
	out, err := RenderSubscriptionConfig(source)
	if err != nil {
		t.Fatalf("渲染失败：%v", err)
	}
	doc := mustLoad(t, out)
	groups, _ := doc["proxy-groups"].([]any)
	if len(groups) != 1 {
		t.Fatalf("组数不对：%d", len(groups))
	}
	group := groups[0].(map[string]any)
	if group["empty-fallback"] != "REJECT" {
		t.Fatalf("空组回落必须为 REJECT：%#v", group)
	}
	if group["type"] != "select" {
		t.Fatalf("自动组必须降级 select：%#v", group["type"])
	}
	if _, has := group["url"]; has {
		t.Fatal("探活设置必须剥掉")
	}
	members, _ := group["proxies"].([]any)
	if len(members) != 1 || members[0] != "n-a" {
		t.Fatalf("组成员不对：%#v", members)
	}
	proxies, _ := doc["proxies"].([]any)
	nodeB := proxies[1].(map[string]any)
	if nodeB["dialer-proxy"] != "中转" {
		t.Fatalf("组依赖解析后 dialer-proxy 应指向运行组名：%#v", nodeB["dialer-proxy"])
	}
	if doc["mode"] != "global" || doc["allow-lan"] != false {
		t.Fatalf("运行期键不对：%#v", doc)
	}
}

func TestLaneCountFor(t *testing.T) {
	cases := []struct{ pool, requested, want int }{
		{0, -1, 0}, {0, 5, 0}, {50, -1, 50}, {200, -1, MaxLanes},
		{500, -1, MaxLanes}, {200, 30, 30}, {50, 80, 50}, {1, -1, 1},
	}
	for _, c := range cases {
		if got := LaneCountFor(c.pool, c.requested); got != c.want {
			t.Fatalf("LaneCountFor(%d,%d)=%d，期望 %d", c.pool, c.requested, got, c.want)
		}
	}
}

func TestValidateRejects(t *testing.T) {
	node := "proxies:\n- name: n\n  type: ss\n  server: s\n  port: 1\n"
	cases := []struct {
		name string
		text string
		want string
	}{
		{"自动组类型", node + "proxy-groups:\n- name: g\n  type: url-test\n  proxies: [n]\n", "会让内核自行决定出口"},
		{"include-all 非探针", node + "proxy-groups:\n- name: g\n  type: select\n  include-all: true\n", "内核自主选路或探活设置"},
		{"规则集", node + "rule-providers:\n  r: {}\n", "规则集"},
		{"地理规则", node + "rules:\n- GEOIP,CN,DIRECT\n", "地理数据"},
		{"地理开关", node + "geodata-mode: true\n", "地理数据加载设置"},
		{"外部资源", node + "external-ui: ui\n", "界面资源设置"},
		{"节点来源", node + "proxy-providers:\n  p: {}\n", "未展开的节点来源"},
		{"DNS 地理", node + "dns:\n  nameserver: ['geosite:cn']\n", "指向地理数据规则"},
	}
	for _, c := range cases {
		err := ValidateExecutionConfigText(c.text)
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Fatalf("%s：应拒绝（含 %q），实际 %v", c.name, c.want, err)
		}
	}
	// 探针 lane 的 include-all 是唯一放行形态
	probe := node + "proxy-groups:\n- name: " + ProbeLaneGroupPrefix + "-1\n  type: select\n  include-all: true\n"
	if err := ValidateExecutionConfigText(probe); err != nil {
		t.Fatalf("探针 include-all 应放行：%v", err)
	}
}

func TestTextOpsBasics(t *testing.T) {
	text := "mixed-port: 1234\ntun:\n  enable: true\n  dns-hijack:\n  - any:53\nproxies:\n- name: n1\ntype: ss\n"
	stripped := StripTunSection(text)
	if strings.Contains(stripped, "tun") {
		t.Fatal("tun 段应被剥除")
	}
	if StripTunSection(stripped) != stripped {
		t.Fatal("tun 剥除应幂等")
	}
	if ParseMixedPort(text) != 1234 {
		t.Fatal("混合口解析不对")
	}
	pinned := PinMixedPort(text, 17890)
	if ParseMixedPort(pinned) != 17890 || !strings.Contains(pinned, "allow-lan: false") {
		t.Fatal("混合口钉定失败")
	}
	if PinMixedPort(pinned, 17890) != pinned {
		t.Fatal("已钉定时应原文返回")
	}
	withSecret := WithControllerSecret("mode: global\n", "abc")
	if !strings.Contains(withSecret, "secret: abc") {
		t.Fatal("密钥追加失败")
	}
	if WithControllerSecret("secret: old\nmode: global\n", "abc") != "secret: abc\nmode: global\n" {
		t.Fatal("密钥原位替换失败")
	}
	names := ParseNodeNames("proxies:\n- name: 香港 01\n  type: ss\n- name: 'b节点'\n  type: ss\nproxies-providers: x\n")
	if len(names) != 2 || names[0] != "香港 01" || names[1] != "b节点" {
		t.Fatalf("节点名解析不对：%#v", names)
	}
}

func TestRenderNodeBundleErrors(t *testing.T) {
	node := func(name string) map[string]any {
		return map[string]any{"name": name, "type": "ss", "server": "s", "port": int64(1)}
	}
	if _, err := RenderNodeBundle([]map[string]any{node("a")}, nil, BundleOptions{}); err != nil {
		t.Fatalf("普通节点应渲染成功：%v", err)
	}
	// 缺失依赖
	broken := node("a")
	broken["dialer-proxy"] = "不存在"
	if _, err := RenderNodeBundle([]map[string]any{broken}, nil, BundleOptions{}); err == nil {
		t.Fatal("缺失依赖应拒绝")
	}
	// 组没有可用成员（include-all 空池）
	emptyGroup := map[string]any{"name": "g", "type": "select", "include-all": true}
	if _, err := RenderNodeBundle(nil, nil, BundleOptions{Groups: []map[string]any{emptyGroup}, RootsSet: true, Roots: []string{"g"}}); err == nil {
		t.Fatal("空组应拒绝")
	}
	// 循环依赖
	loop := node("a")
	loop["dialer-proxy"] = "b"
	loop2 := node("b")
	loop2["dialer-proxy"] = "a"
	if _, err := RenderNodeBundle([]map[string]any{loop, loop2}, nil, BundleOptions{}); err == nil {
		t.Fatal("循环依赖应拒绝")
	}
}

func TestPinnedRuntimeEndpoints(t *testing.T) {
	text := "external-controller: 127.0.0.1:20010\n" +
		"secret: sk\n" +
		"mixed-port: 20011\n" +
		"listeners:\n" +
		"- name: lane-0-in\n  type: mixed\n  port: 20012\n  proxy: lane-0\n" +
		"- name: lane-1-in\n  type: mixed\n  port: 20013\n  proxy: lane-1\n" +
		"- name: " + ProbeLaneGroupPrefix + "-1-in\n  type: mixed\n  port: 21101\n  proxy: " + ProbeLaneGroupPrefix + "-1\n"
	pinned := PinnedRuntimeEndpoints(text)
	if pinned == nil {
		t.Fatal("现役端点解析失败")
	}
	if pinned.Controller != "127.0.0.1:20010" || pinned.Secret != "sk" || pinned.MixedPort != 20011 {
		t.Fatalf("端点不对：%#v", pinned)
	}
	if len(pinned.LanePorts) != 2 || pinned.LanePorts[0] != 20012 || pinned.LanePorts[1] != 20013 {
		t.Fatalf("lane 端口不对：%#v", pinned.LanePorts)
	}
	if len(pinned.ProbeLanePorts) != 1 || pinned.ProbeLanePorts[0] != 21101 {
		t.Fatalf("探测 lane 端口不对：%#v", pinned.ProbeLanePorts)
	}
	if PinnedRuntimeEndpoints("mode: global\n") != nil {
		t.Fatal("缺控制器时必须视为无现役端点")
	}
}
