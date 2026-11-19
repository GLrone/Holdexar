//go:build !windows

package clashkernel

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
)

func ImageProcessPIDs(imageName string) []uint32 {
	if imageName == "" {
		return nil
	}
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return nil
	}
	var pids []uint32
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		pid64, err := strconv.ParseUint(entry.Name(), 10, 32)
		if err != nil || pid64 == 0 {
			continue
		}
		pid := uint32(pid64)
		if procMatchesImageName(pid, imageName) {
			pids = append(pids, pid)
		}
	}
	return pids
}

func procMatchesImageName(pid uint32, imageName string) bool {
	if exe, err := os.Readlink(fmt.Sprintf("/proc/%d/exe", pid)); err == nil {
		exe = strings.TrimSuffix(exe, " (deleted)")
		if filepath.Base(exe) == imageName {
			return true
		}
	}
	if raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid)); err == nil && len(raw) > 0 {
		first, _, _ := bytes.Cut(raw, []byte{0})
		if len(first) > 0 && filepath.Base(string(first)) == imageName {
			return true
		}
	}
	if len(imageName) < 15 {
		if comm, err := os.ReadFile(fmt.Sprintf("/proc/%d/comm", pid)); err == nil {
			if strings.TrimSpace(string(comm)) == imageName {
				return true
			}
		}
	}
	return false
}

func processCommandLine(pid uint32) string {
	raw, err := os.ReadFile(fmt.Sprintf("/proc/%d/cmdline", pid))
	if err != nil || len(raw) == 0 {
		return ""
	}
	replaced := bytes.ReplaceAll(bytes.TrimRight(raw, "\x00"), []byte{0}, []byte{' '})
	return strings.TrimSpace(string(replaced))
}

func cmdlineMatchesConfig(cmdline, configPath string) bool {
	return cmdline != "" && strings.Contains(cmdline, configPath)
}

func terminateProcess(pid uint32) error {
	if err := syscall.Kill(int(pid), syscall.SIGTERM); err != nil {
		if errors.Is(err, syscall.ESRCH) {
			return nil
		}
		return fmt.Errorf("孤儿进程终止失败：%w", err)
	}
	deadline := time.Now().Add(500 * time.Millisecond)
	for time.Now().Before(deadline) {
		if errors.Is(syscall.Kill(int(pid), 0), syscall.ESRCH) {
			return nil
		}
		time.Sleep(25 * time.Millisecond)
	}
	if err := syscall.Kill(int(pid), syscall.SIGKILL); err != nil && !errors.Is(err, syscall.ESRCH) {
		return fmt.Errorf("孤儿进程强杀失败：%w", err)
	}
	return nil
}

func configureSpawnCmd(cmd *exec.Cmd) {
	cmd.SysProcAttr = &syscall.SysProcAttr{
		Setpgid: true,
	}
}

func attachProcessGuard(_ *os.Process) func() {
	return nil
}

func stopKernelProcess(p *KernelProcess) {
	if p.pid > 0 {
		_ = syscall.Kill(-p.pid, syscall.SIGTERM)
	}
	if p.process != nil {
		_ = p.process.Signal(syscall.SIGTERM)
	}
	if p.waitTimeout(5 * time.Second) {
		return
	}
	if p.pid > 0 {
		_ = syscall.Kill(-p.pid, syscall.SIGKILL)
	}
	if p.process != nil {
		_ = p.process.Kill()
	}
	p.waitTimeout(5 * time.Second)
}
