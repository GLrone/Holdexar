package rates

import (
	"context"
	"time"

	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/sched"
)

func (s *Service) runScheduled(ctx context.Context, name string, body func() (map[string]any, error)) {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return
	}
	s.jobs.Add(1)
	s.mu.Unlock()
	defer s.jobs.Done()
	var err error
	if s.opts.Recorder != nil {
		_, err = s.opts.Recorder.Run(ctx, name, body)
	} else {
		_, err = body()
	}
	if err != nil {
		s.opts.Log("汇率自动任务本轮未完成 │ 任务=%s，原因=%v", name, err)
	}
}

func (s *Service) RegisterSchedules(ctx context.Context, clock *sched.Scheduler) error {
	if err := clock.RegisterDaily("fx_refresh", 3, 0, func(*sched.RunCtx) {
		s.runScheduled(ctx, "fx_refresh", func() (map[string]any, error) {
			changed, e := s.RefreshIfStale(ctx)
			return map[string]any{"refreshed": changed}, e
		})
	}); err != nil {
		return err
	}
	if err := clock.SetNext("fx_refresh", s.now().Add(20*time.Second)); err != nil {
		return err
	}
	return clock.RegisterDaily("fx_history_repair", 4, 0, func(*sched.RunCtx) {
		s.runScheduled(ctx, "fx_history_repair", func() (map[string]any, error) {
			on, e := s.enabled(ctx, "fetch.fx_history")
			if e != nil {
				return nil, e
			}
			if !on {
				return map[string]any{"skipped": "汇率历史补全已关闭"}, nil
			}
			if occupancy.CrawlerBusy() {
				return map[string]any{"skipped": "价格更新正在进行，历史汇率留待下轮补齐"}, nil
			}
			limit := 8
			return s.Repair(ctx, false, &limit)
		})
	})
}

func (s *Service) Close(ctx context.Context) error {
	s.mu.Lock()
	s.closing = true
	s.mu.Unlock()
	done := make(chan struct{})
	go func() { s.jobs.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
