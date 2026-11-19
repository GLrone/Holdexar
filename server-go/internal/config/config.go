package config

import (
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"

	"holdexar/hostkit"
)

const (
	AppSlug    = hostkit.AppSlug
	EnvPrefix  = hostkit.EnvPrefix
	DBFileName = hostkit.DBFileName

	PortableFlag = hostkit.PortableFlag
	DevPointer   = hostkit.DevPointerFile
)

// AppVersion 构建期经 -ldflags -X 注入（-X 对 const 无效）；缺省为开发态版本。
var AppVersion = "0.3.6-beta"

var executablePath = os.Executable

// ServerAddr 返回监听地址（默认 127.0.0.1:28765，支持 HOLDEXAR_HOST 覆盖，与 windowhost 同源）。
func ServerAddr() string {
	const defaultPort = "28765"
	host := strings.TrimSpace(os.Getenv(EnvPrefix + "HOST"))
	if host == "" {
		return "127.0.0.1:" + defaultPort
	}
	if _, _, err := net.SplitHostPort(host); err == nil {
		return host
	}
	return host + ":" + defaultPort
}

// AppName 品牌名；环境变量 HOLDEXAR_APP_NAME 显式设置时优先（空串同为显式值）。
func AppName() string {
	if value, ok := os.LookupEnv(EnvPrefix + "APP_NAME"); ok {
		return value
	}
	return hostkit.AppName
}

func Version() string {
	if value, ok := os.LookupEnv(EnvPrefix + "VERSION"); ok {
		return value
	}
	return AppVersion
}

// InstallRoot 解析安装根目录（HOLDEXAR_INSTALL_ROOT 优先，否则按 exe 布局上溯）。
func InstallRoot() (string, error) {
	if override := strings.TrimSpace(os.Getenv(EnvPrefix + "INSTALL_ROOT")); override != "" {
		return absPath(expandUser(override)), nil
	}
	exe, err := executablePath()
	if err != nil {
		return "", fmt.Errorf("程序位置无法确定：%w", err)
	}
	root, _, _ := hostkit.InstallRootFromExe(exe)
	return root, nil
}

// WebDistDir 优先返回 versions/<版本>/ 内就绪的 web/dist，否则回退安装根 web/dist。
func WebDistDir() (string, error) {
	exe, exeErr := executablePath()
	root, err := InstallRoot()
	if err != nil {
		return "", err
	}
	if exeErr == nil {
		if _, versionDir, ok := hostkit.InstallRootFromExe(exe); ok && versionDir != "" {
			versionDist := filepath.Join(versionDir, "web", "dist")
			if distReady(versionDist) {
				return versionDist, nil
			}
		}
	}
	return filepath.Join(root, "web", "dist"), nil
}

func distReady(dir string) bool {
	info, err := os.Stat(filepath.Join(dir, "index.html"))
	return err == nil && info.Mode().IsRegular()
}

func ReadDevPointer(root string) string {
	return hostkit.ReadDevPointer(root)
}

// ResolveDataDir 委托 hostkit 按统一规则解析首个可写数据目录（零系统用户目录回退）。
func ResolveDataDir() (string, error) {
	var exe string
	if strings.TrimSpace(os.Getenv(EnvPrefix+"DATA_DIR")) == "" &&
		strings.TrimSpace(os.Getenv(EnvPrefix+"INSTALL_ROOT")) == "" {
		var err error
		exe, err = executablePath()
		if err != nil {
			return "", fmt.Errorf("程序位置无法确定：%w", err)
		}
	} else if resolved, err := executablePath(); err == nil {
		exe = resolved
	}
	return hostkit.ResolveDataDir(hostkit.DataDirOptions{ExePath: exe})
}

func ensureWritable(path string) error {
	return hostkit.EnsureWritable(path)
}

func absPath(path string) string {
	return hostkit.CanonicalPath(path)
}

func expandUser(path string) string {
	if path == "~" || strings.HasPrefix(path, "~/") || strings.HasPrefix(path, `~\`) {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, path[1:])
		}
	}
	return path
}
