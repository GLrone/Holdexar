// Package instance：同一数据目录的生产进程独占（跨进程文件锁）。
// 与数据库写闸/_process_write_lock 属不同层，锁文件固定 production.lock，
// 两层不得共用同一文件。
package instance

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"

	"golang.org/x/sys/windows"
)

const (
	// lockFileName 锁文件名（Python core/instance.py 同名事实源）。
	lockFileName = "production.lock"
	// lockTimeout 抢锁总时限；lockPollStep 非阻塞重试步进。
	lockTimeout  = 200 * time.Millisecond
	lockPollStep = 25 * time.Millisecond
)

// ErrAlreadyRunning 同目录已有生产进程持锁（用户语言文案，Python 同句）。
var ErrAlreadyRunning = errors.New("此数据目录已有后台服务，请打开正在运行的应用")

// AcquireProductionLock 抢占数据目录的生产单实例锁，进程生命周期持有。
// 目录缺失先建；锁文件独占非阻塞锁定，失败按 25ms 重试至 200ms，仍失败
// 返回 ErrAlreadyRunning。release 幂等；进程退出由操作系统回收锁。
func AcquireProductionLock(dataDir string) (release func(), err error) {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return nil, fmt.Errorf("数据目录无法创建：%w", err)
	}
	f, err := os.OpenFile(filepath.Join(dataDir, lockFileName), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("单实例锁文件打开失败: %w", err)
	}
	// 不做初始化写入：持锁者已锁住该字节范围，竞争方的写入会以操作系统
	// 原始错误失败、抢在重试循环之前漏出非用户文案——锁定空文件的字节
	// 区间在 Windows 上同样成立，Python msvcrt.locking 同款语义。
	handle := windows.Handle(f.Fd())
	deadline := time.Now().Add(lockTimeout)
	var ol windows.Overlapped
	for {
		if err := windows.LockFileEx(handle,
			windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
			0, 1, 0, &ol); err == nil {
			var once sync.Once
			return func() { once.Do(func() { unlock(f) }) }, nil
		}
		if time.Now().After(deadline) {
			f.Close()
			return nil, ErrAlreadyRunning
		}
		time.Sleep(lockPollStep)
	}
}

func unlock(f *os.File) {
	var ol windows.Overlapped
	_ = windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, &ol)
	_ = f.Close()
}
