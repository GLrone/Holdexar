//go:build windows

// health.go 健康验收探针：只认本次拉起、目标版本、目标数据目录的进程。
// 监听端口经 GetExtendedTcpTable(TCP_TABLE_OWNER_PID_LISTENER) 归属核对。
package main

import (
	"encoding/json"
	"errors"
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
)

const (
	tcpTableOwnerPIDListener = 3 // TCP_TABLE_OWNER_PID_LISTENER
	jobKillOnJobClose        = 0x2000
)

var (
	modIphlpapi             = windows.NewLazySystemDLL("iphlpapi.dll")
	procGetExtendedTcpTable = modIphlpapi.NewProc("GetExtendedTcpTable")

	modKernelJob                 = windows.NewLazySystemDLL("kernel32.dll")
	procCreateJobObjectW         = modKernelJob.NewProc("CreateJobObjectW")
	procSetInformationJobObject  = modKernelJob.NewProc("SetInformationJobObject")
	procAssignProcessToJobObject = modKernelJob.NewProc("AssignProcessToJobObject")
)

// allListeningPorts 本机全部 TCP 监听端口（网络序端口 → 主机序）。
// 验收进程树是「窗口宿主 → 本地服务」两层，监听者属孙进程，按宿主 PID
// 过滤会永久漏配，故候选池取全量监听位，身份由健康载荷逐一核对。
func allListeningPorts() []int {
	const query = 3 // TCP_TABLE_OWNER_PID_LISTENER
	var size uint32
	ret, _, _ := procGetExtendedTcpTable.Call(0, uintptr(unsafe.Pointer(&size)), 0, 2 /* AF_INET */, query, 0)
	if ret != 122 /* ERROR_INSUFFICIENT_BUFFER */ && ret != 0 {
		return nil
	}
	buf := make([]byte, size)
	ret, _, _ = procGetExtendedTcpTable.Call(
		uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)), 0, 2 /* AF_INET */, query, 0)
	if ret != 0 {
		return nil
	}
	count := *(*uint32)(unsafe.Pointer(&buf[0]))
	rowSize := unsafe.Sizeof(uint32(0)) * 6
	base := unsafe.Pointer(&buf[4])
	ports := make([]int, 0, count)
	for i := uint32(0); i < count; i++ {
		row := (*[6]uint32)(unsafe.Pointer(uintptr(base) + uintptr(i)*rowSize))
		port := uint16(row[2]>>8&0xFF) | uint16(row[2]&0xFF)<<8 // 网络序（row[2]=dwLocalPort）
		ports = append(ports, int(port))
	}
	return ports
}

// listeningPortsOf 进程监听中的 TCP 端口（网络序端口 → 主机序）。
func listeningPortsOf(pid int) []int {
	const query = 3 // TCP_TABLE_OWNER_PID_LISTENER
	var size uint32
	procGetExtendedTcpTable.Find()
	ret, _, _ := procGetExtendedTcpTable.Call(0, uintptr(unsafe.Pointer(&size)), 0, 2 /* AF_INET */, query, 0)
	if ret != 122 /* ERROR_INSUFFICIENT_BUFFER */ && ret != 0 {
		return nil
	}
	buf := make([]byte, size)
	ret, _, _ = procGetExtendedTcpTable.Call(
		uintptr(unsafe.Pointer(&buf[0])), uintptr(unsafe.Pointer(&size)), 0, 2 /* AF_INET */, query, 0)
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
		port := uint16(row[2]>>8&0xFF) | uint16(row[2]&0xFF)<<8 // 网络序（row[2]=dwLocalPort）
		ports = append(ports, int(port))
	}
	return ports
}

// answersHTTP 端口秒级探活过滤（单实例锁端口 accepts 不应答，便宜地跳过）。
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
	return strings.EqualFold(filepath.Clean(absPath(first)), filepath.Clean(absPath(second)))
}

// processImage 运行中进程的映像路径（未知返回空串）。
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
	return event == uint32(0xFFFFFFFF) // WAIT_TIMEOUT = 仍在运行
}

// probeTree Job 对象：宿主被硬杀时验收进程树一并终止。
type probeTree struct {
	job windows.Handle
}

func processHandleForJob(proc *os.Process) uintptr {
	handle, err := windows.OpenProcess(windows.PROCESS_SET_QUOTA|windows.PROCESS_TERMINATE, false, uint32(proc.Pid))
	if err != nil {
		return 0
	}
	return uintptr(handle)
}

func assignProbeTree(proc *os.Process) *probeTree {
	job, _, _ := procCreateJobObjectW.Call(0, 0)
	if job == 0 {
		return nil
	}
	// JOBOBJECT_EXTENDED_LIMIT_INFORMATION { LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE }
	var info [144]byte
	*(*uint32)(unsafe.Pointer(&info[32])) = jobKillOnJobClose // JobObjectExtendedLimitInformation.LimitFlags 偏移
	procSetInformationJobObject.Call(job, 9, uintptr(unsafe.Pointer(&info[0])), uintptr(len(info)))
	procAssignProcessToJobObject.Call(job, uintptr(processHandleForJob(proc)))
	return &probeTree{job: windows.Handle(job)}
}

