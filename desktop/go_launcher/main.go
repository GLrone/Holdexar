// Holdexar 全原生启动器（Go 完整实现）。
// 根目录原生入口；版本、迁移与更新事实沿用现有 state/ 与 data/ 账本。
// 版本定位与自愈回退、数据相容校验（穿 WAL 读 user_version）、AppData 迁移
// （VACUUM INTO 在线快照 + 密钥/辅助目录保留 + 冲突拒启）、参数与环境逐字传递。
package main

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"syscall"
	"time"
	"unsafe"

	_ "modernc.org/sqlite"
)

const (
	appName   = "Holdexar"
	appSlug   = "holdexar"
	envPrefix = "HOLDEXAR_"
	dbName    = appSlug + ".db"

	stageCommitted  = "committed"
	stageRolledBack = "rolled_back"

	migrationFailed  = "failed"
	migrationReused  = "reused"
	migrationMoved   = "completed"
	migrationPartial = "incomplete"
	migrationClash   = "conflict"
)

var staleEnvKeys = []string{
	"UPDATE_ATTEMPT_ID", "UPDATE_DATA_TOKEN", "LAUNCHER_PID",
	"DEV_URL", "UPDATE_RESTART", "PREV_PID",
}

var reservedVersionNames = map[string]bool{
	"data": true, "holdexar": true, "app": true, "logs": true, "updater": true,
	"state": true, "versions": true, "update-staging": true, "_internal": true,
	"__old__": true, "__legacy__": true, "cache": true, "temp": true, "tmp": true,
}

// ───────────────────────── 基础工具 ─────────────────────────

func showFatalDialog(args []string) bool {
	for _, arg := range args {
		if arg == "--server" {
			return false
		}
	}
	return os.Getenv("HOLDEXAR_NO_DIALOG") != "1" && os.Getenv("CI") != "1"
}

func showFatal(message string) {
	fmt.Fprintln(os.Stderr, message)
	if !showFatalDialog(os.Args[1:]) {
		return
	}
	title, _ := syscall.UTF16PtrFromString(appName + " 启动失败")
	text, _ := syscall.UTF16PtrFromString(message)
	syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW").Call(
		0, uintptr(unsafe.Pointer(text)), uintptr(unsafe.Pointer(title)), 0x10,
	)
}

func fatal(code int, err error) int {
	showFatal(err.Error())
	return code
}

func fileExists(path string) bool {
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular()
}

