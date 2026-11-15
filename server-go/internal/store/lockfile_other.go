//go:build !windows

// 写闸文件锁原语（POSIX flock）：LOCK_EX|LOCK_NB 非阻塞独占，与 Windows
// LockFileEx 的非阻塞语义对齐。锁文件随数据目录必须在本地盘（flock 在
// NFS/SMB 上语义不可靠）。
package store

import (
	"os"
	"syscall"
)

func lockFileExclusive(f *os.File) error {
	return syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
}

func unlockFileExclusive(f *os.File) {
	_ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
}
