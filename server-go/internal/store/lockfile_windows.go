//go:build windows

// Windows LockFileEx 非阻塞独占字节锁原语（按句柄互斥，含同进程跨句柄）。
package store

import (
	"os"
	"path/filepath"
	"strings"

	"golang.org/x/sys/windows"
)

func lockFileExclusive(f *os.File) error {
	var ol windows.Overlapped
	return windows.LockFileEx(windows.Handle(f.Fd()),
		windows.LOCKFILE_EXCLUSIVE_LOCK|windows.LOCKFILE_FAIL_IMMEDIATELY,
		0, 1, 0, &ol)
}

func unlockFileExclusive(f *os.File) {
	var ol windows.Overlapped
	_ = windows.UnlockFileEx(windows.Handle(f.Fd()), 0, 1, 0, &ol)
}

func resolveFinalPath(path string) string {
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		path = resolved
	}
	openFinal := func(target string) (string, bool) {
		ptr, err := windows.UTF16PtrFromString(target)
		if err != nil {
			return "", false
		}
		h, err := windows.CreateFile(ptr, 0, windows.FILE_SHARE_READ|windows.FILE_SHARE_WRITE|windows.FILE_SHARE_DELETE, nil, windows.OPEN_EXISTING, windows.FILE_FLAG_BACKUP_SEMANTICS, 0)
		if err != nil {
			return "", false
		}
		defer windows.CloseHandle(h)
		buf := make([]uint16, 1024)
		n, err := windows.GetFinalPathNameByHandle(h, &buf[0], uint32(len(buf)), 0)
		if err != nil {
			return "", false
		}
		if n > uint32(len(buf)) {
			buf = make([]uint16, n)
			n, err = windows.GetFinalPathNameByHandle(h, &buf[0], uint32(len(buf)), 0)
			if err != nil || n == 0 || n > uint32(len(buf)) {
				return "", false
			}
		}
		resolved := windows.UTF16ToString(buf[:n])
		resolved = strings.TrimPrefix(resolved, `\\?\`)
		if strings.HasPrefix(resolved, `UNC\`) {
			resolved = `\\` + strings.TrimPrefix(resolved, `UNC\`)
		}
		return resolved, true
	}
	if resolved, ok := openFinal(path); ok {
		return resolved
	}
	cur := path
	var tail []string
	for {
		parent := filepath.Dir(cur)
		if parent == cur {
			return path
		}
		tail = append([]string{filepath.Base(cur)}, tail...)
		if resolved, ok := openFinal(parent); ok {
			parts := append([]string{resolved}, tail...)
			return filepath.Join(parts...)
		}
		cur = parent
	}
}
