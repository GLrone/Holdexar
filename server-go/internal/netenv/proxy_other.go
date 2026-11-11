//go:build !windows

package netenv

func readWindowsProxy() (bool, string) { return false, "" }
