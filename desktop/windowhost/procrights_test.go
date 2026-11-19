//go:build windows

package main

import (
	"os/exec"
	"testing"
	"time"
)

func TestOpenProcessHandleTerminate(t *testing.T) {
	cmd := exec.Command("cmd", "/c", "ping", "-n", "30", "127.0.0.1")
	if err := cmd.Start(); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	handle := openProcessHandle(cmd.Process.Pid)
	if handle == 0 {
		_ = cmd.Process.Kill()
		t.Fatal("openProcessHandle 返回 0")
	}
	terminateProcess(handle)
	call(procCloseHandle, handle)
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		_ = cmd.Process.Kill()
		t.Fatal("TerminateProcess 未生效：句柄缺 PROCESS_TERMINATE")
	}
}

// 先在存活期持句柄防进程对象提前销毁，再读退出码。
func TestPidExitCode(t *testing.T) {
	cmd := exec.Command("cmd", "/c", "exit", "7")
	if err := cmd.Start(); err != nil {
		t.Fatalf("spawn: %v", err)
	}
	handle := openProcessHandle(cmd.Process.Pid)
	if handle == 0 {
		_ = cmd.Process.Kill()
		t.Fatal("openProcessHandle 返回 0")
	}
	defer call(procCloseHandle, handle)
	if status, _, _ := call3(procWaitForSingleObject, handle, 10000); status != 0 {
		t.Fatalf("等待子进程退出失败 status=%d", status)
	}
	code, ok := pidExitCode(cmd.Process.Pid, 1000)
	if !ok || code != 7 {
		t.Fatalf("pidExitCode=%d ok=%v，应为 7", code, ok)
	}
}
