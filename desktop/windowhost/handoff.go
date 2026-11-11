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
	"strconv"
	"strings"
	"syscall"
	"time"
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
	if fileExists(filepath.Join(cfg.dataDir, ".swap-failed")) {
		trace("交接跳过：上次换装失败标记在位")
		return false
	}
	staging := filepath.Join(cfg.installRoot, "update-staging")
	manifestPath := filepath.Join(staging, "manifest.json")
	if !fileExists(manifestPath) {
		staging = filepath.Join(cfg.dataDir, "update-staging")
		manifestPath = filepath.Join(staging, "manifest.json")
	}
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		// 成功换装只删清单（更新宿主仍在暂存包内运行到退出），残留载荷由
		// 下一次启动清理——此刻宿主必已退出，无占用
		if info, serr := os.Stat(staging); serr == nil && info.IsDir() {
			if os.RemoveAll(staging) == nil {
				trace("已清理上次换装的暂存残留")
			}
		}
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
	raw, err := os.ReadFile(filepath.Join(dataDir, "update-handoff.json"))
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
	_ = os.WriteFile(filepath.Join(dataDir, "update-handoff.json"), payload, 0o644)
}

// spawnUpdateHost 分离拉起暂存包内的更新宿主并登记交接；成功后本进程退出。
func spawnUpdateHost(cfg *options, log *hostLog) bool {
	staging := filepath.Join(cfg.installRoot, "update-staging")
	manifestPath := filepath.Join(staging, "manifest.json")
	if !fileExists(manifestPath) {
		staging = filepath.Join(cfg.dataDir, "update-staging")
	}
	raw, err := os.ReadFile(filepath.Join(staging, "manifest.json"))
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
