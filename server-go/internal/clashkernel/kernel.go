package clashkernel

// 内核子进程的唯一执行边界：受控环境、预检错误分类、统一的启动与一次性调用入口。

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/clashcfg"
)

type timeoutError struct{ d time.Duration }

func (e *timeoutError) Error() string { return fmt.Sprintf("内核命令超时（%s）", e.d) }

func execTimeoutContext(timeout time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), timeout)
}

func joinPath(parts ...string) string { return filepath.Join(parts...) }

var overridePrefixes = []string{"CLASH_", "MIHOMO_"}

var overrideNames = map[string]bool{
	"SAFE_PATHS": true, "DISABLE_LOOPBACK_DETECTOR": true, "SKIP_SYSTEM_IPV6_CHECK": true,
}

type PrecheckKind string

const (
	PrecheckAdmission    PrecheckKind = "admission"
	PrecheckTimeout      PrecheckKind = "timeout"
	PrecheckRejected     PrecheckKind = "rejected"
	PrecheckUnlaunchable PrecheckKind = "unlaunchable"
	PrecheckChanged      PrecheckKind = "changed"
)

type PrecheckError struct {
	Kind   PrecheckKind
	Detail string
}

func (e *PrecheckError) Error() string { return e.Detail }

// KernelEnv 内核子进程环境：去掉能覆盖配置、改写运行行为的变量，其余原样保留。
func KernelEnv(base []string) []string {
	out := make([]string, 0, len(base))
	for _, entry := range base {
		eq := strings.Index(entry, "=")
		if eq <= 0 {
			continue
		}
		key := entry[:eq]
		upper := strings.ToUpper(key)
		stripped := false
		for _, prefix := range overridePrefixes {
			if strings.HasPrefix(upper, prefix) {
				stripped = true
				break
			}
		}
		if !stripped && overrideNames[upper] {
			stripped = true
		}
		if !stripped {
			out = append(out, entry)
		}
	}
	return out
}

const MIHOMOVersion = "v1.19.32"

// KernelFilename 带应用前缀，用于与机器上其他内核进程隔离防误杀。
func KernelFilename() string {
	if isWindows() {
		return "holdexar-mihomo.exe"
	}
	return "holdexar-mihomo"
}

func legacyKernelFilename() string {
	if isWindows() {
		return "mihomo.exe"
	}
	return "mihomo"
}

func isWindows() bool { return os.PathSeparator == '\\' }

var versionRe = regexp.MustCompile(`v(\d+(?:\.\d+)+)`)

func ParseKernelVersion(raw string) []int {
	m := versionRe.FindStringSubmatch(raw)
	if m == nil {
		return nil
	}
	parts := strings.Split(m[1], ".")
	out := make([]int, 0, len(parts))
	for _, part := range parts {
		n, err := strconv.Atoi(part)
		if err != nil {
			return nil
		}
		out = append(out, n)
	}
	return out
}

func versionAtLeast(current, target []int) bool {
	if current == nil || target == nil {
		return false
	}
	for i := 0; i < len(current) || i < len(target); i++ {
		c, t := 0, 0
		if i < len(current) {
			c = current[i]
		}
		if i < len(target) {
			t = target[i]
		}
		if c != t {
			return c > t
		}
	}
	return true
}

type RunResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
}

func RunKernelCLI(exePath string, args []string, timeout time.Duration) (RunResult, error) {
	ctx, cancel := execTimeoutContext(timeout)
	defer cancel()
	cmd := exec.CommandContext(ctx, exePath, args...)
	cmd.Env = KernelEnv(os.Environ())
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	err := cmd.Run()
	if ctx.Err() != nil {
		return RunResult{}, &timeoutError{d: timeout}
	}
	res := RunResult{Stdout: stdout.String(), Stderr: stderr.String()}
	if err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			res.ExitCode = exitErr.ExitCode()
		} else {
			return RunResult{}, fmt.Errorf("内核无法执行：%s", err)
		}
	}
	return res, nil
}

func KernelVersion(exePath string) string {
	res, err := RunKernelCLI(exePath, []string{"-v"}, 10*time.Second)
	if err != nil {
		return ""
	}
	text := res.Stdout
	if strings.TrimSpace(text) == "" {
		text = res.Stderr
	}
	for _, line := range strings.Split(text, "\n") {
		line = strings.TrimSpace(line)
		if line != "" {
			return line
		}
	}
	return ""
}

func KernelExeVersion(exePath string) []int {
	return ParseKernelVersion(KernelVersion(exePath))
}

type EnsureResult struct {
	KernelReady bool
	Copied      []string
	Missing     []string
	Upgraded    string
	Adopted     string
	BundleDir   string
}

func KernelDir(dataDir string) string {
	dir := joinPath(dataDir, "clash")
	_ = os.MkdirAll(dir, 0o755)
	return dir
}

func KernelExe(dataDir string) string { return joinPath(KernelDir(dataDir), KernelFilename()) }

// AdoptLegacyKernel 仅在新名缺失时收编兼容名内核，占用无法改名时静默跳过。
func AdoptLegacyKernel(kernelPath string) string {
	legacy := legacyKernelFilename()
	legacyPath := joinPath(kernelPath, legacy)
	current := joinPath(kernelPath, KernelFilename())
	if !fileExists(legacyPath) || fileExists(current) {
		return ""
	}
	if err := os.Rename(legacyPath, current); err != nil {
		return ""
	}
	return legacy
}

