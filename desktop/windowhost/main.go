//go:build windows

package main

import (
	"crypto/sha256"
	"errors"
	"flag"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"holdexar/hostkit"
)

// buildVersion 构建期由 -ldflags "-X main.buildVersion=..." 注入。
var buildVersion string

const (
	envPrefix            = "HOLDEXAR_"
	defaultAppName       = "Holdexar"
	defaultPort          = 28765
	defaultLockPort      = 28965
	portScanSpan         = 20
	healthTimeout        = 90 * time.Second
	migrationMaxDuration = 1800 * time.Second
	ticketName           = "server-bootstrap.ticket"
	activationPortFile   = "windowhost.port"
	windowClassName      = "HoldexarHostWindow"
	dbName               = "holdexar.db"
)

var (
	lockFallbacks = []int{28966, 28967, 28968, 29965, 29966}
	staleEnvKeys  = []string{
		"UPDATE_ATTEMPT_ID", "UPDATE_DATA_TOKEN", "LAUNCHER_PID",
		"DEV_URL", "UPDATE_RESTART", "PREV_PID",
	}
)

type options struct {
	appName     string
	appVersion  string
	windowTitle string
	serverPort  int
	lockPort    int
	dataDir     string
	installRoot string
	versionDir  string
	serverExe   string
	devURL      string
	devMode     bool
	devWatch    bool
	serverMode  bool
	background  bool
	isManaged   bool
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

// resolveDataDir 委托 hostkit.ResolveDataDir 解析活动数据目录（无机器级用户目录回退）。
func resolveDataDir(installRoot string) string {
	explicit := strings.TrimSpace(os.Getenv(envPrefix + "DATA_DIR"))
	dir, err := hostkit.ResolveDataDir(hostkit.DataDirOptions{
		ExplicitDir: explicit,
		InstallRoot: installRoot,
	})
	if err == nil && dir != "" {
		return dir
	}
	dir, err = hostkit.ResolveDataDir(hostkit.DataDirOptions{
		ExplicitDir:        explicit,
		InstallRoot:        installRoot,
		SkipEnsureWritable: true,
	})
	if err == nil && dir != "" {
		return dir
	}
	if installRoot != "" {
		return filepath.Join(hostkit.CanonicalPath(installRoot), "data")
	}
	return absPath("data")
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

func captureRestartPrevPID() int {
	raw := strings.TrimSpace(os.Getenv(envPrefix + "PREV_PID"))
	if raw == "" || os.Getenv(envPrefix+"UPDATE_RESTART") != "1" {
		return 0
	}
	pid, err := strconv.Atoi(raw)
	if err != nil || pid <= 0 || pid == os.Getpid() {
		return 0
	}
	return pid
}

func waitPreviousPID(pid int, log *hostLog) {
	if pid <= 0 || pid == os.Getpid() {
		return
	}
	if waitPIDExit(pid, 15*time.Second) && log != nil {
		log.write("前一实例已退出，继续更新重启")
	}
}

func instanceMutexName(dataDir string) string {
	canon := strings.ToLower(filepath.Clean(hostkit.CanonicalPath(dataDir)))
	sum := sha256.Sum256([]byte(canon))
	return fmt.Sprintf(`Local\Holdexar-WindowHost-%x`, sum[:12])
}

func acquireInstanceMutex(dataDir string) (bool, func(), error) {
	return acquireNamedMutex(instanceMutexName(dataDir))
}

func activationPortPath(dataDir string) string {
	return filepath.Join(dataDir, activationPortFile)
}

func writeActivationPort(dataDir string, port int) error {
	target := activationPortPath(dataDir)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(target), "windowhost.port.*.tmp")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if _, err := tmp.WriteString(strconv.Itoa(port) + "\n"); err != nil {
		_ = tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(name, target)
}

func removeActivationPort(dataDir string) {
	_ = os.Remove(activationPortPath(dataDir))
}

func readActivationPortOnce(dataDir string) int {
	raw, err := os.ReadFile(activationPortPath(dataDir))
	if err != nil {
		return 0
	}
	port, err := strconv.Atoi(strings.TrimSpace(string(raw)))
	if err != nil || port <= 0 || port > 65535 {
		return 0
	}
	return port
}

func waitActivationPort(dataDir string, timeout time.Duration) int {
	deadline := time.Now().Add(timeout)
	for {
		if port := readActivationPortOnce(dataDir); port > 0 {
			return port
		}
		if time.Now().After(deadline) {
			return 0
		}
		time.Sleep(50 * time.Millisecond)
	}
}

func bindActivationListener(cfg *options, log *hostLog) (net.Listener, int, error) {
	candidates := append([]int{cfg.lockPort}, lockFallbacks...)
	for _, port := range candidates {
		if port <= 0 || port == cfg.serverPort {
			continue
		}
		listener, err := net.Listen("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
		if err == nil {
			if port != cfg.lockPort {
				log.write(fmt.Sprintf("唤醒端口 %d 被占用，改用 %d", cfg.lockPort, port))
			}
			cfg.lockPort = port
			return listener, port, nil
		}
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return nil, 0, err
	}
	port := listener.Addr().(*net.TCPAddr).Port
	cfg.lockPort = port
	log.write(fmt.Sprintf("唤醒端口自动分配为 %d", port))
	return listener, port, nil
}

// negotiatePorts 端口被外来占用时换备位；若为同库实例则保留原值。
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
				log.write(fmt.Sprintf("唤醒端口 %d 被占用，改用 %d", cfg.lockPort, candidate))
				cfg.lockPort = candidate
				return
			}
		}
	}
}

