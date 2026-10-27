package main

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

const (
	AppName       = "Holdexar"
	AppSlug       = "holdexar"
	EnvPrefix     = "HOLDEXAR_"
	ProtocolVer   = 1
	MBIconError   = 0x10
)

type CurrentState struct {
	Version         string  `json:"version"`
	InstalledAt     float64 `json:"installedAt,omitempty"`
	LastKnownGood   string  `json:"lastKnownGood,omitempty"`
	PreviousVersion string  `json:"previousVersion,omitempty"`
}

type Compatibility struct {
	DataDir       string   `json:"dataDir"`
	SchemaVersion int      `json:"schemaVersion"`
	Versions      []string `json:"versions"`
}

func showFatal(message string) {
	fmt.Fprintf(os.Stderr, "[Fatal] %s\n", message)
	if os.Getenv("HOLDEXAR_NO_DIALOG") == "1" || os.Getenv("CI") == "1" {
		return
	}
	user32 := syscall.NewLazyDLL("user32.dll")
	msgBox := user32.NewProc("MessageBoxW")
	titlePtr, _ := syscall.UTF16PtrFromString(AppName + " 启动失败")
	msgPtr, _ := syscall.UTF16PtrFromString(message)
	msgBox.Call(0, uintptr(unsafe.Pointer(msgPtr)), uintptr(unsafe.Pointer(titlePtr)), MBIconError)
}

func getInstallRoot() (string, error) {
	if override := strings.TrimSpace(os.Getenv(EnvPrefix + "INSTALL_ROOT")); override != "" {
		return filepath.Abs(override)
	}
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	exePath, err := filepath.EvalSymlinks(exe)
	if err != nil {
		exePath = exe
	}
	return filepath.Dir(exePath), nil
}

func reportProtocol(args []string) bool {
	for _, arg := range args {
		if arg == "--update-protocol" {
			payload, _ := json.Marshal(map[string]int{"updateDataProtocol": ProtocolVer})
			if resultFile := os.Getenv(EnvPrefix + "UPDATE_PROTOCOL_RESULT"); resultFile != "" {
				_ = os.WriteFile(resultFile, payload, 0644)
			} else {
				fmt.Println(string(payload))
			}
			return true
		}
	}
	return false
}

func cleanupOldExecutables(dir string) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	for _, e := range entries {
		name := e.Name()
		if strings.HasSuffix(name, ".old") || strings.HasSuffix(name, "_old.exe") {
			_ = os.Remove(filepath.Join(dir, name))
		}
	}
}

func ensureDirectoryStructure(root string) {
	dirs := []string{"state", "data", "logs", "versions", "updater", "update-staging"}
	for _, d := range dirs {
		_ = os.MkdirAll(filepath.Join(root, d), 0755)
	}
	cleanupOldExecutables(filepath.Join(root, "updater"))
	cleanupOldExecutables(root)
}

func readSchemaVersion(dbPath string) (int, error) {
	f, err := os.Open(dbPath)
	if err != nil {
		return 0, err
	}
	defer f.Close()

	header := make([]byte, 100)
	n, err := f.Read(header)
	if err != nil || n < 68 {
		return 0, fmt.Errorf("数据库文件过小或损坏")
	}
	userVersion := int(binary.BigEndian.Uint32(header[60:64]))
	return userVersion, nil
}

func checkDataCompatibility(root string) (*Compatibility, error) {
	compatFile := filepath.Join(root, "state", "compatibility.json")
	data, err := os.ReadFile(compatFile)
	if err != nil {
		return nil, nil
	}
	var c Compatibility
	if err := json.Unmarshal(data, &c); err != nil {
		return nil, nil
	}

	dbPath := filepath.Join(c.DataDir, AppSlug+".db")
	ver, err := readSchemaVersion(dbPath)
	if err != nil {
		return nil, fmt.Errorf("无法读取数据结构版本：%v", err)
	}
	if ver != c.SchemaVersion {
		return nil, fmt.Errorf("数据结构已变化（库版本 %d != 兼容版本 %d），拒绝运行未经测试的版本", ver, c.SchemaVersion)
	}
	return &c, nil
}

func isVersionValid(root, version string) bool {
	if version == "" {
		return false
	}
	exe := filepath.Join(root, "versions", version, AppName+".exe")
	fi, err := os.Stat(exe)
	return err == nil && !fi.IsDir()
}

