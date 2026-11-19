package hostkit

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

const (
	PortableFlag   = "portable.flag"
	DevPointerFile = "holdexar-dev-home.txt"
	DevDataDirName = "holdexar-dev"
)

func equalCleanPath(a, b string) bool {
	ca := filepath.Clean(a)
	cb := filepath.Clean(b)
	if runtime.GOOS == "windows" {
		return strings.EqualFold(ca, cb)
	}
	return ca == cb
}

// CanonicalPath resolves symlinks/junctions on existing ancestors and returns a clean absolute path.
func CanonicalPath(path string) string {
	abs, err := filepath.Abs(path)
	if err != nil {
		abs = path
	}
	if resolved, err := FinalPath(abs); err == nil {
		return filepath.Clean(resolved)
	}
	cur := filepath.Clean(abs)
	var tail []string
	for {
		parent, name := filepath.Split(cur)
		parent = strings.TrimRight(parent, `\/`)
		if vol := filepath.VolumeName(parent); vol != "" && parent == vol {
			parent = vol + string(filepath.Separator)
		} else if parent == "" && strings.HasPrefix(cur, "/") {
			parent = "/"
		}
		if parent == "" || parent == cur {
			return filepath.Clean(abs)
		}
		if name != "" {
			tail = append([]string{name}, tail...)
		}
		if resolved, err := FinalPath(parent); err == nil {
			parts := append([]string{resolved}, tail...)
			return filepath.Clean(filepath.Join(parts...))
		}
		cur = parent
	}
}

func canonicalPathStrict(abs string) (string, error) {
	resolved, err := FinalPath(abs)
	if err == nil {
		return filepath.Clean(resolved), nil
	}
	if !errors.Is(err, os.ErrNotExist) && !os.IsNotExist(err) {
		return "", err
	}
	parent := filepath.Dir(abs)
	if vol := filepath.VolumeName(parent); vol != "" && parent == vol {
		parent = vol + string(filepath.Separator)
	}
	if parent == abs {
		return "", err
	}
	resolvedParent, err := canonicalPathStrict(parent)
	if err != nil {
		return "", err
	}
	return filepath.Join(resolvedParent, filepath.Base(abs)), nil
}

func checkNoReparseWalk(abs string) error {
	clean := filepath.Clean(abs)
	vol := filepath.VolumeName(clean)
	sep := string(filepath.Separator)
	var cur, rest string
	if vol != "" {
		cur = vol + sep
		rest = strings.TrimPrefix(clean[len(vol):], sep)
	} else if strings.HasPrefix(clean, sep) {
		cur = sep
		rest = strings.TrimPrefix(clean, sep)
	} else {
		return errors.New("需要绝对路径")
	}
	if rest == "" {
		return nil
	}
	parts := strings.Split(rest, sep)
	for i, seg := range parts {
		if seg == "" {
			continue
		}
		cur = filepath.Join(cur, seg)
		info, err := os.Lstat(cur)
		if err != nil {
			if errors.Is(err, os.ErrNotExist) || os.IsNotExist(err) {
				break
			}
			return err
		}
		if IsReparseOrSymlink(cur, info) {
			return errors.New("路径包含符号链接或重解析点")
		}
		if i < len(parts)-1 && !info.IsDir() {
			return errors.New("路径中间节点不是目录")
		}
	}
	return nil
}

// TrustedPath verifies that path contains zero symlinks or reparse points along its chain and resolves to itself.
func TrustedPath(path string) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", errors.New("路径为空")
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	abs = filepath.Clean(abs)
	if err := checkNoReparseWalk(abs); err != nil {
		return "", err
	}
	real, err := canonicalPathStrict(abs)
	if err != nil {
		return "", err
	}
	if !equalCleanPath(real, abs) {
		return "", errors.New("路径解析结果与原路径不一致")
	}
	return real, nil
}

// SamePath reports whether a and b refer to the same canonical path.
func SamePath(a, b string) bool {
	if strings.TrimSpace(a) == "" || strings.TrimSpace(b) == "" {
		return false
	}
	return equalCleanPath(CanonicalPath(a), CanonicalPath(b))
}

