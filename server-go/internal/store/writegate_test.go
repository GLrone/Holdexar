package store

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func openTestDB(t *testing.T) (*sql.DB, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "test.db")
	db, err := Open(path)
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db, path
}

func TestWithWriteConcurrent(t *testing.T) {
	db, path := openTestDB(t)
	if _, err := db.Exec("CREATE TABLE counter(n INTEGER)"); err != nil {
		t.Fatal(err)
	}
	g := NewWriteGate(db, path)
	var ok, fail, busy atomic.Int64
	deadline := time.Now().Add(3 * time.Second)
	var wg sync.WaitGroup
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for time.Now().Before(deadline) {
				err := g.WithWrite(context.Background(), PriorityBackground, func(conn *sql.Conn) error {
					_, e := conn.ExecContext(context.Background(), "INSERT INTO counter(n) VALUES (1)")
					return e
				})
				if err != nil {
					fail.Add(1)
					if strings.Contains(strings.ToLower(err.Error()), "busy") {
						busy.Add(1)
					}
					return
				}
				ok.Add(1)
			}
		}()
	}
	wg.Wait()
	if f := fail.Load(); f != 0 {
		t.Fatalf("并发写失败 %d 笔（其中 busy %d 笔），要求零失败", f, busy.Load())
	}
	var total int64
	if err := db.QueryRow("SELECT COUNT(*) FROM counter").Scan(&total); err != nil {
		t.Fatal(err)
	}
	if total != ok.Load() {
		t.Fatalf("落库 %d 笔与提交 %d 笔不一致", total, ok.Load())
	}
	if total < 1000 {
		t.Fatalf("3 秒仅提交 %d 笔，串行化异常（历史基线 17139）", total)
	}
	t.Logf("8 写者 3 秒提交 %d 笔，零错误零 busy（历史基线 17139）", total)
}

func TestInteractivePriorityHandoff(t *testing.T) {
	db, path := openTestDB(t)
	g := NewWriteGate(db, path)
	bgHold, err := g.Acquire(context.Background(), PriorityBackground)
	if err != nil {
		t.Fatal(err)
	}
	order := make(chan string, 2)
	acquire := func(prio WritePriority) {
		lease, err := g.Acquire(context.Background(), prio)
		if err != nil {
			t.Errorf("Acquire(%v) 失败: %v", prio, err)
			return
		}
		order <- prio.label()
		lease.Done()
	}
	go acquire(PriorityBackground)
	time.Sleep(50 * time.Millisecond)
	go acquire(PriorityInteractive)
	time.Sleep(50 * time.Millisecond)
	bgHold.Done()
	if got := <-order; got != "交互" {
		t.Fatalf("移交顺序错误：首位 %s，应为交互", got)
	}
	if got := <-order; got != "后台" {
		t.Fatalf("移交顺序错误：次位 %s，应为后台", got)
	}
}

func TestWaitTimeout(t *testing.T) {
	db, path := openTestDB(t)
	g := NewWriteGate(db, path)
	g.waitTimeout = 200 * time.Millisecond
	hold, err := g.Acquire(context.Background(), PriorityBackground)
	if err != nil {
		t.Fatal(err)
	}
	start := time.Now()
	_, err = g.Acquire(context.Background(), PriorityBackground)
	if err == nil || !strings.Contains(err.Error(), "等待数据库写入超时") {
		t.Fatalf("应报等待超时，得到 %v", err)
	}
	if elapsed := time.Since(start); elapsed < 150*time.Millisecond {
		t.Fatalf("超时过早返回 %v", elapsed)
	}
	hold.Done()
	lease, err := g.Acquire(context.Background(), PriorityBackground)
	if err != nil {
		t.Fatalf("释放后应可再次取得写者位: %v", err)
	}
	lease.Done()
}

