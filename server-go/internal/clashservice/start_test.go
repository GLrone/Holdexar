package clashservice

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"strings"
	"testing"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

// stubFetch 为各候选装配在线拉取行为（缺省候选 = 在线失败）。
func stubFetch(svc *Service, byID map[int64]func() (*SubscriptionFetch, error)) {
	svc.FetchOnline = func(ctx context.Context, id int64, url string) (*SubscriptionFetch, error) {
		if fn, ok := byID[id]; ok {
			return fn()
		}
		return nil, fmt.Errorf("下载订阅失败：所有通道均未成功")
	}
}

func fetchOK(raw string) (*SubscriptionFetch, error) {
	return &SubscriptionFetch{Raw: []byte(raw), Title: "机场名", Userinfo: "upload=1; download=2; total=100", Nodes: 2}, nil
}

func fetchFail() (*SubscriptionFetch, error) {
	return nil, fmt.Errorf("下载订阅失败：所有通道均未成功")
}

// ─── 端口选项三态 ────────────────────────────────────────────

func TestKernelPortOptionsThreeStates(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	ctx := context.Background()

	// ① 未运行 + 无存档 → 动态分配（并把归一化的 auto 写回 KV）
	port, allocate, err := svc.KernelPortOptions(ctx)
	if err != nil || allocate != true || port != 0 {
		t.Fatalf("未运行缺省应动态分配：%d %v %v", port, allocate, err)
	}
	var mode string
	if err := svc.DB.QueryRowContext(ctx, `SELECT CAST(json_extract(value_json,'$') AS TEXT) FROM app_settings WHERE key=?`, portModeKey).Scan(&mode); err != nil || mode != "auto" {
		t.Fatalf("归一化模式未写回：%v %q", err, mode)
	}

	// ② fixed 模式存档口
	if err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if err := settingsWrite(conn, portModeKey, "fixed"); err != nil {
			return err
		}
		return settingsWrite(conn, portKey, 18347)
	}); err != nil {
		t.Fatal(err)
	}
	port, allocate, err = svc.KernelPortOptions(ctx)
	if err != nil || allocate || port != 18347 {
		t.Fatalf("fixed 模式应用存档口：%d %v %v", port, allocate, err)
	}

	// ③ auto + 在跑沿用现口
	if err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settingsWrite(conn, portModeKey, "auto")
	}); err != nil {
		t.Fatal(err)
	}
	kernel.st = runningStatus("")
	port, allocate, err = svc.KernelPortOptions(ctx)
	if err != nil || allocate || port != 18347 {
		t.Fatalf("在跑应沿用现口：%d %v %v", port, allocate, err)
	}

	// ④ fixed 存档口无效 → 回落 auto（写回）+ 未运行动态分配
	if err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		if err := settingsWrite(conn, portModeKey, "fixed"); err != nil {
			return err
		}
		return settingsWrite(conn, portKey, 70000)
	}); err != nil {
		t.Fatal(err)
	}
	kernel.st = clashkernel.Status{}
	port, allocate, err = svc.KernelPortOptions(ctx)
	if err != nil || !allocate || port != 0 {
		t.Fatalf("无效存档口应回落动态分配：%d %v %v", port, allocate, err)
	}
	var modeAfter string
	if err := svc.DB.QueryRowContext(ctx, `SELECT CAST(json_extract(value_json,'$') AS TEXT) FROM app_settings WHERE key=?`, portModeKey).Scan(&modeAfter); err != nil || modeAfter != "auto" {
		t.Fatalf("无效存档口的模式归一化未写回：%v %q", err, modeAfter)
	}
}

func settingsWrite(conn *sql.Conn, key string, value any) error {
	return settings.WriteValue(context.Background(), conn, key, value)
}

// ─── 候选链 ──────────────────────────────────────────────────

