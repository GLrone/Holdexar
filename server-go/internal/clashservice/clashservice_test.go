package clashservice

import (
	"archive/zip"
	"bytes"
	"context"
	"database/sql"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/store"
)

// ─── 夹具 ────────────────────────────────────────────────────

// subYAML 可渲染的最小订阅原文（RenderSubscriptionConfig / ParseNodes 双消费）。
const subYAML = `mixed-port: 7890
mode: rule
tun:
  enable: true
rules:
  - MATCH,DIRECT
proxies:
  - name: 节点A
    type: ss
    server: 1.2.3.4
    port: 443
    cipher: aes-128-gcm
    password: pw
  - name: 节点B
    type: ss
    server: 5.6.7.8
    port: 443
    cipher: aes-128-gcm
    password: pw
`

func newFixture(t *testing.T) (*Service, *stubRuntime, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "fixture.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	kernel := &stubRuntime{}
	svc := &Service{
		DB: db, Gate: store.NewWriteGate(db, path), Kernel: kernel,
		DataDir: dir, Now: func() time.Time { return time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC) },
	}
	return svc, kernel, dir
}

// seedSubscription 播种一条 clash 订阅行（stats 传空串 = NULL）。
func seedSubscription(t *testing.T, svc *Service, id int64, url, label, stats string, deprecated bool) {
	t.Helper()
	sealed, err := proxypool.SealURL(svc.DataDir, url)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	err = svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `INSERT INTO proxy_subscriptions
			(id,kind,url,label,created_at,last_stats,deprecated,admission_status,auto_refresh)
			VALUES (?,'clash',?,?,?,?,?,'ACTIVE',1)`,
			id, sealed, nullText(label), "2026-11-14 10:00:00.000000", nullText(stats), deprecated)
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
}

