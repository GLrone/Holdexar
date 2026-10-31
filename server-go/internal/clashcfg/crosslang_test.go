package clashcfg

// 配置生成对照验收（核心门禁）：运行配置生成、探针 lane 注入、池 lane 生成、
// 节点依赖解析与 lane 上限，用 worktree venv 直调真实 Python 参考实现对拍，
// YAML 解析后逐值比对（键序不作要求）。venv 缺失自动 skip（跨语言对照先例）。

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const refWorktreeRoot = `E:\Holdexar-go-migration`

func venvPython(t *testing.T) string {
	t.Helper()
	py := filepath.Join(refWorktreeRoot, "server", ".venv", "Scripts", "python.exe")
	if _, err := os.Stat(py); err != nil {
		t.Skip("worktree venv 不存在，跳过跨语言对照")
	}
	return py
}

// runPythonRef 直调真实参考实现：读 inputs.json、写 outputs.json。
func runPythonRef(t *testing.T, refBody string, inputs any, outputs any) {
	t.Helper()
	py := venvPython(t)
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "data-py"), 0o755); err != nil {
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
		"os.environ.setdefault('HOLDEXAR_DATA_DIR', r'" + filepath.Join(dir, "data-py") + "')\n" +
		"sys.path.insert(0, r'" + filepath.Join(refWorktreeRoot, "server") + "')\n" +
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
}

// jsonNormalize JSON 数字统一化（int64/float64 语义对齐后逐值比较）。
func jsonNormalize(t *testing.T, v any) any {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("规范化序列化失败：%v", err)
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.UseNumber()
	var out any
	if err := dec.Decode(&out); err != nil {
		t.Fatalf("规范化解析失败：%v", err)
	}
	return convertJSONNumbers(out)
}

func convertJSONNumbers(v any) any {
	switch val := v.(type) {
	case map[string]any:
		for k, item := range val {
			val[k] = convertJSONNumbers(item)
		}
		return val
	case []any:
		for i, item := range val {
			val[i] = convertJSONNumbers(item)
		}
		return val
	case json.Number:
		text := val.String()
		if !strings.ContainsAny(text, ".eE") {
			if n, err := jsonNumInt(text); err == nil {
				return n
			}
		}
		f, err := val.Float64()
		if err != nil {
			return text
		}
		return f
	default:
		return v
	}
}

func jsonNumInt(text string) (int64, error) {
	var n int64
	neg := false
	for i, c := range text {
		if i == 0 && c == '-' {
			neg = true
			continue
		}
		if c < '0' || c > '9' {
			return 0, fmt.Errorf("非整数")
		}
		n = n*10 + int64(c-'0')
	}
	if neg {
		n = -n
	}
	return n, nil
}

const refScriptBody = `
import yaml
from pathlib import Path
from app.domains.proxies.runtime_config import (
    render_subscription_config, resolve_node_dependencies,
)
from app.domains.proxies.clash_manager import inject_probe_lanes
from app.domains.proxypool.runtime import prepare_runtime_config, lane_count_for


def ref_entry(payload):
    result = {"cases": []}
    for case in payload["cases"]:
        kind = case["kind"]
        if kind == "render":
            text = render_subscription_config(case["text"])
            result["cases"].append({"kind": kind, "text": text, "doc": yaml.safe_load(text)})
        elif kind == "deps":
            proxies = resolve_node_dependencies(case["payloads"], case["names"])
            result["cases"].append({"kind": kind, "proxies": proxies})
        elif kind == "inject":
            text = inject_probe_lanes(
                case["text"],
                port_base=case.get("port_base", 20000),
                port_span=case.get("port_span", 1000),
                pin_ports=case.get("pin_ports"),
            )
            result["cases"].append({"kind": kind, "text": text, "doc": yaml.safe_load(text)})
        elif kind == "prepare":
            data_dir = Path(payload["data_dir"])
            pool_dir = data_dir / "proxypool"
            pool_dir.mkdir(parents=True, exist_ok=True)
            (pool_dir / "crawl-pool.yaml").write_text(case["pool_text"], encoding="utf-8")
            if case.get("runtime_text") is not None:
                (pool_dir / "crawl-runtime.yaml").write_text(case["runtime_text"], encoding="utf-8")
            lanes = case["lanes"]
            if lanes == -1:
                lanes = None
            path = prepare_runtime_config(data_dir, lanes=lanes, pin_running=case["pin_running"])
            text = Path(path).read_text(encoding="utf-8")
            result["cases"].append({"kind": kind, "text": text, "doc": yaml.safe_load(text)})
        elif kind == "lane_count":
            values = []
            for pool, requested in case["pairs"]:
                req = None if requested == -1 else requested
                values.append(lane_count_for(pool, req))
            result["cases"].append({"kind": kind, "values": values})
        elif kind == "render_error":
            try:
                render_subscription_config(case["text"])
                result["cases"].append({"kind": kind, "error": None})
            except ValueError as exc:
                result["cases"].append({"kind": kind, "error": str(exc)})
        else:
            raise ValueError("未知 case kind：" + kind)
    return result
`

