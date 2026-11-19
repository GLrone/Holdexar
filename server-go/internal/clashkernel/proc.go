package clashkernel

import (
	"fmt"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"
)

func KernelProcessAlive(imageName string) bool {
	return len(ImageProcessPIDs(imageName)) > 0
}

// KillOrphanKernels 清理同映像名孤儿进程（configPath 非空时校验命令行）。
func KillOrphanKernels(imageName, configPath string) (int, error) {
	return KillOrphanKernelsExclude(imageName, configPath, 0)
}

func KillOrphanKernelsExclude(imageName, configPath string, excludePid uint32) (int, error) {
	killed := 0
	for _, pid := range orphanProcessPIDs(imageName) {
		if pid == excludePid || pid == uint32(os.Getpid()) {
			continue
		}
		if configPath != "" {
			cmdline := processCommandLine(pid)
			if !cmdlineMatchesConfig(cmdline, configPath) {
				continue
			}
		}
		if err := terminateProcess(pid); err != nil {
			continue
		}
		killed++
	}
	return killed, nil
}

// shortcut: orphanProcessPIDs 含兼容名分支，存量安装消化后可移除。
func orphanProcessPIDs(imageName string) []uint32 {
	seen := map[uint32]bool{}
	var pids []uint32
	names := []string{imageName}
	if legacy := legacyKernelFilename(); !strings.EqualFold(imageName, legacy) &&
		strings.EqualFold(imageName, KernelFilename()) {
		names = append(names, legacy)
	}
	for _, name := range names {
		for _, pid := range ImageProcessPIDs(name) {
			if !seen[pid] {
				seen[pid] = true
				pids = append(pids, pid)
			}
		}
	}
	return pids
}

type KernelProcess struct {
	cmd          *exec.Cmd
	process      *os.Process
	pid          int
	done         chan struct{}
	waitErr      error
	once         sync.Once
	releaseGuard func()
}

func Spawn(exePath string, args []string, logPath string) (*KernelProcess, error) {
	cmd := exec.Command(exePath, args...)
	cmd.Env = KernelEnv(os.Environ())
	configureSpawnCmd(cmd)
	if logPath != "" {
		logFile, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
		if err != nil {
			return nil, fmt.Errorf("内核日志文件打开失败：%w", err)
		}
		cmd.Stdout = logFile
		cmd.Stderr = logFile
		defer logFile.Close()
	}
	if err := cmd.Start(); err != nil {
		return nil, fmt.Errorf("内核进程拉起失败：%w", err)
	}
	proc := &KernelProcess{
		cmd:          cmd,
		process:      cmd.Process,
		pid:          cmd.Process.Pid,
		done:         make(chan struct{}),
		releaseGuard: attachProcessGuard(cmd.Process),
	}
	go func() {
		proc.waitErr = cmd.Wait()
		if proc.releaseGuard != nil {
			proc.releaseGuard()
		}
		close(proc.done)
	}()
	return proc, nil
}

func (p *KernelProcess) Pid() int {
	if p == nil {
		return 0
	}
	return p.pid
}

func (p *KernelProcess) Running() bool {
	if p == nil {
		return false
	}
	select {
	case <-p.done:
		return false
	default:
		return true
	}
}

func (p *KernelProcess) WaitExit() error {
	if p == nil {
		return nil
	}
	<-p.done
	return p.waitErr
}

func (p *KernelProcess) Stop() {
	if p == nil || !p.Running() {
		return
	}
	stopKernelProcess(p)
}

func (p *KernelProcess) waitTimeout(d time.Duration) bool {
	select {
	case <-p.done:
		return true
	case <-time.After(d):
		return false
	}
}
