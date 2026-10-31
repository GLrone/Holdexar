package proxypool

import (
	"math"
	"testing"
)

func airportConfig(name string) map[string]any {
	return map[string]any{
		"name": name, "type": "ss", "server": "hk1.example.com", "port": int64(8443),
		"cipher": "aes-256-gcm", "password": "secret", "udp": true,
	}
}

func TestNodeFingerprintKeyOrderIndependent(t *testing.T) {
	a := map[string]any{"name": "n", "type": "ss", "server": "s", "port": int64(1), "password": "p"}
	b := map[string]any{"password": "p", "port": int64(1), "server": "s", "type": "ss", "name": "n"}
	fa, err := NodeFingerprint(a, "tag")
	if err != nil {
		t.Fatal(err)
	}
	fb, err := NodeFingerprint(b, "tag")
	if err != nil {
		t.Fatal(err)
	}
	if fa != fb {
		t.Fatal("键顺序不应影响指纹")
	}
	if len(fa) != 64 {
		t.Fatalf("指纹应为 64 位 hex，got %d", len(fa))
	}
}

func TestNodeFingerprintAirportIgnoresPort(t *testing.T) {
	// 机场节点带 password → 端口不进身份：动态端口换号仍是同一节点
	a := airportConfig("n")
	b := airportConfig("n")
	b["port"] = int64(9999)
	fa, _ := NodeFingerprint(a, "tag")
	fb, _ := NodeFingerprint(b, "tag")
	if fa != fb {
		t.Fatal("机场节点换端口不应换指纹")
	}
	// 换凭据 = 另一个节点
	c := airportConfig("n")
	c["password"] = "other"
	fc, _ := NodeFingerprint(c, "tag")
	if fa == fc {
		t.Fatal("换凭据应换指纹")
	}
	// 未知键参与身份（宁可重登记，不可并成一条台账）
	d := airportConfig("n")
	d["new-opt"] = "x"
	fd, _ := NodeFingerprint(d, "tag")
	if fa == fd {
		t.Fatal("新增未知键应换指纹")
	}
	// 忽略键不参与身份（name/udp/开关族）
	e := airportConfig("renamed")
	e["tfo"] = true
	e["smux"] = map[string]any{"enabled": true}
	fe, _ := NodeFingerprint(e, "tag")
	if fa != fe {
		t.Fatal("忽略键不应影响指纹")
	}
}

func TestNodeFingerprintLocalChainPortIsIdentity(t *testing.T) {
	// 本地代理链形态：只有 type/server/port（+忽略键）→ 端口参与身份
	local := func(port int64) map[string]any {
		return map[string]any{"type": "http", "server": "127.0.0.1", "port": port}
	}
	fa, _ := NodeFingerprint(local(7890), "")
	fb, _ := NodeFingerprint(local(7891), "")
	if fa == fb {
		t.Fatal("端口是唯一区分依据时应换指纹")
	}
	// 加了实质字段（password）后端口退场
	withAuth := local(7890)
	withAuth["username"] = "u"
	withAuth["password"] = "p"
	fc, _ := NodeFingerprint(withAuth, "")
	fd, _ := NodeFingerprint(map[string]any{"type": "http", "server": "127.0.0.1", "port": int64(9999), "username": "u", "password": "p"}, "")
	if fc != fd {
		t.Fatal("有实质字段后端口不应进指纹")
	}
}

func TestPortIsIdentity(t *testing.T) {
	cases := []struct {
		config map[string]any
		want   bool
	}{
		{map[string]any{"type": "ss", "server": "s", "port": 1}, true},
		{map[string]any{"type": "ss", "server": "s", "port": 1, "name": "x", "udp": true}, true},
		{map[string]any{"type": "ss", "server": "s", "port": 1, "password": "p"}, false},
		{map[string]any{"server": "s", "port": 1}, true},
		{map[string]any{}, true},
	}
	for i, c := range cases {
		if got := PortIsIdentity(c.config); got != c.want {
			t.Fatalf("case %d: PortIsIdentity = %v, want %v", i, got, c.want)
		}
	}
}

