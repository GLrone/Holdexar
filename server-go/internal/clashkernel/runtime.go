package clashkernel

// 本地内核进程的启动 / 停止 / 状态与热重载编排。
// 写盘→spawn→混合口归属核验→控制器就绪；配置更新走热重载，被拒即回滚磁盘。

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"holdexar/server-go/internal/clashcfg"
)

// Status 内核运行状态。
type Status struct {
	Running         bool
	Port            int
	EntryReady      bool
	ConfigPath      string
	ControllerURL   string
	SubscriptionURL string
}

// Runtime 本地内核进程的生命周期持有者（每套内核一个实例，互不相干）。
type Runtime struct {
	Process         *KernelProcess
	Port            int
	ConfigPath      string
	ControllerURL   string
	Secret          string
	SubscriptionURL string
	ProbeLanes      []clashcfg.ProbeLane
	startupText     string
	EntryReady      bool
}

// Status 当前状态快照。
func (r *Runtime) Status() Status {
	running := r.Process != nil && r.Process.Running()
	return Status{
		Running:         running,
		Port:            r.portIf(running),
		EntryReady:      running && r.EntryReady && r.EntryListening(),
		ConfigPath:      r.ConfigPath,
		ControllerURL:   r.controllerIf(running),
		SubscriptionURL: r.subscriptionIf(running),
	}
}

func (r *Runtime) portIf(running bool) int {
	if !running {
		return 0
	}
	return r.Port
}

func (r *Runtime) controllerIf(running bool) string {
	if !running {
		return ""
	}
	return r.ControllerURL
}

func (r *Runtime) subscriptionIf(running bool) string {
	if !running {
		return ""
	}
	return r.SubscriptionURL
}

// EntryListening 混合入口必须由当前存活的内核进程监听。
func (r *Runtime) EntryListening() bool {
	if r.Process == nil || !r.Process.Running() || r.Port <= 0 {
		return false
	}
	return PortListening(r.Port) && PortOwnedByProcess(r.Port, uint32(r.Process.Pid()))
}

// WaitMixedPort 启动后等混合端口真的在听（控制器就绪 ≠ 入口就绪）。
func (r *Runtime) WaitMixedPort(timeout time.Duration) bool {
	if r.Port <= 0 {
		return false
	}
	if timeout <= 0 {
		timeout = 1500 * time.Millisecond
	}
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		if r.Process != nil && !r.Process.Running() {
			break
		}
		if PortListening(r.Port) {
			return true
		}
		time.Sleep(100 * time.Millisecond)
	}
	return false
}

// StartOptions Start 的可选参数（对齐 Python start() 关键字面）。
type StartOptions struct {
	RestartIfChanged  bool
	ForceRestart      bool
	InjectLanes       bool
	SubscriptionURL   string
	MixedPort         int
	AllocateMixedPort bool
	BundledDir        string
}

// readConfigFile 读配置文本；不存在返回空串。
func readConfigFile(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	return string(raw)
}

