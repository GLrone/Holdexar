//go:build windows

package main

import (
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"holdexar/hostkit"
)

var (
	modIphlpapi             = windows.NewLazySystemDLL("iphlpapi.dll")
	procGetExtendedTcpTable = modIphlpapi.NewProc("GetExtendedTcpTable")

	modKernelJob                 = windows.NewLazySystemDLL("kernel32.dll")
	procCreateJobObjectW         = modKernelJob.NewProc("CreateJobObjectW")
	procSetInformationJobObject  = modKernelJob.NewProc("SetInformationJobObject")
	procAssignProcessToJobObject = modKernelJob.NewProc("AssignProcessToJobObject")
)

// allListeningPorts 枚举全量 TCP 监听端口：HTTP 监听者属孙进程 serverd，归属由健康载荷与镜像路径核对。
func allListeningPorts() []int {
	const query = 3
	var size uint32
	ret, _, _ := procGetExtendedTcpTable.Call(0, uintptr(unsafe.Pointer(&size)), 0, 2, query, 0)
	if ret != 122 && ret != 0 {
		return nil
	}
	buf := make([]byte, size)
	ret, _, _ = procGetExtendedTcpTable.Call(
		uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)), 0, 2, query, 0)
	if ret != 0 {
		return nil
	}
	count := *(*uint32)(unsafe.Pointer(&buf[0]))
	rowSize := unsafe.Sizeof(uint32(0)) * 6
	base := unsafe.Pointer(&buf[4])
	ports := make([]int, 0, count)
	for i := uint32(0); i < count; i++ {
		row := (*[6]uint32)(unsafe.Pointer(uintptr(base) + uintptr(i)*rowSize))
		port := uint16(row[2]>>8&0xFF) | uint16(row[2]&0xFF)<<8
		ports = append(ports, int(port))
	}
	return ports
}

func listeningPortsOf(pid int) []int {
	const query = 3
	var size uint32
	_ = procGetExtendedTcpTable.Find()
	ret, _, _ := procGetExtendedTcpTable.Call(0, uintptr(unsafe.Pointer(&size)), 0, 2, query, 0)
	if ret != 122 && ret != 0 {
		return nil
	}
	buf := make([]byte, size)
	ret, _, _ = procGetExtendedTcpTable.Call(
		uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)), 0, 2, query, 0)
	if ret != 0 {
		return nil
	}
	count := *(*uint32)(unsafe.Pointer(&buf[0]))
	ports := make([]int, 0, count)
	rowSize := unsafe.Sizeof(uint32(0)) * 6
	base := unsafe.Pointer(&buf[4])
	for i := uint32(0); i < count; i++ {
		row := (*[6]uint32)(unsafe.Pointer(uintptr(base) + uintptr(i)*rowSize))
		if int(row[5]) != pid {
			continue
		}
		port := uint16(row[2]>>8&0xFF) | uint16(row[2]&0xFF)<<8
		ports = append(ports, int(port))
	}
	return ports
}

// answersHTTP 端口秒级探活过滤（单实例唤醒端口不应答 HTTP，快速跳过）。
func answersHTTP(port int) bool {
	conn, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), answerHTTPTimeout)
	if err != nil {
		return false
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(answerHTTPTimeout))
	_, err = conn.Write([]byte("GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"))
	if err != nil {
		return false
	}
	buf := make([]byte, 128)
	_, err = conn.Read(buf)
	return err == nil
}

func httpGetJSON(port int, path string, timeout time.Duration) (int, map[string]any, error) {
	client := &http.Client{Timeout: timeout}
	resp, err := client.Get(fmt.Sprintf("http://127.0.0.1:%d%s", port, path))
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return resp.StatusCode, nil, err
	}
	var payload map[string]any
	if json.Unmarshal(body, &payload) != nil {
		return resp.StatusCode, nil, fmt.Errorf("响应不是 JSON")
	}
	return resp.StatusCode, payload, nil
}

func samePath(first, second string) bool {
	return hostkit.SamePath(first, second)
}

func isUnderPath(child, parent string) bool {
	return hostkit.IsUnderPath(child, parent)
}

func processImage(pid int) string {
	handle, err := windows.OpenProcess(windows.PROCESS_QUERY_LIMITED_INFORMATION, false, uint32(pid))
	if err != nil {
		return ""
	}
	defer windows.CloseHandle(handle)
	buf := make([]uint16, 1024)
	size := uint32(len(buf))
	if err := windows.QueryFullProcessImageName(handle, 0, &buf[0], &size); err != nil {
		return ""
	}
	return windows.UTF16ToString(buf[:size])
}

