//go:build !windows

// 非 Windows 平台：MachineGuid 注册表键不存在，指纹走 machine-id /
// IOPlatformUUID / 数据目录兜底（fingerprint.go 的平台分支）。
package secretbox

func machineGuid() string { return "" }
