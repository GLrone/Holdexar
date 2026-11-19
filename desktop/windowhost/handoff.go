//go:build windows

package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"sync"
	"syscall"
	"time"
	"unsafe"

	"holdexar/hostkit"
)

func runningFromVersions() bool {
	_, vDir, managed := hostkit.InstallRootFromExe("")
	return managed && vDir != ""
}

func isManagedRun(cfg *options) bool {
	return (cfg.isManaged || runningFromVersions()) && !cfg.devMode
}

func verifyStagedTrust(stagingDir, _ string, targetVersion string) error {
	return hostkit.VerifyStagedTrust(stagingDir, targetVersion)
}

func handoffPending(cfg *options, trace func(string)) bool {
	if !isManagedRun(cfg) {
		trace("交接跳过：非受管发行布局")
		return false
	}
	if os.Getenv(envPrefix+"UPDATE_ATTEMPT_ID") != "" {
		trace("交接跳过：本进程为验收探针子进程")
		return false
	}
	swapFailed := fileExists(filepath.Join(cfg.dataDir, hostkit.SwapFailedFlag))
	for _, dir := range []string{
		filepath.Join(cfg.installRoot, hostkit.StagingDirName),
		filepath.Join(cfg.dataDir, hostkit.StagingDirName),
	} {
		if info, serr := os.Stat(dir); serr != nil || !info.IsDir() {
			continue
		}
		if fileExists(filepath.Join(dir, hostkit.StagingManifestName)) && !swapFailed {
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
	staging := filepath.Join(cfg.installRoot, hostkit.StagingDirName)
	manifestPath := filepath.Join(staging, hostkit.StagingManifestName)
	if !fileExists(manifestPath) {
		staging = filepath.Join(cfg.dataDir, hostkit.StagingDirName)
		manifestPath = filepath.Join(staging, hostkit.StagingManifestName)
	}
	if !fileExists(manifestPath) {
		trace("交接跳过：暂存清单不存在 " + manifestPath)
		return false
	}
	manifest, err := hostkit.ReadStagingManifest(staging)
	if err != nil || manifest == nil {
		return false
	}
	if ready, _ := manifest["ready"].(bool); !ready {
		return false
	}
	tag, _ := manifest["tag"].(string)
	stagedVersion := hostkit.VersionFromTag(tag)
	if stagedVersion == "" || !hostkit.VersionGT(stagedVersion, cfg.appVersion) {
		trace(fmt.Sprintf("交接跳过：暂存版本 %q 不比现装 %q 新", stagedVersion, cfg.appVersion))
		return false
	}
	// 交接账本：同一暂存包只自动交接一次。
	if ledgerTag, count := readHandoffLedger(cfg.dataDir); ledgerTag == tag && count >= 1 {
		_ = os.RemoveAll(staging)
		trace("交接跳过：同一暂存包已交接过一次，已清理暂存")
		return false
	}
	targetVer, _, errReason := hostkit.VerifyStagedPayload(staging, cfg.appVersion)
	if errReason != "" {
		trace("交接跳过：暂存包信任校验失败（" + errReason + "），已清理暂存")
		_ = os.RemoveAll(staging)
		return false
	}
	payload := filepath.Join(staging, hostkit.PayloadDirName)
	hostExe := filepath.Join(payload, "updater", "UpdateHost.exe")
	stagedExe := filepath.Join(payload, hostkit.AppName+".exe")
	if !fileExists(stagedExe) {
		if versionsSub := filepath.Join(payload, "versions", targetVer, hostkit.AppName+".exe"); fileExists(versionsSub) {
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
	raw, err := os.ReadFile(filepath.Join(dataDir, hostkit.HandoffLedgerName))
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
		"tag": tag, "count": next, "pid": os.Getpid(), "ts": float64(time.Now().Unix()),
	})
	_ = os.WriteFile(filepath.Join(dataDir, hostkit.HandoffLedgerName), payload, 0o644)
}

func spawnUpdateHost(cfg *options, log *hostLog) bool {
	staging := filepath.Join(cfg.installRoot, hostkit.StagingDirName)
	manifestPath := filepath.Join(staging, hostkit.StagingManifestName)
	if !fileExists(manifestPath) {
		staging = filepath.Join(cfg.dataDir, hostkit.StagingDirName)
	}
	manifest, err := hostkit.ReadStagingManifest(staging)
	if err != nil || manifest == nil {
		return false
	}
	tag, _ := manifest["tag"].(string)
	attemptID, _ := manifest["attempt_id"].(string)
	if attemptID == "" {
		attemptID = randomHexHost(16)
	}
	payload := filepath.Join(staging, hostkit.PayloadDirName)
	hostExe := filepath.Join(payload, "updater", "UpdateHost.exe")
	cmd := exec.Command(hostExe,
		"--staging", staging,
		"--target", cfg.installRoot,
		"--wait-pid", strconv.Itoa(os.Getpid()),
		"--attempt-id", attemptID,
		"--target-version", hostkit.VersionFromTag(tag),
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

func randomHexHost(bytes int) string {
	buf := make([]byte, bytes)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

const (
	swapMutexName    = "Holdexar-update-swap"
	yieldExitCode    = 75
	mutexSynchronize = 0x00100000
	waitTimeout      = 0x00000102
	waitAbandoned    = 0x00000080
)

func acquireNamedMutex(name string) (bool, func(), error) {
	type result struct {
		held bool
		err  error
	}
	resCh := make(chan result, 1)
	stop := make(chan struct{})
	done := make(chan struct{})
	go func() {
		runtime.LockOSThread()
		defer runtime.UnlockOSThread()
		namePtr, err := syscall.UTF16PtrFromString(name)
		if err != nil {
			resCh <- result{err: err}
			close(done)
			return
		}
		handle, _, callErr := call3(procCreateMutexW, 0, 0, uintptr(unsafe.Pointer(namePtr)))
		if handle == 0 {
			resCh <- result{err: callErr}
			close(done)
			return
		}
		status, _, waitErr := call3(procWaitForSingleObject, handle, 0)
		if status == waitTimeout {
			call(procCloseHandle, handle)
			resCh <- result{held: false}
			close(done)
			return
		}
		if status != 0 && status != waitAbandoned {
			call(procCloseHandle, handle)
			resCh <- result{err: fmt.Errorf("互斥体等待失败：%v", waitErr)}
			close(done)
			return
		}
		resCh <- result{held: true}
		<-stop
		call(procReleaseMutex, handle)
		call(procCloseHandle, handle)
		close(done)
	}()
	res := <-resCh
	if res.err != nil {
		return false, nil, res.err
	}
	if !res.held {
		return false, func() {}, nil
	}
	var once sync.Once
	release := func() {
		once.Do(func() {
			close(stop)
			<-done
		})
	}
	return true, release, nil
}

func acquireUpdateMutex() (func(), error) {
	held, release, err := acquireNamedMutex(swapMutexName)
	if err != nil {
		return nil, err
	}
	if !held {
		return nil, errors.New("已有更新正在进行或无法核对更新权限")
	}
	return release, nil
}

// probeSwapMutexHeld 无占有探测换装互斥体；不存在或 WAIT_ABANDONED 均判空闲。
func probeSwapMutexHeld(name string) bool {
	runtime.LockOSThread()
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
		call(procReleaseMutex, handle)
	}
	call(procCloseHandle, handle)
	return false
}

// swapMutexHeld 换装进行中现装让位；带 UPDATE_ATTEMPT_ID 或 UPDATE_DATA_TOKEN 的事务子进程豁免。
func swapMutexHeld() bool {
	return swapMutexHeldNamed(swapMutexName)
}

func swapMutexHeldNamed(name string) bool {
	if os.Getenv(envPrefix+"UPDATE_ATTEMPT_ID") != "" || os.Getenv(envPrefix+"UPDATE_DATA_TOKEN") != "" {
		return false
	}
	return probeSwapMutexHeld(name)
}