func findHealthyOurs(cfg *options, timeout time.Duration) *healthSnapshot {
	deadline := time.Now().Add(timeout)
	for {
		for candidate := cfg.serverPort; candidate < cfg.serverPort+portScanSpan; candidate++ {
			if payload := healthIsOurs(candidate, cfg, 800*time.Millisecond); payload != nil {
				return payload
			}
		}
		if time.Now().After(deadline) {
			return nil
		}
		time.Sleep(150 * time.Millisecond)
	}
}

func activateExistingInstance(cfg *options, log *hostLog) int {
	actPort := waitActivationPort(cfg.dataDir, 5*time.Second)
	if cfg.serverMode {
		if actPort <= 0 || !activateRunning(actPort, true) {
			return 1
		}
		payload := findHealthyOurs(cfg, 8*time.Second)
		if payload == nil {
			return 1
		}
		log.write(fmt.Sprintf("已有同库实例在运行（PID %d）", payload.PID))
		return waitForPIDExit(payload.PID)
	}
	log.write("已有同库实例在运行，唤醒现有窗口")
	if actPort > 0 && activateRunning(actPort, false) {
		return 0
	}
	if focusWindowByTitle(cfg.windowTitle) {
		return 0
	}
	if payload := findHealthyOurs(cfg, 2*time.Second); payload != nil {
		shellOpen(fmt.Sprintf("http://127.0.0.1:%d", payload.Port))
	}
	return 0
}

func healthIsOurs(port int, cfg *options, timeout time.Duration) *healthSnapshot {
	payload := probeHealth(port, timeout)
	if !identityMatches(payload, cfg) {
		return nil
	}
	return payload
}

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

func launcherRoot() (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", err
	}
	resolved := hostkit.CanonicalPath(executable)
	if resolved == "" {
		return "", errors.New("无法解析程序所在目录")
	}
	return filepath.Dir(resolved), nil
}

func cleanupOldExecutables(directory string) {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if !entry.IsDir() && strings.Contains(entry.Name(), ".old_") {
			_ = os.Remove(filepath.Join(directory, entry.Name()))
		}
	}
}

func regularFileExists(path string) bool {
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular()
}

