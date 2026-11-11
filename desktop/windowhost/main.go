//go:build windows

// Holdexar 原生窗口宿主：自有 Win32 主窗内嵌 WebView2，承载本地 Go 服务子进程。
// 单实例（锁端口+探活身份校验）、一次性引导票据、托盘、关闭意图、退出回收链
// 行为对齐现役桌面壳；版本与更新事实沿用既有 state/ 与 data/ 账本。
package main

import (
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	envPrefix        = "HOLDEXAR_"
	defaultAppName   = "Holdexar"
	defaultPort      = 28765
	defaultLockPort  = 28965
	lockFallbackBase = 28966
	portScanSpan     = 20
	healthTimeout    = 90 * time.Second
	ticketName       = "server-bootstrap.ticket"
	windowClassName  = "HoldexarHostWindow"
	dbName           = "holdexar.db"
)

var lockFallbacks = []int{28966, 28967, 28968, 29965, 29966}

type options struct {
	appName     string
	appVersion  string
	windowTitle string
	serverPort  int
	lockPort    int
	dataDir     string
	installRoot string
	serverExe   string
	devURL      string
	devWatch    bool
	serverMode  bool
	background  bool
	portFree    func(int) bool
}

type hostLog struct {
	mu   sync.Mutex
	file *os.File
}

func (l *hostLog) write(line string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	stamped := time.Now().Format("15:04:05") + " 桌面 · " + line + "\n"
	if l.file != nil {
		_, _ = l.file.WriteString(stamped)
	}
	_, _ = os.Stderr.WriteString(stamped)
}

func (l *hostLog) close() {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.file != nil {
		_ = l.file.Close()
		l.file = nil
	}
}

