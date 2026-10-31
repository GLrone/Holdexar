package proxypool

import (
	"encoding/json"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// 同载荷对照验收（核心门禁）：节点身份哈希与订阅解析用真实 Python 参考实现
// 直调对拍，逐字节一致。venv 缺失自动 skip（跨语言对照的既有先例）。

const worktreeRoot = `E:\Holdexar-go-migration`

func venvPython(t *testing.T) string {
	t.Helper()
	py := filepath.Join(worktreeRoot, "server", ".venv", "Scripts", "python.exe")
	if _, err := os.Stat(py); err != nil {
		t.Skip("worktree venv 不存在，跳过跨语言对照")
	}
	return py
}

// runPythonRef 调真实参考实现：脚本含 ref_entry(payload) 定义与 main 驱动，
// 读 inputs.json、写 outputs.json（sys.path 注入 worktree server 根）。
func runPythonRef(t *testing.T, refBody string, inputs any, outputs any) {
	t.Helper()
	py := venvPython(t)
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "data"), 0o755); err != nil {
		t.Fatal(err)
	}
	inPath := filepath.Join(dir, "inputs.json")
	outPath := filepath.Join(dir, "outputs.json")
	inJSON, err := json.Marshal(inputs)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(inPath, inJSON, 0o644); err != nil {
		t.Fatal(err)
	}
	script := "import sys, json, os\n" +
		"os.environ.setdefault('HOLDEXAR_DATA_DIR', r'" + filepath.Join(dir, "data") + "')\n" +
		"sys.path.insert(0, r'" + filepath.Join(worktreeRoot, "server") + "')\n" +
		"payload = json.load(open(r'" + inPath + "', encoding='utf-8'))\n" +
		refBody + "\n" +
		"result = ref_entry(payload)\n" +
		"json.dump(result, open(r'" + outPath + "', 'w', encoding='utf-8'), ensure_ascii=False)\n"
	scriptPath := filepath.Join(dir, "ref_side.py")
	if err := os.WriteFile(scriptPath, []byte(script), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(py, "-I", scriptPath)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("参考实现执行失败：%v\n%s", err, out)
	}
	outJSON, err := os.ReadFile(outPath)
	if err != nil {
		t.Fatalf("对照输出读不回：%v\n%s", err, out)
	}
	dec := json.NewDecoder(strings.NewReader(string(outJSON)))
	dec.UseNumber()
	if err := dec.Decode(outputs); err != nil {
		t.Fatalf("对照输出解析失败：%v\n%s", err, outJSON)
	}
	_ = io.Discard
}

// refIdentityScript 参考侧：真实 models.py 的三个身份函数。
const refIdentityScript = `
from app.domains.proxypool.models import node_fingerprint, subscription_tag, port_is_identity


def ref_entry(payload):
    return {
        "fingerprints": [node_fingerprint(c["config"], c["tag"]) for c in payload["fingerprint_cases"]],
        "tags": [subscription_tag(u) for u in payload["tag_urls"]],
        "port_identity": [port_is_identity(c) for c in payload["port_identity_cases"]],
    }
`

