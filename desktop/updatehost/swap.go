//go:build windows

// swap.go 换装文件机械：暂存载荷安装、运行期 EXE 安全覆盖、旧临时件清理。
package main

import (
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

func absPath(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		return path
	}
	return abs
}

func trustedPath(path string) bool {
	absolute, err := filepath.Abs(path)
	if err != nil {
		return false
	}
	resolved, err := filepath.EvalSymlinks(absolute)
	if err != nil {
		// 不存在的路径逐级回溯解析
		parent := filepath.Dir(absolute)
		resolvedParent, perr := filepath.EvalSymlinks(parent)
		if perr != nil {
			return false
		}
		resolved = filepath.Join(resolvedParent, filepath.Base(absolute))
	}
	return strings.EqualFold(filepath.Clean(resolved), filepath.Clean(absolute))
}

func copyTree(source, target string) error {
	entries, err := os.ReadDir(source)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		src := filepath.Join(source, entry.Name())
		dst := filepath.Join(target, entry.Name())
		if entry.IsDir() {
			if err := os.MkdirAll(dst, 0o755); err != nil {
				return err
			}
			if err := copyTree(src, dst); err != nil {
				return err
			}
			continue
		}
		if !entry.Type().IsRegular() {
			continue
		}
		if err := copyFile(src, dst); err != nil {
			return err
		}
	}
	return nil
}

func copyFile(source, target string) error {
	in, err := os.Open(source)
	if err != nil {
		return err
	}
	defer in.Close()
	out, err := os.Create(target)
	if err != nil {
		return err
	}
	if _, err = io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	if err = out.Sync(); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// safeReplaceExecutable 运行期 EXE 安全覆盖：先落临时件，占用时旧件改名让位。
func safeReplaceExecutable(src, dest string) error {
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	suffix := randomHex(4)
	temporary := dest + fmt.Sprintf(".%s.tmp", suffix)
	defer os.Remove(temporary)
	if err := copyFile(src, temporary); err != nil {
		return err
	}
	if file, err := os.OpenFile(temporary, os.O_RDWR, 0); err == nil {
		file.Sync()
		file.Close()
	}
	if err := os.Rename(temporary, dest); err == nil {
		return nil
	}
	if !fileExists(dest) {
		return errors.New("目标可执行文件覆盖失败")
	}
	oldDest := strings.TrimSuffix(dest, filepath.Ext(dest)) + fmt.Sprintf(".old_%s%s", suffix, filepath.Ext(dest))
	if err := os.Rename(dest, oldDest); err != nil {
		return err
	}
	if err := os.Rename(temporary, dest); err != nil {
		os.Rename(oldDest, dest)
		return err
	}
	return nil
}

func cleanupOldExecutables(directory string) {
	entries, err := os.ReadDir(directory)
	if err != nil {
		return
	}
	for _, entry := range entries {
		if !entry.IsDir() && strings.Contains(entry.Name(), ".old_") {
			os.Remove(filepath.Join(directory, entry.Name()))
		}
	}
}

func randomHex(bytes int) string {
	buf := make([]byte, bytes)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

// installVersionPayload 暂存载荷 → versions/<target>/；多版本布局时同步
// 刷新根启动器与 updater/UpdateHost.exe（唯一使用运行期安全覆盖的位置）。
func installVersionPayload(root, stagingDir, targetVersion string) error {
	if !isManagedVersionName(targetVersion) {
		return fmt.Errorf("目标版本号不可用作目录名：%q", targetVersion)
	}
	state, _ := readCurrentState(root)
	active := stateString(state, "version")
	if active != "" && strings.EqualFold(getVersionDir(root, active), getVersionDir(root, targetVersion)) {
		return errors.New("更新载荷不得覆盖正在使用的版本目录")
	}
	payload := filepath.Join(stagingDir, appName)
	if !fileExists(payload) && !directoryExists(payload) {
		payload = stagingDir
	}
	if !directoryExists(payload) {
		return errors.New("更新载荷目录不存在")
	}
	dest := getVersionDir(root, targetVersion)
	os.RemoveAll(dest)
	if err := os.MkdirAll(dest, 0o755); err != nil {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(dest), 0o755); err != nil {
		return err
	}
	versionsSub := filepath.Join(payload, versionsDir)
	if directoryExists(versionsSub) {
		srcVer := filepath.Join(versionsSub, targetVersion)
		if !directoryExists(srcVer) {
			return fmt.Errorf("更新载荷的 versions/ 下未找到版本目录：%s", targetVersion)
		}
		if err := copyTree(srcVer, dest); err != nil {
			return err
		}
	} else {
		if err := copyTree(payload, dest); err != nil {
			return err
		}
	}
	if !isVersionValid(root, targetVersion) {
		return fmt.Errorf("安装到 %s 失败：缺少主可执行程序", dest)
	}
	if directoryExists(versionsSub) {
		srcUpdater := filepath.Join(payload, "updater", "UpdateHost.exe")
		if fileExists(srcUpdater) {
			os.MkdirAll(filepath.Join(root, "updater"), 0o755)
			if err := safeReplaceExecutable(srcUpdater, filepath.Join(root, "updater", "UpdateHost.exe")); err != nil {
				return err
			}
		}
		srcLauncher := filepath.Join(payload, appName+".exe")
		if fileExists(srcLauncher) {
			if err := safeReplaceExecutable(srcLauncher, filepath.Join(root, appName+".exe")); err != nil {
				return err
			}
		}
	}
	return nil
}

func directoryExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.IsDir()
}
