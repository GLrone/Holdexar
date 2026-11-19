package config

import (
	"os"
	"path/filepath"
	"testing"
)

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
	t.Setenv("APPDATA", machineRoot)
	t.Setenv("USERPROFILE", machineRoot)
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("空白环境变量应被跳过：%v", err)
	}
	if want := filepath.Join(root, "holdexar-dev"); got != want {
		t.Fatalf("数据目录=%s，应回落开发态目录 %s", got, want)
	}
	if _, err := os.Stat(filepath.Join(machineRoot, "holdexar-dev")); !os.IsNotExist(err) {
		t.Fatalf("不得触碰 LOCALAPPDATA：%v", err)
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
	t.Setenv("APPDATA", machineRoot)
	t.Setenv("USERPROFILE", machineRoot)
	if err := os.WriteFile(filepath.Join(root, DevPointer), []byte("relative/path\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ResolveDataDir()
	if err != nil {
		t.Fatalf("非绝对指针应被忽略：%v", err)
	}
	if want := filepath.Join(root, "holdexar-dev"); got != want {
		t.Fatalf("数据目录=%s，应为 %s", got, want)
	}
}

func TestResolveDataDirIgnoresAppDataAndDefaultsByMode(t *testing.T) {
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
		t.Fatalf("开发态默认解析失败：%v", err)
	}
	if want := filepath.Join(root, "holdexar-dev"); got != want {
		t.Fatalf("开发态数据目录=%s，应为 %s", got, want)
	}
	for _, base := range []string{local, roaming, home} {
		entries, _ := os.ReadDir(base)
		if len(entries) != 0 {
			t.Fatalf("不得写入机器用户目录 %s：%v", base, entries)
		}
	}

	origExe := executablePath
	t.Cleanup(func() { executablePath = origExe })
	for _, relExe := range []string{
		filepath.Join("versions", "0.4.0", "serverd.exe"),
		filepath.Join("versions", "0.4.0", "_internal", "serverd.exe"),
	} {
		pkgRoot := absPath(t.TempDir())
		t.Setenv("HOLDEXAR_INSTALL_ROOT", "")
		executablePath = func() (string, error) { return filepath.Join(pkgRoot, relExe), nil }
		gotPkg, err := ResolveDataDir()
		if err != nil {
			t.Fatalf("打包态 (%s) 解析失败：%v", relExe, err)
		}
		if want := filepath.Join(pkgRoot, "data"); gotPkg != want {
			t.Fatalf("打包态 (%s) 数据目录=%s，应为 %s", relExe, gotPkg, want)
		}
	}
}

func TestInstallRootFromExeLayouts(t *testing.T) {
	origExe := executablePath
	t.Cleanup(func() { executablePath = origExe })
	t.Setenv("HOLDEXAR_INSTALL_ROOT", "")

	root := absPath(t.TempDir())
	cases := []string{
		filepath.Join(root, "versions", "0.3.6", "serverd.exe"),
		filepath.Join(root, "versions", "0.3.6", "_internal", "serverd.exe"),
		filepath.Join(root, "updater", "UpdateHost.exe"),
		filepath.Join(root, "serverd.exe"),
	}
	for _, exe := range cases {
		executablePath = func() (string, error) { return exe, nil }
		got, err := InstallRoot()
		if err != nil {
			t.Fatalf("InstallRoot(%s) 报错：%v", exe, err)
		}
		if got != root {
			t.Fatalf("exe %s 的安装根=%s，应为 %s", exe, got, root)
		}
	}
}

func TestWebDistDirPrefersVersionLocalOverRoot(t *testing.T) {
	origExe := executablePath
	t.Cleanup(func() { executablePath = origExe })

	writeIndex := func(dir string) string {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "index.html"), []byte("<html></html>"), 0o644); err != nil {
			t.Fatal(err)
		}
		return dir
	}

	root := absPath(t.TempDir())
	rootDist := writeIndex(filepath.Join(root, "web", "dist"))
	verDir := filepath.Join(root, "versions", "0.4.0")
	verDist := writeIndex(filepath.Join(verDir, "web", "dist"))
	t.Setenv("HOLDEXAR_INSTALL_ROOT", root)

	for _, exe := range []string{
		filepath.Join(verDir, "serverd.exe"),
		filepath.Join(verDir, "_internal", "serverd.exe"),
	} {
		executablePath = func() (string, error) { return exe, nil }
		got, err := WebDistDir()
		if err != nil {
			t.Fatalf("WebDistDir(%s) 报错：%v", exe, err)
		}
		if got != verDist {
			t.Fatalf("WebDistDir(%s)=%s，应优先版本本地 %s（根级 %s）", exe, got, verDist, rootDist)
		}
	}

	devExe := filepath.Join(absPath(t.TempDir()), "bin", "serverd.exe")
	executablePath = func() (string, error) { return devExe, nil }
	gotDev, err := WebDistDir()
	if err != nil {
		t.Fatalf("开发态 WebDistDir 报错：%v", err)
	}
	if gotDev != rootDist {
		t.Fatalf("开发态 WebDistDir=%s，应回退根级 %s", gotDev, rootDist)
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