func TestResolveOrderAndOnlineFirst(t *testing.T) {
	svc, _, _ := newFixture(t)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "老订阅", "", false)
	seedSubscription(t, svc, 2, "https://sub.example.com/b", "新订阅", "", false)
	seedSubscription(t, svc, 3, "https://sub.example.com/c", "最新订阅", "", false)
	stubFetch(svc, map[int64]func() (*SubscriptionFetch, error){2: func() (*SubscriptionFetch, error) { return fetchOK(subYAML) }})

	// 无显式请求：候选序新→旧 [3,2,1]；3 在线失败被淘汰，2 在线成功即返回
	picked, err := svc.ResolveStartableClash(context.Background(), nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if picked.Subscription.ID != 2 || picked.Cached {
		t.Fatalf("应选中在线成功的 2：%+v", picked.Subscription)
	}
	if len(picked.Attempts) != 1 || picked.Attempts[0].ID != 3 || picked.Attempts[0].Label != "最新订阅" || picked.Attempts[0].Error == "" {
		t.Fatalf("淘汰候选未进 attempts：%+v", picked.Attempts)
	}
	if picked.Title != "机场名" || picked.Nodes != 2 || picked.Userinfo == "" {
		t.Fatalf("候选元数据不符：%+v", picked)
	}
	if _, err := os.Stat(SubscriptionConfigPath(svc.DataDir, 2)); err != nil {
		t.Fatalf("在线取到的原文未写缓存：%v", err)
	}
	if _, err := os.Stat(picked.ConfigPath); err != nil {
		t.Fatalf("运行配置未生成：%v", err)
	}
}

func TestResolveRequestedFirstAndCacheFallback(t *testing.T) {
	svc, _, _ := newFixture(t)
	url := "https://sub.example.com/a"
	seedSubscription(t, svc, 1, url, "老订阅", "", false)
	seedSubscription(t, svc, 2, "https://sub.example.com/b", "新订阅", "", false)
	// 全部在线失败，但 1 有历史成功快照（缓存兜底）
	seedSnapshot(t, svc, 1, url, []byte(subYAML), "缓存面板", "")
	stubFetch(svc, nil)
	ctx := context.Background()

	// 显式请求 2：取不到配置且无缓存 → 淘汰后轮到 1（有缓存，显式请求时命中即用）
	requested := int64(2)
	picked, err := svc.ResolveStartableClash(ctx, &requested, false)
	if err != nil {
		t.Fatal(err)
	}
	if picked.Subscription.ID != 1 || !picked.Cached {
		t.Fatalf("显式请求 2 失败应落到有缓存的 1：%+v", picked.Subscription)
	}
	if len(picked.Attempts) != 1 || picked.Attempts[0].ID != 2 {
		t.Fatalf("attempts 应记录 2 的失败：%+v", picked.Attempts)
	}
	if picked.Title != "缓存面板" {
		t.Fatalf("缓存路径标题应取快照面板名：%q", picked.Title)
	}

	// 无显式请求：候选序 [2,1]，2 失败淘汰、1 缓存命中但留作兜底继续找，最后兜底返回
	picked, err = svc.ResolveStartableClash(ctx, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if picked.Subscription.ID != 1 || !picked.Cached {
		t.Fatalf("缓存兜底应返回 1：%+v", picked.Subscription)
	}
}

func TestResolveAllDeadAggregates(t *testing.T) {
	svc, _, _ := newFixture(t)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)
	seedSubscription(t, svc, 2, "", "乙", "", false) // 无明文 URL（解密失败）
	stubFetch(svc, nil)

	_, err := svc.ResolveStartableClash(context.Background(), nil, false)
	se, detail := statusErrOf(t, err)
	if se.Status != 400 {
		t.Fatalf("全灭应 400：%+v", se)
	}
	if !strings.HasPrefix(detail, "所有可用订阅都取不到配置（") || !strings.Contains(detail, "甲：") {
		t.Fatalf("聚合文案缺候选原因：%s", detail)
	}
}

func TestResolveNoUsableSubs(t *testing.T) {
	svc, _, _ := newFixture(t)
	// 全部废弃（deprecated 列命中）
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", true)
	// 资源封锁（expire 已过）→ 投影为废弃
	seedSubscription(t, svc, 2, "https://sub.example.com/b", "乙", `{"traffic":"upload=0; download=0; total=100; expire=1000000000"}`, false)

	_, err := svc.ResolveStartableClash(context.Background(), nil, false)
	se, detail := statusErrOf(t, err)
	if se.Status != 400 || detail != "没有可用的 Clash 订阅（全部已废弃或尚未添加），请在订阅区手动删除或更换订阅" {
		t.Fatalf("无可用订阅文案不符：%s", detail)
	}
}

