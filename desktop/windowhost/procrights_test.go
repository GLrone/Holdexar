//go:build windows

// 进程句柄权限位的内核级测试：openProcessHandle 必须带等待+读退出码+终止三权限，
// 否则退出码读取与超时强杀兜底静默失败（历史缺陷：只申请 SYNCHRONIZE）。
package main

import (
	"os/exec"
	"testing"
	"time"
)

// TestOpenProcessHandleTerminate 句柄必须能真实终止进程（PROCESS_TERMINATE 生效）。
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

// TestPidExitCode 读退出码必须真实（PROCESS_QUERY_LIMITED_INFORMATION 生效）：
// 先自持句柄再等待退出，进程对象保持可打开（真实用法都在存活期先开句柄）。
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
