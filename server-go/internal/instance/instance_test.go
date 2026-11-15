package instance

import (
	"errors"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
)

// 同目录第二个实例不可抢（独占语义）。
func TestExclusiveSameDir(t *testing.T) {
	dir := t.TempDir()
	release, err := AcquireProductionLock(dir)
	if err != nil {
		t.Fatalf("首次抢锁失败: %v", err)
	}
	defer release()
	_, err = AcquireProductionLock(dir)
	if !errors.Is(err, ErrAlreadyRunning) {
		t.Fatalf("同目录第二次抢锁应失败，得到: %v", err)
	}
}

// 释放后同目录可再抢。
func TestReleaseThenReacquire(t *testing.T) {
	dir := t.TempDir()
	release, err := AcquireProductionLock(dir)
	if err != nil {
		t.Fatalf("首次抢锁失败: %v", err)
	}
	release()
	release() // 幂等
	again, err := AcquireProductionLock(dir)
	if err != nil {
		t.Fatalf("释放后抢锁应成功: %v", err)
	}
	again()
}

// 不同目录互不干扰。
func TestDifferentDirs(t *testing.T) {
	first, err := AcquireProductionLock(filepath.Join(t.TempDir(), "a"))
	if err != nil {
		t.Fatalf("目录一抢锁失败: %v", err)
	}
	defer first()
	second, err := AcquireProductionLock(filepath.Join(t.TempDir(), "b"))
	if err != nil {
		t.Fatalf("目录二抢锁失败: %v", err)
	}
	defer second()
}

// 并发抢锁负案：N 路并发同目录，恰好一路成功。
func TestConcurrentGrab(t *testing.T) {
	dir := t.TempDir()
	const racers = 8
	var mu sync.Mutex
	var releases []func()
	var failures atomic.Int32
	var wg sync.WaitGroup
	for range racers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			release, err := AcquireProductionLock(dir)
			if err == nil {
				mu.Lock()
				releases = append(releases, release)
				mu.Unlock()
				return
			}
			failures.Add(1)
			if !errors.Is(err, ErrAlreadyRunning) {
				t.Errorf("失败方错误应为本实例错误，得到: %v", err)
			}
		}()
	}
	wg.Wait()
	if len(releases) != 1 {
		t.Fatalf("应恰有一路成功，实际 %d，失败 %d", len(releases), failures.Load())
	}
	if failures.Load() != racers-1 {
		t.Fatalf("其余路应全部失败，实际失败 %d", failures.Load())
	}
	for _, release := range releases {
		release()
	}
}
