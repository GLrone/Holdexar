//go:build windows

// execute.go 换装事务编排：execute_update / _commit / _fail / recover_interrupted_update。
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

func updateMutexName() string { return "Holdexar-update-swap" }

// stagingPayloadDir 暂存载荷根（staging/Holdexar/）。
func stagingPayloadDir(staging string) string {
	payload := filepath.Join(staging, appName)
	if directoryExists(payload) {
		return payload
	}
	return staging
}

func readManifest(staging string) map[string]any {
	raw, err := os.ReadFile(filepath.Join(staging, "manifest.json"))
	if err != nil {
		return nil
	}
	var manifest map[string]any
	if json.Unmarshal(raw, &manifest) != nil {
		return nil
	}
	return manifest
}

// verifyStagingPayload 逐文件哈希核验（清单缺 files 表时跳过——跨版本配对兼容）。
func verifyStagingPayload(staging string, manifest map[string]any) string {
	expected, ok := manifest["files"].(map[string]any)
	if !ok || len(expected) == 0 {
		return ""
	}
	payload := stagingPayloadDir(staging)
	if !directoryExists(payload) {
		return "暂存包载荷缺失"
	}
	names := make([]string, 0, len(expected))
	for name := range expected {
		names = append(names, name)
	}
	for _, name := range names {
		path := filepath.Join(payload, filepath.FromSlash(name))
		if !fileExists(path) {
			return "暂存包载荷缺失：" + name
		}
		digest, err := fileSHA256Sum(path)
		if err != nil || !strings.EqualFold(digest, fmt.Sprintf("%v", expected[name])) {
			return "暂存包载荷校验不符：" + name
		}
	}
	return ""
}

func manifestString(manifest map[string]any, keys ...string) string {
	for _, key := range keys {
		if value, ok := manifest[key].(string); ok && value != "" {
			return value
		}
	}
	return ""
}

// executeUpdate 换装事务主流程（持换装互斥体）。
func executeUpdate(root string, opts *options) bool {
	defer func() {
		if recovered := recover(); recovered != nil {
			logUpdate(root, fmt.Sprintf("更新未完成，保留数据等待恢复：%v", recovered))
		}
	}()
	result := executeUpdateLocked(root, opts)
	tx := readTransaction(root)
	if !result && txBool(tx, "relaunchPrevious") {
		_ = updateTransaction(root, txString(tx, "stage"), map[string]any{"relaunchPrevious": false})
		relaunchPrevious(root, txString(tx, "sourceVersion"))
	}
	return result
}

