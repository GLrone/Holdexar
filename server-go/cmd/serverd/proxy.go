package main

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"maps"
	"sync/atomic"
	"time"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/updater"
	"strconv"
	"strings"
)

const proxyCycleJob = "proxypool_cycle"

type proxySchedule struct {
	cycle     proxypool.Cycle
	kernel    clashkernel.Runtime
	pid       atomic.Uint32
	tickBusy  atomic.Bool
	bundleDir string
	recorder  *jobrun.Recorder
	log       func(string, ...any)
	blocked   func(context.Context) bool
	wake      chan struct{}
	tick      chan struct{}
	done      chan struct{}
}

func newProxySchedule(db *sql.DB, gate *store.WriteGate, dataDir, bundleDir string, recorder *jobrun.Recorder, log func(string, ...any)) *proxySchedule {
	return &proxySchedule{
		cycle:     proxypool.Cycle{DB: db, Gate: gate, DataDir: dataDir},
		bundleDir: bundleDir, recorder: recorder, log: log, blocked: netenv.NetworkTakenOver,
		wake: make(chan struct{}, 1), tick: make(chan struct{}, 1), done: make(chan struct{}),
	}
}

func (p *proxySchedule) Register(clock *sched.Scheduler) error {
	return clock.RegisterInterval(proxyCycleJob, time.Now().Add(5*time.Second), 5*time.Minute,
		func(*sched.RunCtx) {
			if p.tickBusy.CompareAndSwap(false, true) {
				signalProxy(p.tick)
			}
		})
}

func signalProxy(ch chan struct{}) {
	select {
	case ch <- struct{}{}:
	default:
	}
}

func (p *proxySchedule) Lanes(ctx context.Context) ([]proxypool.CrawlLane, error) {
	if p.blocked(ctx) {
		return nil, errors.New(netenv.TakeoverReason)
	}
	lanes, err := p.currentLanes(ctx)
	if err != nil && ctx.Err() == nil {
		proxypool.RequestRebuild()
	}
	return lanes, err
}

func (p *proxySchedule) currentLanes(ctx context.Context) ([]proxypool.CrawlLane, error) {
	if pid := p.pid.Load(); pid != 0 {
		return proxypool.ReadCrawlLanes(ctx, p.cycle.DB, p.cycle.DataDir, pid)
	}
	return nil, errors.New("代理采集通道尚未启动")
}

func (p *proxySchedule) run(ctx context.Context, clock *sched.Scheduler) {
	defer close(p.done)
	defer func() {
		if p.pid.Swap(0) != 0 {
			if err := proxypool.InvalidateCrawlLanes(p.cycle.DataDir); err != nil {
				p.log("代理通道记录清理失败：%v", err)
			}
		}
		p.kernel.Stop()
	}()
	for ctx.Err() == nil {
		select {
		case <-ctx.Done():
			return
		case <-p.wake:
			if !p.blocked(ctx) {
				if ran, err := p.rebuild(ctx); err != nil && ctx.Err() == nil {
					p.log("代理通道重建失败：%v", err)
				} else if ran && err == nil {
					p.kickRepair(ctx, clock)
				}
			}
		case <-p.tick:
			_, err := p.recorder.Run(ctx, proxyCycleJob, func() (map[string]any, error) {
				if p.blocked(ctx) {
					p.log("%s", netenv.TakeoverReason)
					return map[string]any{"skipped": netenv.TakeoverReason}, nil
				}
				return p.runCycle(ctx, clock)
			})
			p.tickBusy.Store(false)
			if err != nil && ctx.Err() == nil {
				p.log("代理池周期未完成：%v", err)
			}
		}
	}
}

func (p *proxySchedule) afterCrawl() {
	if proxypool.RebuildPending() {
		signalProxy(p.wake)
	}
}

