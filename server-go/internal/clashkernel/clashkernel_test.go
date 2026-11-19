package clashkernel

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestKernelEnvStripsOverrides(t *testing.T) {
	base := []string{
		"CLASH_API_SECRET=leak", "clash_secret=leak2",
		"MIHOMO_EXTERNAL_CONTROLLER=127.0.0.1:1",
		"SAFE_PATHS=1", "DISABLE_LOOPBACK_DETECTOR=1", "SKIP_SYSTEM_IPV6_CHECK=1",
		"PATH=C:\\Windows", "LANG=zh_CN.UTF-8", "HOLDEXAR_DATA_DIR=E:\\d",
	}
	env := KernelEnv(base)
	joined := strings.Join(env, "\n")
	for _, banned := range []string{"CLASH", "MIHOMO", "SAFE_PATHS", "LOOPBACK", "IPV6_CHECK"} {
		if strings.Contains(joined, banned) {
			t.Fatalf("环境变量 %s 应被剥离：%s", banned, joined)
		}
	}
	for _, keep := range []string{"PATH=C:\\Windows", "LANG=zh_CN.UTF-8", "HOLDEXAR_DATA_DIR=E:\\d"} {
		if !strings.Contains(joined, keep) {
			t.Fatalf("普通变量 %s 应原样保留：%s", keep, joined)
		}
	}
}

func TestParseKernelVersion(t *testing.T) {
	cases := map[string][]int{
		"Mihomo Meta v1.19.32 windows amd64 with go1.24": {1, 19, 32},
		"v1.19.3":      {1, 19, 3},
		"":             nil,
		"not a kernel": nil,
	}
	for raw, want := range cases {
		got := ParseKernelVersion(raw)
		if len(got) != len(want) {
			t.Fatalf("ParseKernelVersion(%q)=%v，期望 %v", raw, got, want)
		}
		for i := range got {
			if got[i] != want[i] {
				t.Fatalf("ParseKernelVersion(%q)=%v，期望 %v", raw, got, want)
			}
		}
	}
	if !versionAtLeast([]int{1, 19, 32}, []int{1, 19, 12}) {
		t.Fatal("同版本比较应视为不升级")
	}
	if versionAtLeast([]int{1, 19, 11}, []int{1, 19, 12}) {
		t.Fatal("低版本不应判为不低于")
	}
	if versionAtLeast(nil, []int{1, 19, 12}) {
		t.Fatal("版本不可辨不应判为不低于")
	}
}

