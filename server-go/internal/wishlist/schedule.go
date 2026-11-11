package wishlist

import (
	"context"
	"encoding/json"
	"time"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/sched"
)

func (s *Service) autoPrice(ctx context.Context) (bool, error) {
	var raw string
	e := s.pool.DB.QueryRowContext(ctx, `SELECT COALESCE((SELECT value_json FROM app_settings WHERE key='crawl.auto_price'),'true')`).Scan(&raw)
	if e != nil {
		return false, e
	}
	var enabled bool
	e = json.Unmarshal([]byte(raw), &enabled)
	return enabled, e
}
func (s *Service) Backfill(ctx context.Context) (bool, error) {
	if s.opts.Crawl == nil {
		return false, nil
	}
	rows, e := s.pool.DB.QueryContext(ctx, `WITH targets AS (
 SELECT w.appid,MAX(w.wishlisted) wished FROM wishlist_items w JOIN monitor_targets t ON t.target_type='game' AND t.target_id=w.appid WHERE (`+monitoring.StateExpression+`)='active' AND w.active=1 AND (w.owned=1 OR w.wishlisted=1) GROUP BY w.appid),
 cfg AS(SELECT COALESCE((SELECT value_json FROM app_settings WHERE key='crawl.owned_regions'),'null') owned)
 SELECT g.appid,g.wished FROM targets g,cfg WHERE EXISTS(SELECT 1 FROM crawl_regions r WHERE
 (g.wished=1 AND r.enabled=1 OR g.wished=0 AND (COALESCE(json_array_length(cfg.owned),0)=0 AND r.enabled=1 OR lower(r.code) IN(SELECT lower(value) FROM json_each(cfg.owned))))
 AND NOT EXISTS(SELECT 1 FROM game_current_prices p WHERE p.appid=g.appid AND lower(p.region_code)=lower(r.code))) ORDER BY g.appid LIMIT 400`)
	if e != nil {
		return false, e
	}
	wished, owned := []int64{}, []int64{}
	for rows.Next() {
		var id int64
		var wish bool
		if e = rows.Scan(&id, &wish); e != nil {
			rows.Close()
			return false, e
		}
		if wish {
			wished = append(wished, id)
		} else {
			owned = append(owned, id)
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return false, e
	}
	if len(wished)+len(owned) == 0 {
		return false, nil
	}
	return s.startPrices(ctx, wished, owned)
}
func (s *Service) startPrices(ctx context.Context, wished, owned []int64) (bool, error) {
	s.mu.Lock()
	s.priceVersion++
	version := s.priceVersion
	s.pricePending = version
	s.mu.Unlock()
	started, e := s.opts.Crawl(ctx, wished, owned)
	if started || e == nil || s.opts.CrawlBusy == nil || !s.opts.CrawlBusy(e) {
		s.mu.Lock()
		if s.pricePending == version {
			s.pricePending = 0
		}
		s.mu.Unlock()
	}
	return started, e
}
func (s *Service) PricesIdle(stopped bool) {
	s.mu.Lock()
	if stopped {
		s.pricePending = 0
	}
	if s.backfilling {
		s.backfillIdle = true
		s.mu.Unlock()
		return
	}
	if s.closing || s.pricePending == 0 {
		s.mu.Unlock()
		return
	}
	s.pricePending = 0
	s.backfilling = true
	s.backfillIdle = false
	s.jobs.Add(1)
	s.mu.Unlock()
	go func() {
		defer s.jobs.Done()
		defer func() {
			s.mu.Lock()
			s.backfilling = false
			again := s.backfillIdle && s.pricePending != 0
			s.backfillIdle = false
			s.mu.Unlock()
			if again {
				s.PricesIdle(false)
			}
		}()
		if _, e := s.Backfill(s.life); e != nil && s.life.Err() == nil {
			s.opts.Log("Steam 新增游戏价格交接尚未完成：%v", e)
		}
	}()
}
func (s *Service) SyncAll(parent context.Context) (map[string]any, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	body := func() (map[string]any, error) {
		accounts, e := s.Accounts(ctx)
		if e != nil {
			return nil, e
		}
		auto, e := s.autoPrice(ctx)
		if e != nil {
			return nil, e
		}
		synced, failed := 0, 0
		for _, a := range accounts {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			sid := a["steamid"].(string)
			v, e := s.SyncAccount(ctx, sid, auto)
			if e != nil {
				failed++
				s.opts.Log("Steam 账号本轮同步未完成 │ SteamID=%s，原因=%v", sid, e)
			} else if v["inflight"] != true {
				synced++
			}
		}
		if auto {
			if _, e = s.Backfill(ctx); e != nil {
				s.opts.Log("Steam 新增游戏价格回补尚未完成：%v", e)
			}
		}
		return map[string]any{"total": len(accounts), "synced": synced, "failed": failed, "autoCrawl": auto}, nil
	}
	if s.opts.Recorder != nil {
		return s.opts.Recorder.Run(ctx, "wishlist_sync", body)
	}
	return body()
}
func (s *Service) RegisterSchedules(ctx context.Context, clock *sched.Scheduler) error {
	return clock.RegisterInterval("wishlist_sync", s.now().Add(30*time.Second), 15*time.Minute, func(*sched.RunCtx) {
		if _, e := s.SyncAll(ctx); e != nil {
			s.opts.Log("Steam 账户自动同步未完成：%v", e)
		}
	})
}
