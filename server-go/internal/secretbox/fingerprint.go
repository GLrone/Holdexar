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
)

// 机器指纹顺序：Windows MachineGuid → Linux machine-id → macOS IOPlatformUUID → 数据目录随机密钥文件。

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

// installSalt 每数据目录随机盐（≥16 字节整体使用，不截断）。
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
