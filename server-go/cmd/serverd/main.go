package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"slices"
	"strings"
	"sync"
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

// processStartedAt 启动收尸时间锚，仅收敛早于本进程启动的遗留运行。
var processStartedAt = time.Now()

// beforeStartupTail 测试钩子，生产运行恒为 nil。
var beforeStartupTail func(context.Context)

type slogBridgeHandler struct {
	emit   func(level, format string, args ...any)
	attrs  []slog.Attr
	prefix string
}

func (h slogBridgeHandler) Enabled(_ context.Context, level slog.Level) bool {
	return level >= slog.LevelInfo
}

func (h slogBridgeHandler) Handle(_ context.Context, r slog.Record) error {
	level := "信息"
	switch {
	case r.Level >= slog.LevelError:
		level = "失败"
	case r.Level >= slog.LevelWarn:
		level = "注意"
	}
	var pairs []string
	for _, a := range h.attrs {
		appendSlogAttr(&pairs, h.prefix, a)
	}
	r.Attrs(func(a slog.Attr) bool {
		appendSlogAttr(&pairs, h.prefix, a)
		return true
	})
	if len(pairs) > 0 {
		h.emit(level, "%s │ %s", r.Message, strings.Join(pairs, " "))
	} else {
		h.emit(level, "%s", r.Message)
	}
	return nil
}

func (h slogBridgeHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	if len(attrs) == 0 {
		return h
	}
	merged := make([]slog.Attr, 0, len(h.attrs)+len(attrs))
	merged = append(merged, h.attrs...)
	merged = append(merged, attrs...)
	h.attrs = merged
	return h
}

func (h slogBridgeHandler) WithGroup(name string) slog.Handler {
	if name == "" {
		return h
	}
	if h.prefix != "" {
		h.prefix = h.prefix + "." + name
	} else {
		h.prefix = name
	}
	return h
}

func appendSlogAttr(dst *[]string, prefix string, a slog.Attr) {
	a.Value = a.Value.Resolve()
	if a.Equal(slog.Attr{}) {
		return
	}
	key := a.Key
	if prefix != "" && key != "" {
		key = prefix + "." + key
	} else if prefix != "" {
		key = prefix
	}
	if a.Value.Kind() == slog.KindGroup {
		for _, sub := range a.Value.Group() {
			appendSlogAttr(dst, key, sub)
		}
		return
	}
	*dst = append(*dst, fmt.Sprintf("%s=%v", key, a.Value.Any()))
}

func injectedPassphrase() string {
	return strings.TrimSpace(os.Getenv("HOLDEXAR_PASSPHRASE"))
}

