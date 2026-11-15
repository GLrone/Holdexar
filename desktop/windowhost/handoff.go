//go:build windows

// handoff.go 更新交接：暂存包就绪时把换装交给暂存包内的更新宿主。
// 守卫链对齐桌面壳 _handoff_pending_update：失败标记、版本比较、交接账本、
// 能力探针；仅发行布局（本进程运行于 versions/<版本>/ 内）启用。
package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

// 更新状态文件路径所有权（单一命名来源）：暂存目录以安装根为正位，数据目录
// 为历史布局残留位置；失败标记与交接账本随数据目录。
const (
	stagingDirName      = "update-staging"
	stagingManifestName = "manifest.json"
	swapFailedFlag      = ".swap-failed"
	handoffLedgerName   = "update-handoff.json"
)

type handoffInfo struct {
	tag       string
	attemptID string
	version   string
	staging   string
	payload   string
	hostExe   string
	dataDir   string
}

// runningFromVersions 本进程是否处于发行版本布局内。
func runningFromVersions() bool {
	exe, err := os.Executable()
	if err != nil {
		return false
	}
	return strings.EqualFold(filepath.Base(filepath.Dir(filepath.Dir(exe))), "versions")
}

func versionFromTagHost(tag string) string {
	text := strings.TrimSpace(tag)
	if len(text) > 1 && (text[0] == 'v' || text[0] == 'V') && text[1] >= '0' && text[1] <= '9' {
		return text[1:]
	}
	return text
}

// versionGTHost 主段逐点分数字比较 + 预发布段判旧（对齐 app_info.version_gt）。
func versionGTHost(first, second string) bool {
	parse := func(v string) ([]int, string) {
		main_, pre := v, ""
		if idx := strings.IndexAny(v, "-+"); idx >= 0 {
			main_, pre = v[:idx], v[idx+1:]
		}
		var parts []int
		for _, piece := range strings.Split(main_, ".") {
			value := 0
			for _, r := range piece {
				if r < '0' || r > '9' {
					break
				}
				value = value*10 + int(r-'0')
			}
			parts = append(parts, value)
		}
		return parts, pre
	}
	left, leftPre := parse(first)
	right, rightPre := parse(second)
	for i := 0; i < len(left) || i < len(right); i++ {
		l, r := 0, 0
		if i < len(left) {
			l = left[i]
		}
		if i < len(right) {
			r = right[i]
		}
		if l != r {
			return l > r
		}
	}
	if leftPre == "" && rightPre != "" {
		return true
	}
	if leftPre != "" && rightPre != "" {
		return leftPre > rightPre
	}
	return false
}

// handoffPending 交接条件逐项校验；拒因经 trace 上报（诊断用）。
func handoffPending(cfg *options, trace func(string)) bool {
	if !runningFromVersions() {
		trace("交接跳过：非发行版本布局")
		return false
	}
	// 验收探针子进程（env 带本次事务 ID）不得再触发交接
	if os.Getenv(envPrefix+"UPDATE_ATTEMPT_ID") != "" {
		trace("交接跳过：本进程为验收探针子进程")
		return false
	}
	// 暂存残留清场（对齐桌面壳 _cleanup_staging_leftover）：清单缺失或换装失败
	// 标记在位的暂存目录不再具备交接资格，两处位置各自清理——此刻宿主必已退出，
	// 无占用；成功换装只删清单、残留载荷由本清理兜底
	swapFailed := fileExists(filepath.Join(cfg.dataDir, swapFailedFlag))
	for _, dir := range []string{filepath.Join(cfg.installRoot, stagingDirName), filepath.Join(cfg.dataDir, stagingDirName)} {
		if info, serr := os.Stat(dir); serr != nil || !info.IsDir() {
			continue
		}
		if fileExists(filepath.Join(dir, stagingManifestName)) && !swapFailed {
			continue
		}
		if os.RemoveAll(dir) == nil {
			trace("已清理暂存残留 " + dir)
		}
	}
	if swapFailed {
		trace("交接跳过：上次换装失败标记在位")
		return false
	}
	staging := filepath.Join(cfg.installRoot, stagingDirName)
	manifestPath := filepath.Join(staging, stagingManifestName)
	if !fileExists(manifestPath) {
		staging = filepath.Join(cfg.dataDir, stagingDirName)
		manifestPath = filepath.Join(staging, stagingManifestName)
	}
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		trace("交接跳过：暂存清单不存在 " + manifestPath)
		return false
	}
	var manifest map[string]any
	if json.Unmarshal(raw, &manifest) != nil {
		return false
	}
	if ready, _ := manifest["ready"].(bool); !ready {
		return false
	}
	tag, _ := manifest["tag"].(string)
	stagedVersion := versionFromTagHost(tag)
	if stagedVersion == "" || !versionGTHost(stagedVersion, cfg.appVersion) {
		trace(fmt.Sprintf("交接跳过：暂存版本 %q 不比现装 %q 新", stagedVersion, cfg.appVersion))
		return false
	}
	// 交接账本：同一暂存包只自动交接一次
	if ledgerTag, count := readHandoffLedger(cfg.dataDir); ledgerTag == tag && count >= 1 {
		os.RemoveAll(staging)
		trace("交接跳过：同一暂存包已交接过一次，已清理暂存")
		return false
	}
	payload := filepath.Join(staging, "Holdexar")
	hostExe := filepath.Join(payload, "updater", "UpdateHost.exe")
	// 主程序探测：扁平载荷在根，多版本载荷在 versions/<目标>/
	stagedExe := filepath.Join(payload, "Holdexar.exe")
	if !fileExists(stagedExe) {
		if versionsSub := filepath.Join(payload, "versions", stagedVersion, "Holdexar.exe"); fileExists(versionsSub) {
			stagedExe = versionsSub
		}
	}
	if !fileExists(hostExe) || !fileExists(stagedExe) {
		trace("交接跳过：暂存包缺主程序或更新宿主")
		return false
	}
	if !supportsDataProtocol(stagedExe) {
		trace("交接跳过：暂存主程序不支持数据保护协议")
		return false
	}
	if !supportsDataProtocol(hostExe) {
		trace("交接跳过：暂存更新宿主不支持数据保护协议")
		return false
	}
	return true
}