func executeUpdateLocked(root string, opts *options) bool {
	logUpdate(root, fmt.Sprintf("开始换装：staging=%s root=%s", opts.staging, root))
	if opts.waitPID > 0 {
		logUpdate(root, fmt.Sprintf("等待旧实例（PID %d）退出...", opts.waitPID))
		if !waitProcessExit(opts.waitPID, waitPIDTimeout) {
			logUpdate(root, "原程序尚未退出，停止更新以保护数据")
			return false
		}
	}
	manifest := readManifest(opts.staging)
	if payloadError := verifyStagingPayload(opts.staging, manifest); payloadError != "" {
		logUpdate(root, "暂存包完整性核验未通过："+payloadError+"，拒绝换装并保留现装")
		return false
	}
	attemptID := opts.attemptID
	if attemptID == "" {
		attemptID = manifestString(manifest, "attempt_id")
	}
	if attemptID == "" {
		attemptID = randomHex(16)
	}
	tag := manifestString(manifest, "tag")
	targetVersion := opts.targetVersion
	if targetVersion == "" {
		targetVersion = versionFromTag(tag)
	}
	if targetVersion == "" {
		targetVersion = tag
	}
	if targetVersion == "" || !isManagedVersionName(targetVersion) {
		logUpdate(root, fmt.Sprintf("错误：无法识别目标版本：%q", targetVersion))
		return false
	}
	previousTx := readTransaction(root)
	if previousTx != nil && txString(previousTx, "dataDir") != "" {
		if err := recoverInterruptedUpdate(root); err != nil {
			logUpdate(root, "恢复上次中断事务失败："+err.Error())
			return false
		}
		currentTx := readTransaction(root)
		if !txBool(currentTx, "dataAccepted") && !txBool(currentTx, "dataRollbackReleased") &&
			txString(currentTx, "stage") != stageRolledBack {
			_ = updateTransaction(root, txString(currentTx, "stage"), map[string]any{"relaunchPrevious": true})
			return false
		}
	}
	state, _ := readCurrentState(root)
	sourceVersion := opts.sourceVersion
	if sourceVersion == "" {
		sourceVersion = manifestString(manifest, "source_version", "previousVersion")
	}
	if sourceVersion == "" {
		sourceVersion = stateString(state, "version")
	}
	if sourceVersion == "" {
		sourceVersion = stateString(state, "lastKnownGood")
	}
	if sourceVersion == "" {
		sourceVersion = "legacy_version"
	}
	dataDir := opts.dataDir
	if dataDir == "" {
		dataDir = filepath.Join(root, dataDirName)
	}
	activeVersion := stateString(state, "version")
	pendingSelf := stateString(state, "pendingVersion")
	// 沿用已落盘文件仅限暂存包已消失的真中断；暂存包在位时以其载荷为准重装
	// （上次尝试可能用旧载荷安装过，直接沿用会让过期二进制参与验收）
	recovering := pendingSelf == targetVersion && isVersionValid(root, targetVersion) &&
		!directoryExists(stagingPayloadDir(opts.staging))
	if activeVersion != "" && (strings.EqualFold(getVersionDir(root, targetVersion), getVersionDir(root, activeVersion)) ||
		sourceVersion != activeVersion) {
		logUpdate(root, "更新请求与当前程序版本不一致或目标已安装，保留现有程序与数据")
		return false
	}

	if err := startTransaction(root, targetVersion, sourceVersion, attemptID); err != nil {
		logUpdate(root, "事务创建失败："+err.Error())
		return false
	}
	diagnosis := diagnosis{
		AttemptID: attemptID, TargetVersion: targetVersion, SourceVersion: sourceVersion,
		PreviousVersion: sourceVersion, CurrentVersion: sourceVersion,
		Notes: manifestString(manifest, "notes"), DataDir: dataDir,
		StartTime: float64(time.Now().UnixNano()) / 1e9,
		Stage:     stageSwitching,
	}
	writeDiagnosis(root, diagnosis)
	_ = updateTransaction(root, stageSwitching, map[string]any{"dataDir": dataDir})

	if recovering {
		logUpdate(root, fmt.Sprintf("检测到未完成的换装（版本 %s），直接沿用已落盘文件重新验收", targetVersion))
	} else if err := installVersionPayload(root, opts.staging, targetVersion); err != nil {
		return failPath(root, &diagnosis, "暂存包载荷安装失败："+err.Error(), "")
	}

	stagedExe, _ := getVersionExe(root, targetVersion)
	dbExists := fileExists(dbPathOf(dataDir))
	if opts.skipHealthCheck && dbExists {
		return failPath(root, &diagnosis, "已有用户数据时不能跳过安全验收", "")
	}
	if !opts.skipHealthCheck {
		if !supportsDataProtocol(stagedExe) {
			return failPath(root, &diagnosis, "目标程序缺少数据保护协议，原数据未改动", "")
		}
		if err := prepareData(root, dataDir, attemptID); err != nil {
			return failPath(root, &diagnosis, "无法准备升级恢复点："+err.Error(), "")
		}
	}
	if err := writeCurrentState(root, targetVersion, sourceVersion, map[string]any{
		"previousVersion": sourceVersion, "pendingVersion": targetVersion, "attemptId": attemptID,
	}); err != nil {
		return failPath(root, &diagnosis, "程序指向写入失败："+err.Error(), "")
	}
	_ = updateTransaction(root, stageVerifying, nil)

	if opts.skipHealthCheck {
		_ = confirmVersion(root, targetVersion)
		return commit(root, opts.staging, targetVersion, &diagnosis, !opts.noLaunch)
	}

	healthy, reason := probeVersionHealth(root, targetVersion, attemptID, dataDir, probeOptions{})
	if !healthy {
		return failPath(root, &diagnosis, "健康检查失败："+reason, targetVersion)
	}
	return commit(root, opts.staging, targetVersion, &diagnosis, !opts.noLaunch)
}

// commit 页面验收与指针提交。
func commit(root, staging, targetVersion string, diagnosis *diagnosis, launchOnFinish bool) bool {
	tx := readTransaction(root)
	protected := txBool(tx, "dataPrepared")
	launched := !protected
	if protected {
		var healthy bool
		var reason string
		healthy, reason = probeVersionHealth(root, targetVersion, diagnosis.AttemptID, diagnosis.DataDir,
			probeOptions{requireWindow: true, keepRunning: true})
		if !healthy {
			return failPath(root, diagnosis, "页面验收失败："+reason, targetVersion)
		}
		launched = true
		if !launchOnFinish {
			if err := acceptData(root); err != nil {
				return failPath(root, diagnosis, "数据验收提交失败："+err.Error(), targetVersion)
			}
		}
	} else if launchOnFinish {
		logUpdate(root, "健康验收通过，拉起正式界面")
		launched = launchGUI(root)
	}
	if !launched {
		return failPath(root, diagnosis, "拉起新版本失败：新版未能拉起", targetVersion)
	}
	_ = confirmVersion(root, targetVersion)
	if isInside(staging, selfExePath()) {
		os.Remove(filepath.Join(staging, "manifest.json"))
	} else {
		os.RemoveAll(staging)
	}
	cleanupOldExecutables(filepath.Join(root, "updater"))
	cleanupOldExecutables(root)
	_ = updateTransaction(root, stageCommitted, map[string]any{"currentVersion": targetVersion})
	pruned := confirmablePrune(root, 2)
	logUpdate(root, fmt.Sprintf("换装成功！清理历史版本：%v", pruned))
	finishDiagnosis(root, targetVersion, diagnosis)
	return true
}