func readCurrentState(root string) *CurrentState {
	target := filepath.Join(root, "state", "current.json")
	data, err := os.ReadFile(target)
	if err != nil {
		return nil
	}
	var s CurrentState
	if err := json.Unmarshal(data, &s); err != nil {
		return nil
	}
	return &s
}

func writeCurrentState(root, version, lastKnownGood string) error {
	s := CurrentState{
		Version:       version,
		InstalledAt:   float64(time.Now().UnixNano()) / 1e9,
		LastKnownGood: lastKnownGood,
	}
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	target := filepath.Join(root, "state", "current.json")
	tmp := filepath.Join(root, "state", "current.json.tmp")
	if err := os.WriteFile(tmp, data, 0644); err != nil {
		return err
	}
	return os.Rename(tmp, target)
}

func listInstalledVersions(root string) []string {
	vdir := filepath.Join(root, "versions")
	entries, err := os.ReadDir(vdir)
	if err != nil {
		return nil
	}
	var res []string
	for _, e := range entries {
		if e.IsDir() && isVersionValid(root, e.Name()) {
			res = append(res, e.Name())
		}
	}
	return res
}

func findTargetVersion(root string) (string, error) {
	compat, err := checkDataCompatibility(root)
	if err != nil {
		return "", err
	}

	isAllowed := func(ver string) bool {
		if !isVersionValid(root, ver) {
			return false
		}
		if compat == nil {
			return true
		}
		for _, a := range compat.Versions {
			if a == ver {
				return true
			}
		}
		return false
	}

	curr := readCurrentState(root)
	if curr != nil {
		if isAllowed(curr.Version) {
			return curr.Version, nil
		}
		if isAllowed(curr.LastKnownGood) {
			_ = writeCurrentState(root, curr.LastKnownGood, "")
			return curr.LastKnownGood, nil
		}
		if isAllowed(curr.PreviousVersion) {
			_ = writeCurrentState(root, curr.PreviousVersion, "")
			return curr.PreviousVersion, nil
		}
	}

	installed := listInstalledVersions(root)
	var valid []string
	for _, v := range installed {
		if isAllowed(v) {
			valid = append(valid, v)
		}
	}
	if len(valid) == 0 {
		return "", nil
	}

	var best string
	var bestTime time.Time
	for _, v := range valid {
		p := filepath.Join(root, "versions", v)
		fi, err := os.Stat(p)
		if err == nil {
			if best == "" || fi.ModTime().After(bestTime) {
				best = v
				bestTime = fi.ModTime()
			}
		}
	}

	if best != "" {
		_ = writeCurrentState(root, best, "")
	}
	return best, nil
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()

	out, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer out.Close()

	if _, err = io.Copy(out, in); err != nil {
		return err
	}
	return out.Sync()
}

func detectAndMigrateLegacyData(root string) error {
	targetData := filepath.Join(root, "data")
	targetDB := filepath.Join(targetData, AppSlug+".db")
	if fi, err := os.Stat(targetDB); err == nil && !fi.IsDir() {
		return nil
	}

	localAppData := os.Getenv("LOCALAPPDATA")
	if localAppData == "" {
		return nil
	}
	appdataDir := filepath.Join(localAppData, AppSlug)
	appdataDB := filepath.Join(appdataDir, AppSlug+".db")
	if fi, err := os.Stat(appdataDB); err != nil || fi.IsDir() {
		return nil
	}

	_ = os.MkdirAll(targetData, 0755)
	tmpDst := filepath.Join(targetData, AppSlug+".db.tmp")
	if err := copyFile(appdataDB, tmpDst); err != nil {
		return fmt.Errorf("历史数据迁移复制失败：%v", err)
	}
	if err := os.Rename(tmpDst, targetDB); err != nil {
		return fmt.Errorf("历史数据迁移提交失败：%v", err)
	}

	auxDirs := []string{"config", "seed", "backups", "logs"}
	for _, aux := range auxDirs {
		srcAux := filepath.Join(appdataDir, aux)
		dstAux := filepath.Join(targetData, aux)
		if fi, err := os.Stat(srcAux); err == nil && fi.IsDir() {
			_ = filepath.Walk(srcAux, func(path string, info os.FileInfo, err error) error {
				if err != nil || info.IsDir() {
					return nil
				}
				rel, _ := filepath.Rel(srcAux, path)
				targetFile := filepath.Join(dstAux, rel)
				_ = os.MkdirAll(filepath.Dir(targetFile), 0755)
				_ = copyFile(path, targetFile)
				return nil
			})
		}
	}
	return nil
}