func TestHoldWarn(t *testing.T) {
	db, path := openTestDB(t)
	g := NewWriteGate(db, path)
	g.holdWarnAfter = 50 * time.Millisecond
	var mu sync.Mutex
	var msgs []string
	g.warnFn = func(format string, args ...any) {
		mu.Lock()
		msgs = append(msgs, fmt.Sprintf(format, args...))
		mu.Unlock()
	}
	lease, err := g.Acquire(context.Background(), PriorityInteractive)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(150 * time.Millisecond)
	mu.Lock()
	n := len(msgs)
	var first string
	if n > 0 {
		first = msgs[0]
	}
	mu.Unlock()
	if n == 0 {
		t.Fatal("持闸超限未告警")
	}
	if !strings.Contains(first, "交互写事务占用写锁已超过") {
		t.Fatalf("告警文案异常: %q", first)
	}
	lease.Done()
	time.Sleep(80 * time.Millisecond)
	mu.Lock()
	defer mu.Unlock()
	if len(msgs) != 1 {
		t.Fatalf("应只告警一次，实际 %d 次", len(msgs))
	}
}

func TestCrossProcessLockExclusive(t *testing.T) {
	lockPath := processWriteLockPath(filepath.Join(t.TempDir(), "lock.db"))
	ctx := context.Background()
	f1, err := lockFile(lockPath, time.Second, ctx)
	if err != nil {
		t.Fatalf("首次取锁失败: %v", err)
	}
	start := time.Now()
	f2, err := lockFile(lockPath, 200*time.Millisecond, ctx)
	if err == nil {
		unlockFile(f2)
		t.Fatal("持锁期间第二次取锁应失败")
	}
	if !strings.Contains(err.Error(), "等待数据库写入超时") {
		t.Fatalf("应报等待超时，得到 %v", err)
	}
	if elapsed := time.Since(start); elapsed < 150*time.Millisecond {
		t.Fatalf("未等待即报错 %v", elapsed)
	}
	unlockFile(f1)
	f2, err = lockFile(lockPath, time.Second, ctx)
	if err != nil {
		t.Fatalf("释放后取锁失败: %v", err)
	}
	unlockFile(f2)
}

func TestTwoGatesMutualExclusive(t *testing.T) {
	db, path := openTestDB(t)
	g1 := NewWriteGate(db, path)
	g2 := NewWriteGate(db, path)
	g2.lockTimeout = 300 * time.Millisecond
	l1, err := g1.Acquire(context.Background(), PriorityBackground)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := g2.Acquire(context.Background(), PriorityBackground); err == nil {
		t.Fatal("第一闸持锁期间第二闸应取锁失败")
	}
	l1.Done()
	l2, err := g2.Acquire(context.Background(), PriorityBackground)
	if err != nil {
		t.Fatalf("释放后第二闸应可取得: %v", err)
	}
	l2.Done()
}

func TestNewWriteGateResolvesNTFSJunctionToSameLockHash(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("NTFS junction test only applies on Windows")
	}
	root := t.TempDir()
	realDir := filepath.Join(root, "real")
	if err := os.MkdirAll(realDir, 0o755); err != nil {
		t.Fatal(err)
	}
	junctionDir := filepath.Join(root, "junction")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", junctionDir, realDir).CombinedOutput(); err != nil {
		t.Fatalf("mklink /J failed: %v (%s)", err, out)
	}
	realPath := filepath.Join(realDir, "holdexar.db")
	junctionPath := filepath.Join(junctionDir, "holdexar.db")

	dbReal, err := Open(realPath)
	if err != nil {
		t.Fatal(err)
	}
	defer dbReal.Close()
	dbJunc, err := Open(junctionPath)
	if err != nil {
		t.Fatal(err)
	}
	defer dbJunc.Close()

	g1 := NewWriteGate(dbReal, realPath)
	g2 := NewWriteGate(dbJunc, junctionPath)
	if g1.lockPath != g2.lockPath {
		t.Fatalf("junction lockPath mismatch:\n  real: %s\n  junc: %s", g1.lockPath, g2.lockPath)
	}
}