func TestResolveKVSelectedUsedAsRequest(t *testing.T) {
	svc, _, _ := newFixture(t)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "老订阅", "", false)
	seedSubscription(t, svc, 2, "https://sub.example.com/b", "新订阅", "", false)
	stubFetch(svc, map[int64]func() (*SubscriptionFetch, error){2: func() (*SubscriptionFetch, error) { return fetchOK(subYAML) }})
	ctx := context.Background()
	if err := svc.RememberSelectedSub(ctx, 1); err != nil {
		t.Fatal(err)
	}

	// KV 选中的 1 在前；1 在线失败且无缓存被淘汰，2 在线成功返回
	picked, err := svc.ResolveStartableClash(ctx, nil, false)
	if err != nil {
		t.Fatal(err)
	}
	if picked.Subscription.ID != 2 || len(picked.Attempts) != 1 || picked.Attempts[0].ID != 1 {
		t.Fatalf("KV 选中应按请求对待：%+v", picked)
	}
}

// ─── 启动链 ──────────────────────────────────────────────────

func TestStartClashBranches(t *testing.T) {
	svc, _, _ := newFixture(t)
	ctx := context.Background()

	// 未装内核
	_, err := svc.StartClash(ctx, nil)
	if se, detail := statusErrOf(t, err); se.Status != 400 || detail != "未找到内核，请先安装" {
		t.Fatalf("未装内核文案不符：%s", detail)
	}
	writeExe(t, svc.DataDir)

	// 无订阅
	_, err = svc.StartClash(ctx, nil)
	if se, detail := statusErrOf(t, err); se.Status != 400 || detail != "尚未保存 Clash 订阅链接，请先添加" {
		t.Fatalf("无订阅文案不符：%s", detail)
	}

	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)

	// 指定不存在的订阅
	missing := int64(9)
	_, err = svc.StartClash(ctx, &missing)
	if se, detail := statusErrOf(t, err); se.Status != 404 || detail != "指定的订阅不存在" {
		t.Fatalf("订阅不存在应 404：%s", detail)
	}

	// 指定废弃订阅（文案含 deprecatedReason）
	seedSubscription(t, svc, 2, "https://sub.example.com/b", "乙", "", false)
	if err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET deprecated=1, deprecated_reason=? WHERE id=2`, "节点大量失效")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	deprecated := int64(2)
	_, err = svc.StartClash(ctx, &deprecated)
	if se, detail := statusErrOf(t, err); se.Status != 400 || !strings.Contains(detail, "该订阅已废弃（节点大量失效）") || !strings.Contains(detail, "手动删除该订阅") {
		t.Fatalf("废弃订阅文案不符：%s", detail)
	}
	// 无理由的废弃行回落固定文案
	if err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET deprecated_reason=NULL WHERE id=2`)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	_, err = svc.StartClash(ctx, &deprecated)
	if se, detail := statusErrOf(t, err); se.Status != 400 || !strings.Contains(detail, "不可用节点超过 95%") {
		t.Fatalf("废弃订阅缺省理由不符：%s", detail)
	}
}

func TestStartClashSuccessChain(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	writeExe(t, svc.DataDir)
	svc.VersionProbe = func(string) string { return "mihomo v1.19.32" }
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "", "", false)
	stubFetch(svc, map[int64]func() (*SubscriptionFetch, error){1: func() (*SubscriptionFetch, error) { return fetchOK(subYAML) }})
	ctx := context.Background()

	var firstChecks []int64
	svc.FirstCheck = func(subID int64, probeAll bool) {
		if !probeAll {
			t.Fatalf("启动首检应全量探测")
		}
		firstChecks = append(firstChecks, subID)
	}

	result, err := svc.StartClash(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Running || !result.Started || result.Reloaded || result.UsedCache {
		t.Fatalf("启动结果标记不符：%+v", result)
	}
	if result.Subscription.ID != 1 || result.FallbackFrom != nil || result.Nodes == nil || *result.Nodes != 2 {
		t.Fatalf("启动响应元数据不符：%+v %+v", result.Subscription, result.FallbackFrom)
	}
	if len(kernel.startOpts) != 1 {
		t.Fatalf("未运行应走 Start：%+v", kernel.startOpts)
	}
	// 规格风险 5：订阅内核 Start 必须 InjectLanes=true（Python 缺省 true，Go 零值 false）
	opts := kernel.startOpts[0]
	if !opts.InjectLanes || opts.SubscriptionURL != "https://sub.example.com/a" {
		t.Fatalf("Start 选项缺 lane 注入或订阅归属：%+v", opts)
	}

	// 收尾账目：selected KV / 走线日志 / 导入回填 / 名称链
	if got := svc.SelectedSubID(ctx); got == nil || *got != 1 {
		t.Fatalf("启动成功应落 selected KV：%v", got)
	}
	var events int
	if err := svc.DB.QueryRowContext(ctx, `SELECT COUNT(*) FROM proxy_events WHERE kind='clash' AND target='subscription' AND proxy_label=?`, "clash:18347").Scan(&events); err != nil || events != 1 {
		t.Fatalf("走线日志未落账：%v %d", err, events)
	}
	var imported sql.NullString
	if err := svc.DB.QueryRowContext(ctx, `SELECT CAST(last_imported_at AS TEXT), CAST(last_stats AS TEXT) FROM proxy_subscriptions WHERE id=1`).Scan(&imported, &imported); err != nil || !imported.Valid {
		t.Fatalf("导入回填未落库：%v", err)
	}
	var label string
	if err := svc.DB.QueryRowContext(ctx, `SELECT label FROM proxy_subscriptions WHERE id=1`).Scan(&label); err != nil || label != "机场名" {
		t.Fatalf("名称链应取机场名：%v %q", err, label)
	}
	var stats string
	if err := svc.DB.QueryRowContext(ctx, `SELECT CAST(last_stats AS TEXT) FROM proxy_subscriptions WHERE id=1`).Scan(&stats); err != nil ||
		!strings.Contains(stats, `"traffic"`) || !strings.Contains(stats, `"cached"`) {
		t.Fatalf("导入回填未按整体替换落库：%v %s", err, stats)
	}
	if len(firstChecks) != 1 || firstChecks[0] != 1 {
		t.Fatalf("首检挂点未触发：%v", firstChecks)
	}
}