// IsUnderPath reports whether child resolves inside or equal to parent after symlink/junction resolution.
func IsUnderPath(child, parent string) bool {
	if strings.TrimSpace(child) == "" || strings.TrimSpace(parent) == "" {
		return false
	}
	c := CanonicalPath(child)
	p := CanonicalPath(parent)
	if equalCleanPath(c, p) {
		return true
	}
	rel, err := filepath.Rel(p, c)
	if err != nil {
		return false
	}
	if rel == "." {
		return true
	}
	sep := string(filepath.Separator)
	return rel != ".." && !strings.HasPrefix(rel, ".."+sep) && !filepath.IsAbs(rel)
}

func regularFileExists(path string) bool {
	info, err := os.Lstat(path)
	return err == nil && info.Mode().IsRegular()
}

func pathFoldEqual(a, b string) bool {
	return strings.EqualFold(a, b)
}

// InstallRootFromExe derives (installRoot, versionDir, isManaged) from an executable path across all managed layouts.
func InstallRootFromExe(exePath string) (root string, versionDir string, isManaged bool) {
	if strings.TrimSpace(exePath) == "" {
		exe, err := os.Executable()
		if err != nil {
			return "", "", false
		}
		exePath = exe
	}
	resolvedExe := CanonicalPath(exePath)
	exeDir := filepath.Dir(resolvedExe)
	exeName := filepath.Base(resolvedExe)

	// <root>/versions/<v>/_internal/serverd.exe
	if pathFoldEqual(filepath.Base(exeDir), "_internal") &&
		(pathFoldEqual(exeName, "serverd.exe") || pathFoldEqual(exeName, "serverd")) {
		vDir := filepath.Dir(exeDir)
		parent := filepath.Dir(vDir)
		if pathFoldEqual(filepath.Base(parent), "versions") && IsManagedVersionName(filepath.Base(vDir)) {
			return filepath.Dir(parent), vDir, true
		}
	}

	// <root>/versions/<v>/Holdexar.exe or <root>/versions/<v>/serverd.exe
	if pathFoldEqual(filepath.Base(filepath.Dir(exeDir)), "versions") && IsManagedVersionName(filepath.Base(exeDir)) {
		if pathFoldEqual(exeName, AppName+".exe") || pathFoldEqual(exeName, AppName) ||
			pathFoldEqual(exeName, "serverd.exe") || pathFoldEqual(exeName, "serverd") {
			return filepath.Dir(filepath.Dir(exeDir)), exeDir, true
		}
	}

	// <root>/updater/UpdateHost.exe
	if pathFoldEqual(filepath.Base(exeDir), "updater") &&
		(pathFoldEqual(exeName, "UpdateHost.exe") || pathFoldEqual(exeName, "UpdateHost")) {
		return filepath.Dir(exeDir), "", true
	}

	// <root>/Holdexar.exe when <root>/state/current.json or <root>/updater/UpdateHost.exe exists
	if pathFoldEqual(exeName, AppName+".exe") || pathFoldEqual(exeName, AppName) {
		if regularFileExists(filepath.Join(exeDir, "state", "current.json")) ||
			regularFileExists(filepath.Join(exeDir, "updater", "UpdateHost.exe")) ||
			regularFileExists(filepath.Join(exeDir, "updater", "UpdateHost")) {
			return exeDir, "", true
		}
	}

	return exeDir, "", false
}

// DataDirOptions configures ResolveDataDir without any machine-profile (%LOCALAPPDATA%/%APPDATA%) fallback.
type DataDirOptions struct {
	ExplicitDir        string
	InstallRoot        string
	ExePath            string
	IsManaged          bool
	IgnoreEnv          bool
	SkipEnsureWritable bool
}