func envInt(name string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(envPrefix + name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 || value > 65535 {
		return fallback
	}
	return value
}

func envFlag(name string) bool {
	return strings.TrimSpace(os.Getenv(envPrefix+name)) != ""
}

// resolveDataDir 与服务端同源：显式数据目录 → 便携 data/ → 既有库 → 开发指针
// → 开发候选。两侧各自判定必然漂移，票据与健康身份校验都依赖同一路径。
func resolveDataDir(installRoot string) string {
	if explicit := strings.TrimSpace(os.Getenv(envPrefix + "DATA_DIR")); explicit != "" {
		return absPath(explicit)
	}
	if installRoot == "" {
		if base := os.Getenv("LOCALAPPDATA"); base != "" {
			return filepath.Join(base, "holdexar")
		}
		return absPath(".")
	}
	dataDir := filepath.Join(installRoot, "data")
	if fileExists(filepath.Join(installRoot, "portable.flag")) || fileExists(filepath.Join(dataDir, dbName)) {
		return dataDir
	}
	if pointed := readDevPointer(installRoot); pointed != "" && dirWritable(pointed) {
		return pointed
	}
	if dev := filepath.Join(installRoot, "holdexar-dev"); dirWritable(dev) {
		return dev
	}
	return dataDir
}

func readDevPointer(root string) string {
	raw, err := os.ReadFile(filepath.Join(root, "holdexar-dev-home.txt"))
	if err != nil {
		return ""
	}
	text := strings.TrimPrefix(string(raw), "\uFEFF")
	for _, line := range strings.Split(text, "\n") {
		entry := strings.TrimSpace(line)
		if entry == "" || strings.HasPrefix(entry, "#") {
			continue
		}
		if filepath.IsAbs(entry) {
			return entry
		}
		return ""
	}
	return ""
}

func dirWritable(path string) bool {
	if err := os.MkdirAll(path, 0o755); err != nil {
		return false
	}
	probe := filepath.Join(path, ".host-write-probe")
	if err := os.WriteFile(probe, []byte("1"), 0o644); err != nil {
		return false
	}
	_ = os.Remove(probe)
	return true
}

func absPath(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return abs
}

func openLog(dataDir string) *hostLog {
	path := filepath.Join(dataDir, "logs")
	if err := os.MkdirAll(path, 0o755); err != nil {
		return &hostLog{}
	}
	file, err := os.OpenFile(filepath.Join(path, "host.log"), os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		return &hostLog{}
	}
	return &hostLog{file: file}
}

// waitPreviousExit 更新重启链：旧实例退场（释放端口与单实例锁）后再继续，
// 否则端口探活会把交接截胡成一次普通唤醒。
func waitPreviousExit(log *hostLog) {
	raw := strings.TrimSpace(os.Getenv(envPrefix + "PREV_PID"))
	if raw == "" || os.Getenv(envPrefix+"UPDATE_RESTART") != "1" {
		return
	}
	pid, err := strconv.Atoi(raw)
	if err != nil || pid == os.Getpid() {
		return
	}
	if waitPIDExit(pid, 15*time.Second) {
		log.write("前一实例已退出，继续更新重启")
	}
}

// negotiatePorts 服务端口被外来占用时自动扫描备位；锁端口被占依次换备位。
// 探活命中本应用另一实例则保留原值，由唤醒分支处理。
func negotiatePorts(cfg *options, log *hostLog) {
	isFree := cfg.portFree
	if !isFree(cfg.serverPort) {
		if healthIsOurs(cfg.serverPort, cfg, 1500*time.Millisecond) != nil {
			return
		}
		moved := false
		for candidate := cfg.serverPort + 1; candidate < cfg.serverPort+portScanSpan; candidate++ {
			if isFree(candidate) {
				log.write(fmt.Sprintf("服务端口 %d 被占用，自动切换到 %d", cfg.serverPort, candidate))
				cfg.serverPort = candidate
				moved = true
				break
			}
		}
		if !moved {
			fatalMessage(fmt.Sprintf("端口 %d~%d 均被占用，无法启动本地服务。",
				cfg.serverPort, cfg.serverPort+portScanSpan-1))
			log.write("服务端口无可用位，退出")
			os.Exit(1)
		}
	}
	if !isFree(cfg.lockPort) {
		for _, candidate := range lockFallbacks {
			if candidate != cfg.serverPort && isFree(candidate) {
				log.write(fmt.Sprintf("锁端口 %d 被占用，改用 %d", cfg.lockPort, candidate))
				cfg.lockPort = candidate
				return
			}
		}
		log.write("锁端口与备位全被占用，单实例保护降级为探活判定")
	}
}

// healthIsOurs 健康身份校验：应用名一致且数据目录一致才算本应用同库实例。
func healthIsOurs(port int, cfg *options, timeout time.Duration) *healthSnapshot {
	payload := probeHealth(port, timeout)
	if !identityMatches(payload, cfg) {
		return nil
	}
	return payload
}

// identityMatches 身份判定：应用名一致，数据目录一致（并行隔离目录互不唤醒）。
func identityMatches(payload *healthSnapshot, cfg *options) bool {
	if payload == nil || payload.App != cfg.appName {
		return false
	}
	if payload.DataDir != "" && !sameDirPath(payload.DataDir, cfg.dataDir) {
		return false
	}
	return true
}

func runUIOnce(cfg *options, log *hostLog, srv *serverProcess) int {
	app := newApp(cfg, log, srv)
	return app.run()
}

func run(args []string) int {
	if err := setDPIAware(); err != nil {
		// 低版本系统无此入口：忽略，走系统默认缩放
		_ = err
	}
	reportDataProtocol(args)

	cfg := &options{portFree: portFree}
	var dataDirFlag string
	flags := flag.NewFlagSet("windowhost", flag.ContinueOnError)
	flags.BoolVar(&cfg.serverMode, "server", false, "无窗口模式，仅启动本地服务")
	flags.BoolVar(&cfg.background, "background", false, "关闭窗口后继续运行，重新打开应用可显示原窗口")
	flags.StringVar(&dataDirFlag, "data-dir", "", "显式数据目录")
	if err := flags.Parse(args); err != nil {
		return 1
	}
	cfg.installRoot = strings.TrimSpace(os.Getenv(envPrefix + "INSTALL_ROOT"))
	cfg.appName = defaultAppName
	if name := strings.TrimSpace(os.Getenv(envPrefix + "APP_NAME")); name != "" {
		cfg.appName = name
	}
	cfg.appVersion = strings.TrimPrefix(strings.TrimSpace(os.Getenv(envPrefix+"VERSION")), "v")
	if runningFromVersions() {
		// 发行布局：版本号以版本目录名为准（交接比较的事实源）
		exe, e := os.Executable()
		if e == nil {
			cfg.appVersion = filepath.Base(filepath.Dir(exe))
		}
	}
	cfg.devWatch = envFlag("DEV_WATCH")
	cfg.windowTitle = cfg.appName
	if cfg.devWatch {
		cfg.windowTitle = cfg.appName + " Go (dev)"
	}
	cfg.devURL = strings.TrimSpace(os.Getenv(envPrefix + "DEV_URL"))
	cfg.serverPort = envInt("PORT", defaultPort)
	cfg.lockPort = envInt("LOCK_PORT", defaultLockPort)
	cfg.serverExe = strings.TrimSpace(os.Getenv(envPrefix + "SERVER_EXE"))
	if dataDirFlag != "" {
		os.Setenv(envPrefix+"DATA_DIR", absPath(dataDirFlag))
	}
	cfg.dataDir = resolveDataDir(cfg.installRoot)
	if err := os.MkdirAll(cfg.dataDir, 0o755); err != nil {
		fatalMessage("数据目录不可用：" + err.Error())
		return 1
	}
	log := openLog(cfg.dataDir)
	defer log.close()
	log.write(fmt.Sprintf("启动 │ 应用=%s，数据目录=%s，服务端口=%d，锁端口=%d",
		cfg.appName, cfg.dataDir, cfg.serverPort, cfg.lockPort))

	waitPreviousExit(log)
	negotiatePorts(cfg, log)

	lock := acquireLock(cfg.lockPort)
	if lock == nil {
		if payload := healthIsOurs(cfg.serverPort, cfg, 8*time.Second); payload != nil {
			log.write(fmt.Sprintf("已有同库实例在运行（PID %d）", payload.PID))
			if cfg.serverMode {
				if !activateRunning(cfg.lockPort, true) {
					return 1
				}
				return waitForPIDExit(payload.PID)
			}
			if focusWindowByTitle(cfg.windowTitle) {
				return 0
			}
			if activateRunning(cfg.lockPort, false) {
				return 0
			}
			shellOpen(fmt.Sprintf("http://127.0.0.1:%d", cfg.serverPort))
			return 0
		}
		log.write("锁端口被其他程序占用，单实例保护降级为探活判定")
	} else {
		defer lock.Close()
		activation := make(chan string, 4)
		go listenActivation(lock, activation)
		go func() {
			for command := range activation {
				if command == "background" {
					setBackgroundMode(true)
					log.write("已切换为后台常驻模式")
				} else if command == "show" {
					requestForeground()
				}
			}
		}()
	}

	// 更新交接：暂存包就绪且未失败过时交给暂存包内的更新宿主，本进程退出
	// （对齐桌面壳 _handoff_pending_update；仅发行布局——运行目录在 versions/ 内——启用）
	if handoffPending(cfg, func(reason string) { log.write(reason) }) {
		if ok := spawnUpdateHost(cfg, log); ok {
			return 0
		}
	}

	srv, err := startServer(cfg, log)
	if err != nil {
		log.write("本地服务启动失败：" + err.Error())
		// 验收探针子进程与无窗口模式没有可交互用户：快速失败，不弹致命框
		if os.Getenv(envPrefix+"UPDATE_ATTEMPT_ID") == "" && !cfg.serverMode {
			fatalMessage("本地服务启动失败：" + err.Error())
		}
		return 1
	}
	defer srv.cleanup()

	if cfg.serverMode {
		// 服务端启动含数据准备，健康就绪需轮询等待；进程先退则快速失败
		deadline := time.Now().Add(healthTimeout)
		ready := false
		for {
			if payload := healthIsOurs(cfg.serverPort, cfg, 1500*time.Millisecond); payload != nil {
				ready = true
				break
			}
			if !srv.alive() || time.Now().After(deadline) {
				break
			}
			time.Sleep(300 * time.Millisecond)
		}
		if !ready {
			log.write("本地服务健康检查超时")
			fmt.Fprintln(os.Stderr, "服务启动失败：90s 内健康检查未通过")
			return 1
		}
		log.write(fmt.Sprintf("服务已就绪：http://127.0.0.1:%d", cfg.serverPort))
		return srv.waitForExit()
	}
	if cfg.background {
		setBackgroundMode(true)
	}
	return runUIOnce(cfg, log, srv)
}

// acquireLock 占用锁端口实现单实例；占用失败返回 nil，真伪由探活判定。
func acquireLock(port int) net.Listener {
	listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		return nil
	}
	return listener
}

