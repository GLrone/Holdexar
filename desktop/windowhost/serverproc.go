//go:build windows

package main

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

type serverProcess struct {
	mu      sync.Mutex
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	srvJob  windows.Handle
	exited  chan struct{}
	code    int
	stopped bool
	cfg     *options
	exe     string
}

// restartExitCode 跨进程重启约定码（与 internal/systemx.RestartExitCode、devwatch 同值）。
const restartExitCode = 75

func createKillOnCloseJob() (windows.Handle, error) {
	job, err := windows.CreateJobObject(nil, nil)
	if err != nil || job == 0 {
		return 0, err
	}
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	_, err = windows.SetInformationJobObject(
		job,
		windows.JobObjectExtendedLimitInformation,
		uintptr(unsafe.Pointer(&info)),
		uint32(unsafe.Sizeof(info)),
	)
	if err != nil {
		_ = windows.CloseHandle(job)
		return 0, err
	}
	return job, nil
}

func attachChildJob(pid int) windows.Handle {
	if pid <= 0 {
		return 0
	}
	job, err := createKillOnCloseJob()
	if err != nil || job == 0 {
		return 0
	}
	procHandle, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(pid))
	if err != nil || procHandle == 0 {
		_ = windows.CloseHandle(job)
		return 0
	}
	defer windows.CloseHandle(procHandle)
	if err := windows.AssignProcessToJobObject(job, procHandle); err != nil {
		_ = windows.CloseHandle(job)
		return 0
	}
	return job
}

func startServer(cfg *options, log *hostLog) (*serverProcess, error) {
	exe, err := serverExecutable(cfg)
	if err != nil {
		return nil, err
	}
	ticket := ticketPath(cfg)
	_ = os.Remove(ticket)
	logDir := filepath.Join(cfg.dataDir, "logs")
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		return nil, err
	}
	logFile, err := os.OpenFile(filepath.Join(logDir, "serverd.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	serverHost := strings.TrimSpace(os.Getenv(envPrefix + "HOST"))
	if serverHost == "" {
		serverHost = "127.0.0.1"
	}
	cmd := exec.Command(exe,
		"-addr", serverHost+":"+strconv.Itoa(cfg.serverPort),
		"-data-dir", cfg.dataDir,
		"--bootstrap-file", ticket,
	)
	if os.Getenv(envPrefix+"DEV_SESSION") == "1" {
		cmd.Args = append(cmd.Args, "--dev-session")
	}
	cmd.Dir = filepath.Dir(exe)
	// 仅对 serverd 隐窗且不设 CREATE_BREAKAWAY_FROM_JOB，宿主自身不入 srvJob。
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		_ = logFile.Close()
		return nil, err
	}
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	cmd.Env = append(os.Environ(), envPrefix+"DEV_CHILD=1")
	startErr := cmd.Start()
	_ = logFile.Close()
	if startErr != nil {
		_ = stdin.Close()
		return nil, startErr
	}
	srvJob := attachChildJob(cmd.Process.Pid)
	log.write(fmt.Sprintf("本地服务进程 %s（PID %d）", filepath.Base(exe), cmd.Process.Pid))
	srv := &serverProcess{
		cmd:    cmd,
		stdin:  stdin,
		srvJob: srvJob,
		exited: make(chan struct{}),
		cfg:    cfg,
		exe:    exe,
	}
	go func() {
		waitErr := cmd.Wait()
		srv.mu.Lock()
		srv.code = 1
		if exitErr, ok := waitErr.(*exec.ExitError); ok {
			srv.code = exitErr.ExitCode()
		} else if waitErr == nil {
			srv.code = 0
		}
		srv.mu.Unlock()
		close(srv.exited)
	}()
	return srv, nil
}

func (s *serverProcess) closeJob() {
	s.mu.Lock()
	job := s.srvJob
	s.srvJob = 0
	s.mu.Unlock()
	if job != 0 {
		_ = windows.CloseHandle(job)
	}
}

func (s *serverProcess) stop(log *hostLog) {
	s.mu.Lock()
	if s.stopped {
		s.mu.Unlock()
		return
	}
	s.stopped = true
	cmd := s.cmd
	stdin := s.stdin
	s.mu.Unlock()
	if cmd == nil || cmd.Process == nil {
		s.closeJob()
		return
	}
	if stdin != nil {
		_ = stdin.Close()
	}
	select {
	case <-s.exited:
	case <-time.After(20 * time.Second):
		log.write("本地服务优雅关闭超时，强制结束")
		s.closeJob()
		if handle := openProcessHandle(cmd.Process.Pid); handle != 0 {
			terminateProcess(handle)
			call(procCloseHandle, handle)
		}
		select {
		case <-s.exited:
		case <-time.After(5 * time.Second):
			log.write("本地服务强制结束未确认，交由系统回收")
		}
	}
	s.closeJob()
}

