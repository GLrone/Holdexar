//go:build !windows

package clashkernel

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"syscall"
)

// PortOwnedByProcess 监听端口是否确由该进程持有（只认回环监听）。
func PortOwnedByProcess(port int, pid uint32) bool {
	if pid == 0 || port <= 0 {
		return false
	}
	if _, err := os.Stat("/proc"); err != nil {
		err := syscall.Kill(int(pid), 0)
		return (err == nil || errors.Is(err, syscall.EPERM)) && PortListening(port)
	}
	inodes := procSocketInodes(pid)
	if len(inodes) == 0 {
		return false
	}
	for _, table := range []string{
		fmt.Sprintf("/proc/%d/net/tcp", pid),
		fmt.Sprintf("/proc/%d/net/tcp6", pid),
	} {
		raw, err := os.ReadFile(table)
		if err != nil {
			raw, err = os.ReadFile("/proc/net/" + filepath.Base(table))
			if err != nil {
				continue
			}
		}
		if procTCPTableOwnsLoopbackPort(string(raw), port, inodes) {
			return true
		}
	}
	return false
}

func procSocketInodes(pid uint32) map[string]bool {
	fdDir := fmt.Sprintf("/proc/%d/fd", pid)
	entries, err := os.ReadDir(fdDir)
	if err != nil {
		return nil
	}
	inodes := map[string]bool{}
	for _, entry := range entries {
		target, err := os.Readlink(filepath.Join(fdDir, entry.Name()))
		if err != nil {
			continue
		}
		if strings.HasPrefix(target, "socket:[") && strings.HasSuffix(target, "]") {
			if inode := target[len("socket:[") : len(target)-1]; inode != "" {
				inodes[inode] = true
			}
		}
	}
	return inodes
}
