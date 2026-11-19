package clashservice

import (
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

var secretBearingSubdirs = []string{"clash", "proxypool"}

// transientSuffixes 可重建明文件后缀；原文 .yaml 为事实源永不清理。
var transientSuffixes = []string{".runtime.yaml", ".tmp"}

// TightenAll 收紧明文权限（POSIX 0600/0700，Windows 依赖用户目录 ACL no-op）。
func TightenAll(dataDir string) int {
	if runtime.GOOS == "windows" {
		return 0
	}
	count := 0
	for _, name := range secretBearingSubdirs {
		base := filepath.Join(dataDir, name)
		info, err := os.Stat(base)
		if err != nil || !info.IsDir() {
			continue
		}
		_ = os.Chmod(base, 0o700)
		_ = filepath.WalkDir(base, func(path string, entry fs.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			if entry.IsDir() {
				if path != base {
					_ = os.Chmod(path, 0o700)
				}
				return nil
			}
			_ = os.Chmod(path, 0o600)
			count++
			return nil
		})
	}
	return count
}

// SweepTransient 删除 *.runtime.yaml 与 *.tmp（需在内核未运行时调用）。
func SweepTransient(dataDir string) int {
	removed := 0
	for _, name := range secretBearingSubdirs {
		base := filepath.Join(dataDir, name)
		info, err := os.Stat(base)
		if err != nil || !info.IsDir() {
			continue
		}
		_ = filepath.WalkDir(base, func(path string, entry fs.DirEntry, err error) error {
			if err != nil || entry.IsDir() {
				return nil
			}
			for _, suffix := range transientSuffixes {
				if strings.HasSuffix(entry.Name(), suffix) {
					if os.Remove(path) == nil {
						removed++
					}
					break
				}
			}
			return nil
		})
	}
	return removed
}
