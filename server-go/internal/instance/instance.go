// Package instance 提供同一数据目录的生产单实例文件锁（production.lock），独立于 store 写闸锁。
package instance

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

const (
	lockFileName = "production.lock"
	lockTimeout  = 200 * time.Millisecond
	lockPollStep = 25 * time.Millisecond
)

var ErrAlreadyRunning = errors.New("此数据目录已有后台服务，请打开正在运行的应用")

// AcquireProductionLock 非阻塞抢占目录单实例锁（25ms 重试至 200ms），release 幂等。
func AcquireProductionLock(dataDir string) (release func(), err error) {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return nil, fmt.Errorf("数据目录无法创建：%w", err)
	}
	f, err := os.OpenFile(filepath.Join(dataDir, lockFileName), os.O_CREATE|os.O_RDWR, 0o600)
	if err != nil {
		return nil, fmt.Errorf("单实例锁文件打开失败: %w", err)
	}
	// 不做初始化写入：持锁者已锁住文件，竞争方的写入可能抢在重试循环之前漏出非用户文案。
	deadline := time.Now().Add(lockTimeout)
	for {
		if err := tryLockExclusive(f); err == nil {
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
	unlockExclusive(f)
	_ = f.Close()
}
