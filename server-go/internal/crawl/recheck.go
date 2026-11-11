package crawl

import (
	"context"
	"fmt"
	"time"

	"holdexar/server-go/internal/sched"
)

func (r *Runtime) recheckCandidates(ctx context.Context, kind string) ([]int64, error) {
	switch kind {
	case "comingsoon_retry":
		return r.planner.planIDs(ctx, `SELECT appid FROM games WHERE type='COMING_SOON' AND (updated_at IS NULL OR julianday(updated_at)<julianday(?)) ORDER BY appid LIMIT 20`, timestamp(r.now().AddDate(0, 0, -14)))
	case "free_promo_retry":
		return r.planner.planIDs(ctx, `SELECT appid FROM games WHERE free_kind='promo' AND promo_end_at IS NOT NULL AND promo_end_at<=? ORDER BY promo_end_at LIMIT 30`, r.now().Unix()+3600)
	default:
		return nil, fmt.Errorf("未知自动复查: %s", kind)
	}
}

func (r *Runtime) RunRecheck(ctx context.Context, kind string) (map[string]any, error) {
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		return nil, context.Canceled
	}
	r.scheduled++
	r.mu.Unlock()
	defer func() { r.mu.Lock(); r.scheduled--; r.mu.Unlock() }()
	body := func() (map[string]any, error) {
		on, e := r.AutoEnabled(ctx)
		if e != nil {
			return nil, e
		}
		if !on {
			return map[string]any{"skipped": "自动价格更新已关闭"}, nil
		}
		ids, e := r.recheckCandidates(ctx, kind)
		if e != nil {
			return nil, e
		}
		if len(ids) == 0 {
			return map[string]any{"skipped": "暂无需要重新检查的游戏"}, nil
		}
		id, e := r.RunDiscovery(ctx, kind, ids)
		return map[string]any{"kind": kind, "candidates": len(ids), "jobId": id}, e
	}
	if r.opts.Recorder != nil {
		return r.opts.Recorder.Run(ctx, kind, body)
	}
	return body()
}

func (r *Runtime) registerRechecks(ctx context.Context, clock *sched.Scheduler) error {
	run := func(kind string) func(*sched.RunCtx) {
		return func(*sched.RunCtx) {
			if _, e := r.RunRecheck(ctx, kind); e != nil {
				r.opts.Log("自动复查未完成 │ 任务=%s，原因=%v", kind, e)
			}
		}
	}
	if e := clock.RegisterDaily("comingsoon_retry", 10, 0, run("comingsoon_retry")); e != nil {
		return e
	}
	return clock.RegisterInterval("free_promo_retry", r.now().Add(6*time.Hour), 6*time.Hour, run("free_promo_retry"))
}
