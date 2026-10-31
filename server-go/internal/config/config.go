// Package config：数据目录判定与品牌常量。
// 打包态由启动器注入 HOLDEXAR_INSTALL_ROOT 与 HOLDEXAR_DATA_DIR，二者恒走
// 环境变量分支；判定链唯一入口是 ResolveDataDir。
package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

const (
	// 品牌常量（库文件名、数据目录名、环境变量前缀同源）
	AppSlug    = "holdexar"
	AppVersion = "0.3.6-beta"
	EnvPrefix  = "HOLDEXAR_"
	DBFileName = AppSlug + ".db"

	// 便携模式显式标记（程序目录内放一个同名空文件即启用）
	PortableFlag = "portable.flag"
	// 开发态数据目录指针（程序目录内的文本文件，内容一行绝对路径即生效）
	DevPointer = AppSlug + "-dev-home.txt"
)

// AppName 品牌名；环境变量 HOLDEXAR_APP_NAME 显式设置时优先（空串同为显式值）。
func AppName() string {
	if value, ok := os.LookupEnv(EnvPrefix + "APP_NAME"); ok {
		return value
	}
	return "Holdexar"
}

// Version 版本号；环境变量 HOLDEXAR_VERSION 显式设置时优先。
func Version() string {
	if value, ok := os.LookupEnv(EnvPrefix + "VERSION"); ok {
		return value
	}
	return AppVersion
}

// InstallRoot 安装根目录：HOLDEXAR_INSTALL_ROOT 显式指定优先；否则从可执行
// 文件位置上溯（versions/<版本>/ 内或 updater/ 内），再否则取程序目录。
func InstallRoot() (string, error) {
	if override := strings.TrimSpace(os.Getenv(EnvPrefix + "INSTALL_ROOT")); override != "" {
		return absPath(expandUser(override)), nil
	}
	exe, err := os.Executable()
	if err != nil {
		return "", fmt.Errorf("程序位置无法确定：%w", err)
	}
	return installRootFromExe(filepath.Dir(exe)), nil
}

// installRootFromExe 从可执行文件所在目录上溯安装根。
func installRootFromExe(exeDir string) string {
	// 运行在 versions/<版本>/ 内：根在 versions 的上一级
	if filepath.Base(filepath.Dir(exeDir)) == "versions" {
		return filepath.Dir(filepath.Dir(exeDir))
	}
	// 运行在 updater/ 内：根在 updater 的上一级
	if filepath.Base(exeDir) == "updater" {
		return filepath.Dir(exeDir)
	}
	return exeDir
}

// WebDistDir 前端构建产物目录：安装根下 web/dist。
func WebDistDir() (string, error) {
	root, err := InstallRoot()
	if err != nil {
		return "", err
	}
	return filepath.Join(root, "web", "dist"), nil
}

// ReadDevPointer 指针文件里写明的开发态数据目录；缺失、空行、注释行、
// 非绝对路径都算未配置（空串）。
func ReadDevPointer(root string) string {
	raw, err := os.ReadFile(filepath.Join(root, DevPointer))
	if err != nil {
		return ""
	}
	text := strings.TrimPrefix(string(raw), "\uFEFF")
	for _, line := range strings.Split(text, "\n") {
		entry := strings.TrimSpace(line)
		if entry == "" || strings.HasPrefix(entry, "#") {
			continue
		}
		if filepath.IsAbs(entry) {
			return entry
		}
		return ""
	}
	return ""
}