func TestValidateConfigRejections(t *testing.T) {
	dir := t.TempDir()
	exePath := filepath.Join(dir, "no-such-kernel.exe")
	workDir := t.TempDir()

	admissionPath := filepath.Join(dir, "bad-admission.yaml")
	if err := os.WriteFile(admissionPath, []byte("proxies:\n- name: n\n  type: ss\n  server: s\n  port: 1\nrules:\n- GEOIP,CN,DIRECT\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	err := ValidateConfig(exePath, admissionPath, workDir)
	pre, ok := err.(*PrecheckError)
	if !ok || pre.Kind != PrecheckAdmission {
		t.Fatalf("准入拒绝应归类 admission，实际 %v", err)
	}

	unlaunchPath := filepath.Join(dir, "ok.yaml")
	if err := os.WriteFile(unlaunchPath, []byte("proxies:\n- name: n\n  type: ss\n  server: s\n  port: 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	fakeExe := filepath.Join(dir, "not-executable-content.exe")
	if err := os.WriteFile(fakeExe, []byte("definitely not a kernel"), 0o644); err != nil {
		t.Fatal(err)
	}
	err = ValidateConfig(fakeExe, unlaunchPath, workDir)
	pre, ok = err.(*PrecheckError)
	if !ok || (pre.Kind != PrecheckUnlaunchable && pre.Kind != PrecheckRejected) {
		t.Fatalf("不可执行应归类 unlaunchable/rejected，实际 %v", err)
	}
}

func TestConfirmConfigText(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(path, []byte("mode: global\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := ConfirmConfigText(path, "mode: global\n"); err != nil {
		t.Fatalf("一致文本应通过：%v", err)
	}
	err := ConfirmConfigText(path, "mode: rule\n")
	pre, ok := err.(*PrecheckError)
	if !ok || pre.Kind != PrecheckChanged {
		t.Fatalf("文本被改应归类 changed，实际 %v", err)
	}
	err = ConfirmConfigText(filepath.Join(dir, "missing.yaml"), "x")
	pre, ok = err.(*PrecheckError)
	if !ok || pre.Kind != PrecheckChanged {
		t.Fatalf("读不回应归类 changed，实际 %v", err)
	}
}

func TestEnsureKernelPlaceAndSkip(t *testing.T) {
	dir := t.TempDir()
	bundledDir := filepath.Join(dir, "assets")
	if err := os.MkdirAll(bundledDir, 0o755); err != nil {
		t.Fatal(err)
	}
	bundled := filepath.Join(bundledDir, KernelFilename())
	if err := os.WriteFile(bundled, []byte("placeholder"), 0o755); err != nil {
		t.Fatal(err)
	}
	dataDir := filepath.Join(dir, "data")
	res, err := EnsureKernel(bundledDir, dataDir)
	if err != nil {
		t.Fatalf("就位失败：%v", err)
	}
	if !res.KernelReady || len(res.Copied) != 1 || len(res.Missing) != 0 {
		t.Fatalf("首次就位应复制：%+v", res)
	}
	res, err = EnsureKernel(bundledDir, dataDir)
	if err != nil {
		t.Fatalf("二次就位失败：%v", err)
	}
	if len(res.Copied) != 0 || res.Upgraded != "" {
		t.Fatalf("同名不覆盖不应再复制或升级：%+v", res)
	}
	res, err = EnsureKernel(filepath.Join(dir, "no-assets"), filepath.Join(dir, "data2"))
	if err != nil {
		t.Fatalf("缺随包就位失败：%v", err)
	}
	if res.KernelReady || len(res.Missing) != 1 {
		t.Fatalf("缺随包应报 missing 且 not ready：%+v", res)
	}
}

func TestEnsureKernelAdoptsLegacyKernel(t *testing.T) {
	dir := t.TempDir()
	bundledDir := filepath.Join(dir, "assets")
	dataDir := filepath.Join(dir, "data")
	kernelPath := filepath.Join(dataDir, "clash")
	if err := os.MkdirAll(kernelPath, 0o755); err != nil {
		t.Fatal(err)
	}
	legacy := filepath.Join(kernelPath, legacyKernelFilename())
	if err := os.WriteFile(legacy, []byte("legacy-kernel-bytes"), 0o755); err != nil {
		t.Fatal(err)
	}
	res, err := EnsureKernel(bundledDir, dataDir)
	if err != nil {
		t.Fatalf("收编就位失败：%v", err)
	}
	if res.Adopted != legacyKernelFilename() {
		t.Fatalf("旧名内核应被收编：%+v", res)
	}
	if !fileExists(filepath.Join(kernelPath, KernelFilename())) {
		t.Fatal("收编后现名内核应就位")
	}
	if fileExists(legacy) {
		t.Fatal("收编后旧名文件应不存在")
	}
	if err := os.WriteFile(legacy, []byte("user-custom"), 0o755); err != nil {
		t.Fatal(err)
	}
	res, err = EnsureKernel(bundledDir, dataDir)
	if err != nil {
		t.Fatalf("二次就位失败：%v", err)
	}
	if res.Adopted != "" {
		t.Fatalf("新名已就位不应收编：%+v", res)
	}
	if !fileExists(legacy) {
		t.Fatal("用户自备旧名文件应保留")
	}
}

func TestEnsureRunningPreservesReloadRejectedError(t *testing.T) {
	dir := t.TempDir()
	cfgPath := filepath.Join(dir, "config.yaml")
	original := "proxies:\n  - name: node-a\n    type: socks5\n    server: 127.0.0.1\n    port: 1080\n"
	if err := os.WriteFile(cfgPath, []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}
	r := &Runtime{
		Process: &KernelProcess{done: make(chan struct{}), pid: 1},
	}
	_, err := r.EnsureRunning(filepath.Join(dir, "missing-mihomo.exe"), cfgPath, EnsureRunningOptions{MixedPort: 7890})
	if err == nil {
		t.Fatal("expected EnsureRunning error when kernel validation rejects")
	}
	var rejected *ReloadRejectedError
	if !errors.As(err, &rejected) {
		t.Fatalf("expected *ReloadRejectedError preserved for HTTP 400 mapping, got %T (%v)", err, err)
	}
	if !r.Process.Running() {
		t.Fatal("running process should remain untouched on ReloadRejectedError")
	}
	after, rerr := os.ReadFile(cfgPath)
	if rerr != nil || string(after) != original {
		t.Fatalf("config should remain intact before write: err=%v body=%q", rerr, string(after))
	}
}

func TestProcTCPTableOwnsLoopbackPort(t *testing.T) {
	tableV4 := "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n" +
		"   0: 0100007F:1ED2 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 42424 1 0000000000000000 100 0 0 10 0\n" +
		"   1: 00000000:1ED3 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 42425 1 0000000000000000 100 0 0 10 0\n" +
		"   2: 0100007F:1ED4 0100007F:C350 01 00000000:00000000 00:00000000 00000000  1000        0 42426 1 0000000000000000 20 4 30 10 -1\n"
	inodes := map[string]bool{"42424": true, "42425": true, "42426": true}
	if !procTCPTableOwnsLoopbackPort(tableV4, 7890, inodes) {
		t.Fatal("127.0.0.1 LISTEN socket with owned inode must match")
	}
	if procTCPTableOwnsLoopbackPort(tableV4, 7891, inodes) {
		t.Fatal("0.0.0.0 wildcard socket must not match loopback-only check")
	}
	if procTCPTableOwnsLoopbackPort(tableV4, 7892, inodes) {
		t.Fatal("ESTABLISHED socket (state 01) must not match")
	}
	if procTCPTableOwnsLoopbackPort(tableV4, 7890, map[string]bool{"99999": true}) {
		t.Fatal("unowned inode must not match")
	}

	tableV6 := "  sl  local_address                         rem_address                        st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n" +
		"   0: 0000000000000000FFFF00000100007F:1ED2 00000000000000000000000000000000:0000 0A 00000000:00000000 00:00000000 00000000  1000        0 55555 1 0000000000000000 100 0 0 10 0\n"
	if !procTCPTableOwnsLoopbackPort(tableV6, 7890, map[string]bool{"55555": true}) {
		t.Fatal("IPv4-mapped loopback ::ffff:127.0.0.1 LISTEN socket must match")
	}
}
