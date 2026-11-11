//go:build windows

// layout.go 安装目录状态账本：state/current.json、state/transaction.json、
// 数据相容记录与版本目录 helpers。语义对齐 app/core/layout.py 与根启动器。
package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	appName          = "Holdexar"
	appSlug          = "holdexar"
	stateDirName     = "state"
	currentStateFile = "current.json"
	transactionF     = "transaction.json"
	versionsDir      = "versions"
	dataDirName      = "data"
	envPrefix        = "HOLDEXAR_"

	stageStaged     = "staged"
	stageSwitching  = "switching"
	stageVerifying  = "verifying"
	stageCommitted  = "committed"
	stageFailed     = "failed"
	stageRolledBack = "rolled_back"

	updateDataProtocol = 1
)

func fileExists(path string) bool {
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular()
}

// isManagedVersionName 版本目录名合法性（对齐启动器：首字符数字或 legacy_version）。
func isManagedVersionName(name string) bool {
	text := name
	if text == "" || text == "." || text == ".." {
		return false
	}
	if strings.TrimSpace(text) != text || strings.HasSuffix(text, ".") {
		return false
	}
	if strings.ContainsAny(text, `/\:`) {
		return false
	}
	switch strings.ToLower(text) {
	case "data", "holdexar", "app", "logs", "updater", "state", "versions",
		"update-staging", "_internal", "__old__", "__legacy__", "cache", "temp", "tmp":
		return false
	}
	if text == "legacy_version" {
		return true
	}
	return text[0] >= '0' && text[0] <= '9'
}

func versionDir(root, version string) (string, error) {
	if !isManagedVersionName(version) {
		return "", errors.New("版本目录名称无效")
	}
	target := filepath.Join(root, versionsDir, version)
	if !trustedPath(target) {
		return "", errors.New("版本目录超出受管安装位置")
	}
	return target, nil
}

func getVersionDir(root, version string) string {
	dir, _ := versionDir(root, version)
	return dir
}

func getVersionExe(root, version string) (string, error) {
	dir, err := versionDir(root, version)
	if err != nil {
		return "", err
	}
	return filepath.Join(dir, appName+".exe"), nil
}

func isVersionValid(root, version string) bool {
	if !isManagedVersionName(version) {
		return false
	}
	dir, err := versionDir(root, version)
	if err != nil {
		return false
	}
	info, err := os.Stat(dir)
	if err != nil || !info.IsDir() {
		return false
	}
	exe := filepath.Join(dir, appName+".exe")
	if !fileExists(exe) {
		return false
	}
	internal := filepath.Join(dir, "_internal")
	if resolved, err := filepath.EvalSymlinks(internal); err == nil {
		dirResolved, err2 := filepath.EvalSymlinks(dir)
		if err2 != nil || !strings.EqualFold(resolved, dirResolved+string(os.PathSeparator)+"_internal") {
			return false
		}
		entries, err := os.ReadDir(internal)
		if err != nil || len(entries) == 0 {
			return false
		}
	}
	return true
}

func listInstalledVersions(root string) []string {
	entries, err := os.ReadDir(filepath.Join(root, versionsDir))
	if err != nil {
		return nil
	}
	var found []string
	for _, entry := range entries {
		if entry.IsDir() && isVersionValid(root, entry.Name()) {
			found = append(found, entry.Name())
		}
	}
	return found
}

// ───────────────────────── state/current.json ─────────────────────────

func readCurrentState(root string) (map[string]any, error) {
	raw, err := os.ReadFile(filepath.Join(root, stateDirName, currentStateFile))
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var fields map[string]any
	if err := json.Unmarshal(raw, &fields); err != nil {
		return nil, fmt.Errorf("程序指向记录无法读取: %w", err)
	}
	return fields, nil
}

func stateString(state map[string]any, key string) string {
	if state == nil {
		return ""
	}
	value, _ := state[key].(string)
	return value
}

// writeCurrentState 原子落盘指向记录；extra 携带 previousVersion/pendingVersion/attemptId。
func writeCurrentState(root, version string, lastKnownGood string, extra map[string]any) error {
	previous, _ := readCurrentState(root)
	payload := map[string]any{}
	for key, value := range previous {
		payload[key] = value
	}
	payload["version"] = version
	payload["installedAt"] = float64(time.Now().UnixNano()) / 1e9
	if lastKnownGood != "" {
		payload["lastKnownGood"] = lastKnownGood
	} else {
		delete(payload, "lastKnownGood")
	}
	for key, value := range extra {
		if value == nil {
			delete(payload, key)
		} else {
			payload[key] = value
		}
	}
	stateDir := filepath.Join(root, stateDirName)
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		return err
	}
	return atomicJSON(filepath.Join(stateDir, currentStateFile), payload)
}