func pidAlive(pid int) bool {
	if pid <= 0 {
		return false
	}
	handle, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(pid))
	if err != nil {
		return false
	}
	defer windows.CloseHandle(handle)
	event, _ := windows.WaitForSingleObject(handle, 0)
	return event == uint32(windows.WAIT_TIMEOUT)
}

// probeTree 验收期进程树 Job 对象：未验收通过或宿主崩溃时随句柄关闭终止；验收通过后先解除标志再关句柄以保持存活。
type probeTree struct {
	job windows.Handle
}

func assignProbeTree(proc *os.Process) *probeTree {
	job, _, _ := procCreateJobObjectW.Call(0, 0)
	if job == 0 {
		return nil
	}
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	info.BasicLimitInformation.LimitFlags = windows.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
	ret, _, _ := procSetInformationJobObject.Call(
		job,
		uintptr(windows.JobObjectExtendedLimitInformation),
		uintptr(unsafe.Pointer(&info)),
		uintptr(unsafe.Sizeof(info)),
	)
	if ret == 0 {
		_ = windows.CloseHandle(windows.Handle(job))
		return nil
	}
	procHandle, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(proc.Pid))
	if err != nil || procHandle == 0 {
		_ = windows.CloseHandle(windows.Handle(job))
		return nil
	}
	defer windows.CloseHandle(procHandle)
	assigned, _, _ := procAssignProcessToJobObject.Call(job, uintptr(procHandle))
	if assigned == 0 {
		_ = windows.CloseHandle(windows.Handle(job))
		return nil
	}
	return &probeTree{job: windows.Handle(job)}
}

func (t *probeTree) disarm() error {
	if t == nil || t.job == 0 {
		return nil
	}
	var info windows.JOBOBJECT_EXTENDED_LIMIT_INFORMATION
	info.BasicLimitInformation.LimitFlags = 0
	ret, _, callErr := procSetInformationJobObject.Call(
		uintptr(t.job),
		uintptr(windows.JobObjectExtendedLimitInformation),
		uintptr(unsafe.Pointer(&info)),
		uintptr(unsafe.Sizeof(info)),
	)
	if ret == 0 {
		return callErr
	}
	return nil
}

func (t *probeTree) detach() {
	if t != nil && t.job != 0 {
		_ = t.disarm()
		_ = windows.CloseHandle(t.job)
		t.job = 0
	}
}

func (t *probeTree) stop(proc *os.Process) {
	if t != nil && t.job != 0 {
		_ = windows.TerminateJobObject(t.job, 1)
		_ = windows.CloseHandle(t.job)
		t.job = 0
	}
	if proc != nil {
		_ = proc.Kill()
	}
}

// 验收窗管迁移后就绪；迁移期由只读信号推进刷新空闲窗（var 供测试收缩），总上限兜底。
var (
	healthTimeout           = 90 * time.Second
	migrationSignalInterval = 2 * time.Second
	migrationIdleWindow     = 300 * time.Second
	migrationMaxDuration    = 1800 * time.Second
)

type migrationSignal struct {
	schemaKnown bool
	schema      int64
	snapshot    int64
}

func (s migrationSignal) differsFrom(other migrationSignal) bool {
	if s.snapshot != other.snapshot {
		return true
	}
	if s.schemaKnown != other.schemaKnown {
		return true
	}
	return s.schemaKnown && s.schema != other.schema
}

func (s migrationSignal) schemaDisplay() string {
	if !s.schemaKnown {
		return "unknown"
	}
	return strconv.FormatInt(s.schema, 10)
}

// readMigrationSignal 只读探测迁移推进，瞬时写锁冲突按无信号忽略。
func readMigrationSignal(dataDir string) migrationSignal {
	signal := migrationSignal{snapshot: -1}
	if matches, _ := filepath.Glob(filepath.Join(dataDir, "*.pre-migration.bak")); len(matches) > 0 {
		if info, err := os.Stat(matches[0]); err == nil {
			signal.snapshot = info.Size()
		}
	}
	if version, err := schemaVersionOf(dbPathOf(dataDir)); err == nil {
		signal.schema, signal.schemaKnown = int64(version), true
	}
	return signal
}

type migrationWatch struct {
	deadline        time.Time
	migrationCap    time.Time
	signal          migrationSignal
	nextSignalCheck time.Time
}

func newMigrationWatch(started time.Time, timeout time.Duration) *migrationWatch {
	return &migrationWatch{
		deadline:        started.Add(timeout),
		migrationCap:    started.Add(migrationMaxDuration),
		signal:          migrationSignal{snapshot: -1},
		nextSignalCheck: started,
	}
}