// ResolveDataDir 按判定顺序解析数据目录，返回已就绪（存在且可写）的绝对路径。
// 顺序（先到先得）：①环境变量 HOLDEXAR_DATA_DIR 显式覆盖 ②程序目录
// portable.flag 便携声明 ③程序目录内已有 data/<库文件> 原地续用 ④开发态
// 指针文件 ⑤开发态机器级 <slug>-dev 候选（LOCALAPPDATA → APPDATA → 主目录
// 点前缀），全不可写时回落程序目录旁 data/。指针目标不可写时继续向后判定，
// 不因一处配置让程序起不来。打包态旧库仲裁与迁移由启动器完成后注入环境变量。
func ResolveDataDir() (string, error) {
	if explicit := strings.TrimSpace(os.Getenv(EnvPrefix + "DATA_DIR")); explicit != "" {
		path := absPath(expandUser(explicit))
		if err := ensureWritable(path); err != nil {
			return "", err
		}
		return path, nil
	}
	root, err := InstallRoot()
	if err != nil {
		return "", err
	}
	dataDir := filepath.Join(root, "data")
	if fileExists(filepath.Join(root, PortableFlag)) {
		if err := ensureWritable(dataDir); err != nil {
			return "", err
		}
		return dataDir, nil
	}
	if fileExists(filepath.Join(dataDir, DBFileName)) {
		if err := ensureWritable(dataDir); err != nil {
			return "", err
		}
		return dataDir, nil
	}
	if pointed := ReadDevPointer(root); pointed != "" {
		if err := ensureWritable(pointed); err == nil {
			return pointed, nil
		}
	}
	return firstWritable(devMachineCandidates(), dataDir)
}

// devMachineCandidates 开发态数据目录候选：`<root>/holdexar-dev`。
// 刻意与打包态机器级目录分离：开发态写入不得污染正式库。
func devMachineCandidates() []string {
	return machineCandidates(AppSlug + "-dev")
}

// machineCandidates 把 `<name>` 解析成各机器级根目录下的落点（按优先级）。
// LOCALAPPDATA 优先于 APPDATA：漫游目录会被域环境整份同步；主目录落点加
// `.` 前缀，避免在家目录可见区散落文件夹。
func machineCandidates(name string) []string {
	var out []string
	for _, key := range []string{"LOCALAPPDATA", "APPDATA"} {
		if base := os.Getenv(key); base != "" {
			out = append(out, filepath.Join(base, name))
		}
	}
	home := os.Getenv("USERPROFILE")
	if home == "" {
		if dir, err := os.UserHomeDir(); err == nil {
			home = dir
		}
	}
	if home != "" {
		out = append(out, filepath.Join(home, "."+name))
	}
	return out
}

// firstWritable 按顺序取第一个可写的候选目录；全不可写时退回 fallback，
// fallback 自身不可写仍是致命错误，原样上抛。
func firstWritable(candidates []string, fallback string) (string, error) {
	for _, candidate := range candidates {
		if err := ensureWritable(candidate); err == nil {
			return candidate, nil
		}
	}
	if err := ensureWritable(fallback); err != nil {
		return "", err
	}
	return fallback, nil
}

// ensureWritable 确保目录及其 logs/ 子目录存在且可写；不可写时由调用方换候选。
// 可写性判定不落任何待删探针：能建出 logs/ 内临时文件即证明可写，删除尽力而为。
func ensureWritable(path string) error {
	logs := filepath.Join(path, "logs")
	if err := os.MkdirAll(logs, 0o755); err != nil {
		return fmt.Errorf("数据目录不可写：%s", path)
	}
	probe, err := os.CreateTemp(logs, ".writable-*")
	if err != nil {
		return fmt.Errorf("数据目录不可写：%s", path)
	}
	name := probe.Name()
	probe.Close()
	os.Remove(name)
	return nil
}

// fileExists 普通文件判定（跟随符号链接）。
func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

// absPath 绝对化并尽力解析符号链接；解析失败退回绝对化结果。
func absPath(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return resolved
	}
	return abs
}

// expandUser `~` 开头的路径展开为用户主目录。
func expandUser(path string) string {
	if path == "~" || strings.HasPrefix(path, "~/") || strings.HasPrefix(path, `~\`) {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, path[1:])
		}
	}
	return path
}
