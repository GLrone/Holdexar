package proxypool

import "testing"

// 样例订阅：块式 + 流式 + 中文 + 嵌套结构 + 信息占位节点 + 换行注释。
const sampleSubscription = `# Clash 订阅样例
port: 7890
mode: rule
proxies:
  - name: "香港 01"
    type: ss
    server: hk1.example.com
    port: 8443
    cipher: aes-256-gcm
    password: "p@ss:word#1"
    udp: true
  - {name: 美国 02, type: vmess, server: us2.example.com, port: 443, uuid: "aaaa-bbbb", alterId: 0, cipher: auto, tls: true, network: ws, ws-opts: {path: /path, headers: {Host: us2.example.com}}}
  - name: "改名 #7"
    type: trojan
    server: jp3.example.com
    port: 443
    password: secret
    sni: jp3.example.com
    alpn: [h2, http/1.1]
  - name: "剩余流量：31.01 GB"
    type: ss
    server: info.example.com
    port: 1
    cipher: aes-128-gcm
    password: x
  - name: "0 号节点"
    type: ss
    server: zero.example.com
    port: 123
    cipher: aes-128-gcm
    password: y
proxy-groups:
  - name: PROXY
    type: select
    proxies: [香港 01, 美国 02]
rules:
  - MATCH,PROXY
`

func TestParseNodesSample(t *testing.T) {
	nodes, err := ParseNodes([]byte(sampleSubscription), FormatYAML)
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	if len(nodes) != 4 {
		t.Fatalf("应解析出 4 个节点（信息占位剔除），got %d", len(nodes))
	}
	first := nodes[0]
	if first["name"] != "香港 01" {
		t.Fatalf("首节点名不符：%v", first["name"])
	}
	if first["port"] != int64(8443) {
		t.Fatalf("端口应解析为整数，got %T %v", first["port"], first["port"])
	}
	if first["udp"] != true {
		t.Fatalf("udp: true 应为布尔，got %T", first["udp"])
	}
	if first["password"] != "p@ss:word#1" {
		t.Fatalf("引号内 # 与 : 不应破坏标量：%v", first["password"])
	}
	second := nodes[1]
	ws, ok := second["ws-opts"].(map[string]any)
	if !ok {
		t.Fatalf("ws-opts 应为嵌套映射，got %T", second["ws-opts"])
	}
	headers := ws["headers"].(map[string]any)
	if headers["Host"] != "us2.example.com" {
		t.Fatalf("流式嵌套映射解析不符：%v", headers)
	}
	third := nodes[2]
	alpn, ok := third["alpn"].([]any)
	if !ok || len(alpn) != 2 || alpn[0] != "h2" {
		t.Fatalf("流式序列解析不符：%v", third["alpn"])
	}
	if third["name"] != "改名 #7" {
		t.Fatalf("引号内版本后缀不应剥离：%v", third["name"])
	}
	// name 以数字开头仍是字符串标量（0 号节点）
	if nodes[3]["name"] != "0 号节点" {
		t.Fatalf("0 前缀名称应是字符串：%v %T", nodes[3]["name"], nodes[3]["name"])
	}
}