func recoverInterruptedUpdate(root string) error {
	release, err := acquireUpdateMutex()
	if err != nil {
		return err
	}
	tx, txErr := hostkit.ReadTransaction(root)
	if txErr != nil {
		release()
		return errors.New("更新事务无法读取，已停止启动并保留数据")
	}
	if tx != nil {
		if attempt, ok := tx["attemptId"].(string); !ok || attempt == "" {
			release()
			return errors.New("更新事务缺少有效标识，已停止启动并保留数据")
		}
	}
	if !hostkit.UpdateRecoveryNeeded(tx) {
		release()
		return nil
	}
	state, stateErr := hostkit.ReadCurrentState(root)
	if stateErr != nil {
		release()
		return stateErr
	}
	complete := hostkit.UpdateRecoveryCompleteForRoot(root, tx, state)
	release()
	if complete {
		return nil
	}
	host := filepath.Join(root, "updater", "UpdateHost.exe")
	if !regularFileExists(host) {
		return errors.New("独立更新宿主缺失或被重定向，已停止启动以保护数据")
	}
	if _, err := hostkit.TrustedPath(host); err != nil {
		return errors.New("独立更新宿主缺失或被重定向，已停止启动以保护数据")
	}
	command := exec.Command(host, "--recover-only", "--target", root)
	command.Dir = root
	command.Env = append(os.Environ(), envPrefix+"INSTALL_ROOT="+root)
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	command.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	if err := command.Run(); err != nil {
		return fmt.Errorf("上次更新恢复未完成，数据已保留：%w", err)
	}
	return nil
}

func activeManagedDataDir(root string, compat *hostkit.DataCompat) string {
	if compat != nil && compat.DataDir != "" {
		return hostkit.CanonicalPath(compat.DataDir)
	}
	if tx, err := hostkit.ReadTransaction(root); err == nil && tx != nil {
		if dataDir, ok := tx["dataDir"].(string); ok && dataDir != "" {
			return hostkit.CanonicalPath(dataDir)
		}
	}
	if explicit := strings.TrimSpace(os.Getenv(envPrefix + "DATA_DIR")); explicit != "" {
		return hostkit.CanonicalPath(explicit)
	}
	return hostkit.CanonicalPath(filepath.Join(root, "data"))
}

func sanitizedVersionEnv(root, dataDir string) []string {
	env := make([]string, 0, len(os.Environ())+3)
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		skip := strings.EqualFold(key, envPrefix+"INSTALL_ROOT") ||
			strings.EqualFold(key, envPrefix+"DATA_DIR") ||
			strings.EqualFold(key, "WEBVIEW2_USER_DATA_FOLDER")
		for _, stale := range staleEnvKeys {
			skip = skip || strings.EqualFold(key, envPrefix+stale)
		}
		if !skip {
			env = append(env, entry)
		}
	}
	return append(env,
		envPrefix+"INSTALL_ROOT="+root,
		envPrefix+"DATA_DIR="+dataDir,
		"WEBVIEW2_USER_DATA_FOLDER="+filepath.Join(dataDir, "cache", "webview"),
	)
}

func launchVersion(root, targetVersion string, args []string) (int, error) {
	exe := filepath.Join(root, "versions", targetVersion, hostkit.AppName+".exe")
	if !regularFileExists(exe) {
		return 1, fmt.Errorf("启动 %s 失败：版本主程序不存在：%s", targetVersion, exe)
	}
	if _, err := hostkit.TrustedPath(exe); err != nil {
		return 1, fmt.Errorf("启动 %s 失败：版本主程序路径不受信任：%s", targetVersion, exe)
	}
	compat, err := hostkit.ReadDataCompatibility(root)
	if err != nil {
		return 1, err
	}
	dataDir := activeManagedDataDir(root, compat)
	env := sanitizedVersionEnv(root, dataDir)

	foreground := false
	for _, arg := range args {
		if arg == "--server" || arg == "-server" {
			foreground = true
			break
		}
	}
	fullArgs := append([]string{exe}, args...)
	attr := &os.ProcAttr{Dir: filepath.Dir(exe), Env: env, Files: []*os.File{os.Stdin, os.Stdout, os.Stderr}}
	if foreground {
		process, err := os.StartProcess(exe, fullArgs, attr)
		if err != nil {
			return 1, fmt.Errorf("启动 %s 失败：%w", targetVersion, err)
		}
		state, err := process.Wait()
		if err != nil {
			return 1, fmt.Errorf("启动 %s 失败：%w", targetVersion, err)
		}
		return state.ExitCode(), nil
	}
	attr.Sys = &syscall.SysProcAttr{
		CreationFlags: 0x00000008 | 0x00000200,
	}
	process, err := os.StartProcess(exe, fullArgs, attr)
	if err != nil {
		return 1, fmt.Errorf("启动 %s 失败：%w", targetVersion, err)
	}
	_ = process.Release()
	return 0, nil
}