// ReadDevPointer reads an absolute path from <root>/holdexar-dev-home.txt, ignoring UTF-8 BOM, blank lines, and '#' comments.
func ReadDevPointer(root string) string {
	if root == "" {
		return ""
	}
	raw, err := os.ReadFile(filepath.Join(root, DevPointerFile))
	if err != nil {
		return ""
	}
	text := strings.TrimPrefix(string(raw), "\uFEFF")
	for _, line := range strings.Split(text, "\n") {
		entry := strings.TrimSpace(line)
		if entry == "" || strings.HasPrefix(entry, "#") {
			continue
		}
		if filepath.IsAbs(entry) {
			return filepath.Clean(entry)
		}
		return ""
	}
	return ""
}

// EnsureWritable creates path/logs and verifies write access via a temporary file probe.
func EnsureWritable(path string) error {
	logs := filepath.Join(path, "logs")
	if err := os.MkdirAll(logs, 0o755); err != nil {
		return fmt.Errorf("数据目录不可写：%s", path)
	}
	probe, err := os.CreateTemp(logs, ".writable-*")
	if err != nil {
		return fmt.Errorf("数据目录不可写：%s", path)
	}
	name := probe.Name()
	_ = probe.Close()
	_ = os.Remove(name)
	return nil
}

func expandHome(path string) string {
	if path == "~" || strings.HasPrefix(path, "~/") || strings.HasPrefix(path, `~\`) {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, path[1:])
		}
	}
	return path
}

func finalizeDataDir(path string, skipWritable bool) (string, error) {
	resolved := CanonicalPath(path)
	if skipWritable {
		return resolved, nil
	}
	if err := EnsureWritable(resolved); err != nil {
		return "", err
	}
	return resolved, nil
}

// ResolveDataDir resolves the active data directory with zero %LOCALAPPDATA%/%APPDATA% fallback.
func ResolveDataDir(opts DataDirOptions) (string, error) {
	explicit := strings.TrimSpace(opts.ExplicitDir)
	if explicit == "" && !opts.IgnoreEnv {
		explicit = strings.TrimSpace(os.Getenv(EnvPrefix + "DATA_DIR"))
	}
	if explicit != "" {
		return finalizeDataDir(expandHome(explicit), opts.SkipEnsureWritable)
	}

	root := strings.TrimSpace(opts.InstallRoot)
	if root == "" && !opts.IgnoreEnv {
		if envRoot := strings.TrimSpace(os.Getenv(EnvPrefix + "INSTALL_ROOT")); envRoot != "" {
			root = CanonicalPath(expandHome(envRoot))
		}
	}
	exeRoot, _, exeManaged := InstallRootFromExe(opts.ExePath)
	isManaged := opts.IsManaged
	if root == "" {
		root = exeRoot
		if exeManaged {
			isManaged = true
		}
	} else {
		root = CanonicalPath(expandHome(root))
		if exeRoot != "" && exeManaged && SamePath(root, exeRoot) {
			isManaged = true
		}
	}
	if root == "" {
		return "", errors.New("程序位置无法确定")
	}

	hasCurrentState := regularFileExists(filepath.Join(root, "state", "current.json"))
	hasUpdater := regularFileExists(filepath.Join(root, "updater", "UpdateHost.exe")) ||
		regularFileExists(filepath.Join(root, "updater", "UpdateHost"))
	if hasCurrentState || hasUpdater {
		isManaged = true
	}

	if hasCurrentState {
		if state, err := ReadCurrentState(root); err == nil && state != nil && state.DataCompatibility != nil {
			if compat, verr := ValidateDataCompat(state.DataCompatibility); verr == nil && compat.DataDir != "" {
				return finalizeDataDir(compat.DataDir, opts.SkipEnsureWritable)
			}
		}
	}

	rootDataDir := filepath.Join(root, "data")
	if regularFileExists(filepath.Join(root, PortableFlag)) ||
		regularFileExists(filepath.Join(rootDataDir, DBFileName)) ||
		isManaged {
		return finalizeDataDir(rootDataDir, opts.SkipEnsureWritable)
	}

	if pointed := ReadDevPointer(root); pointed != "" {
		if dir, err := finalizeDataDir(pointed, opts.SkipEnsureWritable); err == nil {
			return dir, nil
		}
	}
	return finalizeDataDir(filepath.Join(root, DevDataDirName), opts.SkipEnsureWritable)
}
