//go:build windows

// 探针进程判活与 Job 对象的内核级测试：pidAlive 的等待返回值判定曾反转
// （存活被判死），Job kill-on-close 标志曾写错偏移静默失效——两者都由真实
// 子进程的内核行为钉住，防回退。
package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// startSacrifice 用固定参数起一个无窗口的牺牲性子进程（延迟退出）。
func startSacrifice(t *testing.T, seconds string) *os.Process {
	t.Helper()
	cmdPath := filepath.Join(os.Getenv("SystemRoot"), "System32", "cmd.exe")
	argv := []string{"/c", "ping", "-n", seconds, "127.0.0.1"}
	attr := &os.ProcAttr{Files: []*os.File{nil, nil, nil}}
	proc, err := os.StartProcess(cmdPath, argv, attr)
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	return proc
}

// TestPidAliveSemantics 存活→true（WAIT_TIMEOUT 分支），不存在→false。
func TestPidAliveSemantics(t *testing.T) {
	if !pidAlive(os.Getpid()) {
		t.Fatal("自身存活进程被判死：WaitForSingleObject 返回值判定反转")
	}
	proc := startSacrifice(t, "10")
	_ = proc.Kill()
	_, _ = proc.Wait()
	if pidAlive(proc.Pid) {
		t.Fatal("已退出进程被判活")
	}
}

// TestProbeTreeKillOnJobClose 关 Job 句柄必须真实终止挂树子进程。
func TestProbeTreeKillOnJobClose(t *testing.T) {
	proc := startSacrifice(t, "30")
	done := make(chan struct{})
	go func() { _, _ = proc.Wait(); close(done) }()
	tree := assignProbeTree(proc)
	if tree == nil {
		_ = proc.Kill()
		t.Fatal("assignProbeTree 失败")
	}
	tree.detach() // KILL_ON_JOB_CLOSE：关句柄即杀整树
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		_ = proc.Kill()
		t.Fatal("关 Job 句柄后子进程未终止：KILL_ON_JOB_CLOSE 静默失效")
	}
}