func (w *migrationWatch) observe(root, dataDir string, now time.Time) bool {
	if now.After(w.migrationCap) {
		return true
	}
	if !now.Before(w.nextSignalCheck) {
		w.nextSignalCheck = now.Add(migrationSignalInterval)
		if latest := readMigrationSignal(dataDir); latest.differsFrom(w.signal) {
			w.signal = latest
			if extended := now.Add(migrationIdleWindow); extended.After(w.deadline) {
				w.deadline = extended
			}
			logUpdate(root, fmt.Sprintf("迁移推进：schema=%s 快照字节=%d", w.signal.schemaDisplay(), w.signal.snapshot))
		}
	}
	return false
}

type probeOptions struct {
	requireWindow bool
	keepRunning   bool
	requireGuard  *bool
}

func probeVersionHealth(root, targetVersion, attemptID string, dataDir string, opts probeOptions) (bool, string) {
	exe, err := getVersionExe(root, targetVersion)
	if err != nil || !fileExists(exe) {
		return false, "健康验收未绑定本事务：主程序不存在"
	}
	tx := readTransaction(root)
	protected := txBool(tx, "dataPrepared") && !txBool(tx, "dataAccepted") &&
		txString(tx, "dataRecoveryPhase") != "restored"
	if opts.requireGuard != nil {
		protected = *opts.requireGuard
	}

	childEnv := map[string]string{}
	for _, entry := range os.Environ() {
		key, value, _ := strings.Cut(entry, "=")
		childEnv[key] = value
	}
	childEnv[envPrefix+"INSTALL_ROOT"] = root
	childEnv[envPrefix+"DATA_DIR"] = dataDir
	childEnv[envPrefix+"UPDATE_ATTEMPT_ID"] = attemptID
	childEnv[envPrefix+"VERSION"] = targetVersion
	if protected {
		childEnv[envPrefix+"UPDATE_DATA_TOKEN"] = txString(tx, "dataToken")
	} else {
		delete(childEnv, envPrefix+"UPDATE_DATA_TOKEN")
		if opts.requireGuard != nil && !*opts.requireGuard {
			delete(childEnv, envPrefix+"UPDATE_ATTEMPT_ID")
		}
	}
	childEnv["WEBVIEW2_USER_DATA_FOLDER"] = filepath.Join(dataDir, "cache", "webview")
	childEnv["NO_PROXY"] = "127.0.0.1,localhost"
	childEnv["no_proxy"] = "127.0.0.1,localhost"
	if !opts.requireWindow {
		delete(childEnv, envPrefix+"PORT")
	}
	for _, key := range []string{envPrefix + "DEV_URL", envPrefix + "LAUNCHER_PID",
		envPrefix + "UPDATE_RESTART", envPrefix + "PREV_PID"} {
		delete(childEnv, key)
	}
	envList := make([]string, 0, len(childEnv))
	for key, value := range childEnv {
		envList = append(envList, key+"="+value)
	}

	var cmdArgs []string
	if !opts.requireWindow {
		cmdArgs = []string{"--server"}
	}
	cmd := exec.Command(exe, cmdArgs...)
	cmd.Dir = filepath.Dir(exe)
	cmd.Env = envList
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000}
	if err := cmd.Start(); err != nil {
		return false, "拉起新版本失败：" + err.Error()
	}
	probePID := cmd.Process.Pid
	tree := assignProbeTree(cmd.Process)

	waitDone := make(chan struct{})
	var waitExitCode int
	go func() {
		waitErr := cmd.Wait()
		if exitErr, ok := waitErr.(*exec.ExitError); ok {
			waitExitCode = exitErr.ExitCode()
		} else if waitErr != nil {
			waitExitCode = 1
		}
		close(waitDone)
	}()

	if protected {
		children := txChildren(tx)
		children = append(children, map[string]any{
			"pid": probePID, "image": processImage(probePID), "stopped": false,
		})
		_ = updateTransaction(root, stageVerifying, map[string]any{
			"children": children, "launchPending": false, "launchImage": absPath(exe),
		})
	}
	expectedImage := processImage(probePID)
	preserved := false
	defer func() {
		if !preserved {
			tree.stop(cmd.Process)
			select {
			case <-waitDone:
			case <-time.After(2 * time.Second):
			}
			if protected {
				current := readTransaction(root)
				if current != nil {
					children := txChildren(current)
					for _, child := range children {
						if entry, ok := child.(map[string]any); ok && int(entry["pid"].(float64)) == probePID {
							entry["stopped"] = true
						}
					}
					_ = updateTransaction(root, txString(current, "stage"), map[string]any{"children": children})
				}
			}
		}
	}()

	started := time.Now()
	watch := newMigrationWatch(started, healthTimeout)
	lastReason := "健康验收未绑定本事务：无可用健康响应"
	for time.Now().Before(watch.deadline) {
		select {
		case <-waitDone:
			return false, fmt.Sprintf("新版本进程已退出，退出码 %d", waitExitCode)
		default:
		}
		if expectedImage != "" {
			if current := processImage(probePID); current != "" && !samePath(current, expectedImage) {
				return false, "健康验收未绑定本事务：进程镜像被替换为 " + current
			}
		}
		if watch.observe(root, dataDir, time.Now()) {
			return false, fmt.Sprintf("数据库迁移超出总预算（%.0fs），执行回滚", migrationMaxDuration.Seconds())
		}
		candidates := listeningPortsOf(probePID)
		candidates = append(candidates, allListeningPorts()...)
		for _, port := range candidates {
			if !answersHTTP(port) {
				continue
			}
			status, data, err := httpGetJSON(port, "/api/v1/health", probeHTTPTimeout)
			if err != nil || status != 200 {
				continue
			}
			appName2, _ := data["app"].(string)
			if appName2 != appName {
				continue
			}
			if pidValue, ok := data["pid"].(float64); ok && int(pidValue) > 0 {
				image := processImage(int(pidValue))
				if image == "" || !isUnderPath(image, filepath.Dir(exe)) {
					lastReason = "健康验收未绑定本事务：响应进程不属于目标版本目录"
					continue
				}
			}
			reason := verifyHealthResponse(data, targetVersion, attemptID, serverdExeOf(exe), dataDir, "", protected)
			if reason != "" {
				lastReason = "健康验收未绑定本事务：" + reason
				continue
			}
			if opts.requireWindow {
				pageReady, _ := data["pageReady"].(bool)
				if !pageReady {
					lastReason = "程序服务已启动，等待应用页面完成加载"
					continue
				}
			}
			if opts.keepRunning {
				if protected {
					if err := acceptData(root); err != nil {
						return false, "数据验收提交失败：" + err.Error()
					}
				}
				tree.detach()
				preserved = true
			}
			return true, "健康检查通过"
		}
		time.Sleep(probePollInterval)
	}
	budget := fmt.Sprintf("%.0fs", healthTimeout.Seconds())
	if watch.deadline.After(started.Add(healthTimeout)) {
		budget = fmt.Sprintf("验收窗 %.0fs + 迁移空闲窗 %.0fs", healthTimeout.Seconds(), migrationIdleWindow.Seconds())
	}
	return false, fmt.Sprintf("健康验收超时（%s）：%s", budget, lastReason)
}

