//go:build !windows

package updater

import "os"

func isReparseOrSymlink(info os.FileInfo) bool {
	return info.Mode()&os.ModeSymlink != 0
}
