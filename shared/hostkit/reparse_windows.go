//go:build windows

package hostkit

import (
	"os"
	"syscall"
)

const (
	fileAttributeReparsePoint = 0x400
	invalidFileAttributes     = ^uint32(0)
)

// IsReparseOrSymlink reports whether info or path refers to a symlink or Windows reparse point (including NTFS junctions).
func IsReparseOrSymlink(path string, info os.FileInfo) bool {
	if info == nil && path != "" {
		var err error
		info, err = os.Lstat(path)
		if err != nil {
			return false
		}
	}
	if info != nil {
		if info.Mode()&os.ModeSymlink != 0 {
			return true
		}
		if data, ok := info.Sys().(*syscall.Win32FileAttributeData); ok {
			return data.FileAttributes&fileAttributeReparsePoint != 0
		}
	}
	if path != "" {
		if ptr, err := syscall.UTF16PtrFromString(path); err == nil {
			if attrs, err := syscall.GetFileAttributes(ptr); err == nil && attrs != invalidFileAttributes {
				return attrs&fileAttributeReparsePoint != 0
			}
		}
	}
	return false
}
