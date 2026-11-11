//go:build windows

package updateguard

import "golang.org/x/sys/windows"

// processAliveWin OpenProcess+WaitForSingleObject(0) 判活。
func processAliveWin(pid int) bool {
	handle, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	event, _ := windows.WaitForSingleObject(handle, 0)
	return event == uint32(0xFFFFFFFF) // WAIT_TIMEOUT = 仍在运行
}