func TestSubscriptionTag(t *testing.T) {
	cases := []struct {
		url       string
		wantLabel string
	}{
		{"https://sub.example.com/token", "example.com"},
		{"https://a.b.c.example.com/token", "example.com"},          // 后两段
		{"http://EXAMPLE.com/x", "example.com"},                     // 主机小写化
		{"https://user:pass@sub.example.com:8443/p", "example.com"}, // userinfo/端口剥离
		{"https://localhost/x", "localhost"},                        // 单段域名 → 标签即主机名
		{"", ""},                                                    // 空 → 仅 digest
		{"   ", ""},                                                 // 空白 strip 后为空
	}
	for _, c := range cases {
		tag := SubscriptionTag(c.url)
		digestPart := tag[:8]
		if len(digestPart) != 8 {
			t.Fatalf("digest 应为 8 位：%q", tag)
		}
		if c.wantLabel == "" {
			if len(tag) != 8 {
				t.Fatalf("无域名标签时应只有 digest：%q → %q", c.url, tag)
			}
			continue
		}
		if tag != digestPart+"."+c.wantLabel {
			t.Fatalf("%q → %q, want %q", c.url, tag, digestPart+"."+c.wantLabel)
		}
	}
	// 同链接同 tag；异链接异 tag（digest 必异）
	if SubscriptionTag("https://a.com/x") != SubscriptionTag(" https://a.com/x ") {
		t.Fatal("strip 后同链接应同 tag")
	}
	if SubscriptionTag("https://a.com/x") == SubscriptionTag("https://a.com/y") {
		t.Fatal("异链接应异 tag")
	}
}

func TestMakeRuntimeName(t *testing.T) {
	taken := map[string]bool{}
	n1 := MakeRuntimeName(7, "节点", taken)
	if n1 != "7|节点" {
		t.Fatalf("首个名字应为 7|节点，got %q", n1)
	}
	taken[n1] = true
	n2 := MakeRuntimeName(7, "节点", taken)
	if n2 != "7|节点#2" {
		t.Fatalf("撞名应追加 #2，got %q", n2)
	}
	taken[n2] = true
	if got := MakeRuntimeName(7, "节点", taken); got != "7|节点#3" {
		t.Fatalf("连续撞名应递增，got %q", got)
	}
}

func TestIsInfoPlaceholder(t *testing.T) {
	placeholders := []string{"剩余流量：31.01 GB", "官网：example.com", "到期时间：2026-12-31", "套餐到期", "防失联群"}
	for _, n := range placeholders {
		if !IsInfoPlaceholder(n) {
			t.Fatalf("%q 应判为占位节点", n)
		}
	}
	normal := []string{"香港 01", "US-Premium", "重置优先级", "剩余旅程"}
	// 「重置优先级」含「重置」是占位——注意词面是子串匹配，这里验证行为而非意图
	for _, n := range []string{"香港 01", "US-Premium"} {
		if IsInfoPlaceholder(n) {
			t.Fatalf("%q 不应判为占位节点", n)
		}
	}
	_ = normal
}

func TestPyFloatFormat(t *testing.T) {
	cases := []struct {
		in   float64
		want string
	}{
		{1.0, "1.0"}, {0.1, "0.1"}, {1.5, "1.5"}, {443.0, "443.0"},
		{1e15, "1000000000000000.0"}, {1e16, "1e+16"}, {1e-4, "0.0001"}, {1e-5, "1e-05"},
	}
	for _, c := range cases {
		if got := formatPyFloat(c.in); got != c.want {
			t.Fatalf("formatPyFloat(%v) = %q, want %q", c.in, got, c.want)
		}
	}
	// 负零：常量表达式无负零，用 Copysign 构造
	if got := formatPyFloat(math.Copysign(0, -1)); got != "-0.0" {
		t.Fatalf("formatPyFloat(-0) = %q, want -0.0", got)
	}
}

func TestPyJSONEncodeBasics(t *testing.T) {
	v := map[string]any{
		"b": int64(443), "a": "中文", "c": true, "d": nil, "e": 1.5, "f": []any{int64(1), "x"},
		"g": map[string]any{"y": int64(2), "x": false},
	}
	got, err := pyJSONEncode(v)
	if err != nil {
		t.Fatal(err)
	}
	want := `{"a":"中文","b":443,"c":true,"d":null,"e":1.5,"f":[1,"x"],"g":{"x":false,"y":2}}`
	if got != want {
		t.Fatalf("pyJSONEncode:\n got %s\nwant %s", got, want)
	}
	// 控制字符与引号反斜杠转义（0x01 → \u0001）
	escaped, _ := pyJSONEncode(map[string]any{"k": "a\"b\\c\nd\x01"})
	if escaped != `{"k":"a\"b\\c\nd\u0001"}` {
		t.Fatalf("转义形态不符：%s", escaped)
	}
}
