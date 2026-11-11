//go:build windows

// win32.go UpdateHost 的 Win32 基元：换装互斥体、弹窗、分离拉起、能力探针。
package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

var (
	modUser32UH        = windows.NewLazySystemDLL("user32.dll")
	procMessageBoxWUH  = modUser32UH.NewProc("MessageBoxW")
	procCreateMutexWUH = windows.NewLazyDLL("kernel32.dll").NewProc("CreateMutexW")
	procWaitSingleUH   = windows.NewLazyDLL("kernel32.dll").NewProc("WaitForSingleObject")
	procReleaseMutexUH = windows.NewLazyDLL("kernel32.dll").NewProc("ReleaseMutex")
	procCloseHandleUH  = windows.NewLazyDLL("kernel32.dll").NewProc("CloseHandle")
)

func utf16ptrUH(s string) uintptr {
	ptr, err := syscall.UTF16PtrFromString(s)
	if err != nil {
		return 0
	}
	return uintptr(unsafe.Pointer(ptr))
}

func callUH(proc *windows.LazyProc, args ...uintptr) uintptr {
	value, _, _ := proc.Call(args...)
	return value
}

func messageBox(owner uintptr, message, title string, flags uintptr) {
	if os.Getenv("CI") == "1" {
		return
	}
	callUH(procMessageBoxWUH, owner, utf16ptrUH(message), utf16ptrUH(title), flags)
}

// acquireSwapMutex 换装互斥体：0 超时单次尝试，0x80（弃置）算取得。
func acquireSwapMutex() (func(), error) {
	handle, _, _ := procCreateMutexWUH.Call(0, 0, utf16ptrUH(updateMutexName()))
	if handle == 0 {
		return nil, fmt.Errorf("无法创建换装互斥体")
	}
	status, _, _ := procWaitSingleUH.Call(handle, 0)
	if status != 0 && status != 0x80 {
		callUH(procCloseHandleUH, handle)
		return nil, fmt.Errorf("已有更新正在进行或无法核对更新权限")
	}
	return func() {
		callUH(procReleaseMutexUH, handle)
		callUH(procCloseHandleUH, handle)
	}, nil
}

func newDetachedCommand(exe string, args []string, root string) *exec.Cmd {
	cmd := exec.Command(exe, args...)
	cmd.Dir = filepath.Dir(exe)
	cmd.Env = append(os.Environ(), envPrefix+"INSTALL_ROOT="+root)
	env := cmd.Env
	// 分离拉起剥离更新/开发态残留变量
	cleaned := env[:0]
	for _, entry := range env {
		key, _, _ := strings.Cut(entry, "=")
		switch strings.ToUpper(key) {
		case envPrefix + "UPDATE_ATTEMPT_ID", envPrefix + "UPDATE_DATA_TOKEN",
			envPrefix + "LAUNCHER_PID", envPrefix + "DEV_URL", envPrefix + "UPDATE_RESTART",
			envPrefix + "PREV_PID":
			continue
		}
		cleaned = append(cleaned, entry)
	}
	if dataDir := os.Getenv(envPrefix + "DATA_DIR"); dataDir != "" {
		cleaned = append(cleaned, envPrefix+"DATA_DIR="+dataDir)
	} else if _, err := os.Stat(filepath.Join(root, dataDirName, appSlug+".db")); err == nil {
		cleaned = append(cleaned, envPrefix+"DATA_DIR="+filepath.Join(root, dataDirName))
	}
	cmd.Env = cleaned
	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: 0x00000008 | 0x00000200 | 0x08000000, // DETACHED|NEW_GROUP|NO_WINDOW
	}
	return cmd
}

// supportsDataProtocol 能力探针：--update-protocol 应答 updateDataProtocol=1。
func supportsDataProtocol(exe string) bool {
	result := filepath.Join(os.TempDir(), "holdexar-update-protocol-"+randomHex(6)+".json")
	defer os.Remove(result)
	cmd := exec.Command(exe, "--update-protocol", "--help")
	cmd.Env = append(os.Environ(), envPrefix+"UPDATE_PROTOCOL_RESULT="+result)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	if err := cmd.Start(); err != nil {
		return false
	}
	done := make(chan struct{})
	go func() { _ = cmd.Wait(); close(done) }()
	select {
	case <-done:
	case <-time.After(25 * time.Second):
		_ = cmd.Process.Kill()
		return false
	}
	raw, err := os.ReadFile(result)
	if err != nil {
		return false
	}
	return strings.Contains(string(raw), `"updateDataProtocol":1`) ||
		strings.Contains(string(raw), `{"updateDataProtocol":1}`)
}
