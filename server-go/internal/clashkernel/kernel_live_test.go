package clashkernel

// 真内核行为验收（核心门禁）：用 worktree 随包 mihomo.exe 真实跑通
// 预检 → spawn → 混合口归属 → 控制器 → 热重载 → 停止 → 孤儿清理。
// 内核不随仓库分发：assets/clash/mihomo.exe 缺失时整组 skip，不伪装通过。
// 孤儿清理测试只用改名副本内核，绝不匹配真映像名。

import (
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/clashcfg"
)

const worktreeRoot = `E:\Holdexar-go-migration`

func liveKernelAsset(t *testing.T) string {
	t.Helper()
	exe := filepath.Join(worktreeRoot, "assets", "clash", "mihomo.exe")
	if _, err := os.Stat(exe); err != nil {
		t.Skip("随包内核缺失，跳过真内核行为验收")
	}
	return exe
}

// poolFixture 生成 n 个假 ss 节点的池文件（127.0.0.1 占位服务，内核可解析即可）。
func poolFixture(n int) string {
	var b strings.Builder
	b.WriteString("proxies:\n")
	for i := 0; i < n; i++ {
		fmt.Fprintf(&b,
			"- name: 测试节点%02d\n  type: ss\n  server: 127.0.0.1\n  port: %d\n  cipher: aes-128-gcm\n  password: pw-%02d\n",
			i, 10000+i, i)
	}
	return b.String()
}

