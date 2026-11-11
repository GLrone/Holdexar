package main

import (
	"context"
	"database/sql"
	"errors"
	"path/filepath"
	"sync"
	"time"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const subscriptionRefreshJob = "subscription_refresh"

type subscriptionSchedule struct {
	proxy     *proxySchedule
	candidate proxypool.CandidateCheck
	mu        sync.Mutex
	active    sync.WaitGroup
	closed    bool
}

func newSubscriptionSchedule(proxy *proxySchedule) *subscriptionSchedule {
	return &subscriptionSchedule{
		proxy: proxy,
		candidate: proxypool.CandidateCheck{
			DB: proxy.cycle.DB, Gate: proxy.cycle.Gate, DataDir: proxy.cycle.DataDir,
			BundledExe: filepath.Join(proxy.bundleDir, clashkernel.KernelFilename()),
		},
	}
}

func (s *subscriptionSchedule) Register(ctx context.Context, clock *sched.Scheduler) error {
	return clock.RegisterInterval(subscriptionRefreshJob, time.Now().Add(15*time.Second), 30*time.Minute, func(*sched.RunCtx) {
		s.mu.Lock()
		if s.closed || ctx.Err() != nil {
			s.mu.Unlock()
			return
		}
		s.active.Add(1)
		s.mu.Unlock()
		defer s.active.Done()
		_, err := s.proxy.recorder.Run(ctx, subscriptionRefreshJob, func() (map[string]any, error) {
			if occupancy.CurrentHolder() != "" {
				return map[string]any{"skipped": "正在更新价格，本次订阅刷新留到下一轮"}, nil
			}
			result, checked, err := s.refresh(ctx, 0)
			if result == nil {
				return nil, err
			}
			summary := map[string]any{
				"selected": result.SubscriptionCount, "succeeded": len(result.SnapshotSha256),
				"failed": len(result.Failures), "admitted": len(checked.Promoted),
				"checkFailed": len(checked.Failures), "strategySwitched": result.StrategySwitched,
			}
			if err == nil && len(result.Failures) > 0 {
				err = errors.New("部分订阅未能刷新，下轮会重试")
			}
			return summary, err
		})
		if err != nil && ctx.Err() == nil {
			s.proxy.log("代理订阅刷新未全部完成：%v", err)
		}
	})
}

func (s *subscriptionSchedule) Close() {
	s.mu.Lock()
	s.closed = true
	s.mu.Unlock()
	s.active.Wait()
}

func (s *subscriptionSchedule) Refresh(ctx context.Context, id int64) (*proxypool.SyncResult, error) {
	result, _, err := s.refresh(ctx, id)
	return result, err
}

func (s *subscriptionSchedule) refresh(ctx context.Context, id int64) (*proxypool.SyncResult, proxypool.CandidateReport, error) {
	before, beforeErr := s.signature(ctx)
	p := s.proxy
	syncer := proxypool.Syncer{
		DB: p.cycle.DB, Gate: p.cycle.Gate, DataDir: p.cycle.DataDir,
		Channels: p.subscriptionChannels(ctx),
	}
	var result *proxypool.SyncResult
	var err error
	var candidates []int64
	if id == 0 {
		result, err = syncer.SyncAutomatic(ctx)
	} else {
		result, err = syncer.SyncOne(ctx, id)
		candidates = []int64{id}
	}
	var checked proxypool.CandidateReport
	if ctx.Err() == nil && !p.blocked(ctx) {
		var checkErr error
		checked, checkErr = s.candidate.Check(ctx, candidates)
		if checkErr != nil && ctx.Err() == nil {
			p.log("订阅节点首检未完成：%v", checkErr)
		}
		if len(checked.Failures) > 0 {
			p.log("部分订阅节点暂未通过首检 │ 数量=%d", len(checked.Failures))
		}
	}
	if (err == nil && result != nil && len(result.SnapshotSha256) > 0) || len(checked.Promoted) > 0 {
		if result != nil && len(checked.Promoted) > 0 {
			result.StrategySwitched = s.enableDefaultProxy(ctx)
		}
		after, afterErr := s.signature(ctx)
		if beforeErr != nil || afterErr != nil || proxypool.PoolSignatureChanged(before, after) || (len(after) > 0 && p.pid.Load() == 0) {
			p.requestRebuild()
		}
	}
	return result, checked, err
}

func (s *subscriptionSchedule) enableDefaultProxy(ctx context.Context) bool {
	p := s.proxy
	switched := false
	err := p.cycle.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var strategy string
		var explicit bool
		if err := conn.QueryRowContext(ctx, `SELECT
			COALESCE((SELECT json_extract(value_json,'$') FROM app_settings WHERE key='proxy.strategy'),'direct_only'),
			COALESCE((SELECT json_extract(value_json,'$')=1 FROM app_settings WHERE key='proxy.strategy_explicit'),0)`).
			Scan(&strategy, &explicit); err != nil {
			return err
		}
		if explicit || strategy != "direct_only" {
			return nil
		}
		if err := settings.WriteValue(ctx, conn, "proxy.strategy", "proxy_first"); err != nil {
			return err
		}
		switched = true
		return nil
	})
	if err != nil {
		if ctx.Err() == nil {
			p.log("默认代理策略暂未更新：%v", err)
		}
		return false
	}
	if switched && p.cycle.Bus != nil {
		p.cycle.Bus.Publish("settings.changed", events.KV{Key: "keys", Val: []string{"proxy.strategy"}})
	}
	return switched
}

func (s *subscriptionSchedule) signature(ctx context.Context) ([]string, error) {
	conn, err := s.proxy.cycle.DB.Conn(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	return proxypool.PoolSignature(ctx, conn, s.proxy.cycle.DataDir, time.Now())
}

func (p *proxySchedule) requestRebuild() {
	proxypool.RequestRebuild()
	signalProxy(p.wake)
}

func (p *proxySchedule) subscriptionChannels(ctx context.Context) []proxypool.Channel {
	var additional []proxypool.Channel
	if pid := p.pid.Load(); pid != 0 && !occupancy.MaintenanceActive() && !p.blocked(ctx) {
		lanes, err := proxypool.ReadCrawlLanes(ctx, p.cycle.DB, p.cycle.DataDir, pid)
		if err == nil {
			for _, lane := range lanes {
				additional = append(additional, proxypool.Channel{
					Name: proxypool.ChannelPool, Proxy: lane.ProxyURL, EgressID: lane.ExitIP,
				})
			}
		}
	}
	if endpoint, err := netenv.ResolveSystemProxy(ctx); err == nil && endpoint != "" {
		additional = append(additional, proxypool.Channel{Name: proxypool.ChannelLocal, Proxy: endpoint, EgressID: proxypool.ChannelLocal})
	}
	return proxypool.BuildChannels("", additional)
}
