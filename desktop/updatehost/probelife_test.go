//go:build windows

package main

import (
	"os"
	"path/filepath"
	"testing"
	"time"

	"golang.org/x/sys/windows"
)

func TestMain(m *testing.M) {
	if reportDataProtocol(os.Args[1:]) {
		os.Exit(0)
	}
	if os.Getenv("HOLDEXAR_UH_TEST_PROBE_FAIL") == "1" {
		for _, arg := range os.Args[1:] {
			if arg == "--server" {
				os.Exit(2)
			}
		}
	}
	os.Exit(m.Run())
}

func startSacrifice(t *testing.T, seconds string) *os.Process {
	t.Helper()
	cmdPath := filepath.Join(os.Getenv("SystemRoot"), "System32", "cmd.exe")
	argv := []string{"cmd.exe", "/c", "ping", "-n", seconds, "127.0.0.1"}
	attr := &os.ProcAttr{Files: []*os.File{nil, nil, nil}}
	proc, err := os.StartProcess(cmdPath, argv, attr)
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	return proc
}

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

func TestProbeTreeKillOnJobClose(t *testing.T) {
	proc := startSacrifice(t, "30")
	done := make(chan struct{})
	go func() { _, _ = proc.Wait(); close(done) }()
	tree := assignProbeTree(proc)
	if tree == nil {
		_ = proc.Kill()
		t.Fatal("assignProbeTree 失败")
	}
	_ = windows.CloseHandle(tree.job)
	tree.job = 0
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		_ = proc.Kill()
		t.Fatal("未解除标志直接关 Job 句柄后子进程未终止：KILL_ON_JOB_CLOSE 静默失效")
	}
}

func TestProbeTreeDisarmKeepsProcessAlive(t *testing.T) {
	proc := startSacrifice(t, "30")
	t.Cleanup(func() {
		_ = proc.Kill()
		_, _ = proc.Wait()
	})
	tree := assignProbeTree(proc)
	if tree == nil {
		t.Fatal("assignProbeTree 失败")
	}
	tree.detach()
	if tree.job != 0 {
		t.Fatal("detach 后句柄未清零")
	}
	time.Sleep(300 * time.Millisecond)
	if !pidAlive(proc.Pid) {
		t.Fatal("detach 解除 KILL_ON_JOB_CLOSE 后已验收进程仍被误杀")
	}
}
