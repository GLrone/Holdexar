//go:build windows

package updater

import (
	"os"
	"syscall"
)

const fileAttributeReparsePoint = 0x400

func isReparseOrSymlink(info os.FileInfo) bool {
	if info.Mode()&os.ModeSymlink != 0 {
		return true
	}
	if data, ok := info.Sys().(*syscall.Win32FileAttributeData); ok {
		return data.FileAttributes&fileAttributeReparsePoint != 0
	}
	return false
}
