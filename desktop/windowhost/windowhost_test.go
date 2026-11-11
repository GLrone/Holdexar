//go:build windows

package main

import (
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestPortFreeAndNegotiate(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	taken := listener.Addr().(*net.TCPAddr).Port
	if portFree(taken) {
		t.Fatalf("端口 %d 已被占用却判为空闲", taken)
	}
	free := taken + 1
	for !portFree(free) {
		free++
	}
	if !portFree(free) {
		t.Fatalf("端口 %d 空闲却判为占用", free)
	}
}

func TestNegotiatePortsSkipsForeignPort(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	taken := listener.Addr().(*net.TCPAddr).Port
	cfg := &options{
		appName:    "Holdexar",
		serverPort: taken,
		lockPort:   taken + 100,
		portFree:   portFree,
	}
	log := &hostLog{}
	negotiatePorts(cfg, log)
	if cfg.serverPort == taken {
		t.Fatal("外来占用端口未避让")
	}
	if !cfg.portFree(cfg.serverPort) {
		t.Fatalf("协商端口 %d 仍被占用", cfg.serverPort)
	}
}

func TestHealthIdentityRequiresDataDir(t *testing.T) {
	dir := t.TempDir()
	cfg := &options{appName: "Holdexar", dataDir: dir, portFree: portFree}
	payload := &healthSnapshot{Status: "ok", App: "Holdexar", PID: 1, DataDir: dir}
	if !identityMatches(payload, cfg) {
		t.Fatal("同应用同目录应判为本应用")
	}
	payload.DataDir = filepath.Join(dir, "other")
	if identityMatches(payload, cfg) {
		t.Fatal("不同数据目录不得判为本应用（并行隔离目录防线）")
	}
	payload.DataDir = dir
	payload.App = "Other"
	if identityMatches(payload, cfg) {
		t.Fatal("异名应用不得判为本应用")
	}
}

func TestBuildEntryURLRedactsNonceAndGuardsNext(t *testing.T) {
	entry := buildEntryURL("http://127.0.0.1:28765", "abc123", "")
	if entry != "http://127.0.0.1:28765/api/v1/session/bootstrap?nonce=abc123" {
		t.Fatalf("首登入口不合预期：%s", entry)
	}
	withNext := buildEntryURL("http://127.0.0.1:28765", "n", "/games/220?tab=price")
	if !contains(withNext, "next=%2Fgames%2F220") {
		t.Fatalf("回跳路径未正确编码：%s", withNext)
	}
	if buildEntryURL("http://127.0.0.1:1", "n", "//evil.com") != "http://127.0.0.1:1/api/v1/session/bootstrap?nonce=n" {
		t.Fatal("协议相对 next 必须被拒")
	}
	if buildEntryURL("http://127.0.0.1:1", "n", "https://evil.com") != "http://127.0.0.1:1/api/v1/session/bootstrap?nonce=n" {
		t.Fatal("绝对 next 必须被拒")
	}
	redacted := urlRedacted("http://127.0.0.1:1/api/v1/session/bootstrap?nonce=secret")
	if contains(redacted, "secret") {
		t.Fatalf("票据 nonce 不得进日志：%s", redacted)
	}
}

func TestNavigationPolicyAllowsOnlyExactOrigin(t *testing.T) {
	dir := t.TempDir()
	cfg := &options{appName: "Holdexar", dataDir: dir, portFree: portFree}
	log := openLog(dir)
	defer log.close()
	a := newApp(cfg, log, nil)
	var opened []string
	a.openExternal = func(uri string) { opened = append(opened, uri) }
	a.setAppURL("http://127.0.0.1:28765")
	cases := []struct {
		uri   string
		allow bool
	}{
		{"data:text/html,hi", true},
		{"about:blank", true},
		{"http://127.0.0.1:28765/api/v1/session/bootstrap?nonce=x", true},
		{"http://127.0.0.1:28765/games/220", true},
		{"http://localhost:28765/games/220", false},
		{"http://127.0.0.1:28766/games/220", false},
		{"https://store.steampowered.com/app/220", false},
		{"file:///C:/Windows/System32/calc.exe", false},
	}
	for _, c := range cases {
		if got := a.onNavigationStarting(c.uri); got != c.allow {
			t.Fatalf("%s 允许=%v，期望 %v", c.uri, got, c.allow)
		}
	}
	if len(opened) != 3 {
		t.Fatalf("外链应全部转交系统浏览器（%d）：%v", len(opened), opened)
	}
	if !contains(opened[2], "steampowered") {
		t.Fatalf("Steam 外链未转交：%v", opened)
	}
}

func TestBridgeProtocolRoundTrip(t *testing.T) {
	payload, ok := parseJSONMap(`{"jsonrpc":"2.0","id":"c1","method":"restart_app","params":[]}`)
	if !ok || payload["method"] != "restart_app" {
		t.Fatal("桥请求解析失败")
	}
	result := bridgeResult("c1", map[string]any{"ok": true})
	decoded, ok := parseJSONMap(result)
	if !ok || decoded["id"] != "c1" {
		t.Fatal("桥应答序列化失败")
	}
	errBody := bridgeError("c2", -32601, "未知桌面能力：x")
	decoded, ok = parseJSONMap(errBody)
	if !ok {
		t.Fatal("桥错误应答序列化失败")
	}
	errObj, _ := decoded["error"].(map[string]any)
	if errObj["code"] != float64(-32601) {
		t.Fatal("错误码不合预期")
	}
}

func TestReadTicketOnce(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "server-bootstrap.ticket")
	if nonce, err := readTicketOnce(path); err == nil || nonce != "" {
		t.Fatal("缺失票据应报错且为空")
	}
	if err := os.WriteFile(path, []byte(" nonce-1 \n"), 0o600); err != nil {
		t.Fatal(err)
	}
	nonce, err := readTicketOnce(path)
	if err != nil || nonce != "nonce-1" {
		t.Fatalf("票据读取失败：%q %v", nonce, err)
	}
	if got := readTicket(path, 50*time.Millisecond); got != "nonce-1" {
		t.Fatalf("轮询读取失败：%q", got)
	}
	if got := readTicket(filepath.Join(dir, "absent"), 50*time.Millisecond); got != "" {
		t.Fatalf("缺失票据超时后应为空：%q", got)
	}
}

