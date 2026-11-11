//go:build windows

// status.go 换装状态查询与确认：换装成功首次启动的 justUpdated 投影、
// staging 就绪的 pending 投影、确认闭合账本、放弃更新清暂存。
package updater

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// readTransaction 读安装根事务账本（layout.read_transaction 等价）。
func readTransaction(root string) map[string]any {
	raw, err := os.ReadFile(filepath.Join(root, "state", "transaction.json"))
	if err != nil {
		return nil
	}
	var tx map[string]any
	if json.Unmarshal(raw, &tx) != nil {
		return nil
	}
	return tx
}

func readCurrentState(root string) map[string]any {
	raw, err := os.ReadFile(filepath.Join(root, "state", "current.json"))
	if err != nil {
		return nil
	}
	var state map[string]any
	if json.Unmarshal(raw, &state) != nil {
		return nil
	}
	return state
}

func txString(tx map[string]any, key string) string {
	text, _ := tx[key].(string)
	return text
}

func txBool(tx map[string]any, key string) bool {
	value, _ := tx[key].(bool)
	return value
}

// completedUpdateTransaction 事务已提交且与当前版本闭合的判定。
func (s *Service) completedUpdateTransaction(tx map[string]any) bool {
	if tx == nil || txString(tx, "attemptId") == "" || txString(tx, "stage") != "committed" {
		return false
	}
	target := txString(tx, "targetVersion")
	current := txString(tx, "currentVersion")
	if current != target || target != s.Version {
		return false
	}
	return txBool(tx, "dataAccepted") ||
		(!txBool(tx, "dataHadDatabase") && !txBool(tx, "dataPrepared"))
}

// readDiagnosis 更新诊断投影：三候选路径取最新，事务在途时按 attemptId 配对；
// 已提交事务强制投影 committed 形态（对齐 updater._read_update_diagnosis）。
func (s *Service) readDiagnosis() map[string]any {
	root := s.InstallRoot
	candidates := []string{
		filepath.Join(root, "logs", "update-diagnosis.json"),
		filepath.Join(root, "data", "logs", "update-diagnosis.json"),
	}
	var records []map[string]any
	for _, path := range candidates {
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		var data map[string]any
		if json.Unmarshal(raw, &data) != nil || data == nil {
			continue
		}
		records = append(records, data)
	}
	tx := readTransaction(root)
	txAttempt := ""
	if tx != nil {
		txAttempt = txString(tx, "attemptId")
	}
	var data map[string]any
	if txAttempt != "" {
		target := ""
		if tx != nil {
			target = txString(tx, "targetVersion")
		}
		for _, record := range records {
			if stringOf(record["attemptId"]) == txAttempt && stringOf(record["targetVersion"]) == target {
				data = record
				break
			}
		}
		if data == nil {
			data = map[string]any{}
		}
	} else if len(records) > 0 {
		data = records[0]
	} else {
		data = map[string]any{}
	}
	if s.completedUpdateTransaction(tx) {
		acknowledged := stringOf(data["stage"]) == "committed" && txBool(data, "success") && txBool(data, "acknowledged")
		projection := map[string]any{}
		for key, value := range data {
			projection[key] = value
		}
		projection["attemptId"] = txAttempt
		projection["targetVersion"] = s.Version
		projection["currentVersion"] = s.Version
		projection["sourceVersion"] = txString(tx, "sourceVersion")
		projection["stage"] = "committed"
		projection["success"] = true
		projection["acknowledged"] = acknowledged
		return projection
	}
	return data
}

// JustUpdated 已提交更新的未确认成功提示（无则 nil 语义 → 空 map 返回）。
func (s *Service) JustUpdated() map[string]any {
	data := s.readDiagnosis()
	if !txBool(data, "success") || stringOf(data["stage"]) != "committed" {
		return nil
	}
	if txBool(data, "acknowledged") {
		return nil
	}
	target := stringOf(data["targetVersion"])
	if target != "" && target != s.Version {
		return nil
	}
	previous := stringOf(data["sourceVersion"])
	if previous == "" {
		previous = stringOf(data["previousVersion"])
	}
	if previous == "" {
		previous = stringOf(data["currentVersion"])
	}
	if previous == "" {
		state := readCurrentState(s.InstallRoot)
		if state != nil {
			previous = stringOf(state["lastKnownGood"])
			if previous == "" {
				previous = stringOf(state["previousVersion"])
			}
		}
	}
	notes := stringOf(data["notes"])
	if notes == "" {
		notes = s.releaseNotesFromFile()
	}
	return map[string]any{
		"previousVersion": previous,
		"currentVersion":  firstNonEmpty(target, s.Version),
		"notes":           notes,
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value != "" {
			return value
		}
	}
	return ""
}

