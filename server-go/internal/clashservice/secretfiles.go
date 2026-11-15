// 节点认证明文的落盘约束：权限收紧 + 生命周期收敛（对齐 Python
// node_secret_files）。内核运行时必须读到节点认证明文，因此这些文件不能
// 加密——目标是静态存储加密之外的补充：一旦明文落地就限制在最小可读面，
// 不再需要时尽快消失。位置固定在数据目录 clash/ 与 proxypool/ 子树内。
package clashservice

import (
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

// secretBearingSubdirs 承载节点认证明文的产物子目录（相对数据目录）。
var secretBearingSubdirs = []string{"clash", "proxypool"}

// transientSuffixes 可重建明文件的后缀：运行配置与临时文件总能从原文缓存
// 重渲染；原文 .yaml 是来源证据与事实源，只收紧权限、永不清理。
var transientSuffixes = []string{".runtime.yaml", ".tmp"}

// TightenAll 收紧全部节点凭证明文的权限：POSIX 文件 0600、目录 0700；
// Windows 走用户目录 ACL（data 目录默认即用户级），直接 no-op。
// 返回处理文件数；单条目失败跳过不中断。
func TightenAll(dataDir string) int {
	if runtime.GOOS == "windows" {
		return 0
	}
	count := 0
	for _, name := range secretBearingSubdirs {
		base := filepath.Join(dataDir, name)
		info, err := os.Stat(base)
		if err != nil || !info.IsDir() {
			continue
		}
		_ = os.Chmod(base, 0o700)
		_ = filepath.WalkDir(base, func(path string, entry fs.DirEntry, err error) error {
			if err != nil {
				return nil
			}
			if entry.IsDir() {
				if path != base {
					_ = os.Chmod(path, 0o700)
				}
				return nil
			}
			_ = os.Chmod(path, 0o600)
			count++
			return nil
		})
	}
	return count
}

// SweepTransient 删除可重建的明文件（*.runtime.yaml 与 *.tmp），返回删除数。
// 调用时机：内核停止后与下次启动（启动时内核尚未起，运行配置可安全清除）。
func SweepTransient(dataDir string) int {
	removed := 0
	for _, name := range secretBearingSubdirs {
		base := filepath.Join(dataDir, name)
		info, err := os.Stat(base)
		if err != nil || !info.IsDir() {
			continue
		}
		_ = filepath.WalkDir(base, func(path string, entry fs.DirEntry, err error) error {
			if err != nil || entry.IsDir() {
				return nil
			}
			for _, suffix := range transientSuffixes {
				if strings.HasSuffix(entry.Name(), suffix) {
					if os.Remove(path) == nil {
						removed++
					}
					break
				}
			}
			return nil
		})
	}
	return removed
}
