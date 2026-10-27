package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"unsafe"
)

func showFatal(message string) {
	fmt.Fprintln(os.Stderr, message)
	if os.Getenv("HOLDEXAR_NO_DIALOG") == "1" || os.Getenv("CI") == "1" {
		return
	}
	title, _ := syscall.UTF16PtrFromString("Holdexar 启动失败")
	text, _ := syscall.UTF16PtrFromString(message)
	syscall.NewLazyDLL("user32.dll").NewProc("MessageBoxW").Call(
		0, uintptr(unsafe.Pointer(text)), uintptr(unsafe.Pointer(title)), 0x10,
	)
}

func helperPath(root string) (string, error) {
	expected := filepath.Join(root, ".launcher", "SafetyHost.exe")
	resolved, err := filepath.EvalSymlinks(expected)
	if err != nil {
		return "", fmt.Errorf("启动保护组件缺失，请重新解压完整安装包：%w", err)
	}
	if !strings.EqualFold(filepath.Clean(resolved), filepath.Clean(expected)) {
		return "", fmt.Errorf("启动保护组件路径被重定向，已停止启动：%s", expected)
	}
	info, err := os.Stat(resolved)
	if err != nil {
		return "", err
	}
	if !info.Mode().IsRegular() {
		return "", fmt.Errorf("启动保护组件不是文件：%s", expected)
	}
	return resolved, nil
}

func launch(root string, args []string) (int, error) {
	helper, err := helperPath(root)
	if err != nil {
		return 1, err
	}
	cmd := exec.Command(helper, args...)
	cmd.Env = append(os.Environ(), "HOLDEXAR_INSTALL_ROOT="+root)
	cmd.Stdin, cmd.Stdout, cmd.Stderr = os.Stdin, os.Stdout, os.Stderr
	if err = cmd.Run(); err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			return exit.ExitCode(), nil
		}
		return 1, err
	}
	return 0, nil
}

func main() {
	executable, err := os.Executable()
	if err == nil {
		executable, err = filepath.EvalSymlinks(executable)
	}
	code := 1
	if err == nil {
		code, err = launch(filepath.Dir(executable), os.Args[1:])
	}
	if err != nil {
		showFatal(err.Error())
	}
	os.Exit(code)
}