func TestResolveDataDirPrecedence(t *testing.T) {
	t.Setenv("HOLDEXAR_DATA_DIR", "E:\\some\\dir")
	if got := resolveDataDir(""); got != `E:\some\dir` {
		t.Fatalf("显式数据目录优先：%q", got)
	}
	t.Setenv("HOLDEXAR_DATA_DIR", "")
	// 可写安装根（开发态）：开发候选 holdexar-dev 先于 data/
	root := t.TempDir()
	if got := resolveDataDir(root); got != filepath.Join(root, "holdexar-dev") {
		t.Fatalf("可写根应取开发候选：%q", got)
	}
	pointed := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "holdexar-dev-home.txt"), []byte(pointed+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := resolveDataDir(root); got != pointed {
		t.Fatalf("开发指针未生效：%q", got)
	}
	if err := os.WriteFile(filepath.Join(root, "portable.flag"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := resolveDataDir(root); got != filepath.Join(root, "data") {
		t.Fatalf("便携标记优先于开发指针：%q", got)
	}
	// 既有库的 data/ 优先于开发指针（存量接管）
	root2 := t.TempDir()
	pointed2 := t.TempDir()
	if err := os.WriteFile(filepath.Join(root2, "holdexar-dev-home.txt"), []byte(pointed2+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root2, "data"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root2, "data", dbName), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := resolveDataDir(root2); got != filepath.Join(root2, "data") {
		t.Fatalf("既有库目录优先于开发指针：%q", got)
	}
}

func TestSameDirPathCaseInsensitive(t *testing.T) {
	dir := t.TempDir()
	if !sameDirPath(dir, dir) {
		t.Fatal("同目录应相等")
	}
	if !sameDirPath(filepath.Dir(dir)+`\`+filepath.Base(dir), dir) {
		t.Fatal("大小写不敏感比较失败")
	}
	if sameDirPath(dir, dir+`-x`) {
		t.Fatal("不同目录不得判等")
	}
}

func TestServerExecutableResolution(t *testing.T) {
	dir := t.TempDir()
	exe := filepath.Join(dir, "serverd.exe")
	if err := os.WriteFile(exe, []byte("MZ"), 0o755); err != nil {
		t.Fatal(err)
	}
	cfg := &options{serverExe: exe, installRoot: dir}
	got, err := serverExecutable(cfg)
	if err != nil || got != exe {
		t.Fatalf("显式服务程序解析失败：%q %v", got, err)
	}
	cfg = &options{serverExe: filepath.Join(dir, "missing.exe"), installRoot: dir}
	if _, err := serverExecutable(cfg); err == nil {
		t.Fatal("显式缺失服务程序必须报错")
	}
	cfg = &options{installRoot: dir}
	got, err = serverExecutable(cfg)
	if err != nil || got != exe {
		t.Fatalf("相邻服务程序解析失败：%q %v", got, err)
	}
}

func TestURLPathWithQueryAndOrigin(t *testing.T) {
	if got := urlPathWithQuery("http://127.0.0.1:1/games/220?tab=price#sec"); got != "/games/220?tab=price#sec" {
		t.Fatalf("回跳目标不符：%q", got)
	}
	if got := urlOrigin("http://LocalHost:5173/x"); got != "http://localhost:5173" {
		t.Fatalf("源归一失败：%q", got)
	}
}

func contains(haystack, needle string) bool {
	return strings.Contains(haystack, needle)
}