func serverdExeOf(windowhostExe string) string {
	verDir := filepath.Dir(windowhostExe)
	if resolved, err := hostkit.ResolveVersionServerExe(verDir); err == nil {
		return resolved
	}
	flat := filepath.Join(verDir, "serverd.exe")
	if fileExists(flat) {
		return flat
	}
	return filepath.Join(verDir, "_internal", "serverd.exe")
}

func verifyHealthResponse(data map[string]any, targetVersion, attemptID, exe, dataDir, processImage string, requireGuard bool) string {
	if requireGuard {
		for _, key := range []string{"pid", "dataDir", "executable"} {
			if _, ok := data[key]; !ok {
				return "健康响应缺少程序或数据身份"
			}
		}
		protocol, _ := data["updateDataProtocol"].(float64)
		writeProtected, _ := data["writeProtected"].(bool)
		if int(protocol) != updateDataProtocol || !writeProtected {
			return "更新期间的数据写入保护未生效"
		}
	}
	version := strings.TrimPrefix(fmt.Sprintf("%v", data["version"]), "v")
	if version != strings.TrimPrefix(targetVersion, "v") {
		return fmt.Sprintf("版本不符：响应 %s / 目标 %s", version, targetVersion)
	}
	if fmt.Sprintf("%v", data["updateAttemptId"]) != attemptID {
		return "事务标识不符：响应不是本次换装拉起的进程"
	}
	if reported, ok := data["dataDir"].(string); ok && reported != "" && !samePath(reported, dataDir) {
		return fmt.Sprintf("数据目录不符：响应 %s / 目标 %s", reported, dataDir)
	}
	if reported, ok := data["executable"].(string); ok && reported != "" && !samePath(reported, exe) {
		if processImage == "" || !samePath(reported, processImage) {
			return fmt.Sprintf("进程镜像不符：响应 %s / 目标 %s", reported, exe)
		}
	}
	return ""
}