func isInside(parent, child string) bool {
	rel, err := filepath.Rel(parent, child)
	return err == nil && !strings.HasPrefix(rel, "..")
}

func selfExePath() string {
	exe, _ := os.Executable()
	return exe
}

func launchGUI(root string) bool {
	candidates := []string{}
	state, _ := readCurrentState(root)
	if version := stateString(state, "version"); version != "" {
		candidates = append(candidates, getVersionDir(root, version))
	}
	candidates = append(candidates, root)
	for _, dir := range candidates {
		exe := filepath.Join(dir, appName+".exe")
		if !fileExists(exe) {
			continue
		}
		spawnDetached(exe, nil, root)
		return true
	}
	return false
}

func spawnDetached(exe string, args []string, root string) {
	cmd := newDetachedCommand(exe, args, root)
	_ = cmd.Start()
}

// relaunchPrevious 健康探针旧版本并拉起（失败回滚后的用户界面恢复）。
func relaunchPrevious(root, version string) {
	logUpdate(root, "尝试拉起旧版本")
	candidates := []string{}
	if version != "" {
		candidates = append(candidates, getVersionDir(root, version))
	}
	state, _ := readCurrentState(root)
	if previous := stateString(state, "previousVersion"); previous != "" {
		candidates = append(candidates, getVersionDir(root, previous))
	}
	for _, dir := range candidates {
		exe := filepath.Join(dir, appName+".exe")
		if !fileExists(exe) {
			continue
		}
		spawnDetached(exe, nil, root)
		return
	}
}

// failPath 统一失败出口：撤目标版本、指针交还、数据回退、保护释放、留痕。
func failPath(root string, diagnosis *diagnosis, errorText string, launchedVersion string) bool {
	logUpdate(root, "换装故障："+errorText+"，执行回滚...")
	tx := readTransaction(root)
	if txBool(tx, "dataAccepted") {
		diagnosis.Success = false
		diagnosis.DataPreserved = true
		diagnosis.Stage = "accepted_failure"
		message := errorText
		diagnosis.Error = &message
		writeDiagnosis(root, *diagnosis)
		return false
	}
	if txBool(tx, "dataPrepared") {
		if err := rollbackData(root, true); err != nil {
			errorText = fmt.Sprintf("%s；数据恢复未完成，已保留现场：%v", errorText, err)
			_ = updateTransaction(root, "recovery_required", map[string]any{"error": errorText})
			diagnosis.Success = false
			diagnosis.DataPreserved = true
			diagnosis.Stage = "recovery_required"
			message := errorText
			diagnosis.Error = &message
			writeDiagnosis(root, *diagnosis)
			return false
		}
	} else if txString(tx, "dataDir") != "" {
		if err := abortPreparation(root, txString(tx, "dataDir")); err != nil {
			_ = updateTransaction(root, "recovery_required", map[string]any{
				"error": fmt.Sprintf("恢复点准备未结束：%v", err)})
			return false
		}
	}
	targetVersion := diagnosis.TargetVersion
	pointer := launchedVersion
	if pointer == "" {
		pointer = currentVersionOf(root)
	}
	if targetVersion != "" && pointer == targetVersion {
		if abortVersion(root, targetVersion) {
			logUpdate(root, fmt.Sprintf("已撤下未通过验收的版本 %s，指针交还上一有效版本", targetVersion))
		} else {
			logUpdate(root, fmt.Sprintf("未能交还版本指针（目标 %s 无可用回退版本）", targetVersion))
		}
	}
	_ = updateTransaction(root, stageRolledBack, map[string]any{"error": errorText, "relaunchPrevious": true})
	// 换装失败登记：交出 .swap-failed，直到新暂存包出现前不再自动换装
	_ = os.WriteFile(filepath.Join(txDataDir(root), ".swap-failed"),
		[]byte(fmt.Sprintf("更新未完成 target=%s\n", targetVersion)), 0o644)
	if txBool(tx, "dataPrepared") {
		if err := finishRollback(root); err != nil {
			errorText = fmt.Sprintf("%s；程序与数据恢复尚未完成：%v", errorText, err)
			_ = updateTransaction(root, "recovery_required", map[string]any{
				"error": errorText, "relaunchPrevious": false})
			diagnosis.Success = false
			diagnosis.DataPreserved = true
			diagnosis.Stage = "recovery_required"
			message := errorText
			diagnosis.Error = &message
			writeDiagnosis(root, *diagnosis)
			return false
		}
	}
	message := errorText
	diagnosis.Success = false
	diagnosis.Error = &message
	diagnosis.Stage = stageRolledBack
	diagnosis.EndTime = float64(time.Now().UnixNano()) / 1e9
	writeDiagnosis(root, *diagnosis)
	return false
}