func readHandoffLedger(dataDir string) (string, int) {
	raw, err := os.ReadFile(filepath.Join(dataDir, handoffLedgerName))
	if err != nil {
		return "", 0
	}
	var ledger struct {
		Tag   string `json:"tag"`
		Count int    `json:"count"`
	}
	if json.Unmarshal(raw, &ledger) != nil {
		return "", 0
	}
	return ledger.Tag, ledger.Count
}

func recordHandoffLedger(dataDir, tag string) {
	ledgerTag, count := readHandoffLedger(dataDir)
	next := 1
	if ledgerTag == tag {
		next = count + 1
	}
	payload, _ := json.Marshal(map[string]any{
		"tag": tag, "count": next, "pid": os.Getpid(), "ts": float64(timeNowUnix()),
	})
	_ = os.WriteFile(filepath.Join(dataDir, handoffLedgerName), payload, 0o644)
}

// spawnUpdateHost 分离拉起暂存包内的更新宿主并登记交接；成功后本进程退出。
func spawnUpdateHost(cfg *options, log *hostLog) bool {
	staging := filepath.Join(cfg.installRoot, stagingDirName)
	manifestPath := filepath.Join(staging, stagingManifestName)
	if !fileExists(manifestPath) {
		staging = filepath.Join(cfg.dataDir, stagingDirName)
	}
	raw, err := os.ReadFile(filepath.Join(staging, stagingManifestName))
	if err != nil {
		return false
	}
	var manifest map[string]any
	if json.Unmarshal(raw, &manifest) != nil {
		return false
	}
	tag, _ := manifest["tag"].(string)
	attemptID, _ := manifest["attempt_id"].(string)
	if attemptID == "" {
		attemptID = randomHexHost(16)
	}
	payload := filepath.Join(staging, "Holdexar")
	hostExe := filepath.Join(payload, "updater", "UpdateHost.exe")
	cmd := exec.Command(hostExe,
		"--staging", staging,
		"--target", cfg.installRoot,
		"--wait-pid", strconv.Itoa(os.Getpid()),
		"--attempt-id", attemptID,
		"--target-version", versionFromTagHost(tag),
		"--source-version", cfg.appVersion,
		"--data-dir", cfg.dataDir,
	)
	cmd.Dir = filepath.Dir(hostExe)
	cmd.Env = append(os.Environ(), envPrefix+"SOURCE_VERSION="+cfg.appVersion)
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: 0x00000008 | 0x00000200 | 0x08000000}
	if err := cmd.Start(); err != nil {
		log.write("拉起换装进程失败：" + err.Error())
		return false
	}
	go func() { _ = cmd.Process.Release() }()
	recordHandoffLedger(cfg.dataDir, tag)
	log.write(fmt.Sprintf("换装已交给暂存包（%s），本进程退出", tag))
	return true
}

func timeNowUnix() int64 {
	return time.Now().Unix()
}

func randomHexHost(bytes int) string {
	buf := make([]byte, bytes)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

// swapMutexName 换装互斥体名，与更新宿主 execute.go 同名同源。
const swapMutexName = "Holdexar-update-swap"

// yieldExitCode 后台模式下让位/交接的退出码（对齐桌面壳；诊断信号，无自动重启消费方）。
const yieldExitCode = 75

const (
	mutexSynchronize = 0x00100000
	waitTimeout      = 0x00000102 // WAIT_TIMEOUT：互斥体已被他人持有
	waitAbandoned    = 0x00000080 // WAIT_ABANDONED：前任持有者已死，不算进行中
)

// probeSwapMutexHeld 探测换装互斥体是否正被换装进程持有（探测不占有）。
// 互斥体不存在 = 没有换装在跑；探到弃置互斥（前任换装进程暴毙）不算进行中。
func probeSwapMutexHeld(name string) bool {
	runtime.LockOSThread() // 互斥体归属线程：探测取得的占有必须在同线程释放
	defer runtime.UnlockOSThread()
	namePtr, _ := syscall.UTF16PtrFromString(name)
	handle, _, _ := call3(procOpenMutexW, mutexSynchronize, 0, uintptr(unsafe.Pointer(namePtr)))
	if handle == 0 {
		return false
	}
	status, _, _ := call3(procWaitForSingleObject, handle, 0)
	if status == waitTimeout {
		call(procCloseHandle, handle)
		return true
	}
	if status != waitAbandoned {
		call(procReleaseMutex, handle) // 空闲互斥被探测拿到手上，放回去
	}
	call(procCloseHandle, handle)
	return false
}

// swapMutexHeld 换装互斥让位判定：换装进行中现装启动必须退位——换装进程
// 完成时会拉起新版或回滚现装，不能带着「程序目录即将被搬空」继续起服务。
// 更新事务上下文（验收探针/带数据令牌的子进程）豁免：探针恰恰在互斥持有期运行。
func swapMutexHeld() bool {
	return swapMutexHeldNamed(swapMutexName)
}

func swapMutexHeldNamed(name string) bool {
	if os.Getenv(envPrefix+"UPDATE_ATTEMPT_ID") != "" || os.Getenv(envPrefix+"UPDATE_DATA_TOKEN") != "" {
		return false
	}
	return probeSwapMutexHeld(name)
}
