package hostkit

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	StageStaged           = "staged"
	StageSwitching        = "switching"
	StageVerifying        = "verifying"
	StageCommitted        = "committed"
	StageRolledBack       = "rolled_back"
	StageRecoveryRequired = "recovery_required"
)

var reservedVersionNames = map[string]bool{
	"data": true, "holdexar": true, "app": true, "logs": true, "updater": true,
	"state": true, "versions": true, "update-staging": true, "_internal": true,
	"__old__": true, "__legacy__": true, "cache": true, "temp": true, "tmp": true,
}

func decodeSingleJSONObject(raw []byte) (map[string]any, error) {
	if !json.Valid(raw) {
		return nil, errors.New("JSON 格式无效")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var obj map[string]any
	if err := dec.Decode(&obj); err != nil {
		return nil, err
	}
	if obj == nil {
		return nil, errors.New("JSON 顶层不是对象")
	}
	var trailing any
	if err := dec.Decode(&trailing); err != io.EOF {
		return nil, errors.New("JSON 存在多余尾部内容")
	}
	return obj, nil
}

func isStrictIntegerLexeme(text string) bool {
	if text == "" {
		return false
	}
	start := 0
	if text[0] == '-' {
		if len(text) == 1 {
			return false
		}
		start = 1
	}
	for i := start; i < len(text); i++ {
		if text[i] < '0' || text[i] > '9' {
			return false
		}
	}
	return true
}

func parseStrictInt(raw json.RawMessage) (int, error) {
	text := strings.TrimSpace(string(raw))
	if !isStrictIntegerLexeme(text) {
		return 0, errors.New("整数字段词法无效")
	}
	return strconv.Atoi(text)
}

func parseStrictInt64Value(val any) (int64, bool) {
	switch typed := val.(type) {
	case json.Number:
		text := typed.String()
		if !isStrictIntegerLexeme(text) {
			return 0, false
		}
		n, err := strconv.ParseInt(text, 10, 64)
		return n, err == nil
	case int:
		return int64(typed), true
	case int64:
		return typed, true
	case int32:
		return int64(typed), true
	default:
		return 0, false
	}
}

// CurrentState represents state/current.json while preserving unknown extension fields.
type CurrentState struct {
	Version           string         `json:"version"`
	InstalledAt       float64        `json:"installedAt,omitempty"`
	LastKnownGood     *string        `json:"lastKnownGood"`
	PreviousVersion   *string        `json:"previousVersion,omitempty"`
	PendingVersion    *string        `json:"pendingVersion,omitempty"`
	AttemptID         *string        `json:"attemptId,omitempty"`
	DataCompatibility *DataCompat    `json:"dataCompatibility,omitempty"`
	Extra             map[string]any `json:"-"`
	Raw               map[string]any `json:"-"`
}

// DataCompat represents the case-sensitive dataCompatibility block inside state/current.json.
type DataCompat struct {
	DataDir       string         `json:"dataDir"`
	SchemaVersion int            `json:"schemaVersion"`
	Versions      []string       `json:"versions"`
	Extra         map[string]any `json:"-"`
}

func (c *DataCompat) UnmarshalJSON(data []byte) error {
	if !json.Valid(data) {
		return errors.New("数据相容记录格式无效")
	}
	var required map[string]json.RawMessage
	if err := json.Unmarshal(data, &required); err != nil || required == nil {
		return errors.New("数据相容记录不是有效对象")
	}
	for _, key := range []string{"dataDir", "schemaVersion", "versions"} {
		value, ok := required[key]
		if !ok || strings.TrimSpace(string(value)) == "null" {
			return errors.New("数据相容记录缺少必需字段")
		}
	}
	var dataDir string
	if err := json.Unmarshal(required["dataDir"], &dataDir); err != nil {
		return err
	}
	schemaVer, err := parseStrictInt(required["schemaVersion"])
	if err != nil {
		return err
	}
	var rawVersions []json.RawMessage
	if err := json.Unmarshal(required["versions"], &rawVersions); err != nil || rawVersions == nil {
		return errors.New("数据相容版本列表无效")
	}
	versions := make([]string, 0, len(rawVersions))
	for _, item := range rawVersions {
		if strings.TrimSpace(string(item)) == "null" {
			return errors.New("数据相容版本项不能为空")
		}
		var ver string
		if err := json.Unmarshal(item, &ver); err != nil {
			return err
		}
		versions = append(versions, ver)
	}

	rawObj, err := decodeSingleJSONObject(data)
	if err != nil {
		return err
	}
	delete(rawObj, "dataDir")
	delete(rawObj, "schemaVersion")
	delete(rawObj, "versions")
	*c = DataCompat{
		DataDir:       dataDir,
		SchemaVersion: schemaVer,
		Versions:      versions,
		Extra:         rawObj,
	}
	return nil
}

func (c DataCompat) MarshalJSON() ([]byte, error) {
	payload := make(map[string]any, len(c.Extra)+3)
	for key, value := range c.Extra {
		payload[key] = value
	}
	payload["dataDir"] = c.DataDir
	payload["schemaVersion"] = c.SchemaVersion
	payload["versions"] = c.Versions
	return json.Marshal(payload)
}

func (s CurrentState) MarshalJSON() ([]byte, error) {
	payload := map[string]any{}
	for key, value := range s.Raw {
		payload[key] = value
	}
	for key, value := range s.Extra {
		payload[key] = value
	}
	payload["version"] = s.Version
	payload["installedAt"] = s.InstalledAt
	payload["lastKnownGood"] = s.LastKnownGood
	if s.PreviousVersion != nil {
		payload["previousVersion"] = *s.PreviousVersion
	} else {
		delete(payload, "previousVersion")
	}
	if s.PendingVersion != nil {
		payload["pendingVersion"] = *s.PendingVersion
	} else {
		delete(payload, "pendingVersion")
	}
	if s.AttemptID != nil {
		payload["attemptId"] = *s.AttemptID
	} else {
		delete(payload, "attemptId")
	}
	if s.DataCompatibility != nil {
		payload["dataCompatibility"] = s.DataCompatibility
	} else {
		delete(payload, "dataCompatibility")
	}
	return json.Marshal(payload)
}

func optionalStringField(fields map[string]any, key string) (*string, error) {
	raw, exists := fields[key]
	if !exists || raw == nil {
		return nil, nil
	}
	text, ok := raw.(string)
	if !ok {
		return nil, fmt.Errorf("字段 %s 类型无效", key)
	}
	return &text, nil
}

// ValidateDataCompat validates required DataCompat invariants, deduplicates versions, and canonicalizes DataDir.
func ValidateDataCompat(compat *DataCompat) (*DataCompat, error) {
	if compat == nil {
		return nil, errors.New("数据与程序的相容记录无效")
	}
	if compat.DataDir == "" || !filepath.IsAbs(compat.DataDir) {
		return nil, errors.New("数据与程序的相容记录不完整，已停止选择程序")
	}
	if compat.SchemaVersion < 0 {
		return nil, errors.New("数据与程序的相容记录不完整，已停止选择程序")
	}
	if len(compat.Versions) == 0 {
		return nil, errors.New("数据与程序的相容记录不完整，已停止选择程序")
	}
	seen := make(map[string]bool, len(compat.Versions))
	unique := make([]string, 0, len(compat.Versions))
	for _, version := range compat.Versions {
		if !IsManagedVersionName(version) {
			return nil, errors.New("数据与程序的相容记录不完整，已停止选择程序")
		}
		if !seen[version] {
			seen[version] = true
			unique = append(unique, version)
		}
	}
	out := *compat
	out.Versions = unique
	out.DataDir = CanonicalPath(compat.DataDir)
	return &out, nil
}

// ReadCurrentState reads state/current.json; returns (nil, nil) only when the file does not exist.
func ReadCurrentState(root string) (*CurrentState, error) {
	path := filepath.Join(root, "state", "current.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) || os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	fields, err := decodeSingleJSONObject(raw)
	if err != nil {
		return nil, fmt.Errorf("程序指向记录无法读取，已停止选择程序: %w", err)
	}
	version, ok := fields["version"].(string)
	if !ok || version == "" {
		return nil, errors.New("程序指向记录缺少有效版本，已停止选择程序")
	}
	state := &CurrentState{Version: version, Raw: fields}
	if num, ok := fields["installedAt"].(json.Number); ok {
		state.InstalledAt, _ = num.Float64()
	}
	if state.LastKnownGood, err = optionalStringField(fields, "lastKnownGood"); err != nil {
		return nil, err
	}
	if state.PreviousVersion, err = optionalStringField(fields, "previousVersion"); err != nil {
		return nil, err
	}
	if state.PendingVersion, err = optionalStringField(fields, "pendingVersion"); err != nil {
		return nil, err
	}
	if state.AttemptID, err = optionalStringField(fields, "attemptId"); err != nil {
		return nil, err
	}
	if compatRaw, exists := fields["dataCompatibility"]; exists {
		encoded, err := json.Marshal(compatRaw)
		if err != nil {
			return nil, err
		}
		compat := &DataCompat{}
		if err := json.Unmarshal(encoded, compat); err != nil {
			return nil, err
		}
		validated, err := ValidateDataCompat(compat)
		if err != nil {
			return nil, err
		}
		state.DataCompatibility = validated
	}
	return state, nil
}

// ReadDataCompatibility reads and validates the dataCompatibility block from state/current.json.
func ReadDataCompatibility(root string) (*DataCompat, error) {
	state, err := ReadCurrentState(root)
	if err != nil {
		return nil, err
	}
	if state == nil || state.DataCompatibility == nil {
		return nil, nil
	}
	return ValidateDataCompat(state.DataCompatibility)
}

// AtomicWriteJSON marshals v with 2-space indentation to a temporary sibling file, fsyncs, and renames onto path.
func AtomicWriteJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	payload, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), filepath.Base(path)+".*.tmp")
	if err != nil {
		return err
	}
	name := file.Name()
	defer os.Remove(name)
	if _, err = file.Write(payload); err != nil {
		_ = file.Close()
		return err
	}
	if err = file.Sync(); err != nil {
		_ = file.Close()
		return err
	}
	if err = file.Close(); err != nil {
		return err
	}
	return os.Rename(name, path)
}

