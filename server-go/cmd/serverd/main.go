// serverd：服务骨架入口。数据目录判定见 internal/config。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"slices"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/achievements"
	"holdexar/server-go/internal/agent"
	"holdexar/server-go/internal/alerts"
	"holdexar/server-go/internal/backup"
	"holdexar/server-go/internal/bills"
	"holdexar/server-go/internal/boards"
	"holdexar/server-go/internal/bundles"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/clashservice"
	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/contentsched"
	"holdexar/server-go/internal/crawl"
	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/devwatch"
	"holdexar/server-go/internal/epicfree"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/eventstream"
	"holdexar/server-go/internal/family"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/httpapi"
	"holdexar/server-go/internal/humblechoice"
	"holdexar/server-go/internal/instance"
	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/logredact"
	"holdexar/server-go/internal/metadata"
	"holdexar/server-go/internal/middleware"
	"holdexar/server-go/internal/migration"
	"holdexar/server-go/internal/miniseed"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/pilot"
	"holdexar/server-go/internal/plugins"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/rates"
	"holdexar/server-go/internal/redeem"
	"holdexar/server-go/internal/regions"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/steamevents"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/systemx"
	"holdexar/server-go/internal/updateguard"
	"holdexar/server-go/internal/updater"
	"holdexar/server-go/internal/wishlist"
)

var restartRequested atomic.Bool

// processStartedAt 进程启动锚点：收尸只认上一进程遗留的未终态运行，
// 开门后新建的是活运行不得误收敛。
var processStartedAt = time.Now()

// injectedPassphrase 服务器场景的口令免交互注入源：HOLDEXAR_PASSPHRASE
// （systemd 部署经 EnvironmentFile=/etc/holdexar/holdexar.env 0600 下发）。
// 未设置返回空串＝无免交互通道，凭据保持锁定（可在设置页手动解锁）。
func injectedPassphrase() string {
	return strings.TrimSpace(os.Getenv("HOLDEXAR_PASSPHRASE"))
}