func TestStartClashKernelFailure502(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	writeExe(t, svc.DataDir)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)
	stubFetch(svc, map[int64]func() (*SubscriptionFetch, error){1: func() (*SubscriptionFetch, error) { return fetchOK(subYAML) }})
	kernel.startFn = func(clashkernel.StartOptions) (clashkernel.Status, error) {
		return clashkernel.Status{}, errors.New("代理入口未由内核进程监听：18347")
	}

	_, err := svc.StartClash(context.Background(), nil)
	if se, detail := statusErrOf(t, err); se.Status != 502 || !strings.HasPrefix(detail, "内核启动失败: ") {
		t.Fatalf("内核启动失败应 502：%+v", se)
	}
}

func TestStartClashFallbackFrom(t *testing.T) {
	svc, _, _ := newFixture(t)
	writeExe(t, svc.DataDir)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "老订阅", "", false)
	seedSubscription(t, svc, 2, "https://sub.example.com/b", "新订阅", "", false)
	stubFetch(svc, map[int64]func() (*SubscriptionFetch, error){2: func() (*SubscriptionFetch, error) { return fetchOK(subYAML) }})

	// 显式请求 1：1 在线失败且无缓存被淘汰，降级到 2 启动（fallbackFrom 指向 1）
	requested := int64(1)
	result, err := svc.StartClash(context.Background(), &requested)
	if err != nil {
		t.Fatal(err)
	}
	if result.FallbackFrom == nil || result.FallbackFrom.ID != 1 || result.FallbackFrom.Label == nil || *result.FallbackFrom.Label != "老订阅" {
		t.Fatalf("fallbackFrom 应指向被淘汰的请求候选：%+v", result.FallbackFrom)
	}
	if result.Subscription.ID != 2 {
		t.Fatalf("实际启动的订阅应为 2：%+v", result.Subscription)
	}
}

// ─── 切换链 ──────────────────────────────────────────────────

func TestSwitchClashNotRunning(t *testing.T) {
	svc, _, _ := newFixture(t)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)

	_, err := svc.SwitchClash(context.Background(), 1)
	if se, detail := statusErrOf(t, err); se.Status != 400 || detail != "Clash 未运行：点「启动」拉起内核，启动时将使用选中的订阅" {
		t.Fatalf("未运行文案不符：%s", detail)
	}
}

func TestSwitchClashIdempotent(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)
	kernel.st = runningStatus("")
	kernel.runningSub = "https://sub.example.com/a"

	result, err := svc.SwitchClash(context.Background(), 1)
	if err != nil {
		t.Fatal(err)
	}
	if result.Switched || !result.Running || result.Port == nil || *result.Port != 18347 {
		t.Fatalf("同订阅应幂等 switched=false：%+v", result)
	}
	if len(kernel.ensureOpts) != 0 || len(kernel.startOpts) != 0 {
		t.Fatalf("幂等分支不应触碰内核：%+v %+v", kernel.startOpts, kernel.ensureOpts)
	}
}