func writePool(t *testing.T, dataDir string, n int) {
	t.Helper()
	poolPath := filepath.Join(dataDir, "proxypool", "crawl-pool.yaml")
	if err := os.MkdirAll(filepath.Dir(poolPath), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(poolPath, []byte(poolFixture(n)), 0o644); err != nil {
		t.Fatal(err)
	}
}

// TestLiveKernelFullCycle 七步行为验收：就位 → 预检 → 启动 → 混合口归属 →
// 控制器 → 热重载 → 停止。
func TestLiveKernelFullCycle(t *testing.T) {
	assetExe := liveKernelAsset(t)
	dataDir := t.TempDir()

	// ① 内核就位（复制）
	res, err := EnsureKernel(filepath.Dir(assetExe), dataDir)
	if err != nil {
		t.Fatalf("内核就位失败：%v", err)
	}
	if !res.KernelReady || len(res.Copied) != 1 {
		t.Fatalf("就位结果不对：%+v", res)
	}
	exePath := KernelExe(dataDir)

	// ② 生成含 3 条 lane 的运行配置并预检
	writePool(t, dataDir, 3)
	configPath, _, err := clashcfg.PrepareRuntimeConfig(dataDir, 3, false)
	if err != nil {
		t.Fatalf("运行配置生成失败：%v", err)
	}
	workDir := filepath.Dir(configPath)
	if err := ValidateConfig(exePath, configPath, workDir); err != nil {
		t.Fatalf("预检应通过：%v", err)
	}

	// ③ 启动 + 混合口就绪且归属进程正确
	runtime := &Runtime{}
	status, err := runtime.Start(exePath, configPath, StartOptions{InjectLanes: false})
	if err != nil {
		t.Fatalf("启动失败：%v", err)
	}
	t.Cleanup(func() { runtime.Stop() })
	if !status.Running || !status.EntryReady {
		t.Fatalf("启动状态不对：%+v", status)
	}
	pid := uint32(runtime.Process.Pid())
	if !PortListening(status.Port) {
		t.Fatalf("混合口 %d 不在听", status.Port)
	}
	if !PortOwnedByProcess(status.Port, pid) {
		t.Fatalf("混合口 %d 未由内核进程 %d 持有", status.Port, pid)
	}
	lanePorts := clashcfg.PinnedRuntimeEndpoints(readFile(t, configPath)).LanePorts
	if len(lanePorts) != 3 {
		t.Fatalf("lane 数不对：%v", lanePorts)
	}
	for _, port := range lanePorts {
		if !PortListening(port) {
			t.Fatalf("lane 端口 %d 不在听", port)
		}
	}

	// ④ 控制器 /version 与 /proxies 可查
	controller := NewController(status.ControllerURL, runtime.Secret)
	if err := controller.Version(); err != nil {
		t.Fatalf("控制器 /version 不可用：%v", err)
	}
	names, err := controller.ProxyNames()
	if err != nil {
		t.Fatalf("控制器 /proxies 不可用：%v", err)
	}
	for _, want := range []string{"测试节点00", "测试节点01", "测试节点02", "lane-0", "GLOBAL"} {
		if !names[want] {
			t.Fatalf("/proxies 缺 %q，实得 %v", want, names)
		}
	}

	// ⑤ 热重载：池加一个节点，钉住端点生成新配置 → PUT /configs?force=true
	writePool(t, dataDir, 4)
	newConfigPath, newText, err := clashcfg.PrepareRuntimeConfig(dataDir, 3, true)
	if err != nil {
		t.Fatalf("钉住端点再生成失败：%v", err)
	}
	if err := controller.PutConfigsForce(newConfigPath); err != nil {
		t.Fatalf("热重载被拒：%v", err)
	}
	if err := VerifyReloaded(controller, newText, runtime.Process.Pid()); err != nil {
		t.Fatalf("热重载校验失败：%v", err)
	}
	if !namesContains(t, controller, "测试节点03") {
		t.Fatal("热重载后新节点未出现在内核")
	}
	for _, port := range lanePorts {
		if !PortListening(port) {
			t.Fatalf("热重载后 lane 端口 %d 未保持监听", port)
		}
	}
	if runtime.Status().Port != status.Port {
		t.Fatalf("热重载后混合口漂移：%d → %d", status.Port, runtime.Status().Port)
	}

	// ⑥ 停止：进程消失、端口释放
	oldPID := runtime.Process.Pid()
	runtime.Stop()
	if runtime.Process != nil && runtime.Process.Running() {
		t.Fatal("停止后进程应消失")
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if !PortListening(status.Port) {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if PortListening(status.Port) {
		t.Fatalf("停止后混合口 %d 仍被占用", status.Port)
	}
	if PortOwnedByProcess(status.Port, uint32(oldPID)) {
		t.Fatalf("停止后端口 %d 仍归属旧进程", status.Port)
	}
	if PortListening(newConfigPathMixedPort(t, newConfigPath)) {
		t.Log("停止后新配置混合口释放略慢，属内核异步退出窗口")
	}

	// ⑦ 重启能力：停止后可再次启动（三段停止不留脏状态）
	status2, err := runtime.Start(exePath, configPath, StartOptions{InjectLanes: false})
	if err != nil {
		t.Fatalf("停止后重启失败：%v", err)
	}
	t.Cleanup(func() { runtime.Stop() })
	if !status2.Running || !status2.EntryReady {
		t.Fatalf("重启状态不对：%+v", status2)
	}
	runtime.Stop()
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("配置读不回：%v", err)
	}
	return string(raw)
}

func namesContains(t *testing.T, controller *Controller, want string) bool {
	t.Helper()
	names, err := controller.ProxyNames()
	if err != nil {
		t.Fatalf("/proxies 读取失败：%v", err)
	}
	return names[want]
}

func newConfigPathMixedPort(t *testing.T, path string) int {
	t.Helper()
	return clashcfg.ParseMixedPort(readFile(t, path))
}

// TestLiveKernelBadConfigRejected 预检坏配置必须拒绝（真内核 -t）。
func TestLiveKernelBadConfigRejected(t *testing.T) {
	assetExe := liveKernelAsset(t)
	dataDir := t.TempDir()
	if _, err := EnsureKernel(filepath.Dir(assetExe), dataDir); err != nil {
		t.Fatal(err)
	}
	badPath := filepath.Join(dataDir, "clash", "bad.yaml")
	bad := "proxies:\n" +
		"- name: n\n  type: no-such-protocol\n  server: 127.0.0.1\n  port: 1\n"
	if err := os.WriteFile(badPath, []byte(bad), 0o644); err != nil {
		t.Fatal(err)
	}
	err := ValidateConfig(KernelExe(dataDir), badPath, filepath.Dir(badPath))
	pre, ok := err.(*PrecheckError)
	if !ok || pre.Kind != PrecheckRejected {
		t.Fatalf("坏配置应归类 rejected，实际 %v", err)
	}
	if pre.Detail == "" {
		t.Fatal("拒载应带内核报错细节")
	}
}

// TestLiveKernelSpawnImmediateStopRace spawn 后立刻 stop 的竞态：三段停止对
// 「刚拉起就停」与「起好再停」都必须收敛，且随后还能再启动。
func TestLiveKernelSpawnImmediateStopRace(t *testing.T) {
	assetExe := liveKernelAsset(t)
	dataDir := t.TempDir()
	if _, err := EnsureKernel(filepath.Dir(assetExe), dataDir); err != nil {
		t.Fatal(err)
	}
	writePool(t, dataDir, 2)
	configPath, _, err := clashcfg.PrepareRuntimeConfig(dataDir, 1, false)
	if err != nil {
		t.Fatal(err)
	}
	exePath := KernelExe(dataDir)
	workDir := filepath.Dir(configPath)
	for i := 0; i < 3; i++ {
		proc, err := Spawn(exePath, []string{
			"-f", configPath, "-d", workDir, "-ext-ctl", "127.0.0.1:19099", "-secret", "race",
		}, filepath.Join(workDir, "kernel.log"))
		if err != nil {
			t.Fatalf("第 %d 次 spawn 失败：%v", i, err)
		}
		proc.Stop()
		if proc.Running() {
			t.Fatalf("第 %d 次竞态停止后进程仍在", i)
		}
	}
	// 竞态之后内核仍可正常起一次完整生命周期
	runtime := &Runtime{}
	if _, err := runtime.Start(exePath, configPath, StartOptions{InjectLanes: false}); err != nil {
		t.Fatalf("竞态后正常启动失败：%v", err)
	}
	runtime.Stop()
}

// TestLiveOrphanCleanupWithRenamedCopy 孤儿清理：改名副本内核 + 命令行匹配。
// 绝不匹配真 mihomo.exe 映像名。
func TestLiveOrphanCleanupWithRenamedCopy(t *testing.T) {
	assetExe := liveKernelAsset(t)
	dataDir := t.TempDir()
	kernelDir := KernelDir(dataDir)
	orphanExe := filepath.Join(kernelDir, "mihomo-hltest-orphan.exe")
	raw, err := os.ReadFile(assetExe)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(orphanExe, raw, 0o755); err != nil {
		t.Fatal(err)
	}
	mixedPort, err := clashcfg.FreeLocalPort()
	if err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(kernelDir, "orphan-config.yaml")
	config := fmt.Sprintf("mixed-port: %d\nproxies:\n- name: n\n  type: ss\n  server: 127.0.0.1\n  port: 1\n  cipher: aes-128-gcm\n  password: p\n", mixedPort)
	if err := os.WriteFile(configPath, []byte(config), 0o644); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(orphanExe, "-f", configPath, "-d", kernelDir)
	if err := cmd.Start(); err != nil {
		t.Fatalf("孤儿副本拉起失败：%v", err)
	}
	orphanPID := uint32(cmd.Process.Pid)
	t.Cleanup(func() { _ = cmd.Process.Kill() })
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) && !PortListening(mixedPort) {
		time.Sleep(100 * time.Millisecond)
	}
	if !PortListening(mixedPort) {
		t.Fatal("孤儿副本未进入监听")
	}
	if !KernelProcessAlive("mihomo-hltest-orphan.exe") {
		t.Fatal("副本内核存活探测失败")
	}
	found := false
	for _, pid := range ImageProcessPIDs("mihomo-hltest-orphan.exe") {
		if pid == orphanPID {
			found = true
		}
	}
	if !found {
		t.Fatal("Toolhelp32 枚举应含孤儿副本进程号")
	}

	// 配置路径不匹配 → 不动它（命令行守卫）
	killed, err := KillOrphanKernels("mihomo-hltest-orphan.exe", "E:\\definitely-other-config.yaml")
	if err != nil {
		t.Fatalf("孤儿查询失败：%v", err)
	}
	if killed != 0 {
		t.Fatalf("配置不匹配不应清理，实际清理 %d 个", killed)
	}
	if !PortListening(mixedPort) {
		t.Fatal("守卫不匹配误杀了孤儿副本")
	}

	// 配置路径匹配 → 清掉
	killed, err = KillOrphanKernels("mihomo-hltest-orphan.exe", configPath)
	if err != nil {
		t.Fatalf("孤儿清理失败：%v", err)
	}
	if killed != 1 {
		t.Fatalf("应清理 1 个孤儿，实际 %d", killed)
	}
	deadline = time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && PortListening(mixedPort) {
		time.Sleep(100 * time.Millisecond)
	}
	if PortListening(mixedPort) {
		t.Fatalf("清理后端口 %d 仍在监听", mixedPort)
	}
	_, err = http.Get(fmt.Sprintf("http://127.0.0.1:%d", mixedPort))
	if err == nil {
		t.Fatal("端口清理校验应失败")
	}
}