func TestCrossLangIdentityHash(t *testing.T) {
	// 15 组输入：同链接异内容 / 同内容异链接 / 动态端口机场 / 本地代理链 /
	// 域名边界 / 中文与嵌套 / 浮点布尔空值 / 键序打乱 / 未知键 / 控制字符
	config := func(extra map[string]any) map[string]any {
		base := map[string]any{
			"name": "香港 01", "type": "ss", "server": "hk1.example.com",
			"port": json.Number("8443"), "cipher": "aes-256-gcm", "password": "p@ss",
		}
		for k, v := range extra {
			base[k] = v
		}
		return base
	}
	fingerprintCases := []map[string]any{
		{"tag": "abc12345.example.com", "config": config(nil)},
		{"tag": "abc12345.example.com", "config": config(map[string]any{"password": "rotated"})},
		{"tag": "ffff8888.other.org", "config": config(nil)},
		{"tag": "abc12345.example.com", "config": config(map[string]any{"port": json.Number("9999")})},
		{"tag": "abc12345.example.com", "config": config(map[string]any{
			"name": "改名 #7", "udp": true, "tfo": true, "smux": map[string]any{"enabled": true},
		})},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"type": "http", "server": "127.0.0.1", "port": json.Number("7890"),
		}},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"type": "http", "server": "127.0.0.1", "port": json.Number("7891"),
		}},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"type": "http", "server": "127.0.0.1", "port": json.Number("7890"),
			"username": "u", "password": "p",
		}},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"name": "嵌套", "type": "vmess", "server": "s.example.com", "port": json.Number("443"),
			"uuid": "u", "ws-opts": map[string]any{
				"path": "/path", "headers": map[string]any{"Host": "h.example.com", "X-B": "值2"},
			},
		}},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"name": "浮点与空", "type": "ss", "server": "s2", "port": json.Number("1.5"),
			"ratio": json.Number("0.25"), "comment": nil, "flag": true,
			"big": json.Number("12345678901234567890"),
		}},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"name": "列表明", "type": "ss", "server": "s3", "port": json.Number("2"),
			"alpn": []any{"h2", "http/1.1"}, "nums": []any{json.Number("1"), json.Number("2.5")},
		}},
		{"tag": "", "config": config(nil)},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"password": "p@ss", "port": json.Number("8443"), "server": "hk1.example.com",
			"type": "ss", "name": "香港 01", "cipher": "aes-256-gcm",
		}},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"name": "未知键", "type": "ss", "server": "hk1.example.com", "port": json.Number("8443"),
			"cipher": "aes-256-gcm", "password": "p@ss", "new-opt": "x",
		}},
		{"tag": "abc12345.example.com", "config": map[string]any{
			"name": "控制符😀中文", "type": "ss", "server": "s5", "port": json.Number("3"),
			"password": "a\"b\\c\nd\tx",
		}},
	}
	tagURLs := []string{
		"https://sub.example.com/token?x=1",
		"https://a.b.c.example.com/token",
		"http://EXAMPLE.com/x",
		"https://user:pass@sub.example.com:8443/p",
		"https://localhost/x",
		"",
		"   ",
		"https://203.0.113.7:2053/sub",
		"https://[2001:db8::1]:443/sub",
		"vmess://eyJhZGQiOiJleGFtcGxlLmNvbSJ9",
	}
	portIdentityCases := []map[string]any{
		{"type": "ss", "server": "s", "port": json.Number("1")},
		{"type": "ss", "server": "s", "port": json.Number("1"), "name": "x", "udp": true},
		{"type": "ss", "server": "s", "port": json.Number("1"), "password": "p"},
		{"server": "s", "port": json.Number("1")},
		{},
		{"type": "ss", "server": "s", "port": json.Number("1"), "ws-opts": map[string]any{}},
	}

	var got struct {
		Fingerprints []string `json:"fingerprints"`
		Tags         []string `json:"tags"`
		PortIdentity []bool   `json:"port_identity"`
	}
	runPythonRef(t, refIdentityScript, map[string]any{
		"fingerprint_cases":   fingerprintCases,
		"tag_urls":            tagURLs,
		"port_identity_cases": portIdentityCases,
	}, &got)

	if len(got.Fingerprints) != len(fingerprintCases) || len(got.Tags) != len(tagURLs) ||
		len(got.PortIdentity) != len(portIdentityCases) {
		t.Fatalf("对照输出数量不符：%d/%d/%d", len(got.Fingerprints), len(got.Tags), len(got.PortIdentity))
	}
	for i, c := range fingerprintCases {
		want, err := NodeFingerprint(c["config"].(map[string]any), c["tag"].(string))
		if err != nil {
			t.Fatalf("case %d Go 侧失败：%v", i, err)
		}
		if want != got.Fingerprints[i] {
			t.Fatalf("case %d 指纹逐字节不一致：\nGo     %s\nPython %s", i, want, got.Fingerprints[i])
		}
	}
	// 关键语义锚点（对拍背景下的行为断言）
	fp0, _ := NodeFingerprint(fingerprintCases[0]["config"].(map[string]any), fingerprintCases[0]["tag"].(string))
	fp3, _ := NodeFingerprint(fingerprintCases[3]["config"].(map[string]any), fingerprintCases[3]["tag"].(string))
	if fp0 != fp3 {
		t.Fatal("动态端口机场换端口应同指纹")
	}
	fp4, _ := NodeFingerprint(fingerprintCases[4]["config"].(map[string]any), fingerprintCases[4]["tag"].(string))
	if fp4 != fp0 {
		t.Fatal("忽略键（改名/开关族）不应影响指纹")
	}
	fp5, _ := NodeFingerprint(fingerprintCases[5]["config"].(map[string]any), fingerprintCases[5]["tag"].(string))
	fp6, _ := NodeFingerprint(fingerprintCases[6]["config"].(map[string]any), fingerprintCases[6]["tag"].(string))
	if fp5 == fp6 {
		t.Fatal("本地代理链形态换端口应换指纹")
	}
	fp13, _ := NodeFingerprint(fingerprintCases[13]["config"].(map[string]any), fingerprintCases[13]["tag"].(string))
	if fp13 == fp0 {
		t.Fatal("未知键应参与身份")
	}
	for i, u := range tagURLs {
		if want := SubscriptionTag(u); want != got.Tags[i] {
			t.Fatalf("tag %q：Go %s Python %s", u, want, got.Tags[i])
		}
	}
	for i, c := range portIdentityCases {
		if want := PortIsIdentity(c); want != got.PortIdentity[i] {
			t.Fatalf("port_is_identity case %d：Go %v Python %v", i, want, got.PortIdentity[i])
		}
	}
	t.Logf("身份哈希对照：%d 组指纹 + %d 组 tag + %d 组端口分档逐字节一致",
		len(got.Fingerprints), len(got.Tags), len(got.PortIdentity))
}