func main() {
	exitCode := 0
	// 清理级联完成后统一按重启码或错误码退出。
	defer func() {
		if restartRequested.Load() {
			os.Exit(systemx.RestartExitCode)
		}
		if exitCode != 0 {
			os.Exit(exitCode)
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
			exitCode = 1
		}
		return
	}

	// 日志格式「HH:MM:SS [信息|失败|注意] 服务端 · ...」，写 stderr 与环形缓冲前均经脱敏。
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
	slog.SetDefault(slog.New(slogBridgeHandler{emit: logLineAt}))

	if *dataDirFlag != "" {
		os.Setenv(config.EnvPrefix+"DATA_DIR", *dataDirFlag)
	}
	dataDir, err := config.ResolveDataDir()
	if err != nil {
		logFail("%v", err)
		exitCode = 1
		return
	}
	// 生产单实例：同一数据目录只允许一个后台服务进程（先于升级验收授权）。
	releaseInstance, err := instance.AcquireProductionLock(dataDir)
	if err != nil {
		logFail("%v", err)
		exitCode = 1
		return
	}
	defer releaseInstance()
	distDir, err := config.WebDistDir()
	if err != nil {
		logFail("%v", err)
		exitCode = 1
		return
	}
	installRoot, err := config.InstallRoot()
	if err != nil {
		logFail("%v", err)
		exitCode = 1
		return
	}

	updateToken, err := updateguard.Activate(dataDir)
	if err != nil {
		logFail("已停止启动以保护数据：%v", err)
		exitCode = 1
		return
	}
	dbPath := filepath.Join(dataDir, "holdexar.db")
	logLine("正在准备应用数据")
	db, err := migration.PrepareDatabaseAuthorized(lifetime, dbPath, updateToken)
	if err != nil {
		logFail("%v", err)
		exitCode = 1
		return
	}
	defer db.Close()
	logLine("应用数据已就绪")
	writeGate := store.NewWriteGate(db, dbPath)
	if updateguard.Active() {
		if err := writeGate.PauseWrites(lifetime); err != nil {
			logFail("启动更新写保护失败：%v", err)
			exitCode = 1
			return
		}
	}

	backgroundCtx, stopBackground := context.WithCancel(lifetime)
	defer stopBackground()

	resumeKeys := func(ctx context.Context) {
		resume, err := secretbox.ResumePendingMigration(ctx, writeGate, dataDir, "")
		if err != nil {
			if ctx.Err() == nil {
				logFail("密钥换装收尾失败，保留过渡记录待下次续跑：%v", err)
			}
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

	scheduler := sched.NewScheduler()
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
	proxySchedule := newProxySchedule(db, writeGate, dataDir, filepath.Join(installRoot, "assets", "clash"), jobRecorder, logLine)
	// 订阅内核独立于池内核 Runtime（不同 -d 目录），避免池重建停掉订阅通道。
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
	bundleCounts := bundles.NewCounts(db, writeGate, bundles.WithCountsDataDir(dataDir))
	bundleAutomation := &contentsched.Automation{
		Job: bundles.CountsJob, Label: "捆绑包记录", Source: "bundle_counts", Daily: sched.DailyTrigger{Hour: 5, Minute: 40},
		FirstAfter: 90 * time.Second, DB: db, Recorder: jobRecorder, Log: logLine,
		Run: func(ctx context.Context) (map[string]any, error) { return bundleCounts.Refresh(ctx, false) },
	}
	proxySchedule.cycle.Bus = bus
	subscriptionSchedule := newSubscriptionSchedule(proxySchedule)
	rateService := rates.NewService(db, writeGate, rates.ServiceOptions{Log: logLine, Recorder: jobRecorder, Revalue: bills.RevalueAffected})
	// Steam 出站经代理策略引擎解析，避免系统代理关闭时误直连导致二跳失败。
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
	bundleSvc.DataDir = dataDir
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

	var priceRuntime *crawl.Runtime
	steamEvents := steamevents.New(db, writeGate, steamevents.Options{DataDir: dataDir, Recorder: jobRecorder, Log: logLine})
	var epicService *epicfree.Service
	var deliveryService *notifications.DeliveryService
	var achievementService *achievements.SyncService
	var pilotService *pilot.Service
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
	manualHealth := &proxypool.ManualHealth{DB: db, Gate: writeGate, DataDir: dataDir, Bus: bus}
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
		if _, bundleErr := bundleSvc.RefreshWithEnqueue(ctx); bundleErr != nil {
			logLine("捆绑包链尾刷新未完成 │ 原因=%v", bundleErr)
		}
		if _, dealErr := alertService.CheckBundleDeals(ctx); dealErr != nil {
			logLine("捆绑包精选检测未完成 │ 原因=%v", dealErr)
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
	boardDiscovery := boards.NewDiscovery(db, boardClient, pool, boards.DiscoveryOptions{Crawl: priceRuntime.RunDiscovery, Recorder: jobRecorder, Log: logLine})

	var (
		tailOnce  sync.Once
		tailDone  = make(chan struct{})
		tailFatal atomic.Bool
		clashWG   sync.WaitGroup
	)
	runStartupTail := func(ctx context.Context) {
		if beforeStartupTail != nil {
			beforeStartupTail(ctx)
		}
		failTail := func(format string, args ...any) {
			if ctx.Err() != nil {
				return
			}
			logFail(format, args...)
			tailFatal.Store(true)
			stopLifetime()
		}
		if ctx.Err() != nil {
			return
		}
		initLANAccess(ctx, db, writeGate, dataDir, logLine, logFail)

		if ctx.Err() != nil {
			return
		}
		// 密钥免交互注入：仅在口令模式下读取 HOLDEXAR_PASSPHRASE 尝试解锁，失败不阻断启动（fail-closed）。
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
		resumeKeys(ctx)

		if ctx.Err() != nil {
			return
		}
		if _, err := accountService.MigrateLegacyKV(ctx); err != nil && ctx.Err() == nil {
			logFail("旧账号设置接管未完成，原数据已保留：%v", err)
		}
		if _, err := accountService.SealCredentials(ctx); err != nil && ctx.Err() == nil {
			logFail("账号凭据封存未完成，保留兼容读取：%v", err)
		}
		if count, err := settings.SealSecrets(ctx, db, writeGate, dataDir); err != nil {
			if ctx.Err() == nil {
				logFail("设置凭据封存未完成，保留兼容读取：%v", err)
			}
		} else if count > 0 {
			logLine("存量设置凭据已加密保存 │ 条数=%d", count)
		}
		maybeRevertToDirect(ctx, db, writeGate, bus, func(format string, args ...any) {
			logLineAt("注意", format, args...)
		})
		// 内核启动前收紧凭证明文权限并清扫可重建的运行配置残留。
		if tightened := clashservice.TightenAll(dataDir); tightened > 0 {
			logLine("节点凭证明文权限已收紧 │ 文件数=%d", tightened)
		}
		if swept := clashservice.SweepTransient(dataDir); swept > 0 {
			logLine("已清理可重建的节点配置明文件 │ 条数=%d", swept)
		}

		if ctx.Err() != nil {
			return
		}
		if err := jobRecorder.Recover(ctx); err != nil {
			failTail("恢复自动任务记录失败：%v", err)
			return
		}
		if converged, err := agent.ReconcileOrphanRuns(ctx, db, writeGate, processStartedAt); err != nil {
			if ctx.Err() == nil {
				logFail("启动收尸暂未完成：%v", err)
			}
		} else if converged > 0 {
			logLine("启动收尸：%d 条重启前遗留的未完成运行已收敛为已取消", converged)
		}
		if err := steamEvents.Windows.Reload(ctx); err != nil && ctx.Err() == nil {
			logFail("Steam 活动窗口暂无法载入：%v", err)
		}
		if err := epicService.Initialize(ctx); err != nil {
			failTail("Epic 功能状态无法读取：%v", err)
			return
		}
		if err := achievementService.Recover(ctx); err != nil && ctx.Err() == nil {
			logFail("成就同步记录暂无法恢复：%v", err)
		}
		if err := priceRuntime.Recover(ctx); err != nil {
			failTail("恢复价格任务失败：%v", err)
			return
		}
		if _, err := deliveryService.Recover(ctx); err != nil {
			failTail("恢复提醒发送记录失败：%v", err)
			return
		}

		type scheduleEntry struct {
			label    string
			register func() error
		}
		schedules := []scheduleEntry{
			{label: "价格自动任务注册失败", register: func() error { return priceRuntime.RegisterSchedules(ctx, scheduler) }},
			{label: "汇率自动任务注册失败", register: func() error { return rateService.RegisterSchedules(ctx, scheduler) }},
			{label: "榜单自动任务注册失败", register: func() error { return boardDiscovery.RegisterSchedules(ctx, scheduler) }},
			{label: "账号自动任务注册失败", register: func() error { return accountService.RegisterSchedules(ctx, scheduler) }},
			{label: "Steam 账户自动同步注册失败", register: func() error { return wishlistService.RegisterSchedules(ctx, scheduler) }},
			{label: "账单自动同步注册失败", register: func() error { return billService.RegisterSchedules(ctx, scheduler) }},
			{label: "成就自动同步注册失败", register: func() error { return achievementService.RegisterSchedules(ctx, scheduler) }},
			{label: "Steam 活动自动同步注册失败", register: func() error { return steamEvents.RegisterSchedules(ctx, scheduler) }},
			{label: "Epic 免费游戏自动同步注册失败", register: func() error { return epicAutomation.Register(ctx, scheduler) }},
			{label: "Humble Choice 自动同步注册失败", register: func() error { return humbleAutomation.Register(ctx, scheduler) }},
			{label: "捆绑包记录自动同步注册失败", register: func() error { return bundleAutomation.Register(ctx, scheduler) }},
			{label: "代理池周期注册失败", register: func() error { return proxySchedule.Register(scheduler) }},
			{label: "代理池清理注册失败", register: func() error { return proxyRetention.Register(ctx, scheduler) }},
			{label: "数据库日志收缩注册失败", register: func() error { return walAutomation.Register(ctx, scheduler) }},
			{label: "运行记录清理注册失败", register: func() error { return jobRetention.Register(ctx, scheduler) }},
			{label: "价格和通知记录清理注册失败", register: func() error { return priceRetention.Register(ctx, scheduler) }},
			{label: "自动备份注册失败", register: func() error { return backupAutomation.Register(ctx, scheduler) }},
			{label: "基础资料同步注册失败", register: func() error { return seedAutomation.Register(ctx, scheduler) }},
			{label: "代理体检注册失败", register: func() error { return healthAutomation.Register(ctx, scheduler) }},
			{label: "代理订阅刷新注册失败", register: func() error { return subscriptionSchedule.Register(ctx, scheduler) }},
		}
		for _, entry := range schedules {
			if ctx.Err() != nil {
				return
			}
			if err := entry.register(); err != nil {
				failTail("%s：%v", entry.label, err)
				return
			}
		}

		if ctx.Err() != nil {
			return
		}
		occupancy.OnCrawlEnd = proxySchedule.afterCrawl
		go proxySchedule.run(ctx, scheduler)
		scheduler.Start()
		clashWG.Add(1)
		go func() {
			defer clashWG.Done()
			if ctx.Err() != nil {
				return
			}
			clashSvc.EnsureClashRunning(ctx, true)
		}()
	}

	startTail := func() {
		tailOnce.Do(func() {
			go func() {
				defer close(tailDone)
				runStartupTail(backgroundCtx)
			}()
		})
	}
	waitStartupTail := func() {
		tailOnce.Do(func() { close(tailDone) })
		<-tailDone
	}

	type namedCloser struct {
		name  string
		close func(context.Context) error
	}
	closers := []namedCloser{
		{name: "Epic 自动刷新", close: epicAutomation.Close},
		{name: "Humble Choice 自动刷新", close: humbleAutomation.Close},
		{name: "捆绑包记录自动刷新", close: bundleAutomation.Close},
		{name: "代理池清理", close: proxyRetention.Close},
		{name: "数据库日志收缩", close: walAutomation.Close},
		{name: "运行记录清理", close: jobRetention.Close},
		{name: "价格和通知记录清理", close: priceRetention.Close},
		{name: "自动备份", close: backupAutomation.Close},
		{name: "基础资料同步", close: seedAutomation.Close},
		{name: "代理体检", close: healthAutomation.Close},
		{name: "Epic 免费游戏", close: epicService.Close},
		{name: "Steam 活动同步", close: steamEvents.Close},
		{name: "成就同步", close: achievementService.Close},
		{name: "家庭同步", close: familyService.Close},
		{name: "Steam 同步", close: wishlistService.Close},
		{name: "账单同步", close: billService.Close},
		{name: "账号任务", close: accountService.Close},
		{name: "榜单任务", close: boardDiscovery.Close},
		{name: "汇率任务", close: rateService.Close},
		{name: "价格任务", close: priceRuntime.Close},
		{name: "代理池周期", close: func(context.Context) error { proxySchedule.waitDone(); return nil }},
		{name: "提醒发送", close: deliveryService.Close},
	}
	defer func() {
		stopBackground()
		waitStartupTail()
		scheduler.Stop()
		subscriptionSchedule.Close()
		clashWG.Wait()
		subKernel.Stop()
		// 恢复换库重启时写闸与句柄已关闭，跳过逐服务排水，仅等待代理子进程回收。
		if restartRequested.Load() {
			proxySchedule.waitDone()
			return
		}
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		for _, c := range closers {
			if err := c.close(ctx); err != nil {
				logFail("%s退出未完成：%v", c.name, err)
			}
		}
	}()

	gate := access.New()
	var registerErr error
	server := &httpapi.Server{
		App:                    config.AppName(),
		Version:                config.Version(),
		DataDir:                dataDir,
		DistDir:                distDir,
		SchedulerReadyFn:       scheduler.Running,
		UpdateWriteProtectedFn: updateguard.Active,
		Gate:                   gate,
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
			// 派生列日切换维护：读取面与补抓共用，系列覆盖表在写闸外读取。
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
					// 爬取派发绑定进程级 backgroundCtx，不随请求断开取消。
					StartJob: func(_ context.Context, scope string, ids []int64, kind string) (map[string]any, error) {
						return priceRuntime.StartJob(backgroundCtx, scope, ids, nil, kind, nil)
					},
					Log: logLine,
				},
			})
			systemx.Register(mux, db, func(ctx context.Context) (crawl.Readiness, error) {
				return priceRuntime.Readiness(ctx, proxySchedule.currentLanes)
			})
			systemx.RegisterDiagnostics(mux, db, systemx.DiagnosticsDeps{
				Gate:         writeGate,
				Ring:         ring,
				ClashRunning: func() bool { return subKernel.Status().Running },
				ClashSubID:   clashSvc.SelectedSubID,
				Cycles:       priceRuntime.Cycles,
				JobRuns: func(ctx context.Context, limit int) (map[string]any, error) {
					payload, err := proxypool.QueryJobRunsJSON(ctx, db, int64(limit), 0)
					if err != nil {
						return nil, err
					}
					var out map[string]any
					if err := json.Unmarshal([]byte(payload), &out); err != nil {
						return nil, err
					}
					return out, nil
				},
			})
			metadata.Register(mux, db, writeGate, metadata.WithDataDir(dataDir))
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
				// 删除订阅后回收缓存与运行配置，无可用出口时回退直连。
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
				Channels:     proxySchedule.subscriptionChannels,
				KernelProxy:  subscriptionKernelProxy(&subKernel),
				ReloadActive: clashSvc.ReloadActiveSubscription,
				PostSyncCheck: func(subID int64) {
					if clashSvc.FirstCheck != nil {
						clashSvc.FirstCheck(subID, true)
					}
				},
			})
			clashSvc.Register(mux)
			systemx.RegisterSession(mux, gate, *devSession, systemx.UnlockDeps{
				Enabled:     access.LANEnabled,
				Verifier:    unlockVerifierResolver(db, writeGate, dataDir),
				FailPeer:    access.RecordUnlockFailureFor,
				SuccessPeer: access.RecordUnlockSuccessFor,
			})
			mux.HandleFunc("POST /api/v1/system/update-ready", updateguard.HandleUpdateReady)
			updater.Register(mux, updater.Deps{
				DataDir:     dataDir,
				InstallRoot: installRoot,
				Version:     config.Version(),
				Channels:    updateChannels(proxySchedule),
				Log:         logLine,
			})
			settings.Register(mux, db, writeGate, settings.Options{SessionGate: gate, Bus: bus, Changed: func(_ context.Context, keys, kicks []string) {
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
				Deps:          agentDeps,
			}); err != nil {
				registerErr = err
				return
			}
			agent.RegisterHTTPRead(mux, db, agentDeps)
			// mcp.enabled 运行时重载与运行态投影：函数字段注入，settings 不反向依赖 agent
			settings.OnMCPEnabledWrite = agent.Reapply
			settings.MCPStatus = agent.MCPStatus
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
					sealedSecrets := make(map[string]string, len(secrets))
					for key, secret := range secrets {
						if secret == "" {
							sealedSecrets[key] = ""
							continue
						}
						sealed, err := secretbox.EncryptSecretIn(dataDir, secret, secretbox.SecretPurpose(key))
						if err != nil {
							return fmt.Errorf("密钥加密失败: %w", err)
						}
						sealedSecrets[key] = sealed
					}
					return writeGate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
						now := time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
						for key, value := range values {
							if _, err := conn.ExecContext(ctx,
								"INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?) "+
									"ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
								key, string(value), now); err != nil {
								return fmt.Errorf("KV 写入 %s 失败: %w", key, err)
							}
						}
						for key, sealed := range sealedSecrets {
							if sealed == "" {
								if _, err := conn.ExecContext(ctx,
									"DELETE FROM app_settings WHERE key = ?", key); err != nil {
									return err
								}
								continue
							}
							if _, err := conn.ExecContext(ctx,
								"INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?) "+
									"ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
								key, sealed, now); err != nil {
								return err
							}
						}
						return nil
					})
				},
			}, pilotToolBridge(db, writeGate, mux, monitoringService))
			pilotService.SetGate(writeGate)
			pilotService.SetToolSpecs(func() []map[string]any {
				return agentToolSpecs()
			})
			pilot.Register(mux, pilotService)
			proxypool.MaintenanceHealthActive = func() bool {
				return occupancy.MaintenanceKind() == occupancy.MaintenanceHealth
			}
		},
	}

	var tickets *access.BootstrapFile
	corsMW := middleware.CORS{
		AllowOrigins: []string{
			"http://localhost:5173", "http://127.0.0.1:5173",
			"http://localhost:8080", "http://127.0.0.1:8080",
		},
		AllowCredentials: true,
		AllowMethods:     []string{"*"},
	}
	headersMW := middleware.Headers{WriteProtected: updateguard.Active}
	gzipMW := middleware.GZip{}
	handler := http.Handler(updateguard.Middleware(headersMW.Middleware(
		gate.Middleware(gzipMW.Middleware(corsMW.Middleware(server.Handler()))))))
	if registerErr != nil {
		logFail("%v", registerErr)
		exitCode = 1
		return
	}
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
		exitCode = 1
		return
	}
	// 监听就绪且在 Serve 前发布引导票据，保证健康检查通过时票据已在场。
	if tickets != nil {
		if err := tickets.Publish(systemx.CreateBootstrap()); err != nil {
			_ = listener.Close()
			logFail("%v", err)
			exitCode = 1
			return
		}
		defer tickets.Close()
		logLine("引导票据就绪：%s", *bootstrapFileFlag)
	}

	serveErr := make(chan error, 1)
	go func() {
		serveErr <- httpServer.Serve(listener)
	}()

	if updateguard.Active() {
		go func() {
			txPath := filepath.Join(installRoot, "state", "transaction.json")
			ticker := time.NewTicker(500 * time.Millisecond)
			defer ticker.Stop()
			for backgroundCtx.Err() == nil {
				raw, err := os.ReadFile(txPath)
				// 事务文件被清场时解除写保护并补跑后台启动链。
				if os.IsNotExist(err) {
					writeGate.ResumeWrites()
					updateguard.ClearProtection()
					logLine("更新事务已终态清场，写入保护解除")
					startTail()
					return
				}
				if err == nil {
					var tx map[string]any
					if json.Unmarshal(raw, &tx) == nil {
						attempt, _ := tx["attemptId"].(string)
						accepted, _ := tx["dataAccepted"].(bool)
						if attempt == updateguard.AttemptID() && accepted {
							writeGate.ResumeWrites()
							updateguard.ClearProtection()
							logLine("更新验收已提交，写入保护解除")
							startTail()
							return
						}
					}
				}
				select {
				case <-backgroundCtx.Done():
					return
				case <-ticker.C:
				}
			}
		}()
	} else {
		startTail()
	}

	select {
	case err := <-serveErr:
		if err != nil && !errors.Is(err, http.ErrServerClosed) {
			logFail("服务启动失败：%v", err)
			exitCode = 1
			return
		}
	case <-lifetime.Done():
		logLine("收到退出信号，正在关闭")
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := httpServer.Shutdown(ctx); err != nil {
			logLine("服务关闭未完成：%v", err)
			_ = httpServer.Close()
			exitCode = 1
			return
		}
	}
	if tailFatal.Load() {
		exitCode = 1
	}
}
