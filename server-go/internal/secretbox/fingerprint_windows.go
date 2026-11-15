//go:build windows

// Windows 机器指纹：MachineGuid（Cryptography 注册表键，装系统时生成）。
package secretbox

import "golang.org/x/sys/windows/registry"

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