func prepareManagedRoot(root, buildVer string, argv []string) (string, string, string, int, bool, error) {
	if !probeSwapMutexHeld(swapMutexName) {
		if err := recoverInterruptedUpdate(root); err != nil {
			return "", "", "", 1, false, err
		}
		for _, dir := range []string{"state", "data", "logs", "versions", "updater", "update-staging"} {
			if err := os.MkdirAll(filepath.Join(root, dir), 0o755); err != nil {
				return "", "", "", 1, false, err
			}
		}
		cleanupOldExecutables(filepath.Join(root, "updater"))
		cleanupOldExecutables(root)
	}

	state, err := hostkit.ReadCurrentState(root)
	if err != nil {
		return "", "", "", 1, false, err
	}
	targetVersion, err := hostkit.SelectTargetVersion(root, state)
	if err != nil {
		return "", "", "", 1, false, err
	}
	if targetVersion == "" {
		return "", "", "", 1, false, fmt.Errorf("未能在 %s 找到任何可运行的有效版本。\n请检查安装目录或重新解压完整发行包。",
			filepath.Join(root, "versions"))
	}
	compat, err := hostkit.ReadDataCompatibility(root)
	if err != nil {
		return "", "", "", 1, false, err
	}
	managedDataDir := activeManagedDataDir(root, compat)

	normBuild := hostkit.VersionFromTag(buildVer)
	if normBuild != "" && hostkit.VersionFromTag(targetVersion) == normBuild {
		verDir, verr := hostkit.VersionDir(root, targetVersion)
		if verr != nil {
			return "", "", "", 1, false, verr
		}
		return verDir, targetVersion, managedDataDir, 0, false, nil
	}
	code, lerr := launchVersion(root, targetVersion, argv)
	return "", targetVersion, managedDataDir, code, true, lerr
}

func runLauncher(argv []string, root string) (int, error) {
	_, _, _, code, _, err := prepareManagedRoot(root, "", argv)
	return code, err
}

func clearStaleEnvInProcess() {
	for _, key := range staleEnvKeys {
		_ = os.Unsetenv(envPrefix + key)
	}
}