func nodePayload(name string) map[string]any {
	return map[string]any{
		"name": name, "type": "ss", "server": name + ".example.com",
		"port": int64(8388), "cipher": "aes-256-gcm", "password": "pw-" + name,
	}
}

// TestCrossLangRenderSubscription 订阅运行配置渲染对照（含本地链/自动组降级/
// tun 与地理剥除/空组边界）。
func TestCrossLangRenderSubscription(t *testing.T) {
	subscription := "mixed-port: 17890\n" +
		"external-controller: 0.0.0.0:9999\n" +
		"secret: untrusted\n" +
		"tun:\n" +
		"  enable: true\n" +
		"geodata-mode: true\n" +
		"rule-providers:\n" +
		"  region:\n" +
		"    type: http\n" +
		"    url: https://example.invalid/rules\n" +
		"rules:\n" +
		"- GEOIP,CN,DIRECT\n" +
		"- MATCH,DIRECT\n" +
		"dns:\n" +
		"  enable: true\n" +
		"  ipv6: false\n" +
		"  nameserver:\n" +
		"  - 223.5.5.5\n" +
		"  - 119.29.29.29\n" +
		"  nameserver-policy:\n" +
		"    geosite:cn: 223.5.5.5\n" +
		"proxies:\n" +
		"- name: 节点A\n" +
		"  type: ss\n" +
		"  server: a.example.com\n" +
		"  port: 8388\n" +
		"  cipher: aes-256-gcm\n" +
		"  password: pw-a\n" +
		"- name: 落地B\n" +
		"  type: ss\n" +
		"  server: b.example.com\n" +
		"  port: 8443\n" +
		"  cipher: aes-256-gcm\n" +
		"  password: pw-b\n" +
		"  dialer-proxy: 中转组\n" +
		"- name: 中转机\n" +
		"  type: vmess\n" +
		"  server: c.example.com\n" +
		"  port: 443\n" +
		"  uuid: 11111111-2222-3333-4444-555555555555\n" +
		"  alterId: 0\n" +
		"  cipher: auto\n" +
		"  ws-opts:\n" +
		"    path: /ws\n" +
		"    headers:\n" +
		"      Host: c.example.com\n" +
		"proxy-groups:\n" +
		"- name: 中转组\n" +
		"  type: url-test\n" +
		"  proxies: [中转机, 节点A]\n" +
		"  url: http://www.gstatic.com/generate_204\n" +
		"  interval: 300\n" +
		"- name: 未引用组\n" +
		"  type: select\n" +
		"  proxies: [节点A]\n"

	goDoc := func(t *testing.T) map[string]any {
		t.Helper()
		out, err := RenderSubscriptionConfig(subscription)
		if err != nil {
			t.Fatalf("Go 渲染失败：%v", err)
		}
		doc, err := LoadYAML(out)
		if err != nil {
			t.Fatalf("Go 产物解析失败：%v", err)
		}
		return doc
	}

	var outputs struct {
		Cases []struct {
			Kind string         `json:"kind"`
			Text string         `json:"text"`
			Doc  map[string]any `json:"doc"`
		} `json:"cases"`
	}
	runPythonRef(t, refScriptBody, map[string]any{
		"cases": []map[string]any{{"kind": "render", "text": subscription}},
	}, &outputs)
	if len(outputs.Cases) != 1 {
		t.Fatalf("参考侧 case 数不对：%d", len(outputs.Cases))
	}
	pyDoc := jsonNormalize(t, outputs.Cases[0].Doc)
	goResult := jsonNormalize(t, goDoc(t))
	if !deepEqualAny(pyDoc, goResult) {
		pyJSON, _ := json.MarshalIndent(pyDoc, "", " ")
		goJSON, _ := json.MarshalIndent(goResult, "", " ")
		t.Fatalf("渲染结果不等价\nPython：%s\nGo：%s", pyJSON, goJSON)
	}
}

