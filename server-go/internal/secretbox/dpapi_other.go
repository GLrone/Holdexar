//go:build !windows

package secretbox

import "fmt"

func DpapiAvailable() bool { return false }

func dpapiProtect([]byte) ([]byte, error) {
	return nil, fmt.Errorf("系统凭据保护在当前平台不可用")
}

func dpapiUnprotect([]byte) ([]byte, error) {
	return nil, fmt.Errorf("系统凭据保护在当前平台不可用")
}
