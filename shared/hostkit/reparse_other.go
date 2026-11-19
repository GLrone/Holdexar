//go:build !windows

package hostkit

import "os"

// IsReparseOrSymlink reports whether info or path refers to a symbolic link.
func IsReparseOrSymlink(path string, info os.FileInfo) bool {
	if info == nil && path != "" {
		var err error
		info, err = os.Lstat(path)
		if err != nil {
			return false
		}
	}
	if info == nil {
		return false
	}
	return info.Mode()&os.ModeSymlink != 0
}