// TestCrossLangRenderErrors 渲染拒绝边界对照。
func TestCrossLangRenderErrors(t *testing.T) {
	cases := []map[string]any{
		{"kind": "render_error", "text": "proxies: []\n"},
		{"kind": "render_error", "text": "proxies:\n- name: n\n  type: ss\n  server: s\n  port: 1\nproxy-providers:\n  p: {}\n"},
		{"kind": "render_error", "text": ": : 不是 YAML [\n"},
	}
	var outputs struct {
		Cases []struct {
			Kind  string `json:"kind"`
			Error string `json:"error"`
		} `json:"cases"`
	}
	runPythonRef(t, refScriptBody, map[string]any{"cases": cases}, &outputs)
	if len(outputs.Cases) != len(cases) {
		t.Fatalf("case 数不对：%d", len(outputs.Cases))
	}
	for i, c := range outputs.Cases {
		_, goErr := RenderSubscriptionConfig(cases[i]["text"].(string))
		if c.Error == "" {
			if goErr == nil {
				t.Fatalf("case %d：参考侧未拒绝，Go 侧也不应拒绝", i)
			}
			t.Fatalf("case %d：参考侧未拒绝而报错文本缺失（Go 报 %v）", i, goErr)
		}
		if goErr == nil {
			t.Fatalf("case %d：参考侧拒绝（%s），Go 侧却通过", i, c.Error)
		}
		// 语义性错误消息对齐（YAML 解析错误的细节允许实现差异）
		if strings.Contains(c.Error, "没有节点") || strings.Contains(c.Error, "未展开的节点来源") {
			if goErr.Error() != c.Error {
				t.Fatalf("case %d：错误消息不对齐\nPython：%s\nGo：%s", i, c.Error, goErr.Error())
			}
		}
	}
}

// TestCrossLangResolveNodeDependencies 节点依赖解析对照（改名映射 + 本地链 + 组依赖）。
func TestCrossLangResolveNodeDependencies(t *testing.T) {
	payloads := []map[string]any{
		nodePayload("原始A"),
		nodePayload("原始B"),
	}
	payloads[1]["dialer-proxy"] = "原始A"
	payloads[1]["ws-opts"] = map[string]any{
		"path": "/ws", "headers": map[string]any{"Host": "h.example.com"},
	}
	names := map[string]string{"原始A": "sub-1|香港 01", "原始B": "sub-1|落地 02"}

	var outputs struct {
		Cases []struct {
			Kind    string           `json:"kind"`
			Proxies []map[string]any `json:"proxies"`
		} `json:"cases"`
	}
	runPythonRef(t, refScriptBody, map[string]any{
		"cases": []map[string]any{{
			"kind": "deps", "payloads": payloads, "names": names,
		}},
	}, &outputs)
	goProxies, err := ResolveNodeDependencies(payloads, names)
	if err != nil {
		t.Fatalf("Go 依赖解析失败：%v", err)
	}
	pyNormalized := jsonNormalize(t, outputs.Cases[0].Proxies)
	goNormalized := jsonNormalize(t, goProxies)
	if !deepEqualAny(pyNormalized, goNormalized) {
		pyJSON, _ := json.MarshalIndent(pyNormalized, "", " ")
		goJSON, _ := json.MarshalIndent(goNormalized, "", " ")
		t.Fatalf("依赖解析不等价\nPython：%s\nGo：%s", pyJSON, goJSON)
	}
}

