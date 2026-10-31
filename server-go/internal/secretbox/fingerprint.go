package secretbox

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"golang.org/x/sys/windows/registry"
)

// 机器指纹（同机稳定）：Windows MachineGuid → Linux machine-id → macOS
// IOPlatformUUID；全部不可用回数据目录随机密钥文件（首次生成后复用）。

func machineFingerprintOrFallback(dataDir string) ([]byte, error) {
	if fp := machineFingerprint(); len(fp) > 0 {
		return fp, nil
	}
	return fallbackFingerprint(dataDir)
}

func machineFingerprint() []byte {
	if runtime.GOOS == "windows" {
		if v := machineGuid(); v != "" {
			return []byte(v)
		}
	}
	for _, p := range []string{"/etc/machine-id", "/var/lib/dbus/machine-id"} {
		data, err := os.ReadFile(p)
		if err != nil {
			continue
		}
		text := strings.TrimSpace(string(data))
		if text == "" {
			continue
		}
		if !isASCII(text) {
			// 指纹含非 ASCII 视为不可用（Python 侧该情形直接抛错；Windows 目标面不可达）
			continue
		}
		return []byte(text)
	}
	if runtime.GOOS == "darwin" {
		if v := ioPlatformUUID(); v != "" {
			return []byte(v)
		}
	}
	return nil
}

func machineGuid() string {
	k, err := registry.OpenKey(registry.LOCAL_MACHINE, `SOFTWARE\Microsoft\Cryptography`, registry.QUERY_VALUE)
	if err != nil {
		return ""
	}
	defer k.Close()
	v, _, err := k.GetStringValue("MachineGuid")
	if err != nil {
		return ""
	}
	return v
}

func ioPlatformUUID() string {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "ioreg", "-rd1", "-c", "IOPlatformExpertDevice").Output()
	if err != nil {
		return ""
	}
	for _, line := range strings.Split(string(out), "\n") {
		if strings.Contains(line, "IOPlatformUUID") {
			idx := strings.LastIndex(line, "= ")
			if idx < 0 {
				return ""
			}
			return strings.Trim(strings.TrimSpace(line[idx+2:]), `"`)
		}
	}
	return ""
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] > 0x7f {
			return false
		}
	}
	return true
}

// installSalt 安装盐（每数据目录一份随机数，防同指纹克隆机共钥）。
// 文件内容整体使用（≥16 字节即有效，不做截断）。
func installSalt(dataDir string) ([]byte, error) {
	path := filepath.Join(dataDir, saltFile)
	if data, err := os.ReadFile(path); err == nil && len(data) >= saltBytes {
		return data, nil
	}
	data := randBytes(saltBytes)
	if err := writePrivate(dataDir, path, data); err != nil {
		return nil, err
	}
	return data, nil
}

// fallbackFingerprint 无机器指纹平台的兜底：数据目录内随机密钥文件。
func fallbackFingerprint(dataDir string) ([]byte, error) {
	path := filepath.Join(dataDir, fallbackKeyFile)
	if data, err := os.ReadFile(path); err == nil && len(data) >= keyBytes {
		return data, nil
	}
	data := randBytes(keyBytes)
	if err := writePrivate(dataDir, path, data); err != nil {
		return nil, err
	}
	return data, nil
}

// writePrivate 密钥材料落盘（POSIX 下收紧为 0600；目录不可写属致命配置，报错上浮）。
func writePrivate(dataDir, path string, data []byte) error {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return secretBoxErr(fmt.Sprintf("密钥材料写入失败（数据目录不可写？）：%v", err))
	}
	if err := os.WriteFile(path, data, 0o600); err != nil {
		return secretBoxErr(fmt.Sprintf("密钥材料写入失败（数据目录不可写？）：%v", err))
	}
	if runtime.GOOS != "windows" {
		_ = os.Chmod(path, 0o600)
	}
	return nil
}