// writeConfigFile tmp + replace 原子落盘。
func writeConfigFile(path, text string) error {
	tmpPath := path + ".tmp"
	if err := os.WriteFile(tmpPath, []byte(text), 0o644); err != nil {
		return fmt.Errorf("运行配置写入失败：%w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		_ = os.Remove(tmpPath)
		return fmt.Errorf("运行配置落盘失败：%w", err)
	}
	return nil
}

// Start 启动内核。已在跑时按 restart/force 语义幂等返回；新文本经完整预检后
// 才落盘拉起，混合入口必须由新进程持有，否则视为启动失败（动态端口自动重试）。
func (r *Runtime) Start(exePath, configPath string, opts StartOptions) (Status, error) {
	return r.start(exePath, configPath, opts, 0)
}

func (r *Runtime) start(exePath, configPath string, opts StartOptions, retry int) (Status, error) {
	running := r.Status().Running
	if running && !opts.ForceRestart {
		diskText := readConfigFile(configPath)
		if err := clashcfg.ValidateExecutionConfigText(diskText); err != nil {
			return Status{}, err
		}
		if !opts.RestartIfChanged {
			target := strings.TrimSpace(opts.SubscriptionURL)
			if target == "" || r.SubscriptionURL == target {
				return r.Status(), nil
			}
		} else {
			if r.ConfigPath == configPath &&
				clashcfg.ConfigUnchanged(r.startupText, diskText) &&
				(opts.MixedPort == 0 || r.Port == opts.MixedPort) &&
				!opts.AllocateMixedPort {
				return r.Status(), nil
			}
		}
	}
	configText := readConfigFile(configPath)
	selectedPort := 0
	if opts.AllocateMixedPort {
		port, err := clashcfg.FreeLocalPort()
		if err != nil {
			return Status{}, err
		}
		selectedPort = port
	} else {
		selectedPort = opts.MixedPort
	}
	if selectedPort > 0 {
		configText = clashcfg.PinMixedPort(configText, selectedPort)
	}
	configText = clashcfg.StripTunSection(configText)
	if opts.InjectLanes {
		configText = clashcfg.InjectProbeLanes(configText, clashcfg.ProbeLaneOptions{})
	}
	controllerURL, controllerSecret := clashcfg.ResolveController(configText)
	text := clashcfg.WithControllerSecret(configText, controllerSecret)
	workDir := filepath.Dir(configPath)
	if err := ValidateConfigText(exePath, text, workDir); err != nil {
		return Status{}, err
	}
	// 随包内核资产补齐失败不阻断启动（调用方未给随包目录时跳过）
	if opts.BundledDir != "" {
		EnsureKernelFiles(workDir, opts.BundledDir)
	}
	if running || r.Process != nil {
		r.Stop()
	}
	if _, err := KillOrphanKernelsExclude(KernelFilename(), configPath, uint32(r.ProcessPid())); err != nil {
		// 清理失败不阻断启动
	}
	if err := writeConfigFile(configPath, text); err != nil {
		return Status{}, err
	}
	if err := ConfirmConfigText(configPath, text); err != nil {
		return Status{}, err
	}

	r.Port = clashcfg.ParseMixedPort(text)
	// 控制器经命令行参数下发：优先级高于配置文件，文件与命令行用同一密钥。
	r.ControllerURL, r.Secret = controllerURL, controllerSecret
	controllerAddr := strings.TrimPrefix(controllerURL, "http://")
	if opts.InjectLanes {
		r.ProbeLanes, _ = clashcfg.ParseProbeLanes(text)
	} else {
		r.ProbeLanes = nil
	}
	r.ConfigPath = configPath
	r.SubscriptionURL = strings.TrimSpace(opts.SubscriptionURL)
	r.startupText = text
	proc, err := Spawn(exePath, []string{
		"-f", configPath, "-d", workDir,
		"-ext-ctl", controllerAddr, "-secret", r.Secret,
	}, filepath.Join(workDir, "kernel.log"))
	if err != nil {
		return Status{}, err
	}
	r.Process = proc
	r.EntryReady = r.WaitMixedPort(1500*time.Millisecond) &&
		r.Process != nil && PortOwnedByProcess(r.Port, uint32(r.Process.Pid()))
	if !r.EntryReady {
		failedPort := r.Port
		r.Stop()
		if opts.AllocateMixedPort && retry < 3 {
			return r.start(exePath, configPath, opts, retry+1)
		}
		return Status{}, fmt.Errorf("代理入口未由内核进程监听：%d", failedPort)
	}
	return r.Status(), nil
}

// ProcessPid 现役子进程号（无则 0）。
func (r *Runtime) ProcessPid() int {
	if r.Process == nil {
		return 0
	}
	return r.Process.Pid()
}

// RunningSubscriptionIs 内核当前跑的是否该订阅（按启动时登记的订阅 URL 判定）。
func (r *Runtime) RunningSubscriptionIs(subURL string) bool {
	target := strings.TrimSpace(subURL)
	return r.Status().Running && r.SubscriptionURL != "" && target != "" &&
		r.SubscriptionURL == target
}

// Stop 停止内核并清空运行态账目。
func (r *Runtime) Stop() Status {
	if r.Process != nil {
		r.Process.Stop()
	}
	r.Process = nil
	r.ControllerURL = ""
	r.SubscriptionURL = ""
	r.ProbeLanes = nil
	r.startupText = ""
	r.Port = 0
	r.EntryReady = false
	return r.Status()
}

// runningKernelText 给运行中内核热重载用的配置文本：合法性预检（YAML 可解析 +
// 有节点段），不合法返回 ReloadRejected。返回（注入 lane 后文本, 写盘前文件原
// 内容）——拒载回滚恢复的是这个文件自己被覆盖前的内容。
func (r *Runtime) runningKernelText(configPath string, mixedPort int, exePath string) (string, string, error) {
	prevFileText := ""
	if raw, err := os.ReadFile(configPath); err == nil {
		prevFileText = string(raw)
	}
	text := prevFileText
	if _, err := clashcfg.LoadYAML(text); err != nil {
		return "", "", &ReloadRejectedError{Detail: fmt.Sprintf("新配置不是合法 YAML：%s", err)}
	}
	doc, _ := clashcfg.LoadYAML(text)
	if !clashcfg.Truthy(doc["proxies"]) && !clashcfg.Truthy(doc["proxy-providers"]) {
		return "", "", &ReloadRejectedError{Detail: "新配置没有 proxies / proxy-providers 段"}
	}
	text = clashcfg.InjectProbeLanes(text, clashcfg.ProbeLaneOptions{})
	if mixedPort > 0 {
		text = clashcfg.PinMixedPort(text, mixedPort)
	}
	text = clashcfg.StripTunSection(text)
	if r.Secret != "" {
		text = clashcfg.WithControllerSecret(text, r.Secret)
	}
	if exePath != "" {
		if err := ValidateConfigText(exePath, text, filepath.Dir(configPath)); err != nil {
			return "", "", &ReloadRejectedError{Detail: err.Error()}
		}
	} else {
		if err := clashcfg.ValidateExecutionConfigText(text); err != nil {
			return "", "", &ReloadRejectedError{Detail: err.Error()}
		}
	}
	if err := writeConfigFile(configPath, text); err != nil {
		return "", "", err
	}
	return text, prevFileText, nil
}

// EnsureRunningOptions EnsureRunning 的可选参数。
type EnsureRunningOptions struct {
	SubscriptionURL   string
	MixedPort         int
	AllocateMixedPort bool
	ForceRestart      bool
}

// EnsureRunResult EnsureRunning 的返回：状态 + started/reloaded 两个标记。
type EnsureRunResult struct {
	Status   Status
	Started  bool
	Reloaded bool
}

// EnsureRunning 确保内核跑着指定配置：没跑就启动；在跑时热重载生效（控制器
// PUT /configs，内核进程不动）；配置被内核拒绝时回滚磁盘、保留运行态；控制器
// 通路故障才回退进程重启。
func (r *Runtime) EnsureRunning(exePath, configPath string, opts EnsureRunningOptions) (EnsureRunResult, error) {
	subURL := strings.TrimSpace(opts.SubscriptionURL)
	running := r.Process != nil && r.Process.Running()
	if running && opts.AllocateMixedPort {
		if r.EntryListening() {
			opts.MixedPort = r.Port
			opts.AllocateMixedPort = false
		} else {
			status, err := r.Start(exePath, configPath, StartOptions{
				SubscriptionURL: subURL, AllocateMixedPort: true, ForceRestart: true,
			})
			return EnsureRunResult{Status: status, Started: err == nil}, err
		}
	}
	if !running {
		status, err := r.Start(exePath, configPath, StartOptions{
			SubscriptionURL: subURL, MixedPort: opts.MixedPort,
			AllocateMixedPort: opts.AllocateMixedPort,
		})
		return EnsureRunResult{Status: status, Started: err == nil}, err
	}
	if opts.ForceRestart {
		status, err := r.Start(exePath, configPath, StartOptions{
			SubscriptionURL: subURL, MixedPort: opts.MixedPort,
			AllocateMixedPort: opts.AllocateMixedPort, ForceRestart: true,
		})
		return EnsureRunResult{Status: status, Started: err == nil}, err
	}

	candidateText := readConfigFile(configPath)
	if err := clashcfg.ValidateExecutionConfigText(candidateText); err != nil {
		return EnsureRunResult{}, err
	}
	sameSub := subURL != "" && r.SubscriptionURL == subURL
	if sameSub && r.ConfigPath == configPath &&
		(opts.MixedPort == 0 || r.Port == opts.MixedPort) &&
		r.EntryListening() &&
		clashcfg.ConfigUnchanged(r.startupText, candidateText) {
		return EnsureRunResult{Status: r.Status()}, nil
	}

	prevFileText := ""
	wrote := false
	injected, prevFileText, prepErr := r.runningKernelText(configPath, opts.MixedPort, exePath)
	if prepErr != nil {
		if _, isRejected := prepErr.(*ReloadRejectedError); isRejected {
			return EnsureRunResult{}, fmt.Errorf("%s", prepErr.Error())
		}
		return EnsureRunResult{}, prepErr
	}
	wrote = true
	if err := ConfirmConfigText(configPath, injected); err != nil {
		r.rollbackConfig(configPath, prevFileText, wrote)
		return EnsureRunResult{}, fmt.Errorf("%s", err.Error())
	}
	controller := NewController(r.ControllerURL, r.Secret)
	if err := controller.PutConfigsForce(configPath); err != nil {
		if _, isRejected := err.(*ReloadRejectedError); isRejected {
			r.rollbackConfig(configPath, prevFileText, wrote)
			return EnsureRunResult{}, err
		}
		// 通路故障才回退进程重启
		r.Stop()
		status, startErr := r.Start(exePath, configPath, StartOptions{
			SubscriptionURL: subURL, MixedPort: opts.MixedPort,
			AllocateMixedPort: opts.AllocateMixedPort,
		})
		return EnsureRunResult{Status: status, Started: startErr == nil}, startErr
	}
	if err := VerifyReloaded(controller, injected, r.Process.Pid()); err != nil {
		// 校验不过：配置文本已写盘且内核可能已部分加载——回退进程重启收敛
		r.Stop()
		status, startErr := r.Start(exePath, configPath, StartOptions{
			SubscriptionURL: subURL, MixedPort: opts.MixedPort,
			AllocateMixedPort: opts.AllocateMixedPort,
		})
		return EnsureRunResult{Status: status, Started: startErr == nil}, startErr
	}

	r.ConfigPath = configPath
	r.startupText = injected
	r.Port = clashcfg.ParseMixedPort(injected)
	r.ProbeLanes, _ = clashcfg.ParseProbeLanes(injected)
	if subURL != "" {
		r.SubscriptionURL = subURL
	}
	r.EntryReady = true
	return EnsureRunResult{Status: r.Status(), Reloaded: true}, nil
}

// rollbackConfig 拒载回滚：目标文件恢复到本进程写入前的内容（本次新建则删除），
// 内核继续跑旧配置。
func (r *Runtime) rollbackConfig(configPath, prevFileText string, wrote bool) {
	if !wrote {
		return
	}
	if prevFileText != "" {
		_ = os.WriteFile(configPath, []byte(prevFileText), 0o644)
	} else {
		_ = os.Remove(configPath)
	}
}