// confirmVersion 指针回正补齐「已验收」事实：清 pendingVersion、lastKnownGood 指源版本。
func confirmVersion(root, version string) error {
	state, _ := readCurrentState(root)
	if state == nil {
		state = map[string]any{}
	}
	delete(state, "pendingVersion")
	state["version"] = version
	state["confirmedAt"] = float64(time.Now().UnixNano()) / 1e9
	if stateString(state, "lastKnownGood") == "" {
		if previous := stateString(state, "previousVersion"); previous != "" {
			state["lastKnownGood"] = previous
		}
	}
	return atomicJSON(filepath.Join(root, stateDirName, currentStateFile), state)
}

// abortVersion 撤下未通过验收的版本，指针交还 lastKnownGood/previousVersion。
func abortVersion(root, attempted string) bool {
	state, _ := readCurrentState(root)
	fallback := ""
	if state != nil {
		if value := stateString(state, "lastKnownGood"); value != "" {
			fallback = value
		} else if value := stateString(state, "previousVersion"); value != "" {
			fallback = value
		}
	}
	if fallback == "" || !isManagedVersionName(attempted) ||
		strings.EqualFold(getVersionDir(root, fallback), getVersionDir(root, attempted)) {
		return false
	}
	if !isVersionValid(root, fallback) {
		return false
	}
	if err := writeCurrentState(root, fallback, "", nil); err != nil {
		return false
	}
	os.RemoveAll(getVersionDir(root, attempted))
	return true
}

// ───────────────────────── state/transaction.json ─────────────────────────

func readTransaction(root string) map[string]any {
	raw, err := os.ReadFile(filepath.Join(root, stateDirName, transactionF))
	if err != nil {
		return nil
	}
	var tx map[string]any
	if json.Unmarshal(raw, &tx) != nil {
		return nil
	}
	return tx
}

func txString(tx map[string]any, key string) string {
	if tx == nil {
		return ""
	}
	value, _ := tx[key].(string)
	return value
}

func txBool(tx map[string]any, key string) bool {
	if tx == nil {
		return false
	}
	value, _ := tx[key].(bool)
	return value
}

func startTransaction(root, targetVersion, sourceVersion, attemptID string) error {
	now := float64(time.Now().UnixNano()) / 1e9
	tx := map[string]any{
		"attemptId":     attemptID,
		"stage":         stageStaged,
		"targetVersion": targetVersion,
		"sourceVersion": sourceVersion,
		"startTime":     now,
		"updatedAt":     now,
		"error":         nil,
	}
	stateDir := filepath.Join(root, stateDirName)
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		return err
	}
	return atomicJSON(filepath.Join(stateDir, transactionF), tx)
}

func updateTransaction(root, stage string, kwargs map[string]any) error {
	tx := readTransaction(root)
	if tx == nil {
		tx = map[string]any{}
	}
	tx["stage"] = stage
	tx["updatedAt"] = float64(time.Now().UnixNano()) / 1e9
	for key, value := range kwargs {
		tx[key] = value
	}
	return atomicJSON(filepath.Join(root, stateDirName, transactionF), tx)
}

// ───────────────────────── 数据相容记录 ─────────────────────────

func readDataCompatibility(root string) (map[string]any, error) {
	state, err := readCurrentState(root)
	if err != nil {
		return nil, err
	}
	if state == nil {
		return nil, nil
	}
	compat, ok := state["dataCompatibility"].(map[string]any)
	if !ok {
		return nil, nil
	}
	return compat, nil
}

func writeDataCompatibility(root string, compatibility map[string]any) error {
	state, _ := readCurrentState(root)
	if state == nil {
		state = map[string]any{}
	}
	state["dataCompatibility"] = compatibility
	stateDir := filepath.Join(root, stateDirName)
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		return err
	}
	return atomicJSON(filepath.Join(root, stateDirName, currentStateFile), state)
}

// ───────────────────────── 版本维护 ─────────────────────────

func confirmablePrune(root string, minKeep int) []string {
	all := listInstalledVersions(root)
	state, _ := readCurrentState(root)
	protected := map[string]bool{
		stateString(state, "version"):       true,
		stateString(state, "lastKnownGood"): true,
	}
	delete(protected, "")
	if len(protected) < minKeep && len(all) > 0 {
		sort.Slice(all, func(i, j int) bool {
			first, _ := os.Stat(getVersionDir(root, all[i]))
			second, _ := os.Stat(getVersionDir(root, all[j]))
			if first == nil || second == nil {
				return false
			}
			return first.ModTime().After(second.ModTime())
		})
		for _, version := range all {
			if len(protected) >= minKeep {
				break
			}
			protected[version] = true
		}
	}
	var pruned []string
	for _, version := range all {
		if !protected[version] {
			if os.RemoveAll(getVersionDir(root, version)) == nil {
				pruned = append(pruned, version)
			}
		}
	}
	return pruned
}

func atomicJSON(target string, value any) error {
	payload, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(target), filepath.Base(target)+".*.tmp")
	if err != nil {
		return err
	}
	name := file.Name()
	defer os.Remove(name)
	if _, err = file.Write(payload); err != nil {
		file.Close()
		return err
	}
	if err = file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(name, target)
}