func listenActivation(listener net.Listener, out chan<- string) {
	for {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		func() {
			defer conn.Close()
			_ = conn.SetDeadline(time.Now().Add(time.Second))
			buf := make([]byte, 16)
			n, err := conn.Read(buf)
			if err != nil {
				return
			}
			command := strings.TrimSpace(string(buf[:n]))
			if command == "background" {
				_, _ = conn.Write([]byte("ok"))
			}
			select {
			case out <- command:
			default:
			}
		}()
	}
}

func activateRunning(port int, background bool) bool {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), time.Second)
	if err != nil {
		return false
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(time.Second))
	payload := "show"
	if background {
		payload = "background"
	}
	if _, err := conn.Write([]byte(payload)); err != nil {
		return false
	}
	if !background {
		return true
	}
	buf := make([]byte, 16)
	n, err := conn.Read(buf)
	return err == nil && strings.TrimSpace(string(buf[:n])) == "ok"
}

// waitForPIDExit 返回已退出进程的退出码；等待失败按失败计。
func waitForPIDExit(pid int) int {
	code, ok := pidExitCode(pid, time.Hour)
	if !ok {
		return 1
	}
	return code
}

func reportDataProtocol(args []string) {
	for _, arg := range args {
		if arg == "--update-protocol" {
			payload := []byte(`{"updateDataProtocol":1}`)
			if path := os.Getenv(envPrefix + "UPDATE_PROTOCOL_RESULT"); path != "" {
				_ = os.WriteFile(path, payload, 0o600)
			} else {
				_, _ = fmt.Fprintln(os.Stdout, string(payload))
			}
			os.Exit(0)
		}
	}
}

func serverExecutable(cfg *options) (string, error) {
	if cfg.serverExe != "" {
		if fileExists(cfg.serverExe) {
			return cfg.serverExe, nil
		}
		return "", fmt.Errorf("外部服务程序不存在：%s", cfg.serverExe)
	}
	candidates := []string{
		filepath.Join(exeDir(), "serverd.exe"),
		filepath.Join(exeDir(), "_internal", "serverd.exe"),
	}
	if cfg.installRoot != "" {
		candidates = append(candidates,
			filepath.Join(cfg.installRoot, "_internal", "serverd.exe"),
			filepath.Join(cfg.installRoot, "serverd.exe"),
		)
	}
	for _, candidate := range candidates {
		if fileExists(candidate) {
			return candidate, nil
		}
	}
	return "", errors.New("未找到本地服务程序（serverd.exe）")
}

func exeDir() string {
	exe, err := os.Executable()
	if err != nil {
		return "."
	}
	return filepath.Dir(exe)
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

func main() {
	os.Exit(run(os.Args[1:]))
}