// seedSnapshot 播种一条成功快照（经真实 PersistSnapshot，raw 落盘 + sha 校验齐全）。
func seedSnapshot(t *testing.T, svc *Service, subID int64, url string, raw []byte, title, userinfo string) {
	t.Helper()
	nodes, err := proxypool.ParseNodes(raw, proxypool.FormatYAML)
	if err != nil {
		t.Fatal(err)
	}
	snap := proxypool.BuildSnapshot(subID, url, &proxypool.FetchResult{
		Raw: raw, HTTPStatus: 200, Fmt: proxypool.FormatYAML, Channel: "直连",
		ProfileTitle: title, Userinfo: userinfo,
	}, nodes, time.Now())
	ctx := context.Background()
	conn, err := svc.DB.Conn(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := proxypool.PersistSnapshot(ctx, conn, svc.DataDir, snap); err != nil {
		t.Fatal(err)
	}
}

func nullText(v string) any {
	if v == "" {
		return nil
	}
	return v
}

func writeExe(t *testing.T, dir string) string {
	t.Helper()
	exe := clashkernel.KernelExe(dir)
	if err := os.WriteFile(exe, []byte("fake-kernel-bytes"), 0o755); err != nil {
		t.Fatal(err)
	}
	return exe
}

// statusErrOf 断言错误是带 HTTP 语义的 StatusError 并返回其状态码与文案。
func statusErrOf(t *testing.T, err error) (*StatusError, string) {
	t.Helper()
	if err == nil {
		t.Fatal("期望出错，实际为 nil")
	}
	se, ok := err.(*StatusError)
	if !ok {
		t.Fatalf("期望 StatusError，实际 %T: %v", err, err)
	}
	return se, se.Detail
}

// ─── 内核桩 ──────────────────────────────────────────────────

type stubRuntime struct {
	mu         sync.Mutex
	st         clashkernel.Status
	runningSub string
	startOpts  []clashkernel.StartOptions
	ensureOpts []clashkernel.EnsureRunningOptions
	stopCount  int
	startFn    func(clashkernel.StartOptions) (clashkernel.Status, error)
	ensureFn   func(clashkernel.EnsureRunningOptions) (clashkernel.EnsureRunResult, error)
}

func runningStatus(configPath string) clashkernel.Status {
	return clashkernel.Status{
		Running: true, Port: 18347, EntryReady: true,
		ConfigPath: configPath, ControllerURL: "http://127.0.0.1:19090",
	}
}

func (r *stubRuntime) Start(exePath, configPath string, opts clashkernel.StartOptions) (clashkernel.Status, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.startOpts = append(r.startOpts, opts)
	if r.startFn != nil {
		return r.startFn(opts)
	}
	r.st = runningStatus(configPath)
	r.st.SubscriptionURL = opts.SubscriptionURL
	r.runningSub = opts.SubscriptionURL
	return r.st, nil
}

func (r *stubRuntime) EnsureRunning(exePath, configPath string, opts clashkernel.EnsureRunningOptions) (clashkernel.EnsureRunResult, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.ensureOpts = append(r.ensureOpts, opts)
	if r.ensureFn != nil {
		return r.ensureFn(opts)
	}
	r.st = runningStatus(configPath)
	r.st.SubscriptionURL = opts.SubscriptionURL
	r.runningSub = opts.SubscriptionURL
	return clashkernel.EnsureRunResult{Status: r.st, Reloaded: true}, nil
}

func (r *stubRuntime) Stop() clashkernel.Status {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.stopCount++
	r.st = clashkernel.Status{ConfigPath: r.st.ConfigPath}
	r.runningSub = ""
	return r.st
}

func (r *stubRuntime) Status() clashkernel.Status {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.st
}

func (r *stubRuntime) RunningSubscriptionIs(subURL string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.st.Running && r.runningSub != "" && r.runningSub == subURL
}

// ─── 状态面 ──────────────────────────────────────────────────

func TestStatusPayloadDetectAndSelected(t *testing.T) {
	svc, _, dir := newFixture(t)
	ctx := context.Background()

	payload := svc.StatusPayload(ctx)
	if payload.Kernel.Found || payload.Kernel.Builtin || payload.Kernel.Path != nil {
		t.Fatalf("未装内核应 found=false/builtin=false/path=null：%+v", payload.Kernel)
	}
	if payload.Version != nil {
		t.Fatalf("未装内核 version 应为 null：%v", *payload.Version)
	}
	if payload.SelectedSubscriptionID != nil {
		t.Fatalf("无点选记录 selectedSubscriptionId 应为 null")
	}

	exe := writeExe(t, dir)
	svc.VersionProbe = func(string) string { return "mihomo v1.19.32" }
	if err := svc.RememberSelectedSub(ctx, 7); err != nil {
		t.Fatal(err)
	}
	payload = svc.StatusPayload(ctx)
	if !payload.Kernel.Found || !payload.Kernel.Builtin || payload.Kernel.Path == nil || *payload.Kernel.Path != exe {
		t.Fatalf("装了内核应 found=true/builtin=true（builtin 恒 true）：%+v", payload.Kernel)
	}
	if payload.Version == nil || *payload.Version != "mihomo v1.19.32" {
		t.Fatalf("version 探测未进快照：%v", payload.Version)
	}
	if payload.SelectedSubscriptionID == nil || *payload.SelectedSubscriptionID != 7 {
		t.Fatalf("selectedSubscriptionId 往返失败：%v", payload.SelectedSubscriptionID)
	}
	if payload.KernelDir != filepath.Join(dir, "clash") {
		t.Fatalf("kernelDir 不符：%s", payload.KernelDir)
	}
}

func TestSelectedSubIDRejectsBadValue(t *testing.T) {
	svc, _, _ := newFixture(t)
	ctx := context.Background()
	if err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) VALUES (?,?,?)`,
			SelectedSubKey, `abc`, "2026-11-14 10:00:00.000000")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if got := svc.SelectedSubID(ctx); got != nil {
		t.Fatalf("非法值应返回 nil，实际 %v", *got)
	}
}

// ─── 安装面 ──────────────────────────────────────────────────

func buildZip(t *testing.T, name, data string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	w, err := zw.Create(name)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write([]byte(data)); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	return buf.Bytes()
}

func TestInstallBundledFirst(t *testing.T) {
	svc, _, dir := newFixture(t)
	bundled := t.TempDir()
	if err := os.WriteFile(filepath.Join(bundled, clashkernel.KernelFilename()), []byte("bundled"), 0o755); err != nil {
		t.Fatal(err)
	}
	svc.BundledDir = bundled

	result := svc.InstallKernel(context.Background())
	if !result.OK || result.Source == nil || *result.Source != "bundled" || result.Via == nil || *result.Via != "随包资产" {
		t.Fatalf("随包优先分支不符：%+v", result)
	}
	exe := clashkernel.KernelExe(dir)
	if result.Path == nil || *result.Path != exe {
		t.Fatalf("安装路径不符：%v", result.Path)
	}
	raw, err := os.ReadFile(exe)
	if err != nil || string(raw) != "bundled" {
		t.Fatalf("随包内核未复制到 data/clash/：%v", err)
	}
}

func TestInstallProgressStates(t *testing.T) {
	progress := &progressStore{}
	// 无记录 = 全零值
	zero := progress.snapshot()
	if zero.Running || zero.OK || zero.Phase != nil || zero.Percent != nil || zero.Received != 0 ||
		zero.Total != nil || zero.Source != nil || zero.Via != nil || zero.Error != nil {
		t.Fatalf("无记录应为全零值形态：%+v", zero)
	}
	progress.record(clashkernel.Progress{Running: true})
	progress.record(clashkernel.Progress{Running: true, Phase: "连接下载源", Source: "github", Via: "直连"})
	progress.record(clashkernel.Progress{Running: true, Phase: "下载内核", Received: 100, Total: 200, Percent: 50, Source: "github", Via: "直连"})
	mid := progress.snapshot()
	if !mid.Running || mid.Phase == nil || *mid.Phase != "下载内核" || mid.Received != 100 || mid.Total == nil || *mid.Total != 200 ||
		mid.Percent == nil || *mid.Percent != 50 || mid.Source == nil || *mid.Source != "github" || mid.Via == nil || *mid.Via != "直连" {
		t.Fatalf("下载中快照不符：%+v", mid)
	}
	// 完成：终帧只带 ok/percent，合并语义保留 phase/received 等字段
	progress.record(clashkernel.Progress{OK: true, Percent: 100})
	done := progress.snapshot()
	if done.Running || !done.OK || done.Percent == nil || *done.Percent != 100 ||
		done.Phase == nil || *done.Phase != "下载内核" || done.Received != 100 {
		t.Fatalf("完成快照不符：%+v", done)
	}
	// 失败：error 落账，running 归 false，其余字段保留
	progress.record(clashkernel.Progress{Error: "所有下载源均失败，可手动放置内核到 data/clash/"})
	failed := progress.snapshot()
	if failed.Running || failed.OK || failed.Error == nil || *failed.Error != "所有下载源均失败，可手动放置内核到 data/clash/" ||
		failed.Phase == nil || *failed.Phase != "下载内核" {
		t.Fatalf("失败快照不符：%+v", failed)
	}
}

func TestInstallDownloadViaMirror(t *testing.T) {
	svc, _, dir := newFixture(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(buildZip(t, "mihomo.exe", "kernel-bytes"))
	}))
	defer srv.Close()
	svc.InstallMirrors = []string{srv.URL + "/"}

	result := svc.InstallKernel(context.Background())
	if !result.OK || result.Source == nil || *result.Source != srv.URL+"/" || result.Via == nil || *result.Via != "直连" {
		t.Fatalf("下载分支结果不符：%+v", result)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "clash", clashkernel.KernelFilename()))
	if err != nil || string(raw) != "kernel-bytes" {
		t.Fatalf("内核未按 zip 内容落盘：%v", err)
	}
	progress := svc.InstallProgress()
	if !progress.OK || progress.Running || progress.Percent == nil || *progress.Percent != 100 {
		t.Fatalf("下载完成后的进度快照不符：%+v", progress)
	}
}

func TestInstallAllMirrorsFailed(t *testing.T) {
	svc, _, _ := newFixture(t)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srv.Close()
	svc.InstallMirrors = []string{srv.URL + "/"}

	result := svc.InstallKernel(context.Background())
	if result.OK || result.Error == nil ||
		*result.Error != "所有下载源均失败，可手动放置内核到 data/clash/（文件名 holdexar-mihomo.exe）" {
		t.Fatalf("全灭文案不符：%+v", result)
	}
	progress := svc.InstallProgress()
	if progress.Running || progress.OK || progress.Error == nil || *progress.Error != *result.Error {
		t.Fatalf("失败后的进度快照不符：%+v", progress)
	}
}

// ─── subcache 面 ─────────────────────────────────────────────

func TestSubscriptionConfigPathNaming(t *testing.T) {
	dir := t.TempDir()
	path := SubscriptionConfigPath(dir, 5)
	if path != filepath.Join(dir, "clash", "clash-sub-5.yaml") {
		t.Fatalf("缓存命名不符：%s", path)
	}
	if id, ok := SubscriptionIDOf(path); !ok || id != 5 {
		t.Fatalf("反解失败：%d %v", id, ok)
	}
	if _, ok := SubscriptionIDOf(filepath.Join(dir, "config.yaml")); ok {
		t.Fatalf("共享单文件不应反解出订阅 id")
	}
	if _, ok := SubscriptionIDOf(filepath.Join(dir, "clash-sub-abc.yaml")); ok {
		t.Fatalf("非法命名不应反解出订阅 id")
	}
}

func TestMaterializeSubscriptionConfig(t *testing.T) {
	dir := t.TempDir()
	rawPath := filepath.Join(dir, "clash-sub-5.yaml")
	if err := os.WriteFile(rawPath, []byte(subYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	out, err := MaterializeSubscriptionConfig(rawPath)
	if err != nil {
		t.Fatal(err)
	}
	if out != filepath.Join(dir, "clash-sub-5.runtime.yaml") {
		t.Fatalf("运行配置命名不符：%s", out)
	}
	raw, err := os.ReadFile(out)
	if err != nil {
		t.Fatal(err)
	}
	text := string(raw)
	if !bytes.Contains(raw, []byte("mode: global")) || !bytes.Contains(raw, []byte("节点A")) {
		t.Fatalf("运行配置缺基础形态：%s", text)
	}
	if bytes.Contains(raw, []byte("tun:")) || bytes.Contains(raw, []byte("MATCH,DIRECT")) {
		t.Fatalf("规则/接管段不应进运行配置：%s", text)
	}
	// 原文缓存保持不动
	original, _ := os.ReadFile(rawPath)
	if !bytes.Contains(original, []byte("MATCH,DIRECT")) {
		t.Fatalf("原文缓存被改写")
	}
}

func TestMaterializeRejectsNodelessConfig(t *testing.T) {
	dir := t.TempDir()
	rawPath := filepath.Join(dir, "clash-sub-6.yaml")
	if err := os.WriteFile(rawPath, []byte("mixed-port: 7890\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err := MaterializeSubscriptionConfig(rawPath)
	se, detail := statusErrOf(t, err)
	if se.Status != 400 || detail == "" {
		t.Fatalf("无节点配置应 400 语义错误：%+v", se)
	}
}

func TestFetchSubscriptionToCacheGuard(t *testing.T) {
	svc, _, _ := newFixture(t)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "A", "", false)
	ctx := context.Background()

	_, err := svc.FetchSubscriptionToCache(ctx, 1, "https://sub.example.com/other")
	se, detail := statusErrOf(t, err)
	if se.Status != 400 || detail != "订阅地址与缓存归属不一致" {
		t.Fatalf("归属不一致守卫未生效：%+v", se)
	}
	_, err = svc.FetchSubscriptionToCache(ctx, 99, "https://sub.example.com/a")
	if se, _ := err.(*StatusError); se == nil || se.Status != 400 {
		t.Fatalf("订阅不存在应归属守卫拦下：%v", err)
	}
}

func TestFetchSubscriptionToCacheRestoresSnapshot(t *testing.T) {
	svc, _, dir := newFixture(t)
	url := "https://sub.example.com/a"
	seedSubscription(t, svc, 1, url, "A", "", false)
	seedSnapshot(t, svc, 1, url, []byte(subYAML), "面板A", "upload=1; download=2; total=10")

	fetch, err := svc.FetchSubscriptionToCache(context.Background(), 1, url)
	if err != nil {
		t.Fatal(err)
	}
	if fetch.Title != "面板A" || fetch.Userinfo != "upload=1; download=2; total=10" || fetch.Nodes != 2 {
		t.Fatalf("快照恢复元数据不符：%+v", fetch)
	}
	raw, err := os.ReadFile(SubscriptionConfigPath(dir, 1))
	if err != nil || !bytes.Contains(raw, []byte("节点A")) {
		t.Fatalf("缓存正文未写盘：%v", err)
	}
}

func TestSubscriptionAutoNameFromURL(t *testing.T) {
	cases := map[string]string{
		"https://panel.example.com/api/v1/client/subscribe?token=abcdef": "subscribe",
		"https://panel.example.com/dl/my-airport-sub":                    "my-airport-sub",
		"https://panel.example.com/" + "abcdef0123456789abcdef0123456789": "", // 凭据末段不当名字
		"https://panel.example.com/":                                      "", // 根路径无名字
	}
	for input, want := range cases {
		if got := subscriptionAutoNameFromURL(input); got != want {
			t.Fatalf("autoName(%q) = %q, want %q", input, got, want)
		}
	}
}

func TestBriefErrorCollapsesAndTruncates(t *testing.T) {
	long := ""
	for i := 0; i < 300; i++ {
		long += "字"
	}
	got := briefError(fmt.Errorf("a\n  b\tc"))
	if got != "a b c" {
		t.Fatalf("多行折叠不符：%q", got)
	}
	if len([]rune(briefError(fmt.Errorf("%s", long)))) != 158 {
		t.Fatalf("超长应截断到 157 字符加省略号")
	}
}