func (t *probeTree) detach() {
	if t != nil && t.job != 0 {
		windows.CloseHandle(t.job)
		t.job = 0
	}
}

func (t *probeTree) stop(proc *os.Process) {
	if t != nil && t.job != 0 {
		windows.TerminateJobObject(t.job, 1)
		windows.CloseHandle(t.job)
		t.job = 0
	}
	if proc != nil {
		_ = proc.Kill()
	}
}

var _ = errors.New

type probeOptions struct {
	requireWindow bool
	keepRunning   bool
	requireGuard  *bool
}

// probeVersionHealth 健康验收：身份四要素（版本/事务/数据目录/进程镜像）+
// 保护模式附加（writeProtected/updateDataProtocol）+ 页面就绪（pageReady）。
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
	// 目标版本号经环境注入：健康响应版本身份直接可比（打包版以构建时版本为准）
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
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x08000000} // CREATE_NO_WINDOW
	if err := cmd.Start(); err != nil {
		return false, "拉起新版本失败：" + err.Error()
	}
	tree := assignProbeTree(cmd.Process)
	if protected {
		children := txChildren(tx)
		children = append(children, map[string]any{
			"pid": cmd.Process.Pid, "image": processImage(cmd.Process.Pid), "stopped": false,
		})
		_ = updateTransaction(root, stageVerifying, map[string]any{
			"children": children, "launchPending": false, "launchImage": absPath(exe),
		})
	}
	expectedImage := processImage(cmd.Process.Pid)
	preserved := false
	defer func() {
		if !preserved {
			tree.stop(cmd.Process)
			if protected {
				current := readTransaction(root)
				if current != nil {
					children := txChildren(current)
					for _, child := range children {
						if entry, ok := child.(map[string]any); ok && int(entry["pid"].(float64)) == cmd.Process.Pid {
							entry["stopped"] = true
						}
					}
					_ = updateTransaction(root, txString(current, "stage"), map[string]any{"children": children})
				}
			}
		}
	}()

	deadline := time.Now().Add(healthTimeout)
	lastReason := "健康验收未绑定本事务：无可用健康响应"
	for time.Now().Before(deadline) {
		if cmd.ProcessState != nil && cmd.ProcessState.Exited() {
			return false, fmt.Sprintf("新版本进程已退出，退出码 %d", cmd.ProcessState.ExitCode())
		}
		if done, code := tryWaitProbe(cmd); done {
			return false, fmt.Sprintf("新版本进程已退出，退出码 %d", code)
		}
		if expectedImage != "" {
			if current := processImage(cmd.Process.Pid); current != "" && !samePath(current, expectedImage) {
				return false, "健康验收未绑定本事务：进程镜像被替换为 " + current
			}
		}
		candidates := listeningPortsOf(cmd.Process.Pid)
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
			// 响应者进程绑定：监听者是验收树孙进程（本地服务），验其存活且
			// 映像位于目标版本目录内；attemptId 为本次事务独有凭据兜底
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
			if opts.keepRunning && protected {
				if err := acceptData(root); err != nil {
					return false, "数据验收提交失败：" + err.Error()
				}
				tree.detach()
				preserved = true
			}
			return true, "健康检查通过"
		}
		time.Sleep(probePollInterval)
	}
	return false, fmt.Sprintf("健康验收超时（%.1fs）：%s", healthTimeout.Seconds(), lastReason)
}

// serverdExeOf 窗口宿主 exe → 同版本目录内的本地服务 exe（健康载荷的
// executable 由服务端自身报告，身份比对以其为准）。
func serverdExeOf(windowhostExe string) string {
	return filepath.Join(filepath.Dir(windowhostExe), "_internal", "serverd.exe")
}

// isUnderPath 子路径判定（大小写不敏感，防跨目录冒充）。
func isUnderPath(child, parent string) bool {
	rel, err := filepath.Rel(parent, child)
	if err != nil {
		return false
	}
	rel = filepath.ToSlash(rel)
	return rel == "." || (!strings.HasPrefix(rel, "../") && rel != "..")
}

func tryWaitProbe(cmd *exec.Cmd) (bool, int) {
	done := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(done)
	}()
	select {
	case <-done:
		return true, cmd.ProcessState.ExitCode()
	default:
		return false, 0
	}
}

// verifyHealthResponse 健康响应身份校验；不匹配返回原因，全部匹配返回空串。
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

var errProbeUnused = errors.New("unused")
var _ = io.Discard
var _ = errProbeUnused
