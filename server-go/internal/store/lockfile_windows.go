//go:build windows

// 写闸文件锁原语（Windows LockFileEx）：region 字节锁，同进程跨句柄不互斥
// 的语义由调用方轮询兜底——历史行为保持不变。
package store

import (
	"os"

	"golang.org/x/sys/windows"
)

func lockFileExclusive(f *os.File) error {
	var ol windows.Overlapped
	return windows.LockFileEx(windows.Handle(f.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
		0, 1, 0, &ol)
}

func unlockFileExclusive(f *os.File) {
	var ol windows.Overlapped
	_ = windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, &ol)
}