// refParseScript 参考侧：真实 subscription.py 的 parse_nodes + 逐节点指纹。
const refParseScript = `
import json
from app.domains.proxypool.subscription import parse_nodes
from app.domains.proxypool.models import node_fingerprint


def ref_entry(payload):
    raw = open(payload["sample_path"], "rb").read()
    nodes = parse_nodes(raw, "yaml")
    return {
        "count": len(nodes),
        "nodes": nodes,
        "fingerprints": [node_fingerprint(n, "crosslang") for n in nodes],
    }
`

func TestCrossLangParseNodes(t *testing.T) {
	dir := t.TempDir()
	samplePath := filepath.Join(dir, "sample.yaml")
	if err := os.WriteFile(samplePath, []byte(sampleSubscription), 0o644); err != nil {
		t.Fatal(err)
	}
	var got struct {
		Count        int            `json:"count"`
		Nodes        []symmetricMap `json:"nodes"`
		Fingerprints []string       `json:"fingerprints"`
	}
	runPythonRef(t, refParseScript, map[string]any{"sample_path": samplePath}, &got)

	nodes, err := ParseNodes([]byte(sampleSubscription), FormatYAML)
	if err != nil {
		t.Fatalf("Go 侧解析失败：%v", err)
	}
	if len(nodes) != got.Count {
		t.Fatalf("节点数不一致：Go %d Python %d", len(nodes), got.Count)
	}
	for i, node := range nodes {
		// 树深度相等（数值按值比较，对齐 Python ==）
		if !pyConfigEqual(node, got.Nodes[i].m) {
			t.Fatalf("节点 %d 树不一致：\nGo     %v\nPython %v", i, node, got.Nodes[i].m)
		}
		fp, err := NodeFingerprint(node, "crosslang")
		if err != nil {
			t.Fatal(err)
		}
		if fp != got.Fingerprints[i] {
			t.Fatalf("节点 %d 指纹不一致：Go %s Python %s", i, fp, got.Fingerprints[i])
		}
	}
	t.Logf("解析对照：%d 节点树逐值一致 + 指纹逐字节一致", got.Count)
}

// symmetricMap JSON 对象解码（UseNumber，数字形态保真）。
type symmetricMap struct{ m map[string]any }

func (s *symmetricMap) UnmarshalJSON(data []byte) error {
	return jsonUnmarshalUseNumber(data, &s.m)
}
