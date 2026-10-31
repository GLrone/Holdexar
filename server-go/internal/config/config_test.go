package config

import (
	"os"
	"path/filepath"
	"testing"
)

// unsetForTest 恢复现场地删除单个环境变量（t.Setenv 只能设值不能删除）。
func unsetForTest(t *testing.T, key string) {
	t.Helper()
	saved, had := os.LookupEnv(key)
	if err := os.Unsetenv(key); err != nil {
		t.Fatalf("无法删除环境变量 %s：%v", key, err)
	}
	t.Cleanup(func() {
		if had {
			os.Setenv(key, saved)
		}
	})
}

func TestResolveDataDirEnvOverride(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "data-custom")
	t.Setenv("HOLDEXAR_DATA_DIR", dir)
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("环境变量覆盖应直接生效：%v", err)
	}
	if want := absPath(dir); got != want {
		t.Fatalf("数据目录=%s，应为 %s", got, want)
	}
	if info, err := os.Stat(filepath.Join(got, "logs")); err != nil || !info.IsDir() {
		t.Fatalf("logs 子目录未创建：%v", err)
	}
}

func TestResolveDataDirEnvBlankIgnored(t *testing.T) {
	root := absPath(t.TempDir())
	machineRoot := absPath(t.TempDir())
	t.Setenv("HOLDEXAR_DATA_DIR", "   ")
	t.Setenv("HOLDEXAR_INSTALL_ROOT", root)
	t.Setenv("LOCALAPPDATA", machineRoot)
	t.Setenv("APPDATA", "")
	t.Setenv("USERPROFILE", machineRoot)
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("空白环境变量应被跳过：%v", err)
	}
	if want := filepath.Join(machineRoot, "holdexar-dev"); got != want {
		t.Fatalf("数据目录=%s，应回落开发态机器级候选 %s", got, want)
	}
}

func TestResolveDataDirPortableFlag(t *testing.T) {
	root := absPath(t.TempDir())
	t.Setenv("HOLDEXAR_DATA_DIR", "")
	t.Setenv("HOLDEXAR_INSTALL_ROOT", root)
	if err := os.WriteFile(filepath.Join(root, PortableFlag), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("便携声明应启用程序目录 data/：%v", err)
	}
	if want := filepath.Join(root, "data"); got != want {
		t.Fatalf("数据目录=%s，应为 %s", got, want)
	}
}

func TestResolveDataDirExistingDB(t *testing.T) {
	root := absPath(t.TempDir())
	t.Setenv("HOLDEXAR_DATA_DIR", "")
	t.Setenv("HOLDEXAR_INSTALL_ROOT", root)
	if err := os.MkdirAll(filepath.Join(root, "data"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "data", DBFileName), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("存量库应原地续用：%v", err)
	}
	if want := filepath.Join(root, "data"); got != want {
		t.Fatalf("数据目录=%s，应为 %s", got, want)
	}
}

func TestResolveDataDirDevPointer(t *testing.T) {
	root := absPath(t.TempDir())
	target := filepath.Join(absPath(t.TempDir()), "dev-home")
	t.Setenv("HOLDEXAR_DATA_DIR", "")
	t.Setenv("HOLDEXAR_INSTALL_ROOT", root)
	if err := os.WriteFile(filepath.Join(root, DevPointer), []byte(target+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("指针文件应生效：%v", err)
	}
	if got != target {
		t.Fatalf("数据目录=%s，应为指针原文 %s", got, target)
	}
	if info, err := os.Stat(filepath.Join(got, "logs")); err != nil || !info.IsDir() {
		t.Fatalf("logs 子目录未创建：%v", err)
	}
}

func TestResolveDataDirDevPointerWithBOMAndComments(t *testing.T) {
	root := absPath(t.TempDir())
	target := filepath.Join(absPath(t.TempDir()), "dev-home")
	t.Setenv("HOLDEXAR_DATA_DIR", "")
	t.Setenv("HOLDEXAR_INSTALL_ROOT", root)
	content := "\uFEFF# 开发态数据目录\n\n" + target + "\n"
	if err := os.WriteFile(filepath.Join(root, DevPointer), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("带 BOM 与注释行的指针应生效：%v", err)
	}
	if got != target {
		t.Fatalf("数据目录=%s，应为指针原文 %s", got, target)
	}
}

func TestResolveDataDirRelativePointerIgnored(t *testing.T) {
	root := absPath(t.TempDir())
	machineRoot := absPath(t.TempDir())
	t.Setenv("HOLDEXAR_DATA_DIR", "")
	t.Setenv("HOLDEXAR_INSTALL_ROOT", root)
	t.Setenv("LOCALAPPDATA", machineRoot)
	t.Setenv("APPDATA", "")
	t.Setenv("USERPROFILE", machineRoot)
	if err := os.WriteFile(filepath.Join(root, DevPointer), []byte("relative/path\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("非绝对指针应被忽略：%v", err)
	}
	if want := filepath.Join(machineRoot, "holdexar-dev"); got != want {
		t.Fatalf("数据目录=%s，应为 %s", got, want)
	}
}

func TestResolveDataDirMachineCandidateOrder(t *testing.T) {
	root := absPath(t.TempDir())
	local := absPath(t.TempDir())
	roaming := absPath(t.TempDir())
	home := absPath(t.TempDir())
	t.Setenv("HOLDEXAR_DATA_DIR", "")
	t.Setenv("HOLDEXAR_INSTALL_ROOT", root)
	t.Setenv("LOCALAPPDATA", local)
	t.Setenv("APPDATA", roaming)
	t.Setenv("USERPROFILE", home)
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("机器级候选应按 LOCALAPPDATA 优先：%v", err)
	}
	if want := filepath.Join(local, "holdexar-dev"); got != want {
		t.Fatalf("数据目录=%s，应为 %s", got, want)
	}
}

func TestInstallRootFromExeLayouts(t *testing.T) {
	root := filepath.Join(`E:`, "install")
	cases := map[string]string{
		filepath.Join(root, "versions", "0.3.6"): root,
		filepath.Join(root, "updater"):           root,
		root:                                     root,
	}
	for exeDir, want := range cases {
		if got := installRootFromExe(exeDir); got != want {
			t.Fatalf("exe 目录 %s 的安装根=%s，应为 %s", exeDir, got, want)
		}
	}
}

func TestEnsureWritableRejectsFile(t *testing.T) {
	blocker := filepath.Join(t.TempDir(), "blocked")
	if err := os.WriteFile(blocker, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := ensureWritable(blocker); err == nil {
		t.Fatal("普通文件路径应判定为不可写")
	}
}

func TestVersionEnvOverride(t *testing.T) {
	t.Setenv("HOLDEXAR_VERSION", "9.9.9-test")
	if got := Version(); got != "9.9.9-test" {
		t.Fatalf("版本=%s，应为环境变量覆盖值", got)
	}
}

func TestAppNameDefault(t *testing.T) {
	unsetForTest(t, "HOLDEXAR_APP_NAME")
	if got := AppName(); got != "Holdexar" {
		t.Fatalf("品牌名=%s，应为默认值", got)
	}
}