// WriteCurrentState atomically writes state/current.json, preserving DataCompatibility and applying optional extra fields.
func WriteCurrentState(root, version string, lastKnownGood *string, extra ...map[string]any) (*CurrentState, error) {
	previous, _ := ReadCurrentState(root)
	state := &CurrentState{
		Version:       version,
		InstalledAt:   float64(time.Now().UnixNano()) / 1e9,
		LastKnownGood: lastKnownGood,
	}
	if previous != nil && previous.DataCompatibility != nil {
		state.DataCompatibility = previous.DataCompatibility
	}
	for _, m := range extra {
		for k, v := range m {
			switch k {
			case "previousVersion":
				if v == nil {
					state.PreviousVersion = nil
				} else if s, ok := v.(string); ok {
					state.PreviousVersion = &s
				}
			case "pendingVersion":
				if v == nil {
					state.PendingVersion = nil
				} else if s, ok := v.(string); ok {
					state.PendingVersion = &s
				}
			case "attemptId":
				if v == nil {
					state.AttemptID = nil
				} else if s, ok := v.(string); ok {
					state.AttemptID = &s
				}
			default:
				if state.Extra == nil {
					state.Extra = map[string]any{}
				}
				if v == nil {
					delete(state.Extra, k)
				} else {
					state.Extra[k] = v
				}
			}
		}
	}
	target := filepath.Join(root, "state", "current.json")
	if err := AtomicWriteJSON(target, state); err != nil {
		return nil, err
	}
	return state, nil
}