func (s *serverProcess) waitExit() (int, bool) {
	<-s.exited
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.code, true
}

func (s *serverProcess) alive() bool {
	select {
	case <-s.exited:
		return false
	default:
		return true
	}
}

func (s *serverProcess) restart(log *hostLog) (*serverProcess, error) {
	s.mu.Lock()
	stdin := s.stdin
	s.stdin = nil
	s.mu.Unlock()
	if stdin != nil {
		_ = stdin.Close()
	}
	s.closeJob()
	return startServer(s.cfg, log)
}

func (s *serverProcess) waitForExit() int {
	<-s.exited
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.code
}

func (s *serverProcess) cleanup() {
	s.stop(&hostLog{})
}

type devFrontendProcess struct {
	mu      sync.Mutex
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	job     windows.Handle
	exited  chan struct{}
	stopped bool
	url     string
}

func startDevFrontend(cfg *options, log *hostLog) (*devFrontendProcess, error) {
	root := cfg.installRoot
	if root == "" {
		root = exeDir()
	}
	webDir := filepath.Join(root, "web")
	script := filepath.Join(webDir, "scripts", "dev-desktop.mjs")
	if !fileExists(script) {
		return nil, fmt.Errorf("未找到前端开发启动脚本：%s", script)
	}
	nodeExe, err := exec.LookPath("node")
	if err != nil {
		return nil, errors.New("未找到 node，请安装 Node.js 后再使用 --dev")
	}
	cmd := exec.Command(nodeExe, filepath.Join("scripts", "dev-desktop.mjs"))
	cmd.Dir = webDir
	cmd.Env = append(os.Environ(), fmt.Sprintf("VITE_PROXY_TARGET=http://127.0.0.1:%d", cfg.serverPort))
	cmd.Stderr = os.Stderr
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		_ = stdin.Close()
		return nil, err
	}
	if err := cmd.Start(); err != nil {
		_ = stdin.Close()
		return nil, err
	}
	job := attachChildJob(cmd.Process.Pid)
	exited := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(exited)
	}()
	portCh := make(chan string, 1)
	go func() {
		reader := bufio.NewReader(stdout)
		line, _ := reader.ReadString('\n')
		portCh <- strings.TrimSpace(line)
		_, _ = io.Copy(io.Discard, reader)
	}()
	dev := &devFrontendProcess{cmd: cmd, stdin: stdin, job: job, exited: exited}
	select {
	case rawPort := <-portCh:
		port, perr := strconv.Atoi(rawPort)
		if perr != nil || port < 1 || port > 65535 {
			dev.stop()
			return nil, fmt.Errorf("前端开发服务未返回有效端口：%q", rawPort)
		}
		dev.url = fmt.Sprintf("http://127.0.0.1:%d", port)
		log.write("前端开发服务已就绪：" + dev.url)
		return dev, nil
	case <-exited:
		dev.stop()
		return nil, errors.New("前端开发服务提前退出")
	case <-time.After(30 * time.Second):
		dev.stop()
		return nil, errors.New("前端开发服务启动超时（30s）")
	}
}

func (d *devFrontendProcess) stop() {
	if d == nil {
		return
	}
	d.mu.Lock()
	if d.stopped {
		d.mu.Unlock()
		return
	}
	d.stopped = true
	stdin := d.stdin
	d.stdin = nil
	d.mu.Unlock()
	if stdin != nil {
		_ = stdin.Close()
	}
	select {
	case <-d.exited:
	case <-time.After(5 * time.Second):
	}
	d.mu.Lock()
	job := d.job
	d.job = 0
	d.mu.Unlock()
	if job != 0 {
		_ = windows.CloseHandle(job)
	}
}

type healthSnapshot struct {
	Status         string `json:"status"`
	App            string `json:"app"`
	Version        string `json:"version"`
	PID            int    `json:"pid"`
	DataDir        string `json:"dataDir"`
	SchedulerReady bool   `json:"schedulerReady"`
	Port           int    `json:"-"`
}

func probeHealth(port int, timeout time.Duration) *healthSnapshot {
	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d/api/v1/health", port))
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	var payload healthSnapshot
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		return nil
	}
	payload.Port = port
	return &payload
}

func ticketPath(cfg *options) string {
	return filepath.Join(cfg.dataDir, ticketName)
}

func readTicket(path string, timeout time.Duration) string {
	deadline := time.Now().Add(timeout)
	for {
		if nonce, err := readTicketOnce(path); err == nil && nonce != "" {
			return nonce
		}
		if time.Now().After(deadline) {
			return ""
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func readTicketOnce(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(raw)), nil
}