func getActiveDataDir(root string) string {
	compat, _ := checkDataCompatibility(root)
	if compat != nil && compat.DataDir != "" {
		return compat.DataDir
	}
	txFile := filepath.Join(root, "state", "transaction.json")
	if data, err := os.ReadFile(txFile); err == nil {
		var tx map[string]interface{}
		if json.Unmarshal(data, &tx) == nil {
			if dd, ok := tx["dataDir"].(string); ok && dd != "" {
				return dd
			}
		}
	}
	return filepath.Join(root, "data")
}

func launchVersion(root, targetVer string, argv []string, foreground bool) (int, error) {
	exe := filepath.Join(root, "versions", targetVer, AppName+".exe")
	if _, err := os.Stat(exe); err != nil {
		return 1, fmt.Errorf("目标主程序不存在：%s", exe)
	}

	dataDir := getActiveDataDir(root)
	envMap := make(map[string]string)
	for _, env := range os.Environ() {
		parts := strings.SplitN(env, "=", 2)
		if len(parts) == 2 {
			k := parts[0]
			if strings.HasPrefix(k, EnvPrefix) {
				sub := strings.TrimPrefix(k, EnvPrefix)
				switch sub {
				case "UPDATE_ATTEMPT_ID", "UPDATE_DATA_TOKEN", "LAUNCHER_PID", "DEV_URL", "UPDATE_RESTART", "PREV_PID":
					continue
				}
			}
			envMap[k] = parts[1]
		}
	}
	envMap[EnvPrefix+"INSTALL_ROOT"] = root
	envMap[EnvPrefix+"DATA_DIR"] = dataDir
	envMap["WEBVIEW2_USER_DATA_FOLDER"] = filepath.Join(dataDir, "cache", "webview")

	var envSlice []string
	for k, v := range envMap {
		envSlice = append(envSlice, fmt.Sprintf("%s=%s", k, v))
	}

	cmd := exec.Command(exe, argv...)
	cmd.Dir = filepath.Dir(exe)
	cmd.Env = envSlice

	if foreground {
		cmd.Stdin = os.Stdin
		cmd.Stdout = os.Stdout
		cmd.Stderr = os.Stderr
		if err := cmd.Run(); err != nil {
			if exitErr, ok := err.(*exec.ExitError); ok {
				return exitErr.ExitCode(), nil
			}
			return 1, err
		}
		return 0, nil
	}

	cmd.SysProcAttr = &syscall.SysProcAttr{
		CreationFlags: syscall.CREATE_NEW_PROCESS_GROUP | 0x00000008, // DETACHED_PROCESS
	}
	if err := cmd.Start(); err != nil {
		return 1, err
	}
	return 0, nil
}

func main() {
	args := os.Args[1:]
	if reportProtocol(args) {
		return
	}

	root, err := getInstallRoot()
	if err != nil {
		showFatal(fmt.Sprintf("无法获取安装目录：%v", err))
		os.Exit(1)
	}

	ensureDirectoryStructure(root)

	if err := detectAndMigrateLegacyData(root); err != nil {
		showFatal(fmt.Sprintf("历史数据迁移失败，已停止启动以保护数据：%v", err))
		os.Exit(1)
	}

	targetVer, err := findTargetVersion(root)
	if err != nil {
		showFatal(fmt.Sprintf("当前数据不兼容，请使用新解压的完整版本。\n%v", err))
		os.Exit(1)
	}
	if targetVer == "" {
		showFatal(fmt.Sprintf("未在 %s 找到任何可运行的有效版本。\n请检查安装目录或重新解压发行包。", filepath.Join(root, "versions")))
		os.Exit(1)
	}

	foreground := false
	for _, arg := range args {
		if arg == "--server" {
			foreground = true
			break
		}
	}

	code, err := launchVersion(root, targetVer, args, foreground)
	if err != nil {
		showFatal(fmt.Sprintf("启动版本 %s 失败：%v", targetVer, err))
		os.Exit(1)
	}
	os.Exit(code)
}
