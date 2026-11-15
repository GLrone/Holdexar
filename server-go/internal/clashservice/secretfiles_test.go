package clashservice

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

// secretTree 造 clash/proxypool 子树：可重建明文件、原文缓存与嵌套目录。
func secretTree(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	write := func(rel, content string) {
		path := filepath.Join(root, rel)
		if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write("clash/config.runtime.yaml", "runtime")
	write("clash/scratch.tmp", "tmp")
	write("clash/sub/nested.runtime.yaml", "nested")
	write("clash/subscription-origin.yaml", "origin") // 原文：事实源，永不清理
	write("proxypool/pool.runtime.yaml", "runtime")
	write("proxypool/keep.yaml", "origin")
	return root
}

// 清扫删净 *.runtime.yaml 与 *.tmp，保留原文 .yaml。
func TestSweepTransientRemovesRebuildableFiles(t *testing.T) {
	root := secretTree(t)
	removed := SweepTransient(root)
	if removed != 4 {
		t.Fatalf("应删除 4 个可重建明文件，实际 %d", removed)
	}
	for _, gone := range []string{
		"clash/config.runtime.yaml", "clash/scratch.tmp",
		"clash/sub/nested.runtime.yaml", "proxypool/pool.runtime.yaml",
	} {
		if _, err := os.Stat(filepath.Join(root, gone)); !os.IsNotExist(err) {
			t.Fatalf("%s 应已被清扫", gone)
		}
	}
	for _, kept := range []string{"clash/subscription-origin.yaml", "proxypool/keep.yaml"} {
		if _, err := os.Stat(filepath.Join(root, kept)); err != nil {
			t.Fatalf("原文 %s 不得被清理: %v", kept, err)
		}
	}
}

// POSIX 权限收紧：文件 0600、目录 0700（Windows 跳过）。
func TestTightenAllPOSIX(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX 权限语义仅 POSIX 生效")
	}
	root := secretTree(t)
	count := TightenAll(root)
	if count != 6 {
		t.Fatalf("应收紧 6 个文件，实际 %d", count)
	}
	check := func(rel string, mode os.FileMode) {
		t.Helper()
		info, err := os.Stat(filepath.Join(root, rel))
		if err != nil {
			t.Fatal(err)
		}
		if info.Mode().Perm() != mode {
			t.Fatalf("%s 权限应为 %o，实际 %o", rel, mode, info.Mode().Perm())
		}
	}
	check("clash/subscription-origin.yaml", 0o600)
	check("clash/sub", 0o700)
	check("proxypool/keep.yaml", 0o600)
}

// Windows no-op：不在 POSIX 平台上验证（本平台编译守卫）。
func TestTightenAllWindowsNoOp(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("no-op 语义仅 Windows 生效")
	}
	root := secretTree(t)
	if count := TightenAll(root); count != 0 {
		t.Fatalf("Windows 下应为 no-op，实际处理 %d 个", count)
	}
	// 权限未被动过（Windows 权限位仅只读位，收紧前后不变即可）
	if _, err := os.Stat(filepath.Join(root, "clash/subscription-origin.yaml")); err != nil {
		t.Fatal(err)
	}
}