// TestCrossLangInjectProbeLanes 探针 lane 注入对照（钉住全量端口保证确定性）。
func TestCrossLangInjectProbeLanes(t *testing.T) {
	base := "mixed-port: 17890\n" +
		"external-controller: 127.0.0.1:19090\n" +
		"proxies:\n" +
		"- name: n1\n" +
		"  type: ss\n" +
		"  server: s.example.com\n" +
		"  port: 8388\n" +
		"proxy-groups:\n" +
		"- name: 旧" + ProbeLaneGroupPrefix + "-1\n" +
		"  type: select\n" +
		"  include-all: true\n" +
		"listeners:\n" +
		"- name: 旧" + ProbeLaneGroupPrefix + "-1-in\n" +
		"  type: mixed\n" +
		"  port: 9999\n" +
		"  proxy: 旧" + ProbeLaneGroupPrefix + "-1\n"
	pins := make([]int, ProbeLaneCount)
	for i := range pins {
		pins[i] = 21100 + i
	}
	var outputs struct {
		Cases []struct {
			Kind string         `json:"kind"`
			Text string         `json:"text"`
			Doc  map[string]any `json:"doc"`
		} `json:"cases"`
	}
	runPythonRef(t, refScriptBody, map[string]any{
		"cases": []map[string]any{{
			"kind": "inject", "text": base, "port_base": 21100, "port_span": 900, "pin_ports": pins,
		}},
	}, &outputs)
	goText := InjectProbeLanes(base, ProbeLaneOptions{
		PortBase: 21100, PortSpan: 900, PinPorts: pins,
	})
	goDoc, err := LoadYAML(goText)
	if err != nil {
		t.Fatalf("Go 产物解析失败：%v", err)
	}
	pyNormalized := jsonNormalize(t, outputs.Cases[0].Doc)
	goNormalized := jsonNormalize(t, goDoc)
	if !deepEqualAny(pyNormalized, goNormalized) {
		pyJSON, _ := json.MarshalIndent(pyNormalized, "", " ")
		goJSON, _ := json.MarshalIndent(goNormalized, "", " ")
		t.Fatalf("探针 lane 注入不等价\nPython：%s\nGo：%s", pyJSON, goJSON)
	}
}

