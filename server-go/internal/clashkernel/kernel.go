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

// timeoutError 一次性内核命令超时（对齐 subprocess.TimeoutExpired 分支）。
type timeoutError struct{ d time.Duration }

func (e *timeoutError) Error() string { return fmt.Sprintf("内核命令超时（%s）", e.d) }

func execTimeoutContext(timeout time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), timeout)
}

func joinPath(parts ...string) string { return filepath.Join(parts...) }

// OverridePrefixes 能覆盖配置、改写运行行为的环境变量前缀（大小写不敏感）。
var overridePrefixes = []string{"CLASH_", "MIHOMO_"}

// OverrideNames 额外剥离的内核侧环境开关（大小写不敏感）。
var overrideNames = map[string]bool{
	"SAFE_PATHS": true, "DISABLE_LOOPBACK_DETECTOR": true, "SKIP_SYSTEM_IPV6_CHECK": true,
}

// PrecheckKind 预检拒绝分类：准入拒绝、内核拒载、超时、无法执行、文件被替换。
type PrecheckKind string

const (
	PrecheckAdmission    PrecheckKind = "admission"
	PrecheckTimeout      PrecheckKind = "timeout"
	PrecheckRejected     PrecheckKind = "rejected"
	PrecheckUnlaunchable PrecheckKind = "unlaunchable"
	PrecheckChanged      PrecheckKind = "changed"
)

// PrecheckError 内核启动前的拒绝。
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

// MIHOMOVersion 随包内核版本（与 Python kernel_release.MIHOMO_VERSION 对齐）。
const MIHOMOVersion = "v1.19.32"

// KernelFilename 内核可执行文件名（Windows 带 .exe）。
func KernelFilename() string {
	if isWindows() {
		return "mihomo.exe"
	}
	return "mihomo"
}

func isWindows() bool { return os.PathSeparator == '\\' }

var versionRe = regexp.MustCompile(`v(\d+(?:\.\d+)+)`)

// ParseKernelVersion mihomo -v 输出 → 点分版本切片；无法识别返回 nil。
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

// RunResult 一次性内核命令输出。
type RunResult struct {
	Stdout   string
	Stderr   string
	ExitCode int
}

// RunKernelCLI 跑一次性内核命令（版本探测、配置预检）并捕获输出。
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

// KernelVersion 就位内核的版本输出首行；失败返回空串。
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

// KernelExeVersion 就位内核的版本切片（-v 失败/超时/输出非 mihomo 格式 = nil）。
func KernelExeVersion(exePath string) []int {
	return ParseKernelVersion(KernelVersion(exePath))
}

// EnsureResult 随包内核就位结果。
type EnsureResult struct {
	KernelReady bool
	Copied      []string
	Missing     []string
	Upgraded    string
	BundleDir   string
}

// KernelDir 内核工作目录（config.yaml 所在处，也是 -d 的工作目录）。
func KernelDir(dataDir string) string {
	dir := joinPath(dataDir, "clash")
	_ = os.MkdirAll(dir, 0o755)
	return dir
}

func KernelExe(dataDir string) string { return joinPath(KernelDir(dataDir), KernelFilename()) }

// EnsureKernel 随包内核就位（data_dir 版本）：把内核补进 data/clash/（同名不
// 覆盖），随包版本高于就位版本时替换（升级不夺回）。发行包缺内核时
// KernelReady=false，由调用方决定回退下载还是提示用户。
func EnsureKernel(bundledDir, dataDir string) (EnsureResult, error) {
	result := EnsureKernelFiles(KernelDir(dataDir), bundledDir)
	result.KernelReady = fileExists(KernelExe(dataDir))
	result.BundleDir = bundledDir
	return result, nil
}

// EnsureKernelFiles 内核目录补齐（内核目录本身版本）：可执行文件同名不覆盖，
// 另按版本升级。
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

// maybeUpgradeKernel 版本感知的内核换装：随包版本高于就位版本才替换。
// 就位 ≥ 随包、版本不可辨、内核进程在跑时一律保留（升级不夺回）。
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

// ValidateConfig 启动前校验运行配置：先静态准入再跑内核 `-t`；任何失败都返回
// PrecheckError。
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

// ValidateConfigText 校验最终内存态配置文本：写临时副本后走完整预检。
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