func run(args []string) int {
	if handled, err := reportProtocol(args); handled {
		if err != nil {
			fatalMessageForArgs(args, err.Error())
			return 1
		}
		return 0
	}

	cfg := &options{portFree: portFree}
	var dataDirFlag string
	var portFlag int
	flags := flag.NewFlagSet("Holdexar", flag.ContinueOnError)
	flags.SetOutput(os.Stdout)
	flags.BoolVar(&cfg.serverMode, "server", false, "无窗口模式，仅启动本地服务")
	flags.BoolVar(&cfg.background, "background", false, "关闭窗口后继续运行，重新打开应用可显示原窗口")
	flags.StringVar(&dataDirFlag, "data-dir", "", "显式数据目录")
	flags.IntVar(&portFlag, "port", 0, "指定本地服务端口")
	flags.BoolVar(&cfg.devMode, "dev", false, "启动前端热更新开发服务")
	if err := flags.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		return 1
	}
	if portFlag < 0 || portFlag > 65535 {
		fatalMessageForArgs(args, fmt.Sprintf("无效端口：%d", portFlag))
		return 1
	}

	if err := setDPIAware(); err != nil {
		_ = err
	}

	prevPID := captureRestartPrevPID()
	exeRoot, exeVersionDir, exeManaged := hostkit.InstallRootFromExe("")

	if exeManaged && exeVersionDir == "" && !cfg.devMode {
		clearStaleEnvInProcess()
		waitPreviousPID(prevPID, nil)
		prevPID = 0
		if swapMutexHeld() {
			if cfg.background {
				return yieldExitCode
			}
			return 0
		}
		verDir, targetVer, managedDataDir, chainCode, chainExited, err := prepareManagedRoot(exeRoot, buildVersion, args)
		if err != nil {
			fatalMessageForArgs(args, err.Error())
			return 1
		}
		if chainExited {
			return chainCode
		}
		cfg.installRoot = exeRoot
		cfg.versionDir = verDir
		cfg.isManaged = true
		cfg.appVersion = targetVer
		cfg.dataDir = managedDataDir
		_ = os.Setenv(envPrefix+"INSTALL_ROOT", exeRoot)
		if dataDirFlag == "" {
			_ = os.Setenv(envPrefix+"DATA_DIR", managedDataDir)
		}
	} else if exeManaged && exeVersionDir != "" && !cfg.devMode {
		cfg.installRoot = exeRoot
		cfg.versionDir = exeVersionDir
		cfg.isManaged = true
		cfg.appVersion = filepath.Base(exeVersionDir)
	} else {
		cfg.installRoot = strings.TrimSpace(os.Getenv(envPrefix + "INSTALL_ROOT"))
		if cfg.installRoot == "" {
			cfg.installRoot = exeRoot
		}
		cfg.appVersion = hostkit.VersionFromTag(os.Getenv(envPrefix + "VERSION"))
		if cfg.appVersion == "" {
			cfg.appVersion = hostkit.VersionFromTag(buildVersion)
		}
		cfg.devWatch = envFlag("DEV_WATCH")
		cfg.devURL = strings.TrimSpace(os.Getenv(envPrefix + "DEV_URL"))
	}

	cfg.appName = defaultAppName
	if name := strings.TrimSpace(os.Getenv(envPrefix + "APP_NAME")); name != "" {
		cfg.appName = name
	}
	cfg.windowTitle = cfg.appName
	if cfg.devWatch || cfg.devMode {
		cfg.windowTitle = cfg.appName + " Go (dev)"
	}
	cfg.serverPort = envInt("PORT", defaultPort)
	if portFlag > 0 {
		cfg.serverPort = portFlag
	}
	cfg.lockPort = envInt("LOCK_PORT", defaultLockPort)
	cfg.serverExe = strings.TrimSpace(os.Getenv(envPrefix + "SERVER_EXE"))
	if dataDirFlag != "" {
		absExplicit := hostkit.CanonicalPath(dataDirFlag)
		_ = os.Setenv(envPrefix+"DATA_DIR", absExplicit)
		cfg.dataDir = absExplicit
	}
	if cfg.dataDir == "" {
		cfg.dataDir = resolveDataDir(cfg.installRoot)
	}
	if err := os.MkdirAll(cfg.dataDir, 0o755); err != nil {
		fatalMessageForArgs(args, "数据目录不可用："+err.Error())
		return 1
	}

	log := openLog(cfg.dataDir)
	defer log.close()
	log.write(fmt.Sprintf("启动 │ 应用=%s，版本=%s，数据目录=%s，服务端口=%d",
		cfg.appName, cfg.appVersion, cfg.dataDir, cfg.serverPort))

	watchLauncher(cfg, log, func() {
		if a := activeApp.Load(); a != nil && a.hwnd != 0 {
			call(procPostMessageW, a.hwnd, wmAppQuit, 0, 0)
			return
		}
		os.Exit(0)
	})

	waitPreviousPID(prevPID, log)

	held, releaseInstance, mutexErr := acquireInstanceMutex(cfg.dataDir)
	if mutexErr != nil {
		fatalMessageForArgs(args, "单实例互斥体创建失败："+mutexErr.Error())
		return 1
	}
	if !held {
		return activateExistingInstance(cfg, log)
	}
	defer releaseInstance()
	removeActivationPort(cfg.dataDir)

	if swapMutexHeld() {
		log.write("检测到换装进程进行中，本实例退出等待其完成")
		if cfg.background {
			return yieldExitCode
		}
		return 0
	}

	if handoffPending(cfg, func(reason string) { log.write(reason) }) {
		if ok := spawnUpdateHost(cfg, log); ok {
			return 0
		}
	}

	negotiatePorts(cfg, log)

	lock, actPort, listenErr := bindActivationListener(cfg, log)
	if listenErr == nil && lock != nil {
		_ = writeActivationPort(cfg.dataDir, actPort)
		defer removeActivationPort(cfg.dataDir)
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
	} else {
		log.write("唤醒监听绑定失败，仅保留互斥体单实例保护")
	}

	if cfg.devMode && !cfg.serverMode && cfg.devURL == "" {
		devSrv, derr := startDevFrontend(cfg, log)
		if derr != nil {
			log.write("前端开发服务启动失败：" + derr.Error())
			fatalMessageForArgs(args, "前端开发服务启动失败："+derr.Error())
			return 1
		}
		defer devSrv.stop()
		cfg.devURL = devSrv.url
	}

	srv, err := startServer(cfg, log)
	if err != nil {
		log.write("本地服务启动失败：" + err.Error())
		if os.Getenv(envPrefix+"UPDATE_ATTEMPT_ID") == "" && !cfg.serverMode {
			fatalMessageForArgs(args, "本地服务启动失败："+err.Error())
		}
		return 1
	}
	defer srv.cleanup()

	if cfg.serverMode {
		deadline := time.Now().Add(healthTimeout)
		protected := os.Getenv(envPrefix+"UPDATE_ATTEMPT_ID") != ""
		budget := time.Now().Add(migrationMaxDuration)
		ready := false
		for {
			if payload := healthIsOurs(cfg.serverPort, cfg, 1500*time.Millisecond); payload != nil {
				ready = true
				break
			}
			if !srv.alive() {
				break
			}
			if protected {
				if time.Now().After(budget) {
					break
				}
			} else if time.Now().After(deadline) {
				break
			}
			time.Sleep(300 * time.Millisecond)
		}
		if !ready {
			log.write("本地服务健康检查超时")
			fmt.Fprintln(os.Stderr, "服务启动失败：健康检查未通过")
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

var (
	openLauncherHandle = openProcessHandle
	waitLauncherExit   = waitHandleInfinite
)

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}

func launcherPID() int {
	raw := strings.TrimSpace(os.Getenv(envPrefix + "LAUNCHER_PID"))
	if !isDigits(raw) {
		return 0
	}
	pid, err := strconv.Atoi(raw)
	if err != nil || pid <= 0 || pid == os.Getpid() {
		return 0
	}
	return pid
}

func launcherExempt(cfg *options) bool {
	if cfg.background || cfg.serverMode {
		return true
	}
	if a := activeApp.Load(); a != nil {
		return a.background.Load()
	}
	return false
}

// watchLauncher 监视父启动器 PID，非后台实例随其退出；句柄打不开不阻塞启动。
func watchLauncher(cfg *options, log *hostLog, onGone func()) {
	if cfg.background || cfg.serverMode {
		return
	}
	pid := launcherPID()
	if pid == 0 {
		return
	}
	handle := openLauncherHandle(pid)
	if handle == 0 {
		return
	}
	go func() {
		waitLauncherExit(handle)
		call(procCloseHandle, handle)
		if launcherExempt(cfg) {
			return
		}
		log.write(fmt.Sprintf("启动器进程 %d 已终止，本实例随之退出", pid))
		onGone()
	}()
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
			if command == "background" || command == "show" {
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
	if port <= 0 {
		return false
	}
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
	buf := make([]byte, 16)
	n, err := conn.Read(buf)
	return err == nil && strings.TrimSpace(string(buf[:n])) == "ok"
}

func waitForPIDExit(pid int) int {
	code, ok := pidExitCode(pid, time.Hour)
	if !ok {
		return 1
	}
	return code
}

func reportProtocol(args []string) (bool, error) {
	for _, arg := range args {
		if arg == "--update-protocol" {
			payload := []byte(`{"updateDataProtocol":1}`)
			if path := os.Getenv(envPrefix + "UPDATE_PROTOCOL_RESULT"); path != "" {
				return true, os.WriteFile(path, payload, 0o600)
			}
			_, err := fmt.Fprintln(os.Stdout, string(payload))
			return true, err
		}
	}
	return false, nil
}

func serverExecutable(cfg *options) (string, error) {
	if cfg.serverExe != "" {
		if fileExists(cfg.serverExe) {
			return cfg.serverExe, nil
		}
		return "", fmt.Errorf("外部服务程序不存在：%s", cfg.serverExe)
	}
	if cfg.versionDir != "" {
		return hostkit.ResolveVersionServerExe(cfg.versionDir)
	}
	candidates := []string{
		filepath.Join(exeDir(), "serverd.exe"),
		filepath.Join(exeDir(), "_internal", "serverd.exe"),
	}
	if cfg.installRoot != "" {
		candidates = append(candidates,
			filepath.Join(cfg.installRoot, "serverd.exe"),
			filepath.Join(cfg.installRoot, "_internal", "serverd.exe"),
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