func TestSwitchClashSuccessHotReload(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	writeExe(t, svc.DataDir)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)
	kernel.st = runningStatus("")
	kernel.runningSub = "https://sub.example.com/old"
	ctx := context.Background()

	var firstChecks []int64
	svc.FirstCheck = func(subID int64, probeAll bool) {
		if !probeAll {
			t.Fatalf("切换首检应全量探测")
		}
		firstChecks = append(firstChecks, subID)
	}

	// 缓存缺失：在线拉取补缓存
	stubFetch(svc, map[int64]func() (*SubscriptionFetch, error){1: func() (*SubscriptionFetch, error) { return fetchOK(subYAML) }})
	result, err := svc.SwitchClash(ctx, 1)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Switched || !result.Reloaded || result.Started {
		t.Fatalf("切换应热重载（进程不动）：%+v", result)
	}
	if len(kernel.ensureOpts) != 1 || kernel.ensureOpts[0].SubscriptionURL != "https://sub.example.com/a" {
		t.Fatalf("切换应走 EnsureRunning（在跑）：%+v", kernel.ensureOpts)
	}
	if _, err := os.Stat(SubscriptionConfigPath(svc.DataDir, 1)); err != nil {
		t.Fatalf("切换下载的原文未写缓存：%v", err)
	}
	if got := svc.SelectedSubID(ctx); got == nil || *got != 1 {
		t.Fatalf("切换成功应落 selected KV：%v", got)
	}
	if len(firstChecks) != 1 || firstChecks[0] != 1 {
		t.Fatalf("切换首检挂点未触发：%v", firstChecks)
	}
}

func TestSwitchClashReloadRejected400(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	writeExe(t, svc.DataDir)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)
	kernel.st = runningStatus("")
	kernel.runningSub = "https://sub.example.com/old"
	// 预置缓存（跳过下载）
	if err := os.WriteFile(SubscriptionConfigPath(svc.DataDir, 1), []byte(subYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	kernel.ensureFn = func(clashkernel.EnsureRunningOptions) (clashkernel.EnsureRunResult, error) {
		return clashkernel.EnsureRunResult{}, &clashkernel.ReloadRejectedError{Detail: "内核拒绝新配置（HTTP 400）"}
	}

	_, err := svc.SwitchClash(context.Background(), 1)
	if se, detail := statusErrOf(t, err); se.Status != 400 || !strings.Contains(detail, "内核拒绝新配置") {
		t.Fatalf("拒载应 400：%+v", se)
	}
	// 运行态保留（拒载不清账）
	if !kernel.Status().Running {
		t.Fatalf("拒载不应停内核")
	}
}

func TestSwitchClashFailure502(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	writeExe(t, svc.DataDir)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)
	kernel.st = runningStatus("")
	kernel.runningSub = "https://sub.example.com/old"
	if err := os.WriteFile(SubscriptionConfigPath(svc.DataDir, 1), []byte(subYAML), 0o600); err != nil {
		t.Fatal(err)
	}
	kernel.ensureFn = func(clashkernel.EnsureRunningOptions) (clashkernel.EnsureRunResult, error) {
		return clashkernel.EnsureRunResult{}, errors.New("控制器通路故障")
	}

	_, err := svc.SwitchClash(context.Background(), 1)
	if se, detail := statusErrOf(t, err); se.Status != 502 || !strings.HasPrefix(detail, "切换失败: ") {
		t.Fatalf("通路故障应 502：%+v", se)
	}
}

func TestSwitchClashDeprecatedAndMissing(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", true)
	kernel.st = runningStatus("")

	_, err := svc.SwitchClash(context.Background(), 1)
	if se, detail := statusErrOf(t, err); se.Status != 400 || !strings.Contains(detail, "后端不再使用") || strings.Contains(detail, "手动删除该订阅") {
		t.Fatalf("切换的废弃文案应短版（无恢复引导）：%s", detail)
	}
	missing := int64(9)
	_, err = svc.SwitchClash(context.Background(), missing)
	if se, _ := err.(*StatusError); se == nil || se.Status != 404 {
		t.Fatalf("切换订阅不存在应 404：%v", err)
	}
}

// ─── 点选 / 停止 ─────────────────────────────────────────────

