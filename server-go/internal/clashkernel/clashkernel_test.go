package clashkernel

// 单元测试：环境清洗、版本解析、预检分类与配置文本一致性闸。

import (
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

	// 准入拒绝（地理规则）在跑内核之前就被拦下
	admissionPath := filepath.Join(dir, "bad-admission.yaml")
	if err := os.WriteFile(admissionPath, []byte("proxies:\n- name: n\n  type: ss\n  server: s\n  port: 1\nrules:\n- GEOIP,CN,DIRECT\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	err := ValidateConfig(exePath, admissionPath, workDir)
	pre, ok := err.(*PrecheckError)
	if !ok || pre.Kind != PrecheckAdmission {
		t.Fatalf("准入拒绝应归类 admission，实际 %v", err)
	}

	// 内核拒载（协议不识别）需要真内核，由 kernel_live_test 覆盖；
	// 这里验证不可执行文件的 unlaunchable 分类。
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
	// 非内核文件：-v 解析不出版本 → 版本不可辨保留（升级不夺回的不可辨分支）
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
	// 随包缺失
	res, err = EnsureKernel(filepath.Join(dir, "no-assets"), filepath.Join(dir, "data2"))
	if err != nil {
		t.Fatalf("缺随包就位失败：%v", err)
	}
	if res.KernelReady || len(res.Missing) != 1 {
		t.Fatalf("缺随包应报 missing 且 not ready：%+v", res)
	}
}
