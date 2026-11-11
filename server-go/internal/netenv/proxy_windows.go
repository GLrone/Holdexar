//go:build windows

package netenv

import "golang.org/x/sys/windows/registry"

func readWindowsProxy() (bool, string) {
	key, err := registry.OpenKey(registry.CURRENT_USER, `Software\Microsoft\Windows\CurrentVersion\Internet Settings`, registry.QUERY_VALUE)
	if err != nil {
		return false, ""
	}
	defer key.Close()
	enabled, _, err := key.GetIntegerValue("ProxyEnable")
	if err != nil || enabled == 0 {
		return false, ""
	}
	server, _, err := key.GetStringValue("ProxyServer")
	if err != nil {
		return false, ""
	}
	return true, server
}