// releaseNotesFromFile 换装成功提示的说明兜底（随包 latest.json / RELEASE_NOTES）。
func (s *Service) releaseNotesFromFile() string {
	candidates := []string{
		filepath.Join(s.InstallRoot, "release", "latest.json"),
		filepath.Join(s.InstallRoot, "latest.json"),
		filepath.Join(s.InstallRoot, "RELEASE_NOTES.md"),
		filepath.Join(s.InstallRoot, "release", "RELEASE_NOTES.md"),
	}
	for _, path := range candidates {
		raw, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		if strings.HasSuffix(path, ".json") {
			var payload map[string]any
			if json.Unmarshal(raw, &payload) == nil && stringOf(payload["notes"]) != "" {
				return plainNotes(stringOf(payload["notes"]))
			}
			continue
		}
		return plainNotes(string(raw))
	}
	return ""
}

// AcknowledgeUpdate 确认本次更新报告已查看（诊断文件内 acknowledged 置位）。
func (s *Service) AcknowledgeUpdate() map[string]any {
	data := s.readDiagnosis()
	if !txBool(data, "success") || stringOf(data["stage"]) != "committed" ||
		firstNonEmpty(stringOf(data["targetVersion"]), s.Version) != s.Version {
		return map[string]any{"acknowledged": false, "updated": false}
	}
	if txBool(data, "acknowledged") {
		return map[string]any{"acknowledged": true, "updated": false}
	}
	path := filepath.Join(s.InstallRoot, "logs", "update-diagnosis.json")
	if _, err := os.Stat(path); err != nil {
		path = filepath.Join(s.InstallRoot, "data", "logs", "update-diagnosis.json")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return map[string]any{"acknowledged": false, "updated": false}
	}
	var record map[string]any
	if json.Unmarshal(raw, &record) != nil {
		return map[string]any{"acknowledged": false, "updated": false}
	}
	record["acknowledged"] = true
	payload, err := json.MarshalIndent(record, "", "  ")
	if err != nil {
		return map[string]any{"acknowledged": false, "updated": false}
	}
	if err := os.WriteFile(path, payload, 0o644); err != nil {
		return map[string]any{"acknowledged": false, "updated": false}
	}
	return map[string]any{"acknowledged": true, "updated": true}
}

// PendingStatus staging 就绪状态（前端「重启以完成更新」提示依据）。
// swapFailed 投影换装失败标记（前端给「重新下载」出路），justUpdated 投影
// 换装成功首次启动事实。
func (s *Service) PendingStatus() map[string]any {
	tx := readTransaction(s.InstallRoot)
	diagnosis := s.readDiagnosis()
	installing := false
	if tx != nil && txString(tx, "attemptId") != "" &&
		txString(tx, "targetVersion") == s.Version {
		stage := txString(tx, "stage")
		if stage == "verifying" || stage == "committed" {
			installing = stringOf(diagnosis["attemptId"]) == txString(tx, "attemptId") &&
				stringOf(diagnosis["targetVersion"]) == s.Version &&
				stringOf(diagnosis["stage"]) == "switching" &&
				stringOf(diagnosis["error"]) == ""
		}
	}
	result := map[string]any{
		"pending":    false,
		"installing": installing,
		"swapFailed": fileExists(filepath.Join(s.dataDir(), SwapFailedFlag)),
	}
	if justUpdated := s.JustUpdated(); justUpdated != nil {
		result["justUpdated"] = justUpdated
	} else {
		result["justUpdated"] = nil
	}
	manifestPath := filepath.Join(s.stagingDir(), StagingManifest)
	raw, err := os.ReadFile(manifestPath)
	if err != nil {
		return result
	}
	var manifest map[string]any
	if json.Unmarshal(raw, &manifest) != nil {
		return result // 坏 manifest 视为无暂存
	}
	tag := stringOf(manifest["tag"])
	completed := stringOf(diagnosis["stage"]) == "committed" && txBool(diagnosis, "success") &&
		stringOf(diagnosis["targetVersion"]) == s.Version
	installedTarget := (installing || completed) && strings.TrimPrefix(tag, "v") == s.Version
	result["pending"] = txBool(manifest, "ready") && !installedTarget
	result["tag"] = tag
	return result
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && !info.IsDir()
}

var _ = fmt.Sprintf
