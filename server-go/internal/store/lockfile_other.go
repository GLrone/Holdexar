//go:build !windows

// POSIX flock 非阻塞独占锁原语（LOCK_EX|LOCK_NB），依赖系统临时目录所在本地盘。
package store

import (
	"os"
	"path/filepath"
	"syscall"
)

func lockFileExclusive(f *os.File) error {
	return syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
}

func unlockFileExclusive(f *os.File) {
	_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
}

func resolveFinalPath(path string) string {
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		return resolved
	}
	cur := path
	var tail []string
	for {
		parent := filepath.Dir(cur)
		if parent == cur {
			return path
		}
		tail = append([]string{filepath.Base(cur)}, tail...)
		if resolved, err := filepath.EvalSymlinks(parent); err == nil {
			parts := append([]string{resolved}, tail...)
			return filepath.Join(parts...)
		}
		cur = parent
	}
}
