//go:build !windows

// 非 Windows 平台：系统凭据保护不可用，mode=dpapi 的切换在入口即失败
// （fail-closed）；口令封装模式（keyring.go passphrase）是跨平台保护层。
package secretbox

import "fmt"

func DpapiAvailable() bool { return false }

func dpapiProtect([]byte) ([]byte, error) {
	return nil, fmt.Errorf("系统凭据保护在当前平台不可用")
}

func dpapiUnprotect([]byte) ([]byte, error) {
	return nil, fmt.Errorf("系统凭据保护在当前平台不可用")
}