func TestParseNodesScalarSemantics(t *testing.T) {
	yaml := "proxies:\n  - name: a\n    type: ss\n    server: s\n    port: 0700\n    alterId: 0\n    skip: no\n    keep: yes\n    weight: 1.5\n    ratio: .5\n    tag: 0899\n"
	nodes, err := ParseNodes([]byte(yaml), FormatYAML)
	if err != nil {
		t.Fatalf("解析失败：%v", err)
	}
	n := nodes[0]
	if v, ok := n["port"].(int64); !ok || v != int64(448) {
		// 0700 按 YAML 1.1 八进制 = 448；0888 越界不成八进制 → 字符串（见 tag）
		t.Fatalf("0700 应按 YAML 1.1 八进制解析为 448，got %T %v", n["port"], n["port"])
	}
	if n["alterId"] != int64(0) {
		t.Fatalf("0 应为整数：%T", n["alterId"])
	}
	if n["skip"] != false {
		t.Fatalf("no 应为布尔 false（YAML 1.1），got %T", n["skip"])
	}
	if n["keep"] != true {
		t.Fatalf("yes 应为布尔 true（YAML 1.1），got %T", n["keep"])
	}
	if v, ok := n["weight"].(float64); !ok || v != 1.5 {
		t.Fatalf("1.5 应为浮点，got %T %v", n["weight"], n["weight"])
	}
	if v, ok := n["ratio"].(float64); !ok || v != 0.5 {
		t.Fatalf(".5 应为浮点 0.5，got %T %v", n["ratio"], n["ratio"])
	}
	if n["tag"] != "0899" {
		// 0899 不构成八进制（8/9 越界）→ 字符串
		t.Fatalf("0899 应为字符串，got %T %v", n["tag"], n["tag"])
	}
}

func TestParseNodesFailures(t *testing.T) {
	if _, err := ParseNodes([]byte("proxies: []"), FormatYAML); err == nil {
		t.Fatal("空节点集应报错")
	} else if _, ok := err.(*EmptyNodeSetError); !ok {
		t.Fatalf("空集应 EmptyNodeSetError，got %T", err)
	}
	if _, err := ParseNodes([]byte("proxies:\n  - name: a\n"), FormatBase64); err == nil {
		t.Fatal("非 yaml 格式应报不支持")
	}
	// 只有 proxy-providers 无 inline proxies → provider 格式不支持
	providerOnly := "proxy-providers:\n  sub:\n    type: http\n    url: https://x/y\n"
	if _, err := ParseNodes([]byte(providerOnly), FormatYAML); err == nil {
		t.Fatal("仅 provider 的订阅应报 provider_yaml 不支持")
	}
	// 全部是信息占位节点 → 空集
	placeholder := "proxies:\n  - {name: \"到期时间：2026-12-31\", type: ss, server: a, port: 1, cipher: x, password: y}\n"
	if _, err := ParseNodes([]byte(placeholder), FormatYAML); err == nil {
		t.Fatal("占位节点全剔除后应报空集")
	}
	// 锚点不支持 → fail-closed
	anchor := "base: &b {cipher: x}\nproxies:\n  - {name: a, type: ss, server: s, port: 1, <<: *b}\n"
	if _, err := ParseNodes([]byte(anchor), FormatYAML); err == nil {
		t.Fatal("锚点/merge 键应显式报错而非近似解析")
	}
}

func TestDetectFormat(t *testing.T) {
	cases := []struct {
		raw  string
		want string
	}{
		{"", FormatEmpty},
		{"proxies:\n  - name: a\n", FormatYAML},
		{"proxy-providers:\n  a:\n    type: http\n", FormatProviderYAML},
		{"{a: 1}", FormatJSON},
		{"<html><body>x</body></html>", FormatHTML},
		{"dm1lc3M6Ly9hYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAxMjM0NTY3ODlF", FormatBase64}, // >40 字符纯 base64 字面
		{"ss://abcdef@host:443#n1\nss://uvwxyz@host2:443#n2", FormatURI},               // 每行 URI
		{"-----BEGIN AGE ENCRYPTED FILE-----\nxyz", FormatAge},
		{"just some text that is neither yaml nor anything else!!", FormatUnknown},
	}
	for _, c := range cases {
		got, err := DetectFormat([]byte(c.raw))
		if err != nil {
			t.Fatalf("DetectFormat(%q) err %v", c.raw, err)
		}
		if got != c.want {
			t.Fatalf("DetectFormat(%q) = %s, want %s", c.raw, got, c.want)
		}
	}
	if _, err := DetectFormat([]byte{0xff, 0xfe, 0x00}); err == nil {
		t.Fatal("非法 UTF-8 应报错")
	}
}
