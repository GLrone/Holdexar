//go:build windows

// 本地服务子进程：生命周期随窗口宿主，退出经标准输入 EOF 走服务端优雅关闭
// （调度、推流、监听与所属代理子进程全部回收）；异常退出再强杀兜底。
package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

type serverProcess struct {
	mu      sync.Mutex
	cmd     *exec.Cmd
	stdin   io.WriteCloser
	logFile *os.File
	exited  chan struct{}
	code    int
	stopped bool
	cfg     *options
	exe     string
}

// restartExitCode 数据库恢复换库的重启请求退出码（跨进程契约，与
// 服务端 internal/systemx.RestartExitCode、开发监视同值）。
const restartExitCode = 75

// startServer 拉起本地服务：健康就绪后服务端把一次性引导票据写入约定文件。
func startServer(cfg *options, log *hostLog) (*serverProcess, error) {
	exe, err := serverExecutable(cfg)
	if err != nil {
		return nil, err
	}
	ticket := ticketPath(cfg)
	_ = os.Remove(ticket) // 清掉上次实例残留的旧票据，避免读到失效 nonce
	logDir := filepath.Join(cfg.dataDir, "logs")
	if err := os.MkdirAll(logDir, 0o755); err != nil {
		return nil, err
	}
	logFile, err := os.OpenFile(filepath.Join(logDir, "serverd.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return nil, err
	}
	cmd := exec.Command(exe,
		"-addr", "127.0.0.1:"+strconv.Itoa(cfg.serverPort),
		"-data-dir", cfg.dataDir,
		"--bootstrap-file", ticket,
	)
	if os.Getenv(envPrefix+"DEV_SESSION") == "1" {
		// 开发验收便利：由环境显式开启，发行态不会设置
		cmd.Args = append(cmd.Args, "--dev-session")
	}
	cmd.Dir = filepath.Dir(exe)
	// 标准输入寿命管道：宿主退出（含硬杀）子进程即收到 EOF 自行优雅退出
	stdin, err := cmd.StdinPipe()
	if err != nil {
		logFile.Close()
		return nil, err
	}
	cmd.Stdout = logFile
	cmd.Stderr = logFile
	env := append(os.Environ(), envPrefix+"DEV_CHILD=1")
	cmd.Env = env
	if err := cmd.Start(); err != nil {
		stdin.Close()
		logFile.Close()
		return nil, err
	}
	log.write(fmt.Sprintf("本地服务进程 %s（PID %d）", filepath.Base(exe), cmd.Process.Pid))
	srv := &serverProcess{cmd: cmd, stdin: stdin, logFile: logFile, exited: make(chan struct{}), cfg: cfg, exe: exe}
	go func() {
		err := cmd.Wait()
		srv.mu.Lock()
		srv.code = 1
		if exitErr, ok := err.(*exec.ExitError); ok {
			srv.code = exitErr.ExitCode()
		} else if err == nil {
			srv.code = 0
		}
		srv.mu.Unlock()
		close(srv.exited)
	}()
	return srv, nil
}

// stop 退出链：先给优雅关闭窗口（服务端自行回收调度/推流/代理子进程），超时强杀。
func (s *serverProcess) stop(log *hostLog) {
	s.mu.Lock()
	if s.stopped {
		s.mu.Unlock()
		return
	}
	s.stopped = true
	cmd := s.cmd
	s.mu.Unlock()
	if cmd == nil || cmd.Process == nil {
		return
	}
	_ = s.stdin.Close() // EOF → 服务端停止接收并走完整关闭路径
	select {
	case <-s.exited:
	case <-time.After(20 * time.Second):
		log.write("本地服务优雅关闭超时，强制结束")
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

// restart 恢复换库后的同参重启：旧进程已退场（端口与句柄随之释放），
// 新进程按原命令行拉起并发布新引导票据。
func (s *serverProcess) restart(log *hostLog) (*serverProcess, error) {
	s.mu.Lock()
	if s.stdin != nil {
		_ = s.stdin.Close()
	}
	s.mu.Unlock()
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
	if s.logFile != nil {
		_ = s.logFile.Close()
	}
}

type healthSnapshot struct {
	Status         string `json:"status"`
	App            string `json:"app"`
	Version        string `json:"version"`
	PID            int    `json:"pid"`
	DataDir        string `json:"dataDir"`
	SchedulerReady bool   `json:"schedulerReady"`
}

// probeHealth 健康端点快照；网络错误与非 200 一律按不可用处理。
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
	return &payload
}

func ticketPath(cfg *options) string {
	return filepath.Join(cfg.dataDir, ticketName)
}

// readTicket 轮询票据文件取一次性 nonce（服务端健康就绪后写入）。
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
