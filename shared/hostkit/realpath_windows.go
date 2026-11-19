//go:build windows

package hostkit

import (
	"strings"
	"syscall"
	"unsafe"
)

var (
	modKernel32                  = syscall.NewLazyDLL("kernel32.dll")
	procGetFinalPathNameByHandle = modKernel32.NewProc("GetFinalPathNameByHandleW")
)

// FinalPath resolves symlinks, NTFS junctions, and DOS device mappings via GetFinalPathNameByHandleW.
func FinalPath(path string) (string, error) {
	pathPtr, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return "", err
	}
	handle, err := syscall.CreateFile(
		pathPtr,
		0,
		syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE|syscall.FILE_SHARE_DELETE,
		nil,
		syscall.OPEN_EXISTING,
		syscall.FILE_FLAG_BACKUP_SEMANTICS,
		0,
	)
	if err != nil {
		return "", err
	}
	defer syscall.CloseHandle(handle)

	buf := make([]uint16, 1024)
	n, _, callErr := procGetFinalPathNameByHandle.Call(
		uintptr(handle),
		uintptr(unsafe.Pointer(&buf[0])),
		uintptr(len(buf)),
		0,
	)
	if n == 0 {
		return "", callErr
	}
	if n >= uintptr(len(buf)) {
		buf = make([]uint16, n+1)
		n, _, callErr = procGetFinalPathNameByHandle.Call(
			uintptr(handle),
			uintptr(unsafe.Pointer(&buf[0])),
			uintptr(len(buf)),
			0,
		)
		if n == 0 || n >= uintptr(len(buf)) {
			return "", callErr
		}
	}
	resolved := syscall.UTF16ToString(buf[:n])
	if strings.HasPrefix(resolved, `\\?\UNC\`) {
		return `\\` + strings.TrimPrefix(resolved, `\\?\UNC\`), nil
	}
	return strings.TrimPrefix(resolved, `\\?\`), nil
}