func txDataDir(root string) string {
	tx := readTransaction(root)
	if dir := txString(tx, "dataDir"); dir != "" {
		return dir
	}
	return filepath.Join(root, dataDirName)
}

// recoverInterruptedUpdate 恢复中断事务（--recover-only）。
func recoverInterruptedUpdate(root string) error {
	tx := readTransaction(root)
	if tx == nil {
		return nil
	}
	if txString(tx, "attemptId") == "" {
		return errors.New("更新事务缺少有效标识")
	}
	switch {
	case txBool(tx, "dataRollbackReleased"):
		if err := finishRollback(root); err != nil {
			return err
		}
		state, _ := readCurrentState(root)
		if stateString(state, "version") != txString(tx, "sourceVersion") {
			return errors.New("已恢复的数据与当前程序指向不一致，已停止启动并保留数据")
		}
		return nil
	case txBool(tx, "dataAccepted"):
		if err := acceptData(root); err != nil {
			return err
		}
		compatible := findCompatibleVersion(root)
		if compatible == "" {
			return errors.New("缺少与当前数据相容的程序，数据已保留；请重新解压已验收版本")
		}
		if err := confirmVersion(root, compatible); err != nil {
			return err
		}
		_ = updateTransaction(root, stageCommitted, map[string]any{"currentVersion": compatible})
		finishDiagnosis(root, compatible, nil)
		return nil
	case txBool(tx, "dataPrepared"):
		if txBool(tx, "launchPending") {
			children := txChildren(tx)
			for _, child := range children {
				entry, _ := child.(map[string]any)
				pid, _ := entry["pid"].(float64)
				if pid > 0 && pidAlive(int(pid)) {
					return errors.New("验收进程仍在运行，保留更新数据保护")
				}
			}
			_ = updateTransaction(root, txString(tx, "stage"), map[string]any{"launchPending": false})
		}
		seed := &diagnosis{
			AttemptID: txString(tx, "attemptId"), TargetVersion: txString(tx, "targetVersion"),
			SourceVersion: txString(tx, "sourceVersion"), DataDir: txString(tx, "dataDir"),
		}
		failPath(root, seed, "上次更新中断，恢复原程序与数据", "")
		current := readTransaction(root)
		if !txBool(current, "dataRollbackReleased") || txString(current, "stage") != stageRolledBack ||
			currentVersionOf(root) != txString(current, "sourceVersion") ||
			fileExists(markerPath(txString(current, "dataDir"))) {
			return errors.New("更新恢复尚未完成，原始快照与故障数据均已保留，请查看更新诊断")
		}
		return nil
	case txString(tx, "dataDir") != "":
		return abortPreparation(root, txString(tx, "dataDir"))
	}
	return nil
}

// findCompatibleVersion 相容记录允许范围内的可用版本定位。
func findCompatibleVersion(root string) string {
	compatibility, err := readDataCompatibility(root)
	if err != nil || compatibility == nil {
		return ""
	}
	dataDir, _ := compatibility["dataDir"].(string)
	schemaValue, _ := compatibility["schemaVersion"].(float64)
	allowed := map[string]bool{}
	if versions, ok := compatibility["versions"].([]any); ok {
		for _, item := range versions {
			if text, ok := item.(string); ok {
				allowed[text] = true
			}
		}
	}
	if version, err := schemaVersionOf(dbPathOf(dataDir)); err != nil || int64(version) != int64(schemaValue) {
		return "" // 数据结构已变化：拒绝选择未经相容验收的程序
	}
	state, _ := readCurrentState(root)
	for _, candidate := range []string{stateString(state, "version"), stateString(state, "lastKnownGood"),
		stateString(state, "previousVersion")} {
		if candidate != "" && isVersionValid(root, candidate) && allowed[candidate] {
			return candidate
		}
	}
	installed := []string{}
	for _, version := range listInstalledVersions(root) {
		if allowed[version] {
			installed = append(installed, version)
		}
	}
	if len(installed) == 0 {
		return ""
	}
	best, bestTime := "", time.Time{}
	for _, version := range installed {
		if info, err := os.Stat(getVersionDir(root, version)); err == nil && info.ModTime().After(bestTime) {
			best, bestTime = version, info.ModTime()
		}
	}
	return best
}

var _ = sha256.Sum256
var _ = hex.EncodeToString
var _ = json.Marshal
var _ = io.Discard