func main() {
	// 恢复换库后的重启交棒：清理级联走完（含代理子进程回收）后以 75 退出，
	// 窗口宿主/开发监视据此重启服务（跨进程契约，见 internal/systemx/data.go）。
	defer func() {
		if restartRequested.Load() {
			os.Exit(systemx.RestartExitCode)
		}
	}()
	addr := flag.String("addr", config.ServerAddr(), "监听地址（默认回环；HOLDEXAR_HOST 可回退绑定面）")
	dataDirFlag := flag.String("data-dir", "", "显式数据目录，跳过自动判定")
	devSession := flag.Bool("dev-session", false, "开放开发态会话发放通道（发行版无此入口）")
	mcpEnabled := flag.Bool("mcp", false, "启用 MCP 面（等价环境变量 HOLDEXAR_MCP=1）")
	bootstrapFileFlag := flag.String("bootstrap-file", "", "健康就绪后写入一次性引导票据的文件路径（桌面壳桥接，父目录由调用方创建）")
	flag.Parse()
	lifetime, stopLifetime := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stopLifetime()
	if os.Getenv(devwatch.EnvChild) == "1" {
		go func() { _, _ = io.Copy(io.Discard, os.Stdin); stopLifetime() }()
	}
	if root := os.Getenv(devwatch.EnvWatch); root != "" {
		executable, err := os.Executable()
		if err == nil {
			err = devwatch.Run(lifetime, root, executable, os.Args[1:], os.Stderr)
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		return
	}

	// 日志接线点：stderr 之外同步写入环形缓冲（日志页 SSE 的回放事实源）。
	// 行格式对齐 py HumanFormatter：HH:MM:SS [结果词] 模块中文名 · 主句——
	// 前端日志页按行首结果词分色（失败/严重→error，注意→warning，其余 info）；
	// 模块中文名服务端面统一「服务端」。出口统一过脱敏链（Ring 写入前另有
	// 同链兜底，双过幂等）。
	bus := events.NewBus()
	ring := eventstream.NewRing(2000)
	logLineAt := func(level string, format string, args ...any) {
		msg := fmt.Sprintf(format, args...)
		line := time.Now().Format("15:04:05") + " [" + level + "] 服务端 · " + msg
		line = logredact.Redact(line)
		fmt.Fprintln(os.Stderr, line)
		ring.Write(line)
	}
	logLine := func(format string, args ...any) { logLineAt("信息", format, args...) }
	logFail := func(format string, args ...any) { logLineAt("失败", format, args...) }

	if *dataDirFlag != "" {
		os.Setenv(config.EnvPrefix+"DATA_DIR", *dataDirFlag)
	}
	dataDir, err := config.ResolveDataDir()
	if err != nil {
		logFail("%v", err)
		os.Exit(1)
	}
	// 生产单实例：同一数据目录只允许一个后台服务进程（先于升级验收授权）。
	releaseInstance, err := instance.AcquireProductionLock(dataDir)
	if err != nil {
		logFail("%v", err)
		os.Exit(1)
	}
	defer releaseInstance()
	distDir, err := config.WebDistDir()
	if err != nil {
		logFail("%v", err)
		os.Exit(1)
	}

	// 升级验收授权：env+标记+登记窗口逐项校验；失败即拒绝触碰受保护数据
	updateToken, err := updateguard.Activate(dataDir)
	if err != nil {
		logFail("已停止启动以保护数据：%v", err)
		os.Exit(1)
	}
	dbPath := filepath.Join(dataDir, "holdexar.db")
	logLine("正在准备应用数据")
	db, err := migration.PrepareDatabaseAuthorized(lifetime, dbPath, updateToken)
	if err != nil {
		logFail("%v", err)
		os.Exit(1)
	}
	defer db.Close()
	logLine("应用数据已就绪")
	writeGate := store.NewWriteGate(db, dbPath)
	backgroundCtx, stopBackground := context.WithCancel(lifetime)
	defer stopBackground()
	// 局域网访问模式初始化：库就绪后、路由装配前（Python init_db 后同位）；
	// 失败按关闭处理（fail-closed），不影响正常启动。
	initLANAccess(backgroundCtx, db, writeGate, dataDir, logLine, logFail)
	// 密钥免交互注入（服务器场景）：HOLDEXAR_PASSPHRASE_FILE（systemd
	// LoadCredential 挂载路径，文件内容即口令）优先于 HOLDEXAR_PASSPHRASE。
	// 解锁失败不阻断启动（fail-closed：应用照常运行、凭据面保持锁定，
	// 可在设置页手动解锁）；仅口令模式有意义，其余模式静默跳过。
	if injected := injectedPassphrase(); injected != "" {
		if mode, _ := secretbox.ModeOf(dataDir); mode == secretbox.ModePassphrase {
			unlocked, err := secretbox.Unlock(dataDir, injected)
			switch {
			case err != nil:
				logLine("密钥自动解锁失败，凭据保持锁定 │ 原因=%v", err)
			case unlocked:
				logLine("密钥已解锁（凭据注入通道）")
			default:
				logLine("密钥自动解锁未通过（口令不符），凭据保持锁定")
			}
		}
	}
	// 换钥续跑：崩溃遗留的过渡记录在此收尾（Python 同语义：无验收令牌立即
	// 续跑；验收期等验收提交后再跑，不在受保护窗口动库）。失败只记日志
	// 保留过渡记录，启动不中断。
	resumeKeys := func() {
		resume, err := secretbox.ResumePendingMigration(backgroundCtx, writeGate, dataDir, "")
		if err != nil {
			logFail("密钥换装收尾失败，保留过渡记录待下次续跑：%v", err)
			return
		}
		if !resume.Resumed {
			if resume.Reason == "过渡记录需要口令解锁" {
				logLine("存在未完成的密钥换装，需解锁后才能继续")
			}
			return
		}
		migrated := resume.Counts.AccountCookies + resume.Counts.SettingSecrets + resume.Counts.Subscriptions + resume.Counts.Snapshots + resume.Counts.ProxyCredentials
		if migrated > 0 {
			logLine("上次中断的密钥换装已续完 │ 迁移条数=%d", migrated)
		} else {
			logLine("上次密钥换装未生效，已清理过渡记录")
		}
	}
	if !updateguard.Active() {
		resumeKeys()
	}
	jobRecorder := jobrun.New(db, writeGate, time.Now, logLine)
	walAutomation := &contentsched.Automation{
		Job: "wal_truncate", Label: "数据库日志收缩", Daily: sched.DailyTrigger{Hour: 4, Minute: 30},
		Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) {
			if err := occupancy.BeginMaintenance(occupancy.MaintenanceStorage); err != nil {
				return map[string]any{"skipped": "正在更新数据，已跳过数据库日志收缩"}, nil
			}
			defer occupancy.EndMaintenance()
			return writeGate.TruncateWAL(ctx, dbPath)
		},
	}
	if err := jobRecorder.Recover(backgroundCtx); err != nil {
		logFail("恢复自动任务记录失败：%v", err)
		os.Exit(1)
	}
	installRoot, err := config.InstallRoot()
	if err != nil {
		logFail("%v", err)
		os.Exit(1)
	}
	proxySchedule := newProxySchedule(db, writeGate, dataDir, filepath.Join(installRoot, "assets", "clash"), jobRecorder, logLine)
	// 订阅内核：与池内核结构性分离的第二个 Runtime（共用 exe，-d 工作目录不同——
	// 订阅=data/clash，池=data/proxypool；共用实例会让一次池重建停掉订阅内核）
	subKernel := clashkernel.Runtime{}
	clashSvc := clashservice.New(db, writeGate, &subKernel, filepath.Join(installRoot, "assets", "clash"), dataDir)
	clashSvc.Logf = logLine
	clashSvc.Channels = func() []proxypool.Channel { return proxySchedule.subscriptionChannels(context.Background()) }
	seedAutomation := &contentsched.Automation{
		Job: miniseed.Job, Label: "基础资料同步", Interval: 30 * time.Minute, FirstAfter: 25 * time.Second,
		Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) {
			path, err := miniseed.Find(installRoot)
			if err != nil {
				return nil, err
			}
			result, err := miniseed.Import(ctx, db, writeGate, path, dataDir)
			if err != nil {
				return nil, err
			}
			if result.Skipped != "" {
				return map[string]any{"skipped": result.Skipped}, nil
			}
			logLine("基础资料已导入 │ 版本=%s，数据截至=%s，目录=%d，人工资料=%d，捆绑包=%d",
				result.Version, result.AsOf, result.Games, result.Curated, result.Bundles)
			return map[string]any{"version": result.Version, "asOf": result.AsOf, "games": result.Games, "curated": result.Curated, "bundles": result.Bundles}, nil
		},
	}
	proxyRetention := &contentsched.Automation{
		Job: proxypool.RetentionJob, Label: "代理池清理", Daily: sched.DailyTrigger{Hour: 4, Minute: 35},
		Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) {
			return proxypool.PruneTelemetry(ctx, db, writeGate, dataDir, time.Now())
		},
	}
	bundleCounts := bundles.NewCounts(db, writeGate)
	bundleAutomation := &contentsched.Automation{
		Job: bundles.CountsJob, Label: "捆绑包记录", Source: "bundle_counts", Daily: sched.DailyTrigger{Hour: 5, Minute: 40},
		FirstAfter: 90 * time.Second, DB: db, Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) { return bundleCounts.Refresh(ctx, false) },
	}
	proxySchedule.cycle.Bus = bus
	subscriptionSchedule := newSubscriptionSchedule(proxySchedule)
	rateService := rates.NewService(db, writeGate, rates.ServiceOptions{Log: logLine, Recorder: jobRecorder, Revalue: bills.RevalueAffected})
	// Steam 出站走策略引擎（GET /proxies/resolve 同链）：裸系统代理解析在
	// 系统代理关闭时直连 Steam，续期二跳（store/community 域）必败。
	kernelPort := func() (bool, int) {
		status := subKernel.Status()
		return status.Running, status.Port
	}
	steamProxy := func(ctx context.Context) (string, error) {
		proxyURL, _, err := proxypool.ResolveStrategyProxy(ctx, db, writeGate, dataDir, kernelPort)
		if err != nil {
			return "", nil
		}
		return proxyURL, nil
	}
	boardClient := boards.New(boards.Options{Log: logLine, Proxy: steamProxy})
	monitoringService := monitoring.New(db, writeGate)
	monitoringService.Log = logLine
	bundleSvc := bundles.NewService(db, writeGate, monitoringService)
	bundleSvc.LoadOverrides = func() (map[string][]int64, []string) { return derived.LoadSeriesOverrides(dataDir) }
	bundleSvc.Logf = logLine
	derived.RefreshBundleSortCache = bundleSvc.RefreshSortCacheHook
	pool := wishlist.NewPool(db, writeGate, monitoringService)
	pool.Log = logLine
	steamClient := account.NewSteamClient(account.SteamOptions{Proxy: steamProxy})
	steamAPIKey := func(ctx context.Context) (string, error) {
		return settings.ReadSecret(ctx, db, dataDir, "account.steam_api_key")
	}
	var wishlistService *wishlist.Service
	var familyService *family.Service
	var billService *bills.Service
	var steamLists *wishlist.Steam
	var redeemService *redeem.Service
	accountService := account.NewService(backgroundCtx, db, writeGate, account.ServiceOptions{DataDir: dataDir, Steam: steamClient, Recorder: jobRecorder, Log: logLine, Pool: pool, APIKey: steamAPIKey, Summaries: func(ctx context.Context, ids []string, key string) (map[string]wishlist.PlayerSummary, error) {
		return steamLists.PlayerSummaries(ctx, "", ids, key)
	}, RedeemUsed: func(steamID string) int {
		if redeemService == nil {
			return 0
		}
		return redeemService.AccountUsage(steamID)
	}, AfterBind: func(sid string) {
		wishlistService.RequestSync(sid, true)
		familyService.RequestSync(sid)
		billService.RequestSync(sid)
	}})
	billService = bills.NewService(backgroundCtx, db, writeGate, bills.SyncOptions{Accounts: accountService, Steam: steamClient, Recorder: jobRecorder, Log: logLine})
	redeemService = redeem.NewService(db, writeGate, redeem.ServiceOptions{Accounts: accountService, Proxy: steamProxy, Log: logLine})
	if _, err := accountService.MigrateLegacyKV(backgroundCtx); err != nil {
		logFail("旧账号设置接管未完成，原数据已保留：%v", err)
	}
	if _, err := accountService.SealCredentials(backgroundCtx); err != nil {
		logFail("账号凭据封存未完成，保留兼容读取：%v", err)
	}
	if count, err := settings.SealSecrets(backgroundCtx, db, writeGate, dataDir); err != nil {
		logFail("设置凭据封存未完成，保留兼容读取：%v", err)
	} else if count > 0 {
		logLine("存量设置凭据已加密保存 │ 条数=%d", count)
	}
	// 无可用代理出口时回退默认直连（降级提示走注意级）。
	maybeRevertToDirect(backgroundCtx, db, writeGate, bus, func(format string, args ...any) {
		logLineAt("注意", format, args...)
	})
	// 节点凭证明文落盘约束：权限收紧（Windows 走用户目录 ACL，no-op）+
	// 崩溃残留清扫（此刻内核尚未启动，运行配置可安全清除——总是从原文
	// 缓存重渲染）。
	if tightened := clashservice.TightenAll(dataDir); tightened > 0 {
		logLine("节点凭证明文权限已收紧 │ 文件数=%d", tightened)
	}
	if swept := clashservice.SweepTransient(dataDir); swept > 0 {
		logLine("已清理可重建的节点配置明文件 │ 条数=%d", swept)
	}
	// 启动链收尸：重启前遗留的未终态运行一律收敛（失败不中断启动）。
	if converged, err := agent.ReconcileOrphanRuns(backgroundCtx, db, writeGate, processStartedAt); err != nil {
		logFail("启动收尸暂未完成：%v", err)
	} else if converged > 0 {
		logLine("启动收尸：%d 条重启前遗留的未完成运行已收敛为已取消", converged)
	}
	var priceRuntime *crawl.Runtime
	steamEvents := steamevents.New(db, writeGate, steamevents.Options{DataDir: dataDir, Recorder: jobRecorder, Log: logLine})
	if err := steamEvents.Windows.Reload(backgroundCtx); err != nil {
		logFail("Steam 活动窗口暂无法载入：%v", err)
	}
	var epicService *epicfree.Service
	var deliveryService *notifications.DeliveryService
	var achievementService *achievements.SyncService
	var pilotService *pilot.Service
	// 内部模块开关登记：名称与提示由后端下发，前端按 id 渲染不加白名单。
	var humbleService *humblechoice.Service
	type builtinModule struct {
		id      string
		name    string
		state   func(context.Context) (bool, bool, error)
		enabled func(context.Context, bool) error
		key     string
		logName string
	}
	builtinModules := []builtinModule{
		{key: epicfree.ModuleEnabledKey, id: "core.epic_free", name: "Epic 免费游戏", logName: "Epic 免费游戏",
			state:   func(ctx context.Context) (bool, bool, error) { return epicService.State(ctx) },
			enabled: func(ctx context.Context, on bool) error { return epicService.SetEnabled(ctx, on) }},
		{key: humblechoice.ModuleEnabledKey, id: "core.humble_choice", name: "Humble Choice 每月合集", logName: "Humble Choice",
			state:   func(ctx context.Context) (bool, bool, error) { return humbleService.State(ctx) },
			enabled: func(ctx context.Context, on bool) error { return humbleService.SetEnabled(ctx, on) }},
		{key: achievements.ModuleEnabledKey, id: "core.achievements", name: "成就同步", logName: "成就",
			state:   func(ctx context.Context) (bool, bool, error) { return achievementService.State(ctx) },
			enabled: func(ctx context.Context, on bool) error { return achievementService.SetEnabled(ctx, on) }},
		{key: pilot.ModuleEnabledKey, id: "core.pilot", name: "领航员", logName: "领航员",
			state:   func(ctx context.Context) (bool, bool, error) { return pilotService.State(ctx) },
			enabled: func(ctx context.Context, on bool) error { return pilotService.SetEnabled(ctx, on) }},
	}
	pluginManager := plugins.New(db, writeGate, dataDir, plugins.Options{
		Settings: settings.Options{Bus: bus},
		BuiltinList: func(ctx context.Context) (any, error) {
			out := make([]map[string]any, 0, len(builtinModules))
			for _, mod := range builtinModules {
				enabled, running, err := mod.state(ctx)
				if err != nil {
					return nil, err
				}
				out = append(out, map[string]any{"id": mod.id, "name": mod.name, "enabled": enabled, "running": running})
			}
			return out, nil
		},
		BuiltinSet: func(ctx context.Context, id string, enabled bool) error {
			for _, mod := range builtinModules {
				if mod.id != id {
					continue
				}
				if err := mod.enabled(ctx, enabled); err != nil {
					return err
				}
				bus.Publish("settings.changed", events.KV{Key: "keys", Val: []string{mod.key}})
				logLine("%s功能状态已更新 │ 开启=%t", mod.logName, enabled)
				return nil
			}
			return errors.New("built-in feature is unknown")
		},
		Test: func(ctx context.Context, id string) error { return deliveryService.TestPlugin(ctx, id) },
	})
	deliveryService = notifications.NewDelivery(db, writeGate, notifications.DeliveryOptions{Plugins: pluginManager, DataDir: dataDir, Log: logLine, Watched: func(ctx context.Context) ([]int64, error) { return priceRuntime.PoolAppIDs(ctx) }})
	dispatchPending := func(ctx context.Context) error { _, e := deliveryService.Dispatch(ctx, nil); return e }
	alertService := alerts.New(db, writeGate, alerts.Options{Log: logLine, Dispatch: dispatchPending, Proxy: steamProxy})
	backupService := backup.New(db, dbPath, time.Now, writeGate)
	backupAutomation := &contentsched.Automation{
		Job: backup.Job, Label: "自动备份", Interval: 24 * time.Hour, FirstAfter: 5 * time.Minute,
		Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) {
			enabled, err := settings.BackupEnabled(ctx, db)
			if err != nil {
				return nil, err
			}
			if !enabled {
				return map[string]any{"skipped": "自动备份已关闭"}, nil
			}
			result, skipped, err := backupService.Automatic(ctx)
			if err != nil {
				if ctx.Err() == nil {
					err = errors.Join(err, alertService.RecordBackupFailure(ctx, err))
				}
				return nil, err
			}
			if skipped {
				return map[string]any{"skipped": "已有最近备份"}, nil
			}
			logLine("自动备份完成 │ 文件=%s，字节=%d，游戏=%d", result.Name, result.SizeBytes, result.Games)
			return map[string]any{"name": result.Name, "sizeBytes": result.SizeBytes, "games": result.Games}, nil
		},
	}
	manualHealth := &proxypool.ManualHealth{DB: db, Gate: writeGate, DataDir: dataDir}
	healthAutomation := &contentsched.Automation{
		Job: proxypool.ManualHealthJob, Label: "代理体检", Interval: 6 * time.Hour,
		Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) {
			on, err := manualHealth.Enabled(ctx)
			if err != nil {
				return nil, err
			}
			if !on {
				return map[string]any{"skipped": "自动节点体检已关闭"}, nil
			}
			items, probeErr := manualHealth.Check(ctx, 0, true)
			running := proxySchedule.pid.Load() != 0
			var names []string
			if running {
				names = proxypool.PoolFileNames(dataDir)
			}
			created, alertErr := alertService.RecordProxyHealth(ctx, running, names, time.Now())
			if created {
				alertErr = dispatchPending(ctx)
			}
			return map[string]any{"checked": len(items), "alert": created}, errors.Join(probeErr, alertErr)
		},
	}
	epicService = epicfree.New(backgroundCtx, db, writeGate, epicfree.Options{Log: logLine})
	if err := epicService.Initialize(backgroundCtx); err != nil {
		logFail("Epic 功能状态无法读取：%v", err)
		os.Exit(1)
	}
	epicAutomation := &contentsched.Automation{Job: "epic_free", Label: "Epic 免费游戏", Field: "epic_schedule", Key: settings.EpicFreeScheduleKey, DB: db, Recorder: jobRecorder, Log: logLine, Run: func(ctx context.Context) (map[string]any, error) {
		if enabled, err := epicService.Enabled(ctx); err != nil {
			return nil, err
		} else if !enabled {
			return map[string]any{"skipped": "Epic 免费游戏功能已停用"}, nil
		}
		result, err := epicService.Refresh(ctx)
		if err != nil {
			return result, err
		}
		if result["ok"] != true {
			return result, fmt.Errorf("%v", result["error"])
		}
		if err := dispatchPending(ctx); err != nil {
			logFail("Epic 免费游戏提醒暂未送达：%v", err)
		}
		return result, nil
	}}
	priceRuntime = crawl.New(db, writeGate, bus, crawl.Options{Board: boardClient.Get, Recorder: jobRecorder, Log: logLine, EventKey: steamEvents.Windows.KeyAt, ProxyLanes: proxySchedule.Lanes, NetworkBlocked: netenv.NetworkTakenOver, DataDir: dataDir, AfterRepair: dispatchPending, AfterCycle: func(ctx context.Context, id int64) error {
		_, sendErr := deliveryService.Dispatch(ctx, &id)
		_, featuredErr := alertService.CheckFeatured(ctx, nil)
		// 主轮链尾捆绑包刷新：异常只记日志不外溢（对齐 Python 链尾 try/except）
		if _, bundleErr := bundleSvc.RefreshWithEnqueue(ctx); bundleErr != nil {
			logLine("捆绑包链尾刷新未完成 │ 原因=%v", bundleErr)
		}
		return errors.Join(sendErr, featuredErr)
	}, AfterIdle: func(stopped bool) { wishlistService.PricesIdle(stopped) }, AfterJob: func(ctx context.Context, ids []int64) error {
		_, alertErr := alertService.CheckAppIDs(ctx, ids)
		_, featuredErr := alertService.CheckFeatured(ctx, ids)
		_, releaseErr := pool.ReleaseFree(ctx, ids)
		return errors.Join(releaseErr, alertErr, featuredErr)
	}})
	bundleSvc.Discovery = priceRuntime.RunDiscovery
	// 目录导入分类出口注入（games 包；经注入避免 crawl→games→wishlist 生产环）
	priceRuntime.ImportAppids = func(ctx context.Context, appids []any) (map[string]any, error) {
		return games.ImportAppids(ctx, db, appids)
	}
	jobRetention := &contentsched.Automation{
		Job: jobrun.RetentionJob, Label: "运行记录清理", Daily: sched.DailyTrigger{Hour: 4, Minute: 36},
		Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) {
			jobRuns, jobCapped, err := jobRecorder.Prune(ctx)
			summary := map[string]any{"job_runs": jobRuns, "truncated": jobCapped}
			if err != nil {
				return summary, err
			}
			crawlJobs, crawlCapped, err := priceRuntime.PruneJobs(ctx)
			summary["crawl_jobs"], summary["truncated"] = crawlJobs, jobCapped || crawlCapped
			if jobRuns > 0 || crawlJobs > 0 {
				logLine("运行记录清理 │ 自动任务=%d，抓取任务=%d", jobRuns, crawlJobs)
			}
			return summary, err
		},
	}
	priceRetention := &contentsched.Automation{
		Job: crawl.PriceRetentionJob, Label: "价格和通知记录清理", Daily: sched.DailyTrigger{Hour: 4, Minute: 37},
		Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) {
			notices, err := deliveryService.PruneCandidates(ctx)
			summary := map[string]any{"notifications": notices}
			if err != nil {
				return summary, err
			}
			prices, err := priceRuntime.PrunePriceHistory(ctx)
			summary["prices"] = prices
			if notices.Recovered > 0 || notices.Deleted > 0 || prices.Events > 0 || prices.Cycles > 0 || notices.Truncated || prices.Truncated {
				logLine("价格和通知记录清理 │ 投递结果未知=%d，旧通知=%d，旧价格事件=%d，旧周期=%d，达到单轮上限=%t",
					notices.Recovered, notices.Deleted, prices.Events, prices.Cycles, notices.Truncated || prices.Truncated)
			}
			return summary, err
		},
	}
	humble := humblechoice.New(db, writeGate, humblechoice.Options{Crawl: priceRuntime.RunHumbleBackfill, AutoPrice: priceRuntime.AutoEnabled, Log: logLine, Proxy: clashSvc.ResolveOutboundProxy})
	humbleService = humble
	humbleAutomation := &contentsched.Automation{Job: "hb_choice", Label: "Humble Choice 每月合集", Field: "hb_schedule", Key: settings.HumbleChoiceScheduleKey, DB: db, Recorder: jobRecorder, Log: logLine, Run: func(ctx context.Context) (map[string]any, error) {
		result, err := humble.Run(ctx, false)
		if err != nil {
			return result, err
		}
		if result["ok"] != true {
			return result, fmt.Errorf("Humble Choice 未能刷新：%v", result["error"])
		}
		if result["recorded"] == true {
			if err := dispatchPending(ctx); err != nil {
				logFail("Humble Choice 提醒暂未送达：%v", err)
			}
		}
		return result, nil
	}}
	pool.OnAdded = func(_ context.Context, ids []int64) (bool, error) {
		_, e := priceRuntime.StartJob(backgroundCtx, "appids", ids, nil, "pool_add", nil)
		return e == nil, e
	}
	steamLists = wishlist.NewSteam(wishlist.SteamOptions{Proxy: steamProxy, Token: accountService.OwnedToken, APIKey: steamAPIKey})
	wishlistService = wishlist.NewService(backgroundCtx, pool, wishlist.ServiceOptions{Steam: steamLists, Persona: steamClient.ProfilePreview, Avatar: account.NormalizeAvatar, Crawl: func(_ context.Context, wished, owned []int64) (bool, error) {
		return priceRuntime.StartAccountDiscovery(backgroundCtx, wished, owned)
	}, CrawlBusy: func(e error) bool { return errors.Is(e, crawl.ErrBusy) || errors.Is(e, occupancy.ErrCrawlerBusy) }, Recorder: jobRecorder, Log: logLine})
	familyService = family.New(backgroundCtx, db, writeGate, family.Options{Lists: steamLists, Token: accountService.BoundToken, Profile: steamClient.ProfilePreview, APIKey: steamAPIKey, OnMembers: func(ids []string) {
		for _, sid := range ids {
			wishlistService.RequestSync(sid, true)
		}
	}, OnUncatalogued: priceRuntime.StartFamilyWishlist, Bus: bus, Log: logLine})
	levelService := achievements.LevelService{DB: db, Gate: writeGate}
	challengeService := achievements.ChallengeService{DB: db, Gate: writeGate}
	achievementService = achievements.NewSync(backgroundCtx, db, writeGate, achievements.SyncOptions{
		APIKey: steamAPIKey, Token: accountService.BoundToken, HasToken: accountService.HasStoredToken,
		Proxy: steamProxy, Shared: familyService.AchievementCandidates, Recorder: jobRecorder, Log: logLine,
		Award: func(ctx context.Context, sid string) error {
			return errors.Join(levelService.AwardSync(ctx, sid), challengeService.Recompute(ctx, sid))
		},
	})
	levelService.Account = achievementService.Account
	challengeService.Account = achievementService.Account
	achievementReader := achievements.Reader{DB: db, Account: achievementService.Account, Snapshot: achievementService.SnapshotInfo, Warm: achievementService.Warm}
	achievementSocial := achievements.SocialService{DB: db, Gate: writeGate, Account: achievementService.Account}
	if err := achievementService.Recover(backgroundCtx); err != nil {
		logFail("成就同步记录暂无法恢复：%v", err)
	}
	boardDiscovery := boards.NewDiscovery(db, boardClient, pool, boards.DiscoveryOptions{Crawl: priceRuntime.RunDiscovery, Recorder: jobRecorder, Log: logLine})
	if err := priceRuntime.Recover(backgroundCtx); err != nil {
		logFail("恢复价格任务失败：%v", err)
		os.Exit(1)
	}
	if _, err := deliveryService.Recover(backgroundCtx); err != nil {
		logFail("恢复提醒发送记录失败：%v", err)
		os.Exit(1)
	}
	scheduler := sched.NewScheduler()
	if err := priceRuntime.RegisterSchedules(backgroundCtx, scheduler); err != nil {
		logFail("价格自动任务注册失败：%v", err)
		os.Exit(1)
	}
	if err := rateService.RegisterSchedules(backgroundCtx, scheduler); err != nil {
		logFail("汇率自动任务注册失败：%v", err)
		os.Exit(1)
	}
	if err := boardDiscovery.RegisterSchedules(backgroundCtx, scheduler); err != nil {
		logFail("榜单自动任务注册失败：%v", err)
		os.Exit(1)
	}
	if err := accountService.RegisterSchedules(backgroundCtx, scheduler); err != nil {
		logFail("账号自动任务注册失败：%v", err)
		os.Exit(1)
	}
	if err := wishlistService.RegisterSchedules(backgroundCtx, scheduler); err != nil {
		logFail("Steam 账户自动同步注册失败：%v", err)
		os.Exit(1)
	}
	if err := billService.RegisterSchedules(backgroundCtx, scheduler); err != nil {
		logFail("账单自动同步注册失败：%v", err)
		os.Exit(1)
	}
	if err := achievementService.RegisterSchedules(backgroundCtx, scheduler); err != nil {
		logFail("成就自动同步注册失败：%v", err)
		os.Exit(1)
	}
	if err := steamEvents.RegisterSchedules(backgroundCtx, scheduler); err != nil {
		logFail("Steam 活动自动同步注册失败：%v", err)
		os.Exit(1)
	}
	if err := epicAutomation.Register(backgroundCtx, scheduler); err != nil {
		logFail("Epic 免费游戏自动同步注册失败：%v", err)
		os.Exit(1)
	}
	if err := humbleAutomation.Register(backgroundCtx, scheduler); err != nil {
		logFail("Humble Choice 自动同步注册失败：%v", err)
		os.Exit(1)
	}
	if err := bundleAutomation.Register(backgroundCtx, scheduler); err != nil {
		logFail("捆绑包记录自动同步注册失败：%v", err)
		os.Exit(1)
	}
	if err := proxySchedule.Register(scheduler); err != nil {
		logFail("代理池周期注册失败：%v", err)
		os.Exit(1)
	}
	if err := proxyRetention.Register(backgroundCtx, scheduler); err != nil {
		logFail("代理池清理注册失败：%v", err)
		os.Exit(1)
	}
	if err := walAutomation.Register(backgroundCtx, scheduler); err != nil {
		logFail("数据库日志收缩注册失败：%v", err)
		os.Exit(1)
	}
	if err := jobRetention.Register(backgroundCtx, scheduler); err != nil {
		logFail("运行记录清理注册失败：%v", err)
		os.Exit(1)
	}
	if err := priceRetention.Register(backgroundCtx, scheduler); err != nil {
		logFail("价格和通知记录清理注册失败：%v", err)
		os.Exit(1)
	}
	if err := backupAutomation.Register(backgroundCtx, scheduler); err != nil {
		logFail("自动备份注册失败：%v", err)
		os.Exit(1)
	}
	if err := seedAutomation.Register(backgroundCtx, scheduler); err != nil {
		logFail("基础资料同步注册失败：%v", err)
		os.Exit(1)
	}
	if err := healthAutomation.Register(backgroundCtx, scheduler); err != nil {
		logFail("代理体检注册失败：%v", err)
		os.Exit(1)
	}
	if err := subscriptionSchedule.Register(backgroundCtx, scheduler); err != nil {
		logFail("代理订阅刷新注册失败：%v", err)
		os.Exit(1)
	}
	defer func() {
		stopBackground()
		scheduler.Stop()
		subscriptionSchedule.Close()
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		// 恢复换库重启：写闸已拒绝新写、句柄已释放，逐服务排水只会刷一屏
		// 失败日志——任务停止交给 stopBackground，代理子进程照常回收
		drain := !restartRequested.Load()
		if !drain {
			proxySchedule.waitDone()
			return
		}
		if err := epicAutomation.Close(ctx); err != nil {
			logFail("Epic 自动刷新退出未完成：%v", err)
		}
		if err := humbleAutomation.Close(ctx); err != nil {
			logFail("Humble Choice 自动刷新退出未完成：%v", err)
		}
		if err := bundleAutomation.Close(ctx); err != nil {
			logFail("捆绑包记录自动刷新退出未完成：%v", err)
		}
		if err := proxyRetention.Close(ctx); err != nil {
			logFail("代理池清理退出未完成：%v", err)
		}
		if err := walAutomation.Close(ctx); err != nil {
			logFail("数据库日志收缩退出未完成：%v", err)
		}
		if err := jobRetention.Close(ctx); err != nil {
			logFail("运行记录清理退出未完成：%v", err)
		}
		if err := priceRetention.Close(ctx); err != nil {
			logFail("价格和通知记录清理退出未完成：%v", err)
		}
		if err := backupAutomation.Close(ctx); err != nil {
			logFail("自动备份退出未完成：%v", err)
		}
		if err := seedAutomation.Close(ctx); err != nil {
			logFail("基础资料同步退出未完成：%v", err)
		}
		if err := healthAutomation.Close(ctx); err != nil {
			logFail("代理体检退出未完成：%v", err)
		}
		if err := epicService.Close(ctx); err != nil {
			logFail("Epic 免费游戏退出未完成：%v", err)
		}
		if err := steamEvents.Close(ctx); err != nil {
			logFail("Steam 活动同步退出未完成：%v", err)
		}
		if err := achievementService.Close(ctx); err != nil {
			logFail("成就同步退出未完成：%v", err)
		}
		if err := familyService.Close(ctx); err != nil {
			logFail("家庭同步退出未完成：%v", err)
		}
		if err := wishlistService.Close(ctx); err != nil {
			logFail("Steam 同步退出未完成：%v", err)
		}
		if err := billService.Close(ctx); err != nil {
			logFail("账单同步退出未完成：%v", err)
		}
		if err := accountService.Close(ctx); err != nil {
			logFail("账号任务退出未完成：%v", err)
		}
		if err := boardDiscovery.Close(ctx); err != nil {
			logFail("榜单任务退出未完成：%v", err)
		}
		if err := rateService.Close(ctx); err != nil {
			logFail("汇率任务退出未完成：%v", err)
		}
		if err := priceRuntime.Close(ctx); err != nil {
			logFail("价格任务退出未完成：%v", err)
		}
		proxySchedule.waitDone()
		if err := deliveryService.Close(ctx); err != nil {
			logFail("提醒发送退出未完成：%v", err)
		}
	}()

	gate := access.New()
	server := &httpapi.Server{
		App:            config.AppName(),
		Version:        config.Version(),
		DataDir:        dataDir,
		DistDir:        distDir,
		SchedulerReady: true,
		Register: func(mux *http.ServeMux) {
			monitoringService.Register(mux)
			pool.Register(mux)
			registerRecovery(mux, accountService, db, gate, logLine)
			accountService.Register(mux)
			wishlistService.Register(mux)
			familyService.Register(mux)
			billService.Register(mux)
			redeemService.Register(mux)
			achievementService.Register(mux)
			achievementReader.Register(mux)
			achievementReader.RegisterCareer(mux)
			levelService.Register(mux)
			challengeService.Register(mux)
			achievementSocial.Register(mux)
			steamEvents.Register(mux)
			epicService.Register(mux)
			humble.Register(mux)
			bundleCounts.Register(mux)
			// 派生列日切换维护：bundles/games 读取面入口与补抓拍点共用
			// （版本一致零重算；系列覆盖表闸外装填）
			maintainDerived := func(ctx context.Context) {
				overrides, order := derived.LoadSeriesOverrides(dataDir)
				if err := derived.MaintainDerived(ctx, db, writeGate, time.Now(), overrides, order); err != nil {
					logLine("派生列日切换维护未完成：%v", err)
				}
			}
			bundleSvc.Register(mux, bundles.RegisterOptions{Accounting: func(ctx context.Context, job string, body func() (map[string]any, error)) (map[string]any, error) {
				return jobRecorder.RunManual(ctx, job, "manual_refresh_button", body)
			}, Maintain: maintainDerived})
			alertService.Register(mux)
			backupService.Register(mux)
			systemx.RegisterData(mux, db, writeGate, gate, dataDir, backupService, func() {
				restartRequested.Store(true)
				stopLifetime()
			})
			priceRuntime.Register(mux, backgroundCtx)
			regions.Register(mux, db, regions.Options{Gate: writeGate, Settings: settings.Options{Bus: bus}})
			rates.Register(mux, db, rateService)
			historyBackfill := games.NewHistoryBackfill(backgroundCtx, db, writeGate, logLine)
			historyBackfill.SetSource(&pluginHistorySource{manager: pluginManager})
			games.Register(mux, db, historyBackfill, games.Options{
				BoardGet:        boardClient.Get,
				Proxy:           steamProxy,
				MaintainDerived: maintainDerived,
				Writes: &games.WriteDeps{
					Gate:    writeGate,
					Monitor: monitoringService,
					// 爬取派发挂进程级 lifetime（与 crawl 路由同口径）：
					// 请求断开不中断后台任务，入参 ctx 不透传。
					StartJob: func(_ context.Context, scope string, ids []int64, kind string) (map[string]any, error) {
						return priceRuntime.StartJob(backgroundCtx, scope, ids, nil, kind, nil)
					},
					Log: logLine,
				},
			})
			systemx.Register(mux, db, func(ctx context.Context) (crawl.Readiness, error) {
				return priceRuntime.Readiness(ctx, proxySchedule.currentLanes)
			})
			// 三诊断面（write-scheduler/logs/diagnostics）：日志环与订阅内核
			// 运行态在此注入。
			systemx.RegisterDiagnostics(mux, db, systemx.DiagnosticsDeps{
				Gate:         writeGate,
				Ring:         ring,
				ClashRunning: func() bool { return subKernel.Status().Running },
				ClashSubID:   clashSvc.SelectedSubID,
			})
			metadata.Register(mux, db, writeGate)
			proxypool.RegisterRuns(mux, db)
			proxypool.RegisterProxyList(mux, db, dataDir)
			proxypool.RegisterUserProxyRoutes(mux, db, writeGate, dataDir, proxypool.UserProxyOptions{
				KernelStatus: func() (bool, int) {
					status := subKernel.Status()
					return status.Running, status.Port
				},
			})
			manualHealth.Register(mux)
			proxypool.RegisterSubscriptionRoutes(mux, db, writeGate, dataDir, proxypool.SubscriptionRouteOptions{
				Refresh:   subscriptionSchedule.Refresh,
				OnChanged: proxySchedule.requestRebuild,
				BeforeAdd: func(ctx context.Context, _ string) error { return clashSvc.EnsureKernelForAdd(ctx) },
				AfterAdd:  clashSvc.EffectuateNewSubscription,
				// 删除订阅收尾链：缓存原文与运行配置随行回收（面板名/流量头
				// Go 侧入库，无 meta sidecar）；删到无出口回退直连。
				AfterDelete: func(_ context.Context, subID int64, _ string) {
					cache := clashservice.SubscriptionConfigPath(dataDir, subID)
					_ = os.Remove(cache)
					_ = os.Remove(strings.TrimSuffix(cache, ".yaml") + ".runtime.yaml")
				},
				RevertToDirect: func(ctx context.Context) {
					maybeRevertToDirect(ctx, db, writeGate, bus, func(format string, args ...any) {
						logLineAt("注意", format, args...)
					})
				},
				Channels:    proxySchedule.subscriptionChannels,
				KernelProxy: subscriptionKernelProxy(&subKernel),
			})
			clashSvc.Register(mux)
			systemx.RegisterSession(mux, gate, *devSession, systemx.UnlockDeps{
				Enabled:  access.LANEnabled,
				Password: unlockPasswordResolver(db, writeGate, dataDir),
				Fail:     access.RecordUnlockFailure,
				Success:  access.RecordUnlockSuccess,
			})
			mux.HandleFunc("POST /api/v1/system/update-ready", updateguard.HandleUpdateReady)
			updater.Register(mux, updater.Deps{
				DataDir:     dataDir,
				InstallRoot: installRoot,
				Version:     config.Version(),
				Channels:    updateChannels(proxySchedule),
				Log:         logLine,
			})
			settings.Register(mux, db, writeGate, settings.Options{Bus: bus, Changed: func(_ context.Context, keys, kicks []string) {
				epicAutomation.SettingsChanged(backgroundCtx, keys, kicks)
				humbleAutomation.SettingsChanged(backgroundCtx, keys, kicks)
				bundleAutomation.SettingsChanged(backgroundCtx, keys, kicks)
				priceRuntime.SettingsChanged(backgroundCtx, keys, slices.DeleteFunc(slices.Clone(kicks), func(job string) bool {
					return job == "epic_free" || job == "hb_choice" || job == bundles.CountsJob
				}))
			}})
			notifications.Register(mux, db, notifications.Options{Gate: writeGate, Bus: bus, Delivery: deliveryService})
			pluginManager.Register(mux)
			eventstream.Register(mux, bus, ring)
			agentDeps := &agent.Deps{
				LogRing:      ring,
				WriteGate:    writeGate,
				ResolveProxy: clashSvc.ResolveOutboundProxy,
				ImportAppids: func(ctx context.Context, appids []any) (map[string]any, error) {
					return games.ImportAppids(ctx, db, appids)
				},
				AppIDFreshness: priceRuntime.AppIDFreshness,
			}
			if err := agent.Register(mux, db, gate, agent.Options{
				DataDir:       dataDir,
				EnabledByFlag: *mcpEnabled,
				Log:           logLine,
				// 工具共享依赖面：日志环（read_logs）、写闸（write_gate_status）、
				// 出站代理解析（search_steam/web_search）、目录导入与对象新鲜度。
				Deps: agentDeps,
			}); err != nil {
				logFail("%v", err)
				os.Exit(1)
			}
			// agent 读面五端点（tasks/runs/events/cancel）：与 MCP MountPath 共存。
			agent.RegisterHTTPRead(mux, db, agentDeps)
			// mcp.enabled 运行时重载与运行态投影：函数字段注入，settings 不反向依赖 agent
			settings.OnMCPEnabledWrite = agent.Reapply
			settings.MCPStatus = agent.MCPStatus
			// 领航员域：KV 桥直连 app_settings（direct SQL，与 Python settings_service 同口径）
			pilotService = pilot.New(pilot.Deps{
				DB:      db,
				DataDir: dataDir,
				Version: config.Version(),
				Mux:     mux,
				Log:     logLine,
				KVGet: func(key string) (json.RawMessage, bool) {
					var raw string
					err := db.QueryRowContext(context.Background(),
						"SELECT value_json FROM app_settings WHERE key = ?", key).Scan(&raw)
					if err != nil || raw == "" {
						return nil, false
					}
					return json.RawMessage(raw), true
				},
				KVGetSecret: func(key string) (string, bool) {
					value, err := settings.ReadSecret(context.Background(), db, dataDir, key)
					if err != nil || value == "" {
						return "", false
					}
					return value, true
				},
				KVSet: func(ctx context.Context, values map[string]json.RawMessage, secrets map[string]string) error {
					for key, value := range values {
						now := time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
						if _, err := db.ExecContext(ctx,
							"INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?) "+
								"ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
							key, string(value), now); err != nil {
							return fmt.Errorf("KV 写入 %s 失败: %w", key, err)
						}
					}
					for key, secret := range secrets {
						if secret == "" {
							if _, err := db.ExecContext(ctx,
								"DELETE FROM app_settings WHERE key = ?", key); err != nil {
								return err
							}
							continue
						}
						sealed, err := secretbox.EncryptSecretIn(dataDir, secret, secretbox.SecretPurpose(key))
						if err != nil {
							return fmt.Errorf("密钥加密失败: %w", err)
						}
						now := time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
						if _, err := db.ExecContext(ctx,
							"INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?) "+
								"ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
							key, sealed, now); err != nil {
							return err
						}
					}
					return nil
				},
			}, pilotToolBridge(db, writeGate, mux, epicService, monitoringService, redeemService))
			pilotService.SetGate(writeGate)
			pilotService.SetToolSpecs(func() []map[string]any {
				return agentToolSpecs()
			})
			pilot.Register(mux, pilotService)
			// 体检进度发布位：仅维护包络为体检种类时发布（重建在位不发布）
			proxypool.MaintenanceHealthActive = func() bool {
				return occupancy.MaintenanceKind() == occupancy.MaintenanceHealth
			}
		},
	}

	// 启动链尾：调度器与代理周期只在验收流程收尾（或无验收）后开跑——
	// Python 验收窗口期 HTTP 已开但零调度，此处对齐。EnsureClashRunning
	// 不写库、失败不影响服务，保留原位（登记为已接受差异）。
	startupTail := func() {
		go proxySchedule.run(backgroundCtx, scheduler)
		scheduler.Start()
	}
	if updateguard.Active() {
		// 验收期写保护：全库写入闸暂停，直到验收提交（事务 dataAccepted 轮询）
		writeGate.PauseWrites(lifetime)
		server.UpdateWriteProtected = true
		go func() {
			txPath := filepath.Join(installRoot, "state", "transaction.json")
			for lifetime.Err() == nil {
				if raw, err := os.ReadFile(txPath); err == nil {
					var tx map[string]any
					if json.Unmarshal(raw, &tx) == nil {
						attempt, _ := tx["attemptId"].(string)
						accepted, _ := tx["dataAccepted"].(bool)
						if attempt == updateguard.AttemptID() && accepted {
							writeGate.ResumeWrites()
							updateguard.ClearProtection()
							server.UpdateWriteProtected = false
							logLine("更新验收已提交，写入保护解除")
							resumeKeys()
							startupTail()
							return
						}
					}
				}
				time.Sleep(500 * time.Millisecond)
			}
		}()
	}

	var tickets *access.BootstrapFile
	// 叠序对齐现网：安全响应头（最外）→ 访问闸 → CORS → 路由。
	corsMW := middleware.CORS{
		AllowOrigins: []string{
			"http://localhost:5173", "http://127.0.0.1:5173",
			"http://localhost:8080", "http://127.0.0.1:8080",
		},
		AllowCredentials: true,
		AllowMethods:     []string{"*"},
	}
	headersMW := middleware.Headers{WriteProtected: func() bool { return server.UpdateWriteProtected }}
	gzipMW := middleware.GZip{}
	// 叠序对齐现网：安全响应头（最外）→ 访问闸 → GZip → CORS → 路由。
	handler := http.Handler(updateguard.Middleware(headersMW.Middleware(
		gate.Middleware(gzipMW.Middleware(corsMW.Middleware(server.Handler()))))))
	if *bootstrapFileFlag != "" {
		tickets = access.NewBootstrapFile(*bootstrapFileFlag)
		tickets.Log = logLine
		handler = tickets.Middleware(handler)
	}
	httpServer := &http.Server{Addr: *addr, Handler: handler, BaseContext: func(net.Listener) context.Context { return lifetime }}

	logLine("数据目录：%s", dataDir)
	logLine("数据库：%s", dbPath)
	logLine("监听地址：%s", *addr)

	listener, err := net.Listen("tcp", *addr)
	if err != nil {
		logFail("服务启动失败：%v", err)
		os.Exit(1)
	}
	// 监听就绪即健康可用（路由已装配完成）：此刻发布引导票据，壳侧
	// health 轮询通过时票据必然已在场。
	if tickets != nil {
		if err := tickets.Publish(systemx.CreateBootstrap()); err != nil {
			logFail("%v", err)
			os.Exit(1)
		}
		defer tickets.Close()
		logLine("引导票据就绪：%s", *bootstrapFileFlag)
	}
	occupancy.OnCrawlEnd = proxySchedule.afterCrawl
	if !updateguard.Active() {
		startupTail()
	}
	// Clash 订阅内核自启：按自启开关与本地订阅快照拉起（对齐启动链尾
	// ensure_clash_running；snapshot_only 不在线拉取，失败不影响服务）
	go clashSvc.EnsureClashRunning(backgroundCtx, true)

	serveErr := make(chan error, 1)
	go func() {
		serveErr <- httpServer.Serve(listener)
	}()

	select {
	case err := <-serveErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logFail("服务启动失败：%v", err)
			stopBackground()
			proxySchedule.waitDone()
			os.Exit(1)
		}
	case <-lifetime.Done():
		logLine("收到退出信号，正在关闭")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(ctx); err != nil {
			logLine("服务关闭未完成：%v", err)
			stopBackground()
			proxySchedule.waitDone()
			os.Exit(1)
		}
	}
}
