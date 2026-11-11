package account

import (
	"context"
	"math/rand/v2"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/sched"
)

func storedTime(raw string) (time.Time, bool) {
	for _, layout := range []string{"2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999", time.RFC3339Nano} {
		if at, e := time.ParseInLocation(layout, raw, accountZone); e == nil {
			return at, true
		}
	}
	return time.Time{}, false
}
func (s *Service) rotationSkip(row accountRow, freshMinutes float64) map[string]any {
	if row.Frozen {
		return map[string]any{"ok": false, "status": "frozen", "error": row.Error}
	}
	if row.Wallet["check_ok"] == true {
		if at, ok := storedTime(textField(row.Wallet["checked_at"])); ok && s.now().Sub(at).Minutes() < freshMinutes {
			return map[string]any{"ok": true, "status": "fresh_skip"}
		}
	}
	if row.Error != "" && row.Checked.Valid {
		if at, ok := storedTime(row.Checked.String); ok {
			cooldown := []float64{2, 5, 15, 30}[min(max(row.Backoff, 0), 3)]
			if row.Streak >= 5 {
				cooldown = 10
			}
			if strings.Contains(row.Error, "429") || strings.Contains(row.Error, "403") {
				cooldown = 30
			}
			if s.now().Sub(at).Minutes() < cooldown {
				return map[string]any{"ok": false, "status": "backoff"}
			}
		}
	}
	return nil
}
func (s *Service) Rotate(parent context.Context) (map[string]any, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	body := func() (map[string]any, error) {
		rows, e := s.rows(ctx)
		if e != nil {
			return nil, e
		}
		if len(rows) == 0 {
			return map[string]any{"ok": false, "status": "no_cookie"}, nil
		}
		freshMinutes := 30.0
		s.mu.Lock()
		seen := s.lastSeen
		s.mu.Unlock()
		if !seen.IsZero() && s.now().Sub(seen) < 90*time.Second {
			freshMinutes = 2
		}
		results := make([]map[string]any, len(rows))
		var workers sync.WaitGroup
		for i, row := range rows {
			if skip := s.rotationSkip(row, freshMinutes); skip != nil {
				results[i] = skip
				continue
			}
			workers.Add(1)
			go func() {
				defer workers.Done()
				delay := time.Duration((1 + rand.Float64()*7) * float64(time.Second))
				var delayErr error
				if s.opts.Sleep != nil {
					delayErr = s.opts.Sleep(ctx, delay)
				} else {
					delayErr = s.opts.Steam.sleep(ctx, delay)
				}
				if delayErr != nil {
					results[i] = map[string]any{"ok": false, "error": "钱包自动同步已停止"}
					return
				}
				result, err := s.SyncWallet(ctx, row.ID, false)
				if err != nil {
					result = map[string]any{"ok": false, "error": err.Error()}
				}
				results[i] = result
			}()
		}
		workers.Wait()
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		stale := make([]accountRow, 0, len(rows))
		now := s.now()
		for _, row := range rows {
			at, valid := storedTime(row.OnlineChecked.String)
			if !row.OnlineChecked.Valid || !valid || now.Sub(at).Minutes() >= freshMinutes {
				stale = append(stale, row)
			}
		}
		if err := s.refreshOnline(ctx, stale); err != nil && ctx.Err() == nil {
			s.opts.Log("Steam 官方在线状态本轮未刷新 │ 原因=%v", err)
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		ok, skipped, frozen := 0, 0, 0
		reasons := map[string]int{}
		for _, result := range results {
			if result["ok"] == true {
				ok++
				continue
			}
			if result["status"] == "backoff" {
				skipped++
			}
			if result["status"] == "frozen" {
				frozen++
			}
			reason := textField(result["error"])
			if reason == "" {
				reason = textField(result["status"])
			}
			runes := []rune(reason)
			reasons[string(runes[:min(len(runes), 120)])]++
		}
		return map[string]any{"ok": ok > 0, "ok_count": ok, "skipped": skipped, "frozen": frozen, "total": len(rows), "reasons": reasons}, nil
	}
	if s.opts.Recorder != nil {
		return s.opts.Recorder.Run(ctx, "wallet_sync", body)
	}
	return body()
}
func (s *Service) RegisterSchedules(ctx context.Context, clock *sched.Scheduler) error {
	return clock.RegisterInterval("wallet_sync", s.now().Add(time.Minute), time.Minute, func(*sched.RunCtx) {
		result, e := s.Rotate(ctx)
		if e != nil {
			s.opts.Log("Steam 钱包本轮自动同步未完成：%v", e)
			return
		}
		if result["status"] != "no_cookie" {
			s.opts.Log("Steam 钱包本轮自动同步结束 │ 成功=%v，账号=%v，暂缓=%v，冻结=%v", result["ok_count"], result["total"], result["skipped"], result["frozen"])
		}
	})
}