// TestCrossLangPrepareRuntimeConfig 池 lane 生成对照：钉住端点的热重载通道逐值
// 等价；冷通道动态端点掩码后等价。
func TestCrossLangPrepareRuntimeConfig(t *testing.T) {
	poolText := "proxies:\n" +
		"- name: 池节点A\n  type: ss\n  server: a.example.com\n  port: 8388\n  cipher: aes-256-gcm\n  password: pa\n" +
		"- name: 池节点B\n  type: ss\n  server: b.example.com\n  port: 8388\n  cipher: aes-256-gcm\n  password: pb\n" +
		"- name: 池节点C\n  type: ss\n  server: c.example.com\n  port: 8388\n  cipher: aes-256-gcm\n  password: pc\n"
	probePins := make([]string, 0, ProbeLaneCount)
	for i := 0; i < ProbeLaneCount; i++ {
		probePins = append(probePins,
			fmt.Sprintf("- name: %s-%d-in\n  type: mixed\n  port: %d\n  listen: 127.0.0.1\n  proxy: %s-%d\n",
				ProbeLaneGroupPrefix, i+1, 21100+i, ProbeLaneGroupPrefix, i+1))
	}
	pinnedRuntime := "external-controller: 127.0.0.1:21154\n" +
		"secret: pinned-secret\n" +
		"mixed-port: 21153\n" +
		"listeners:\n" +
		"- name: lane-0-in\n  type: mixed\n  port: 21150\n  listen: 127.0.0.1\n  proxy: lane-0\n" +
		"- name: lane-1-in\n  type: mixed\n  port: 21151\n  listen: 127.0.0.1\n  proxy: lane-1\n" +
		"- name: lane-2-in\n  type: mixed\n  port: 21152\n  listen: 127.0.0.1\n  proxy: lane-2\n" +
		strings.Join(probePins, "")

	cases := []map[string]any{
		{"kind": "prepare", "pool_text": poolText, "runtime_text": pinnedRuntime,
			"lanes": 3, "pin_running": true},
		{"kind": "prepare", "pool_text": poolText, "runtime_text": nil,
			"lanes": 2, "pin_running": false},
		{"kind": "prepare", "pool_text": poolText, "runtime_text": nil,
			"lanes": -1, "pin_running": false},
		{"kind": "lane_count", "pairs": [][2]int{
			{0, -1}, {0, 5}, {50, -1}, {200, -1}, {500, -1}, {200, 30}, {50, 80}, {1, -1}, {120, -1},
		}},
	}

	dir := t.TempDir()
	goDataDir := filepath.Join(dir, "data-go")
	pyDataDir := filepath.Join(dir, "data-py")

	var outputs struct {
		Cases []struct {
			Kind   string         `json:"kind"`
			Text   string         `json:"text"`
			Doc    map[string]any `json:"doc"`
			Values []int          `json:"values"`
		} `json:"cases"`
	}
	inputs := map[string]any{
		"data_dir": pyDataDir,
		"cases":    cases,
	}
	// 参考侧需要 data_dir 字面量，改写为本次临时目录
	runPythonRef(t, refScriptBody, inputs, &outputs)
	if len(outputs.Cases) != len(cases) {
		t.Fatalf("case 数不对：%d", len(outputs.Cases))
	}

	for i, caseDef := range cases {
		kind, _ := caseDef["kind"].(string)
		switch kind {
		case "prepare":
			lanes := caseDef["lanes"].(int)
			pinRunning := caseDef["pin_running"].(bool)
			poolPath := filepath.Join(goDataDir, "proxypool", "crawl-pool.yaml")
			if err := os.MkdirAll(filepath.Dir(poolPath), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(poolPath, []byte(poolText), 0o644); err != nil {
				t.Fatal(err)
			}
			if rt, ok := caseDef["runtime_text"].(string); ok {
				rtPath := filepath.Join(goDataDir, "proxypool", "crawl-runtime.yaml")
				if err := os.WriteFile(rtPath, []byte(rt), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			_, goText, err := PrepareRuntimeConfig(goDataDir, lanes, pinRunning)
			if err != nil {
				t.Fatalf("case %d：Go 生成失败：%v", i, err)
			}
			goDoc, err := LoadYAML(goText)
			if err != nil {
				t.Fatalf("case %d：Go 产物解析失败：%v", i, err)
			}
			pyDoc := outputs.Cases[i].Doc
			if !pinRunning {
				maskDynamic(pyDoc)
				maskDynamic(goDoc)
			}
			pyNormalized := jsonNormalize(t, pyDoc)
			goNormalized := jsonNormalize(t, goDoc)
			if !deepEqualAny(pyNormalized, goNormalized) {
				pyJSON, _ := json.MarshalIndent(pyNormalized, "", " ")
				goJSON, _ := json.MarshalIndent(goNormalized, "", " ")
				t.Fatalf("case %d：运行配置不等价\nPython：%s\nGo：%s", i, pyJSON, goJSON)
			}
		case "lane_count":
			pairs := caseDef["pairs"].([][2]int)
			for j, pair := range pairs {
				if got := LaneCountFor(pair[0], pair[1]); got != outputs.Cases[i].Values[j] {
					t.Fatalf("lane_count %d：Go=%d Python=%d", j, got, outputs.Cases[i].Values[j])
				}
			}
		}
	}
}

// maskDynamic 掩掉冷通道的动态端点（密钥/控制器/混合口/全部 listener 端口），
// 只比结构。
func maskDynamic(doc map[string]any) {
	doc["secret"] = "*"
	doc["external-controller"] = "*"
	doc["mixed-port"] = 0
	if listeners, ok := doc["listeners"].([]any); ok {
		for _, entry := range listeners {
			if em, ok := entry.(map[string]any); ok {
				em["port"] = 0
			}
		}
	}
}