func (p *proxySchedule) runCycle(ctx context.Context, clock *sched.Scheduler) (map[string]any, error) {
	rebuilt := false
	var rebuildErr error
	rebuild := func() {
		ran, err := p.rebuild(ctx)
		rebuilt = rebuilt || ran && err == nil && p.kernel.Status().Running
		rebuildErr = errors.Join(rebuildErr, err)
	}
	if !p.kernel.Status().Running {
		proxypool.RequestRebuild()
		rebuild()
	}
	l0, l0Err := p.cycle.RunL0(ctx, p.checker())
	if l0 == nil {
		l0 = map[string]any{}
	}
	if !occupancy.CrawlerBusy() && rebuildErr == nil {
		rebuild()
	}
	maintenance, maintenanceErr := p.cycle.RunMaintenance(ctx, p.checker())
	maps.Copy(l0, maintenance)
	if !occupancy.CrawlerBusy() && rebuildErr == nil && ctx.Err() == nil && proxypool.RebuildPending() {
		rebuild()
	}
	l0["rebuilt"] = rebuilt
	if rebuilt && rebuildErr == nil {
		p.kickRepair(ctx, clock)
	}
	return l0, errors.Join(l0Err, rebuildErr, maintenanceErr)
}

func (p *proxySchedule) checker() *proxypool.HealthChecker {
	if !p.kernel.Status().EntryReady {
		return nil
	}
	return &proxypool.HealthChecker{
		DB: p.cycle.DB, Gate: p.cycle.Gate, DataDir: p.cycle.DataDir,
		ControllerURL: p.kernel.ControllerURL, Secret: p.kernel.Secret, ConfigPath: p.kernel.ConfigPath,
	}
}

func (p *proxySchedule) rebuild(ctx context.Context) (bool, error) {
	if err := ctx.Err(); err != nil {
		return false, err
	}
	ran, err := proxypool.RunPendingRebuild(ctx, func(ctx context.Context) error {
		if !p.kernel.Status().Running {
			result, err := clashkernel.EnsureKernel(p.bundleDir, p.cycle.DataDir)
			if err != nil {
				return err
			}
			if !result.KernelReady {
				return errors.New("代理组件尚未就位")
			}
		}
		p.pid.Store(0)
		exe := clashkernel.KernelExe(p.cycle.DataDir)
		if err := proxypool.KernelRebuildFunc(p.cycle.DB, p.cycle.Gate, p.cycle.DataDir, exe, &p.kernel)(ctx); err != nil {
			return err
		}
		p.pid.Store(uint32(p.kernel.ProcessPid()))
		return nil
	})
	if err != nil {
		return ran, fmt.Errorf("代理通道重建未完成：%w", err)
	}
	return ran, nil
}

func (p *proxySchedule) kickRepair(ctx context.Context, clock *sched.Scheduler) {
	if p.pid.Load() == 0 || ctx.Err() != nil {
		return
	}
	if err := clock.SetNext("price_repair", time.Now().Add(time.Second)); err != nil && ctx.Err() == nil {
		p.log("代理通道更新后的价格补抓安排失败：%v", err)
	}
}

// updateChannels 更新下载通道表（对齐 clash_manager._download_attempts 的
// 出口链：内核运行中走其混合端口，本地备用混合口探活后加入，直连垫底）。
func updateChannels(schedule *proxySchedule) func() []updater.Channel {
	return func() []updater.Channel {
		channels := make([]updater.Channel, 0, 3)
		if schedule != nil {
			status := schedule.kernel.Status()
			if status.Running && status.Port > 0 && clashkernel.PortListening(status.Port) {
				channels = append(channels, updater.Channel{
					Proxy: fmt.Sprintf("http://127.0.0.1:%d", status.Port),
					Label: "经内核代理",
				})
			}
		}
		const localMixedPort = 7897
		used := false
		for _, channel := range channels {
			if strings.Contains(channel.Proxy, strconv.Itoa(localMixedPort)) {
				used = true
			}
		}
		if !used && clashkernel.PortListening(localMixedPort) {
			channels = append(channels, updater.Channel{
				Proxy: fmt.Sprintf("http://127.0.0.1:%d", localMixedPort),
				Label: "本地混合端口 7897",
			})
		}
		channels = append(channels, updater.Channel{Label: "直连"})
		return channels
	}
}