// ReadTransaction reads state/transaction.json; returns (nil, nil) only when missing, and error when corrupt.
func ReadTransaction(root string) (map[string]any, error) {
	path := filepath.Join(root, "state", "transaction.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) || os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	tx, err := decodeSingleJSONObject(raw)
	if err != nil {
		return nil, fmt.Errorf("更新事务无法读取: %w", err)
	}
	return tx, nil
}

// UpdateRecoveryNeeded reports whether tx entered any data-protection or directory-tracking phase.
func UpdateRecoveryNeeded(tx map[string]any) bool {
	if tx == nil {
		return false
	}
	for _, key := range []string{"dataRollbackReleased", "dataAccepted", "dataPrepared"} {
		if tx[key] == true {
			return true
		}
	}
	dir, _ := tx["dataDir"].(string)
	return dir != ""
}

func inactiveUpdateFlag(value any) bool {
	return value == nil || value == false
}

// UpdateRecoveryComplete reports whether tx represents a clean terminal update record with no remaining lock marker.
func UpdateRecoveryComplete(tx map[string]any) bool {
	if tx == nil {
		return false
	}
	attempt, _ := tx["attemptId"].(string)
	if attempt == "" || !inactiveUpdateFlag(tx["launchPending"]) {
		return false
	}
	if !UpdateRecoveryNeeded(tx) {
		return true
	}
	directory, _ := tx["dataDir"].(string)
	if !filepath.IsAbs(directory) {
		return false
	}
	if _, err := os.Lstat(filepath.Join(directory, "backups", ".restore.lock")); !errors.Is(err, os.ErrNotExist) && !os.IsNotExist(err) {
		return false
	}
	children, childrenList := tx["children"].([]any)
	source, _ := tx["sourceVersion"].(string)
	target, _ := tx["targetVersion"].(string)
	if inactiveUpdateFlag(tx["dataPrepared"]) && inactiveUpdateFlag(tx["dataAccepted"]) &&
		IsManagedVersionName(source) && IsManagedVersionName(target) && target != source &&
		(tx["children"] == nil || (childrenList && len(children) == 0)) {
		return true
	}
	database, _ := tx["dataDbPath"].(string)
	if !filepath.IsAbs(database) || !SamePath(database, filepath.Join(directory, DBFileName)) {
		return false
	}
	if tx["stage"] == StageRolledBack {
		return tx["dataRollbackReleased"] == true &&
			inactiveUpdateFlag(tx["dataAccepted"]) &&
			tx["dataRecoveryPhase"] == "restored" &&
			IsManagedVersionName(source)
	}
	if released, exists := tx["dataRollbackReleased"]; exists && released != false {
		return false
	}
	_, schemaOK := parseStrictInt64Value(tx["dataSchemaVersion"])
	currentVer, _ := tx["currentVersion"].(string)
	return tx["stage"] == StageCommitted &&
		tx["dataPrepared"] == true &&
		tx["dataAccepted"] == true &&
		tx["dataRecoveryPhase"] == "accepted" &&
		IsManagedVersionName(currentVer) &&
		IsManagedVersionName(target) &&
		schemaOK
}

// UpdateRecoveryCompleteForRoot cross-checks tx against root and state/current.json compatibility invariants.
func UpdateRecoveryCompleteForRoot(root string, tx map[string]any, state *CurrentState) bool {
	if state == nil || tx == nil || !IsManagedVersionName(state.Version) {
		return false
	}
	if state.PendingVersion != nil && *state.PendingVersion != "" {
		return false
	}
	if !UpdateRecoveryComplete(tx) {
		return false
	}
	attempt, _ := tx["attemptId"].(string)
	directory, _ := tx["dataDir"].(string)
	compat := state.DataCompatibility
	allowed := map[string]bool{}
	if compat != nil {
		validated, err := ValidateDataCompat(compat)
		if err != nil || !SamePath(validated.DataDir, directory) {
			return false
		}
		for _, ver := range validated.Versions {
			allowed[ver] = true
		}
		if !allowed[state.Version] {
			return false
		}
		compat = validated
	} else if !SamePath(directory, filepath.Join(root, "data")) {
		return false
	}

	children, childrenList := tx["children"].([]any)
	target, _ := tx["targetVersion"].(string)
	if inactiveUpdateFlag(tx["dataPrepared"]) && inactiveUpdateFlag(tx["dataAccepted"]) &&
		tx["sourceVersion"] == state.Version && IsManagedVersionName(target) && target != state.Version &&
		(tx["children"] == nil || (childrenList && len(children) == 0)) {
		return true
	}
	if state.AttemptID != nil && *state.AttemptID != "" && *state.AttemptID != attempt {
		return false
	}
	if tx["stage"] == StageRolledBack {
		return tx["dataRollbackReleased"] == true &&
			inactiveUpdateFlag(tx["dataAccepted"]) &&
			tx["dataRecoveryPhase"] == "restored" &&
			tx["sourceVersion"] == state.Version
	}
	acceptedSchema, ok := parseStrictInt64Value(tx["dataSchemaVersion"])
	return tx["stage"] == StageCommitted &&
		tx["dataPrepared"] == true &&
		tx["dataAccepted"] == true &&
		tx["dataRecoveryPhase"] == "accepted" &&
		tx["currentVersion"] == state.Version &&
		compat != nil &&
		allowed[target] &&
		ok &&
		acceptedSchema == int64(compat.SchemaVersion)
}

// IsManagedVersionName reports whether name is a safe version directory identifier.
func IsManagedVersionName(name string) bool {
	if name == "" || name == "." || name == ".." {
		return false
	}
	if strings.TrimSpace(name) != name || strings.HasSuffix(name, ".") {
		return false
	}
	if strings.ContainsAny(name, `/\:`) {
		return false
	}
	if reservedVersionNames[strings.ToLower(name)] {
		return false
	}
	if name == "legacy_version" {
		return true
	}
	return name[0] >= '0' && name[0] <= '9'
}

// VersionDir returns <root>/versions/<ver> after verifying ver name and zero reparse points.
func VersionDir(root, ver string) (string, error) {
	if !IsManagedVersionName(ver) {
		return "", errors.New("版本目录名称无效")
	}
	versionsRoot := filepath.Join(root, "versions")
	target := filepath.Join(versionsRoot, ver)
	if _, err := TrustedPath(versionsRoot); err != nil {
		return "", errors.New("版本目录超出受管安装位置")
	}
	if _, err := TrustedPath(target); err != nil {
		return "", errors.New("版本目录超出受管安装位置")
	}
	return target, nil
}

// IsVersionValid verifies <root>/versions/<ver> with Holdexar.exe and optional flat or _internal serverd.exe, rejecting symlinks/junctions.
func IsVersionValid(root, ver string) bool {
	if !IsManagedVersionName(ver) {
		return false
	}
	dir, err := VersionDir(root, ver)
	if err != nil {
		return false
	}
	info, err := os.Lstat(dir)
	if err != nil || !info.IsDir() || IsReparseOrSymlink(dir, info) {
		return false
	}
	exe := filepath.Join(dir, AppName+".exe")
	if !regularFileExists(exe) {
		return false
	}
	if _, err := TrustedPath(exe); err != nil {
		return false
	}
	flatServer := filepath.Join(dir, "serverd.exe")
	if sInfo, sErr := os.Lstat(flatServer); sErr == nil {
		if !sInfo.Mode().IsRegular() || IsReparseOrSymlink(flatServer, sInfo) {
			return false
		}
		if _, err := TrustedPath(flatServer); err != nil {
			return false
		}
	} else if !errors.Is(sErr, os.ErrNotExist) && !os.IsNotExist(sErr) {
		return false
	}

	internal := filepath.Join(dir, "_internal")
	if _, err := TrustedPath(internal); err != nil {
		return false
	}
	if iInfo, iErr := os.Lstat(internal); iErr == nil {
		if !iInfo.IsDir() || IsReparseOrSymlink(internal, iInfo) {
			return false
		}
		entries, rErr := os.ReadDir(internal)
		if rErr != nil || len(entries) == 0 {
			return false
		}
		legacyServer := filepath.Join(internal, "serverd.exe")
		if lsInfo, lsErr := os.Lstat(legacyServer); lsErr == nil {
			if !lsInfo.Mode().IsRegular() || IsReparseOrSymlink(legacyServer, lsInfo) {
				return false
			}
			if _, err := TrustedPath(legacyServer); err != nil {
				return false
			}
		} else if !errors.Is(lsErr, os.ErrNotExist) && !os.IsNotExist(lsErr) {
			return false
		}
	} else if !errors.Is(iErr, os.ErrNotExist) && !os.IsNotExist(iErr) {
		return false
	}
	return true
}

// ResolveVersionServerExe locates serverd.exe in <verDir>/serverd.exe or <verDir>/_internal/serverd.exe with TrustedPath verification.
func ResolveVersionServerExe(verDir string) (string, error) {
	for _, candidate := range []string{
		filepath.Join(verDir, "serverd.exe"),
		filepath.Join(verDir, "_internal", "serverd.exe"),
	} {
		if regularFileExists(candidate) {
			resolved, err := TrustedPath(candidate)
			if err != nil {
				return "", err
			}
			return resolved, nil
		}
	}
	return "", errors.New("未找到本地服务程序（serverd.exe）")
}

// ListInstalledVersions returns all valid version directory names under <root>/versions.
func ListInstalledVersions(root string) []string {
	versionsRoot := filepath.Join(root, "versions")
	if _, err := TrustedPath(versionsRoot); err != nil {
		return nil
	}
	entries, err := os.ReadDir(versionsRoot)
	if err != nil {
		return nil
	}
	var found []string
	for _, entry := range entries {
		if entry.IsDir() && IsVersionValid(root, entry.Name()) {
			found = append(found, entry.Name())
		}
	}
	return found
}

// SelectTargetVersion selects a compatible installed version, updating state/current.json on fallback.
func SelectTargetVersion(root string, state *CurrentState) (string, error) {
	if state == nil {
		loaded, err := ReadCurrentState(root)
		if err != nil {
			return "", err
		}
		state = loaded
	}
	var compat *DataCompat
	if state != nil && state.DataCompatibility != nil {
		validated, err := ValidateDataCompat(state.DataCompatibility)
		if err != nil {
			return "", err
		}
		compat = validated
		liveVersion, err := ReadSQLiteUserVersion(filepath.Join(compat.DataDir, DBFileName))
		if err != nil {
			return "", fmt.Errorf("数据库结构版本无法读取，已停止选择程序: %w", err)
		}
		if liveVersion != compat.SchemaVersion {
			return "", errors.New("数据结构已变化，拒绝选择未经相容验收的程序")
		}
	}

	allowed := map[string]bool{}
	if compat != nil {
		for _, ver := range compat.Versions {
			allowed[ver] = true
		}
	}
	usable := func(ver string) bool {
		return ver != "" && IsVersionValid(root, ver) && (compat == nil || allowed[ver])
	}

	if state != nil {
		if usable(state.Version) {
			return state.Version, nil
		}
		if state.LastKnownGood != nil && usable(*state.LastKnownGood) {
			if _, err := WriteCurrentState(root, *state.LastKnownGood, nil); err != nil {
				return "", err
			}
			return *state.LastKnownGood, nil
		}
		if state.PreviousVersion != nil && usable(*state.PreviousVersion) {
			if _, err := WriteCurrentState(root, *state.PreviousVersion, nil); err != nil {
				return "", err
			}
			return *state.PreviousVersion, nil
		}
	}

	var installed []string
	for _, ver := range ListInstalledVersions(root) {
		if usable(ver) {
			installed = append(installed, ver)
		}
	}
	if len(installed) == 0 {
		return "", nil
	}
	best, bestTime := "", int64(-1)
	for _, ver := range installed {
		info, err := os.Stat(filepath.Join(root, "versions", ver))
		if err != nil {
			continue
		}
		modNano := info.ModTime().UnixNano()
		if modNano > bestTime || (modNano == bestTime && VersionGT(ver, best)) {
			best, bestTime = ver, modNano
		}
	}
	if best == "" {
		return "", nil
	}
	if _, err := WriteCurrentState(root, best, nil); err != nil {
		return "", err
	}
	return best, nil
}
