package steamevents

import (
	"context"
	"encoding/json"
	"net/http"

	"holdexar/server-go/internal/sched"
)

const scheduleID = "steam_events_sync"

func (s *Service) RegisterSchedules(ctx context.Context, clock *sched.Scheduler) error {
	return clock.RegisterDaily(scheduleID, 5, 0, func(*sched.RunCtx) {
		if !s.enter() {
			return
		}
		defer s.jobs.Done()
		body := func() (map[string]any, error) {
			var raw string
			if err := s.db.QueryRowContext(ctx, `SELECT COALESCE((SELECT value_json FROM app_settings WHERE key='fetch.steam_events'),'true')`).Scan(&raw); err != nil {
				return nil, err
			}
			var enabled bool
			if err := json.Unmarshal([]byte(raw), &enabled); err != nil {
				return nil, err
			}
			if !enabled {
				s.opts.Log("Steam 活动日历定时同步跳过 │ 自动更新已关闭")
				return map[string]any{"skipped": "Steam 活动日历自动更新已关闭"}, nil
			}
			result, err := s.Sync(ctx)
			if err != nil {
				return nil, err
			}
			return map[string]any{"count": result.Count, "backfilled": result.Backfilled, "fetchedAt": result.FetchedAt}, nil
		}
		var err error
		if s.opts.Recorder != nil {
			_, err = s.opts.Recorder.Run(ctx, scheduleID, body)
		} else {
			_, err = body()
		}
		if err != nil && ctx.Err() == nil {
			s.opts.Log("Steam 活动日历自动同步未完成，次日重试 │ 原因=%v", err)
		}
	}, sched.WithLabel("Steam 活动同步"))
}

func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/steam-events", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.List(r.Context())
		if err != nil {
			s.opts.Log("Steam 活动日历暂不可读 │ 原因=%v", err)
			s.reply(w, http.StatusBadGateway, map[string]string{"detail": "Steam 活动日历暂不可读"})
			return
		}
		s.reply(w, http.StatusOK, result)
	})
	mux.HandleFunc("POST /api/v1/steam-events/sync", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.Sync(r.Context())
		if err != nil {
			s.opts.Log("Steam 活动日历同步未完成 │ 原因=%v", err)
			s.reply(w, http.StatusBadGateway, map[string]string{"detail": "Steam 活动日历同步未完成，请查看日志"})
			return
		}
		s.reply(w, http.StatusOK, result)
	})
}

func (s *Service) reply(w http.ResponseWriter, code int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	if err := json.NewEncoder(w).Encode(body); err != nil {
		s.opts.Log("Steam 活动日历响应未发出 │ 原因=%v", err)
	}
}