func TestSelectClashRoundtrip(t *testing.T) {
	svc, _, _ := newFixture(t)
	seedSubscription(t, svc, 1, "https://sub.example.com/a", "甲", "", false)
	ctx := context.Background()

	selected, err := svc.SelectClash(ctx, 1)
	if err != nil || selected != 1 {
		t.Fatalf("点选返回不符：%d %v", selected, err)
	}
	if got := svc.SelectedSubID(ctx); got == nil || *got != 1 {
		t.Fatalf("点选未落 KV：%v", got)
	}
	missing := int64(9)
	_, err = svc.SelectClash(ctx, missing)
	if se, _ := err.(*StatusError); se == nil || se.Status != 404 {
		t.Fatalf("点选不存在的订阅应 404：%v", err)
	}
}

func TestStopClashClearsRuntime(t *testing.T) {
	svc, kernel, _ := newFixture(t)
	kernel.st = runningStatus("x")
	kernel.runningSub = "https://sub.example.com/a"

	result := svc.StopClash()
	if kernel.stopCount != 1 {
		t.Fatalf("Stop 未触达内核实例")
	}
	if result.Running || result.Port != nil || result.SubscriptionURL != nil {
		t.Fatalf("停止后运行态应清账：%+v", result)
	}
	// configPath 保留（对齐 Python stop 清账清单）
	if result.ConfigPath == nil || *result.ConfigPath != "x" {
		t.Fatalf("停止后 configPath 应保留：%+v", result)
	}
}

// ─── 名称三档链 ──────────────────────────────────────────────

func TestApplySubscriptionNameChain(t *testing.T) {
	svc, _, _ := newFixture(t)
	ctx := context.Background()

	seedSubscription(t, svc, 1, "https://sub.example.com/a", "", "", false)
	name, err := svc.ApplySubscriptionNameChain(ctx, 1, "机场名")
	if err != nil || name != "机场名" {
		t.Fatalf("无名行应取机场名：%q %v", name, err)
	}
	name, _ = svc.ApplySubscriptionNameChain(ctx, 1, "机场改名")
	if name != "机场改名" {
		t.Fatalf("panel 来源应跟随机场改名：%q", name)
	}

	// 用户命名锁死
	if err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET label=?, last_stats=? WHERE id=1`, "我起的名", `{"labelSource":"user"}`)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	name, _ = svc.ApplySubscriptionNameChain(ctx, 1, "机场又改名")
	if name != "我起的名" {
		t.Fatalf("用户命名不应被机场名覆盖：%q", name)
	}

	// 无标记 + 名字≠机场名 → 视为用户命名（老数据归位）锁死
	if err := svc.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET label=?, last_stats=NULL WHERE id=1`, "旧自动名")
		return err
	}); err != nil {
		t.Fatal(err)
	}
	name, _ = svc.ApplySubscriptionNameChain(ctx, 1, "机场名")
	if name != "旧自动名" {
		t.Fatalf("老数据不等机场名应视为用户命名锁死：%q", name)
	}

	// 名字==机场名 + 无标记 → 补 panel 标记
	name, _ = svc.ApplySubscriptionNameChain(ctx, 1, "旧自动名")
	if name != "旧自动名" {
		t.Fatalf("同名应保持：%q", name)
	}
	var stats string
	if err := svc.DB.QueryRowContext(ctx, `SELECT CAST(last_stats AS TEXT) FROM proxy_subscriptions WHERE id=1`).Scan(&stats); err != nil || !strings.Contains(stats, `"labelSource":"panel"`) {
		t.Fatalf("同名历史行应补 panel 标记：%v %s", err, stats)
	}
}

// ─── 在线拉取默认实现（SyncOne 真链路 + 归属守卫）────────────

func TestFetchOnlineDefaultGuardAndFlow(t *testing.T) {
	svc, _, _ := newFixture(t)
	url := "https://sub.example.com/a"
	seedSubscription(t, svc, 1, url, "甲", "", false)
	ctx := context.Background()

	// 归属守卫：URL 与库内不一致 → 400
	if _, err := svc.fetchOnlineDefault(ctx, 1, "https://sub.example.com/other"); err == nil || !strings.Contains(err.Error(), "订阅地址与缓存归属不一致") {
		t.Fatalf("默认在线拉取缺归属守卫：%v", err)
	}

	// SyncOne 真链路：没有可达通道 → 失败（非 nil 错误）
	if _, err := svc.fetchOnlineDefault(ctx, 1, url); err == nil {
		t.Fatalf("无通道时应失败")
	}
}