func fsyncFile(path string) error {
	handle, err := os.OpenFile(path, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer handle.Close()
	return handle.Sync()
}

// finalPath 对齐 Path.resolve()：GetFinalPathNameByHandleW 解析符号链接与
// NTFS junction（filepath.EvalSymlinks 不解析 junction，junction 逃逸检测必须用它）。
func finalPath(path string) (string, error) {
	kernel32 := syscall.NewLazyDLL("kernel32.dll")
	createFile := kernel32.NewProc("CreateFileW")
	finalByHandle := kernel32.NewProc("GetFinalPathNameByHandleW")
	closeHandle := kernel32.NewProc("CloseHandle")
	pathPtr, err := syscall.UTF16PtrFromString(path)
	if err != nil {
		return "", err
	}
	handle, _, callErr := createFile.Call(
		uintptr(unsafe.Pointer(pathPtr)),
		0,
		syscall.FILE_SHARE_READ|syscall.FILE_SHARE_WRITE|syscall.FILE_SHARE_DELETE,
		0,
		syscall.OPEN_EXISTING,
		syscall.FILE_FLAG_BACKUP_SEMANTICS,
		0)
	if handle == uintptr(syscall.InvalidHandle) {
		return "", callErr
	}
	defer closeHandle.Call(handle)
	buf := make([]uint16, 1024)
	n, _, callErr := finalByHandle.Call(uintptr(handle), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), 0)
	if n == 0 || n > uintptr(len(buf)) {
		if n > uintptr(len(buf)) {
			buf = make([]uint16, n)
			n, _, callErr = finalByHandle.Call(uintptr(handle), uintptr(unsafe.Pointer(&buf[0])), uintptr(len(buf)), 0)
			if n == 0 {
				return "", callErr
			}
		} else {
			return "", callErr
		}
	}
	resolved := syscall.UTF16ToString(buf[:n])
	resolved = strings.TrimPrefix(resolved, `\\?\`)
	if strings.HasPrefix(resolved, `UNC\`) {
		resolved = `\\` + strings.TrimPrefix(resolved, `UNC\`)
	}
	return resolved, nil
}

// resolvePath 对齐 Path.resolve()：返回绝对真实路径；不存在时退回绝对化。
func resolvePath(path string) string {
	if resolved, err := finalPath(path); err == nil {
		return resolved
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	parent, name := filepath.Split(filepath.Clean(abs))
	parent = strings.TrimRight(parent, `\/`)
	if parent == "" {
		return abs
	}
	return filepath.Join(resolvePath(parent), name)
}

// ───────────────────────── 状态指针 ─────────────────────────

type currentState struct {
	Version           string         `json:"version"`
	InstalledAt       float64        `json:"installedAt,omitempty"`
	LastKnownGood     *string        `json:"lastKnownGood"`
	PreviousVersion   *string        `json:"previousVersion,omitempty"`
	PendingVersion    *string        `json:"pendingVersion,omitempty"`
	AttemptID         *string        `json:"attemptId,omitempty"`
	DataCompatibility *dataCompat    `json:"dataCompatibility,omitempty"`
	Extra             map[string]any `json:"-"`
	Raw               map[string]any `json:"-"`
}

type dataCompat struct {
	DataDir       string         `json:"dataDir"`
	SchemaVersion int            `json:"schemaVersion"`
	Versions      []string       `json:"versions"`
	Extra         map[string]any `json:"-"`
}

func (c *dataCompat) UnmarshalJSON(data []byte) error {
	var required map[string]json.RawMessage
	if err := json.Unmarshal(data, &required); err != nil {
		return err
	}
	for _, key := range []string{"dataDir", "schemaVersion", "versions"} {
		if value, ok := required[key]; !ok || string(value) == "null" {
			return errors.New("数据相容记录缺少必需字段")
		}
	}
	*c = dataCompat{}
	if err := json.Unmarshal(required["dataDir"], &c.DataDir); err != nil {
		return err
	}
	if err := json.Unmarshal(required["schemaVersion"], &c.SchemaVersion); err != nil {
		return err
	}
	if err := json.Unmarshal(required["versions"], &c.Versions); err != nil {
		return err
	}
	var raw map[string]any
	decoder := json.NewDecoder(strings.NewReader(string(data)))
	decoder.UseNumber()
	if err := decoder.Decode(&raw); err == nil {
		delete(raw, "dataDir")
		delete(raw, "schemaVersion")
		delete(raw, "versions")
		c.Extra = raw
	}
	return nil
}

func (c dataCompat) MarshalJSON() ([]byte, error) {
	payload := map[string]any{"dataDir": c.DataDir, "schemaVersion": c.SchemaVersion, "versions": c.Versions}
	for key, value := range c.Extra {
		payload[key] = value
	}
	return json.Marshal(payload)
}

func (s *currentState) MarshalJSON() ([]byte, error) {
	payload := map[string]any{}
	for key, value := range s.Raw {
		payload[key] = value
	}
	payload["version"] = s.Version
	payload["installedAt"] = s.InstalledAt
	payload["lastKnownGood"] = s.LastKnownGood
	if s.PreviousVersion != nil {
		payload["previousVersion"] = *s.PreviousVersion
	}
	if s.PendingVersion != nil {
		payload["pendingVersion"] = *s.PendingVersion
	}
	if s.AttemptID != nil {
		payload["attemptId"] = *s.AttemptID
	}
	if s.DataCompatibility != nil {
		payload["dataCompatibility"] = s.DataCompatibility
	}
	return json.Marshal(payload)
}

// readCurrentState 对齐 layout.read_current_state：缺失或无 version 字段返回 nil；
// 文件存在但解析失败返回错误（区分「没有指针」与「指针坏了」）。
func readCurrentState(root string) (*currentState, error) {
	path := filepath.Join(root, "state", "current.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	var fields map[string]any
	if !json.Valid(raw) {
		return nil, errors.New("程序指向记录无法读取，已停止选择程序")
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	if err := decoder.Decode(&fields); err != nil {
		return nil, fmt.Errorf("程序指向记录无法读取，已停止选择程序: %w", err)
	}
	version, _ := fields["version"].(string)
	if version == "" {
		return nil, nil
	}
	state := &currentState{Version: version, Raw: fields}
	if value, ok := fields["installedAt"].(json.Number); ok {
		state.InstalledAt, _ = value.Float64()
	}
	if value, ok := fields["lastKnownGood"].(string); ok {
		state.LastKnownGood = &value
	}
	if value, ok := fields["previousVersion"].(string); ok {
		state.PreviousVersion = &value
	}
	if value, ok := fields["pendingVersion"].(string); ok {
		state.PendingVersion = &value
	}
	if value, ok := fields["attemptId"].(string); ok {
		state.AttemptID = &value
	}
	if compatRaw, ok := fields["dataCompatibility"]; ok {
		encoded, err := json.Marshal(compatRaw)
		if err != nil {
			return nil, err
		}
		compat := &dataCompat{}
		if err := json.Unmarshal(encoded, compat); err != nil {
			return nil, err
		}
		state.DataCompatibility = compat
	}
	return state, nil
}

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
	if reservedVersionNames[strings.ToLower(text)] {
		return false
	}
	if text == "legacy_version" {
		return true
	}
	return len(text) > 0 && text[0] >= '0' && text[0] <= '9'
}

func versionDir(root, version string) (string, error) {
	if !isManagedVersionName(version) {
		return "", errors.New("版本目录名称无效")
	}
	versions := filepath.Join(root, "versions")
	target := filepath.Join(versions, version)
	if !trustedPath(versions) || !trustedPath(target) {
		return "", errors.New("版本目录超出受管安装位置")
	}
	return filepath.Join(root, "versions", version), nil
}

func isVersionValid(root, version string) bool {
	if !isManagedVersionName(version) {
		return false
	}
	directory, err := versionDir(root, version)
	if err != nil {
		return false
	}
	info, err := os.Stat(directory)
	if err != nil || !info.IsDir() {
		return false
	}
	exe := filepath.Join(directory, appName+".exe")
	if !fileExists(exe) || !trustedPath(exe) {
		return false
	}
	internal := filepath.Join(directory, "_internal")
	if !trustedPath(internal) {
		return false
	}
	if resolved, err := finalPath(internal); err == nil {
		directoryResolved, err := finalPath(directory)
		if err != nil {
			return false
		}
		if !samePath(resolved, filepath.Join(directoryResolved, "_internal")) {
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
	versions := filepath.Join(root, "versions")
	entries, err := os.ReadDir(versions)
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

// writeCurrentState 对齐 layout.write_current_state：原子落盘，新状态只含
// version/installedAt/lastKnownGood/dataCompatibility —— pendingVersion、
// attemptId 等旧字段随回退一并清除（fallback 向量的关键断言）。
func writeCurrentState(root, version string, lastKnownGood *string) (*currentState, error) {
	previous, _ := readCurrentState(root)
	state := &currentState{
		Version:       version,
		InstalledAt:   float64(time.Now().UnixNano()) / 1e9,
		LastKnownGood: lastKnownGood,
	}
	if previous != nil && previous.DataCompatibility != nil {
		state.DataCompatibility = previous.DataCompatibility
	}
	stateDir := filepath.Join(root, "state")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		return nil, err
	}
	target := filepath.Join(stateDir, "current.json")
	if err := atomicJSON(target, state); err != nil {
		return nil, err
	}
	return state, nil
}

// ───────────────────────── 数据相容与 schema ─────────────────────────

func validateDataCompat(compat *dataCompat) (*dataCompat, error) {
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
	seen := map[string]bool{}
	var unique []string
	for _, version := range compat.Versions {
		if !isManagedVersionName(version) {
			return nil, errors.New("数据与程序的相容记录不完整，已停止选择程序")
		}
		if !seen[version] {
			seen[version] = true
			unique = append(unique, version)
		}
	}
	compat.Versions = unique
	compat.DataDir = resolvePath(compat.DataDir)
	return compat, nil
}

func readDataCompatibility(root string) (*dataCompat, error) {
	path := filepath.Join(root, "state", "current.json")
	state, err := readCurrentState(root)
	if err != nil {
		return nil, fmt.Errorf("程序指向记录无法读取，已停止选择程序: %w", err)
	}
	if state == nil {
		if _, err := os.Stat(path); !os.IsNotExist(err) {
			return nil, errors.New("程序指向记录无法读取，已停止选择程序")
		}
		return nil, nil
	}
	if state.DataCompatibility == nil {
		return nil, nil
	}
	return validateDataCompat(state.DataCompatibility)
}

// readSchemaVersion 对齐 db_safety.read_schema_version：只读连接穿透 WAL。
func readSchemaVersion(dataDir string) (int, error) {
	db := filepath.Join(resolvePath(dataDir), dbName)
	uri := sqliteURI(db)
	connection, err := sql.Open("sqlite", uri)
	if err != nil {
		return 0, fmt.Errorf("数据库结构版本无法读取，已停止选择程序: %w", err)
	}
	defer connection.Close()
	var version int
	if err := connection.QueryRow("PRAGMA user_version").Scan(&version); err != nil {
		return 0, fmt.Errorf("数据库结构版本无法读取，已停止选择程序: %w", err)
	}
	return version, nil
}

// ───────────────────────── 历史数据迁移 ─────────────────────────

func machineCandidates() []string {
	var out []string
	for _, key := range []string{"LOCALAPPDATA", "APPDATA"} {
		if base := os.Getenv(key); base != "" {
			out = append(out, filepath.Join(base, appSlug))
		}
	}
	home := os.Getenv("USERPROFILE")
	if home == "" {
		if current, err := os.UserHomeDir(); err == nil {
			home = current
		}
	}
	if home != "" {
		out = append(out, filepath.Join(home, "."+appSlug))
	}
	return out
}

type legacyLocations struct {
	TargetDataDir string
	HasTargetDB   bool
	AppdataDir    string
	HasAppdataDB  bool
	HasConflict   bool
	IsFresh       bool
}

func readMigrationMeta(targetDataDir string) map[string]any {
	raw, err := os.ReadFile(filepath.Join(targetDataDir, "migration.json"))
	if err != nil {
		return nil
	}
	var meta map[string]any
	if err := json.Unmarshal(raw, &meta); err != nil {
		return nil
	}
	return meta
}

func detectLegacyLocations(root string) legacyLocations {
	targetDataDir := filepath.Join(root, "data")
	targetDB := filepath.Join(targetDataDir, dbName)
	hasTargetDB := fileExists(targetDB)

	appdataDir := ""
	for _, candidate := range machineCandidates() {
		if fileExists(filepath.Join(candidate, dbName)) {
			appdataDir = candidate
			break
		}
	}
	meta := readMigrationMeta(targetDataDir)
	pending := false
	if meta != nil {
		status, _ := meta["status"].(string)
		targetDir, _ := meta["targetDir"].(string)
		database, _ := meta["database"].(string)
		pending = status == migrationPartial && targetDir == resolvePath(targetDataDir) && database == dbName
	}
	if appdataDir == "" && pending {
		if recorded, ok := meta["sourceDir"].(string); ok && filepath.IsAbs(recorded) {
			appdataDir = recorded
		}
	}
	hasAppdataDB := appdataDir != "" && fileExists(filepath.Join(appdataDir, dbName))
	hasConflict := hasTargetDB && (hasAppdataDB || pending)
	return legacyLocations{
		TargetDataDir: targetDataDir, HasTargetDB: hasTargetDB,
		AppdataDir: appdataDir, HasAppdataDB: hasAppdataDB,
		HasConflict: hasConflict,
		IsFresh:     !hasTargetDB && appdataDir == "",
	}
}

func verifySnapshot(path string) (string, error) {
	uri := sqliteURI(resolvePath(path))
	connection, err := sql.Open("sqlite", uri)
	if err != nil {
		return "", err
	}
	defer connection.Close()
	var result string
	if err := connection.QueryRow("PRAGMA quick_check").Scan(&result); err != nil {
		return "", err
	}
	return result, nil
}

func openReadOnly(path string) (*sql.DB, error) {
	uri := sqliteURI(resolvePath(path))
	connection, err := sql.Open("sqlite", uri)
	if err != nil {
		return nil, err
	}
	if _, err := connection.Exec("PRAGMA busy_timeout=30000"); err != nil {
		connection.Close()
		return nil, err
	}
	return connection, nil
}

// adoptSourceDatabase 对齐 migration.adopt_source_database：VACUUM INTO 在线快照。
func adoptSourceDatabase(srcDir, targetDataDir string) (bool, string, error) {
	srcDB := filepath.Join(srcDir, dbName)
	destDB := filepath.Join(targetDataDir, dbName)
	if fileExists(destDB) {
		return false, migrationClash, errors.New("独立数据库冲突")
	}
	if err := os.MkdirAll(targetDataDir, 0o755); err != nil {
		return false, migrationFailed, err
	}

	tmpDest := filepath.Join(targetDataDir, fmt.Sprintf("%s.%x.migrating.tmp", dbName, time.Now().UnixNano()))
	connection, err := openReadOnly(srcDB)
	if err != nil {
		return false, migrationFailed, err
	}
	if _, err := connection.Exec("VACUUM INTO ?", tmpDest); err != nil {
		connection.Close()
		os.Remove(tmpDest)
		return false, migrationFailed, err
	}
	connection.Close()

	result, err := verifySnapshot(tmpDest)
	if err != nil || strings.ToLower(result) != "ok" {
		os.Remove(tmpDest)
		if err == nil {
			err = fmt.Errorf("迁移数据库完整性校验失败：%s", result)
		}
		return false, migrationFailed, err
	}
	if err := fsyncFile(tmpDest); err != nil {
		os.Remove(tmpDest)
		return false, migrationFailed, err
	}
	if err := installMissing(tmpDest, destDB); err != nil {
		os.Remove(tmpDest)
		return false, migrationFailed, err
	}
	if err := finishMigration(srcDir, targetDataDir, true); err != nil {
		return false, migrationPartial, err
	}
	return true, migrationMoved, nil
}

// copyMissing 对齐 migration.copy_missing：只补缺失，密钥冲突即失败。
func samePath(first, second string) bool {
	return strings.EqualFold(filepath.Clean(first), filepath.Clean(second))
}

func canonicalPath(path string) (string, error) {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	resolved, err := finalPath(absolute)
	if err == nil {
		return resolved, nil
	}
	if !os.IsNotExist(err) {
		return "", err
	}
	parent := filepath.Dir(absolute)
	if parent == absolute {
		return "", err
	}
	resolved, err = canonicalPath(parent)
	if err != nil {
		return "", err
	}
	return filepath.Join(resolved, filepath.Base(absolute)), nil
}
func trustedPath(path string) bool {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	real, err := canonicalPath(path)
	return err == nil && samePath(real, absolute)
}
func sqliteURI(path string) string {
	return (&url.URL{Scheme: "file", Path: "/" + filepath.ToSlash(path), RawQuery: "mode=ro"}).String()
}
func isSecret(name string) bool { return strings.HasPrefix(strings.ToLower(name), "secret.") }
func transient(name string) bool {
	return strings.HasSuffix(name, ".lock") || strings.HasSuffix(name, ".tmp") || strings.HasSuffix(name, ".part")
}
func installMissing(source, target string) error {
	from, err := syscall.UTF16PtrFromString(source)
	if err != nil {
		return err
	}
	to, err := syscall.UTF16PtrFromString(target)
	if err != nil {
		return err
	}
	result, _, err := syscall.NewLazyDLL("kernel32.dll").NewProc("MoveFileW").Call(uintptr(unsafe.Pointer(from)), uintptr(unsafe.Pointer(to)))
	if result == 0 {
		return err
	}
	return nil
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
func copyMissing(source, destination string) error {
	if !trustedPath(source) || !trustedPath(destination) {
		return errors.New("迁移附件不能通过链接指向其他位置")
	}
	if info, err := os.Stat(destination); err == nil {
		if !info.Mode().IsRegular() {
			return errors.New("迁移附件目标不是普通文件")
		}
		if isSecret(filepath.Base(source)) {
			src, err := os.ReadFile(source)
			if err != nil {
				return err
			}
			dst, err := os.ReadFile(destination)
			if err != nil {
				return err
			}
			if string(src) != string(dst) {
				return fmt.Errorf("目标已有不同的密钥文件：%s", filepath.Base(source))
			}
		}
		return nil
	} else if !os.IsNotExist(err) {
		return err
	}
	input, err := os.Open(source)
	if err != nil {
		return err
	}
	defer input.Close()
	tmp, err := os.CreateTemp(filepath.Dir(destination), filepath.Base(destination)+".*.tmp")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if _, err = io.Copy(tmp, input); err != nil {
		tmp.Close()
		return err
	}
	if err = tmp.Sync(); err != nil {
		tmp.Close()
		return err
	}
	if err = tmp.Close(); err != nil {
		return err
	}
	if err = installMissing(name, destination); err != nil {
		if os.IsExist(err) {
			return copyMissing(source, destination)
		}
		return err
	}
	return nil
}
func copyTree(source, target string) error {
	if !trustedPath(source) || !trustedPath(target) {
		return errors.New("迁移附件目录不能通过链接指向其他位置")
	}
	if err := os.MkdirAll(target, 0755); err != nil {
		return err
	}
	entries, err := os.ReadDir(source)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if transient(entry.Name()) {
			continue
		}
		src, dst := filepath.Join(source, entry.Name()), filepath.Join(target, entry.Name())
		info, err := os.Stat(src)
		if err != nil {
			return err
		}
		if info.IsDir() {
			err = copyTree(src, dst)
		} else {
			err = copyMissing(src, dst)
		}
		if err != nil {
			return err
		}
	}
	return nil
}
func metaStrings(meta map[string]any, key string) map[string]bool {
	out := map[string]bool{}
	if values, ok := meta[key].([]any); ok {
		for _, value := range values {
			if text, ok := value.(string); ok {
				out[text] = true
			}
		}
	}
	return out
}
func finishMigration(srcDir, targetDataDir string, migrated bool) error {
	meta := readMigrationMeta(targetDataDir)
	if meta == nil {
		meta = map[string]any{}
	}
	requiredKeys, requiredDirs := metaStrings(meta, "requiredKeys"), metaStrings(meta, "requiredDirs")
	copied := metaStrings(meta, "copiedSubdirs")
	warnings := []string{}
	entries, err := os.ReadDir(srcDir)
	if err != nil {
		warnings = append(warnings, fmt.Sprintf("迁移来源暂不可用：%v", err))
	}
	for _, entry := range entries {
		if !entry.IsDir() && isSecret(entry.Name()) && !transient(entry.Name()) {
			requiredKeys[entry.Name()] = true
		}
	}
	for _, sub := range []string{"config", "seed", "backups", "logs"} {
		src, dst := filepath.Join(srcDir, sub), filepath.Join(targetDataDir, sub)
		info, err := os.Stat(src)
		if err != nil || !info.IsDir() {
			if requiredDirs[sub] {
				warnings = append(warnings, "原始辅助目录暂不可用："+sub)
			}
			continue
		}
		if sub != "seed" {
			requiredDirs[sub] = true
		}
		if err = copyTree(src, dst); err != nil {
			if sub != "seed" {
				warnings = append(warnings, fmt.Sprintf("辅助目录 %s 未完整复制：%v", sub, err))
			}
		} else {
			copied[sub] = true
		}
	}
	for _, name := range sortedKeys(requiredKeys) {
		if filepath.Base(name) != name {
			return errors.New("迁移密钥名称无效")
		}
		source := filepath.Join(srcDir, name)
		if err := copyMissing(source, filepath.Join(targetDataDir, name)); err != nil {
			warnings = append(warnings, fmt.Sprintf("密钥文件 %s 未完整复制：%v", name, err))
		}
	}
	status := migrationMoved
	if len(warnings) > 0 {
		status = migrationPartial
	}
	meta["sourceDir"] = srcDir
	meta["targetDir"] = targetDataDir
	meta["database"] = dbName
	meta["copiedSubdirs"] = sortedKeys(copied)
	meta["requiredDirs"] = sortedKeys(requiredDirs)
	meta["requiredKeys"] = sortedKeys(requiredKeys)
	meta["warnings"] = warnings
	meta["status"] = status
	meta["completed"] = len(warnings) == 0
	if migrated {
		meta["migratedAt"] = float64(time.Now().UnixNano()) / 1e9
	}
	if err := atomicJSON(filepath.Join(targetDataDir, "migration.json"), meta); err != nil {
		return err
	}
	notice := "数据库与必需配置已安全迁移；原始数据保持完整。"
	if len(warnings) > 0 {
		notice = "数据库已生成安全副本；部分文件尚未就位，程序暂不启动。\n" + strings.Join(warnings, "\n")
	}
	if err := os.WriteFile(filepath.Join(targetDataDir, "MIGRATION_FROM_APPDATA.txt"), []byte(notice+"\n"), 0644); err != nil {
		return err
	}
	if len(warnings) > 0 {
		return errors.New(strings.Join(warnings, "；"))
	}
	return nil
}

func sortedKeys(set map[string]bool) []string {
	keys := make([]string, 0, len(set))
	for key := range set {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

// resolveDataDirConflict 对齐 migration.resolve_data_dir_conflict：独立库冲突保留双方。
func resolveDataDirConflict(targetDataDir, srcDir string) (bool, error) {
	destDB := filepath.Join(targetDataDir, dbName)
	meta := readMigrationMeta(targetDataDir)
	if srcDir == "" {
		for _, candidate := range machineCandidates() {
			if fileExists(filepath.Join(candidate, dbName)) {
				srcDir = candidate
				break
			}
		}
	}
	if srcDir == "" || !fileExists(filepath.Join(srcDir, dbName)) {
		pending := meta != nil && meta["status"] == migrationPartial
		completed := fileExists(destDB) && !pending
		if !completed {
			return false, errors.New("迁移未完成且来源不可用")
		}
		return true, nil
	}
	reused := false
	if meta != nil {
		status, _ := meta["status"].(string)
		sourceDir, _ := meta["sourceDir"].(string)
		targetDir, _ := meta["targetDir"].(string)
		database, _ := meta["database"].(string)
		if status == migrationMoved || status == migrationPartial {
			check, err := verifySnapshot(destDB)
			reused = sourceDir == resolvePath(srcDir) && targetDir == resolvePath(targetDataDir) &&
				database == dbName && fileExists(destDB) && err == nil && strings.ToLower(check) == "ok"
		}
	}
	if !reused {
		return false, errors.New("independent_databases")
	}
	if err := finishMigration(resolvePath(srcDir), resolvePath(targetDataDir), false); err != nil {
		return false, err
	}
	return true, nil
}

// ───────────────────────── 启动器主流程 ─────────────────────────

func activeDataDir(root string, compat *dataCompat) string {
	if compat != nil {
		return compat.DataDir
	}
	tx := readTransaction(root)
	if tx != nil {
		if dataDir, ok := tx["dataDir"].(string); ok && dataDir != "" {
			return dataDir
		}
	}
	return filepath.Join(root, "data")
}

func readTransaction(root string) map[string]any {
	raw, err := os.ReadFile(filepath.Join(root, "state", "transaction.json"))
	if err != nil {
		return nil
	}
	var tx map[string]any
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.UseNumber()
	if err := decoder.Decode(&tx); err != nil || !json.Valid(raw) {
		return nil
	}
	return tx
}

func findTargetVersion(root string, compat *dataCompat) (string, error) {
	if compat != nil {
		version, err := readSchemaVersion(compat.DataDir)
		if err != nil {
			return "", err
		}
		if version != compat.SchemaVersion {
			return "", errors.New("数据结构已变化，拒绝选择未经相容验收的程序")
		}
	}
	allowed := map[string]bool{}
	if compat != nil {
		for _, version := range compat.Versions {
			allowed[version] = true
		}
	}
	usable := func(version string) bool {
		return version != "" && isVersionValid(root, version) && (compat == nil || allowed[version])
	}

	state, _ := readCurrentState(root)
	if state != nil {
		if usable(state.Version) {
			return state.Version, nil
		}
		if state.LastKnownGood != nil && usable(*state.LastKnownGood) {
			if _, err := writeCurrentState(root, *state.LastKnownGood, nil); err != nil {
				return "", err
			}
			return *state.LastKnownGood, nil
		}
		if state.PreviousVersion != nil && usable(*state.PreviousVersion) {
			if _, err := writeCurrentState(root, *state.PreviousVersion, nil); err != nil {
				return "", err
			}
			return *state.PreviousVersion, nil
		}
	}
	installed := []string{}
	for _, version := range listInstalledVersions(root) {
		if usable(version) {
			installed = append(installed, version)
		}
	}
	if len(installed) == 0 {
		return "", nil
	}
	best, bestTime := "", int64(-1)
	for _, version := range installed {
		info, err := os.Stat(filepath.Join(root, "versions", version))
		if err != nil {
			continue
		}
		if info.ModTime().UnixNano() > bestTime {
			best, bestTime = version, info.ModTime().UnixNano()
		}
	}
	if best == "" {
		return "", nil
	}
	if _, err := writeCurrentState(root, best, nil); err != nil {
		return "", err
	}
	return best, nil
}

func launchVersion(root, targetVersion string, args []string) (int, error) {
	exe := filepath.Join(root, "versions", targetVersion, appName+".exe")
	if !fileExists(exe) || !trustedPath(exe) {
		return 1, fmt.Errorf("启动 %s 失败：版本主程序不存在：%s", targetVersion, exe)
	}
	compat, err := readDataCompatibility(root)
	if err != nil {
		return 1, err
	}
	dataDir := resolvePath(activeDataDir(root, compat))

	env := []string{}
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		skip := strings.EqualFold(key, envPrefix+"INSTALL_ROOT") || strings.EqualFold(key, envPrefix+"DATA_DIR") || strings.EqualFold(key, "WEBVIEW2_USER_DATA_FOLDER")
		for _, stale := range staleEnvKeys {
			skip = skip || strings.EqualFold(key, envPrefix+stale)
		}
		if !skip {
			env = append(env, entry)
		}
	}

	env = append(env,
		envPrefix+"INSTALL_ROOT="+root,
		envPrefix+"DATA_DIR="+dataDir,
		"WEBVIEW2_USER_DATA_FOLDER="+filepath.Join(dataDir, "cache", "webview"),
	)

	foreground := false
	for _, arg := range args {
		if arg == "--server" {
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
	sysAttr := &syscall.SysProcAttr{
		CreationFlags: 0x00000008 | 0x00000200, // DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
	}
	attr.Sys = sysAttr
	process, err := os.StartProcess(exe, fullArgs, attr)
	if err != nil {
		return 1, fmt.Errorf("启动 %s 失败：%w", targetVersion, err)
	}
	process.Release()
	return 0, nil
}

func cleanupOldExecutables(directory string) {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if !entry.IsDir() && strings.Contains(entry.Name(), ".old_") {
			os.Remove(filepath.Join(directory, entry.Name()))
		}
	}
}

func updateMutex() (func(), error) {
	runtime.LockOSThread()
	kernel := syscall.NewLazyDLL("kernel32.dll")
	name, _ := syscall.UTF16PtrFromString(appName + "-update-swap")
	handle, _, err := kernel.NewProc("CreateMutexW").Call(0, 0, uintptr(unsafe.Pointer(name)))
	if handle == 0 {
		runtime.UnlockOSThread()
		return nil, err
	}
	status, _, _ := kernel.NewProc("WaitForSingleObject").Call(handle, 0)
	if status != 0 && status != 0x80 {
		kernel.NewProc("CloseHandle").Call(handle)
		runtime.UnlockOSThread()
		return nil, errors.New("已有更新正在进行或无法核对更新权限")
	}
	return func() {
		kernel.NewProc("ReleaseMutex").Call(handle)
		kernel.NewProc("CloseHandle").Call(handle)
		runtime.UnlockOSThread()
	}, nil
}
func recoveryNeeded(tx map[string]any) bool {
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

func updateRecoveryComplete(root string, tx map[string]any, state *currentState) bool {
	attempt, _ := tx["attemptId"].(string)
	directory, _ := tx["dataDir"].(string)
	database, _ := tx["dataDbPath"].(string)
	if state == nil || attempt == "" || !isManagedVersionName(state.Version) ||
		!filepath.IsAbs(directory) || !inactiveUpdateFlag(tx["launchPending"]) {
		return false
	}
	if pending := state.Raw["pendingVersion"]; pending != nil && pending != "" {
		return false
	}
	// A remaining maintenance marker still belongs to UpdateHost, even after a terminal write.
	if _, err := os.Lstat(filepath.Join(directory, "backups", ".restore.lock")); !os.IsNotExist(err) {
		return false
	}
	compat := state.DataCompatibility
	allowed := map[string]bool{}
	if compat != nil {
		if _, err := validateDataCompat(compat); err != nil || !samePath(compat.DataDir, directory) {
			return false
		}
		for _, version := range compat.Versions {
			allowed[version] = true
		}
		if !allowed[state.Version] {
			return false
		}
	} else if !samePath(directory, filepath.Join(root, "data")) {
		return false
	}
	// Before prepare, abort_preparation leaves no work or terminal flags to repair.
	children, childrenList := tx["children"].([]any)
	target, _ := tx["targetVersion"].(string)
	if inactiveUpdateFlag(tx["dataPrepared"]) && inactiveUpdateFlag(tx["dataAccepted"]) &&
		tx["sourceVersion"] == state.Version && isManagedVersionName(target) && target != state.Version &&
		(tx["children"] == nil || childrenList && len(children) == 0) {
		return true
	}
	if !filepath.IsAbs(database) || !samePath(database, filepath.Join(directory, dbName)) {
		return false
	}
	if currentAttempt := state.Raw["attemptId"]; currentAttempt != nil && currentAttempt != attempt {
		return false
	}
	if tx["stage"] == stageRolledBack {
		return tx["dataRollbackReleased"] == true && inactiveUpdateFlag(tx["dataAccepted"]) &&
			tx["dataRecoveryPhase"] == "restored" && tx["sourceVersion"] == state.Version
	}
	if released, exists := tx["dataRollbackReleased"]; exists && released != false {
		return false
	}
	schema, ok := tx["dataSchemaVersion"].(json.Number)
	acceptedSchema, err := schema.Int64()
	return tx["stage"] == stageCommitted && tx["dataPrepared"] == true && tx["dataAccepted"] == true &&
		tx["dataRecoveryPhase"] == "accepted" &&
		tx["currentVersion"] == state.Version && allowed[target] && compat != nil && ok && err == nil &&
		acceptedSchema == int64(compat.SchemaVersion)
}

func recoverInterruptedUpdate(root string) error {
	release, err := updateMutex()
	if err != nil {
		return err
	}
	tx := readTransaction(root)
	if tx == nil {
		if _, err := os.Lstat(filepath.Join(root, "state", "transaction.json")); !os.IsNotExist(err) {
			release()
			return errors.New("更新事务无法读取，已停止启动并保留数据")
		}
	} else if attempt, ok := tx["attemptId"].(string); !ok || attempt == "" {
		release()
		return errors.New("更新事务缺少有效标识，已停止启动并保留数据")
	}
	if !recoveryNeeded(tx) {
		release()
		return nil
	}
	state, stateErr := readCurrentState(root)
	if stateErr != nil {
		release()
		return stateErr
	}
	complete := updateRecoveryComplete(root, tx, state)
	release()
	if complete {
		return nil
	}
	host := filepath.Join(root, "updater", "UpdateHost.exe")
	if !trustedPath(host) || !fileExists(host) {
		return errors.New("独立更新宿主缺失或被重定向，已停止启动以保护数据")
	}
	command := exec.Command(host, "--recover-only", "--target", root)
	command.Dir = root
	command.Env = append(os.Environ(), envPrefix+"INSTALL_ROOT="+root)
	command.Stdout = os.Stdout
	command.Stderr = os.Stderr
	if err := command.Run(); err != nil {
		return fmt.Errorf("上次更新恢复未完成，数据已保留：%w", err)
	}
	return nil
}
func reportProtocol(args []string) (bool, error) {
	for _, arg := range args {
		if arg == "--update-protocol" {
			payload := []byte(`{"updateDataProtocol":1}`)
			if path := os.Getenv(envPrefix + "UPDATE_PROTOCOL_RESULT"); path != "" {
				return true, os.WriteFile(path, payload, 0600)
			}
			_, err := fmt.Fprintln(os.Stdout, string(payload))
			return true, err
		}
	}
	return false, nil
}
func launcherRoot() (string, error) {
	executable, err := os.Executable()
	if err != nil {
		return "", err
	}
	executable, err = canonicalPath(executable)
	if err != nil {
		return "", err
	}
	return filepath.Dir(executable), nil
}
func runLauncher(argv []string, root string) (int, error) {
	if err := recoverInterruptedUpdate(root); err != nil {
		return 1, err
	}
	// 2. 基础目录结构与遗留清理。
	for _, dir := range []string{"state", "data", "logs", "versions", "updater", "update-staging"} {
		if err := os.MkdirAll(filepath.Join(root, dir), 0755); err != nil {
			return 1, err
		}
	}
	cleanupOldExecutables(filepath.Join(root, "updater"))
	cleanupOldExecutables(root)

	// 3. 历史数据迁移：冲突即拒启；迁移未完成不启动。
	compat, err := readDataCompatibility(root)
	if err != nil {
		return 1, err
	}
	knownData := compat != nil
	if !knownData {
		if tx := readTransaction(root); tx != nil {
			if dataDir, ok := tx["dataDir"].(string); ok && dataDir != "" {
				knownData = true
			}
		}
	}
	if !knownData {
		locations := detectLegacyLocations(root)
		if locations.HasConflict {
			if completed, err := resolveDataDirConflict(locations.TargetDataDir, locations.AppdataDir); err != nil || !completed {
				return 1, fmt.Errorf("旧数据迁移未完成，双方数据已保留：%v", err)
			}
		}
		if locations.HasAppdataDB && !locations.HasTargetDB {
			completed, status, err := adoptSourceDatabase(resolvePath(locations.AppdataDir), filepath.Join(root, "data"))
			if err != nil || !completed {
				reason := status
				if err != nil {
					reason = err.Error()
				}
				return 1, fmt.Errorf("旧数据迁移未完成（%s），已停止启动以免生成空数据库。\n旧库位置：%s",
					reason, locations.AppdataDir)
			}
		}
	}

	// 4. 定位有效版本（含相容校验与自愈回退）。
	targetVersion, err := findTargetVersion(root, compat)
	if err != nil {
		return 1, err
	}
	if targetVersion == "" {
		return 1, fmt.Errorf("未能在 %s 找到任何可运行的有效版本。\n请检查安装目录或重新解压完整发行包。",
			filepath.Join(root, "versions"))
	}

	// 5. 拉起目标版本。
	return launchVersion(root, targetVersion, argv)
}

func main() {
	args := os.Args[1:]
	if handled, err := reportProtocol(args); handled {
		if err != nil {
			os.Exit(fatal(1, err))
		}
		return
	}
	root, err := launcherRoot()
	if err != nil {
		os.Exit(fatal(1, err))
	}
	code, err := runLauncher(args, root)
	if err != nil {
		os.Exit(fatal(code, err))
	}
	os.Exit(code)
}