func EnsureKernel(bundledDir, dataDir string) (EnsureResult, error) {
	adopted := AdoptLegacyKernel(KernelDir(dataDir))
	result := EnsureKernelFiles(KernelDir(dataDir), bundledDir)
	result.Adopted = adopted
	result.KernelReady = fileExists(KernelExe(dataDir))
	result.BundleDir = bundledDir
	return result, nil
}

func EnsureKernelFiles(kernelPath, bundledDir string) EnsureResult {
	exeName := KernelFilename()
	target := joinPath(kernelPath, exeName)
	bundledExe := joinPath(bundledDir, exeName)
	result := EnsureResult{BundleDir: bundledDir}
	if !fileExists(target) {
		if fileExists(bundledExe) {
			if err := copyFile(bundledExe, target); err != nil {
				return result
			}
			result.Copied = append(result.Copied, exeName)
		} else {
			result.Missing = append(result.Missing, exeName)
		}
	}
	result.Upgraded = maybeUpgradeKernel(kernelPath, bundledDir)
	return result
}

// maybeUpgradeKernel 仅在随包版本高于就位版本且内核未运行时替换（不降级不夺回）。
func maybeUpgradeKernel(kernelDir, bundledDir string) string {
	exeName := KernelFilename()
	exe := joinPath(kernelDir, exeName)
	bundledExe := joinPath(bundledDir, exeName)
	if !fileExists(exe) || !fileExists(bundledExe) {
		return ""
	}
	if KernelProcessAlive(exeName) {
		return ""
	}
	current := KernelExeVersion(exe)
	target := ParseKernelVersion(MIHOMOVersion)
	if current == nil || target == nil || versionAtLeast(current, target) {
		return ""
	}
	if err := copyFile(bundledExe, exe); err != nil {
		return ""
	}
	old := make([]string, 0, len(current))
	for _, part := range current {
		old = append(old, fmt.Sprint(part))
	}
	return strings.Join(old, ".") + " → " + MIHOMOVersion
}

func fileExists(path string) bool {
	info, err := os.Stat(path)
	return err == nil && info.Mode().IsRegular()
}

func copyFile(src, dst string) error {
	raw, err := os.ReadFile(src)
	if err != nil {
		return fmt.Errorf("随包内核复制失败：%w", err)
	}
	if err := os.WriteFile(dst, raw, 0o755); err != nil {
		return fmt.Errorf("随包内核写入失败：%w", err)
	}
	return nil
}

func ValidateConfig(exePath, configPath, workDir string) error {
	raw, err := os.ReadFile(configPath)
	if err != nil {
		return &PrecheckError{Kind: PrecheckUnlaunchable, Detail: fmt.Sprintf("运行配置不可读：%s", err)}
	}
	if err := clashcfg.ValidateExecutionConfigText(string(raw)); err != nil {
		return &PrecheckError{Kind: PrecheckAdmission, Detail: err.Error()}
	}
	res, runErr := RunKernelCLI(exePath, []string{"-t", "-f", configPath, "-d", workDir}, 15*time.Second)
	if runErr != nil {
		var te *timeoutError
		if errors.As(runErr, &te) {
			return &PrecheckError{Kind: PrecheckTimeout, Detail: "内核配置校验超时（15s），禁止启动"}
		}
		return &PrecheckError{Kind: PrecheckUnlaunchable, Detail: runErr.Error()}
	}
	after, err := os.ReadFile(configPath)
	if err != nil || string(after) != string(raw) {
		return &PrecheckError{Kind: PrecheckChanged, Detail: "运行配置在预检期间被改动，拒绝启动"}
	}
	if res.ExitCode == 0 {
		return nil
	}
	var details []string
	for _, line := range strings.Split(res.Stdout+res.Stderr, "\n") {
		if idx := strings.Index(line, "level=error"); idx >= 0 {
			details = append(details, strings.TrimSpace(line[idx+len("level=error"):]))
			if len(details) >= 5 {
				break
			}
		}
	}
	shown := fmt.Sprintf("退出码 %d", res.ExitCode)
	if len(details) > 0 {
		shown = strings.Join(details, "；")
	}
	return &PrecheckError{Kind: PrecheckRejected, Detail: "内核拒绝配置（-t 校验未通过）：" + shown}
}

func ValidateConfigText(exePath, text, workDir string) error {
	if err := clashcfg.ValidateExecutionConfigText(text); err != nil {
		return &PrecheckError{Kind: PrecheckAdmission, Detail: err.Error()}
	}
	folder, err := os.MkdirTemp(workDir, ".mihomo-config-check-")
	if err != nil {
		return &PrecheckError{Kind: PrecheckUnlaunchable, Detail: fmt.Sprintf("预检临时目录创建失败：%s", err)}
	}
	defer os.RemoveAll(folder)
	candidate := joinPath(folder, "config.yaml")
	if err := os.WriteFile(candidate, []byte(text), 0o644); err != nil {
		return &PrecheckError{Kind: PrecheckUnlaunchable, Detail: fmt.Sprintf("预检配置写入失败：%s", err)}
	}
	return ValidateConfig(exePath, candidate, workDir)
}

// ConfirmConfigText 启动前确认磁盘上的配置就是通过预检的那份文本。
func ConfirmConfigText(path, expected string) error {
	raw, err := os.ReadFile(path)
	if err != nil {
		return &PrecheckError{Kind: PrecheckChanged, Detail: fmt.Sprintf("启动前读不回运行配置：%s", err)}
	}
	if string(raw) != expected {
		return &PrecheckError{Kind: PrecheckChanged, Detail: "运行配置在预检之后被改动，拒绝启动"}
	}
	return nil
}
