package wishlist

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
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
func (s *Service) backfillTargets(ctx context.Context) ([]int64, []int64, error) {
	rows, e := s.pool.DB.QueryContext(ctx, `WITH targets AS (
 SELECT w.appid,MAX(w.wishlisted) wished FROM wishlist_items w JOIN monitor_targets t ON t.target_type='game' AND t.target_id=w.appid WHERE (`+monitoring.StateExpression+`)='active' AND w.active=1 AND (w.owned=1 OR w.wishlisted=1) GROUP BY w.appid),
 cfg AS(SELECT COALESCE((SELECT value_json FROM app_settings WHERE key='crawl.owned_regions'),'null') owned)
 SELECT g.appid,g.wished FROM targets g,cfg WHERE NOT EXISTS(SELECT 1 FROM game_current_prices p WHERE p.appid=g.appid AND p.region_code='') AND EXISTS(SELECT 1 FROM crawl_regions r WHERE
 (g.wished=1 AND r.enabled=1 OR g.wished=0 AND (COALESCE(json_array_length(cfg.owned),0)=0 AND r.enabled=1 OR lower(r.code) IN(SELECT lower(value) FROM json_each(cfg.owned))))
 AND NOT EXISTS(SELECT 1 FROM game_current_prices p WHERE p.appid=g.appid AND lower(p.region_code)=lower(r.code))) ORDER BY g.appid LIMIT 400`)
	if e != nil {
		return nil, nil, e
	}
	wished, owned := []int64{}, []int64{}
	for rows.Next() {
		var id int64
		var wish bool
		if e = rows.Scan(&id, &wish); e != nil {
			rows.Close()
			return nil, nil, e
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
		return nil, nil, e
	}
	return wished, owned, nil
}
func (s *Service) Backfill(ctx context.Context) (bool, error) {
	started, _, err := s.backfillWithIDs(ctx)
	return started, err
}
func (s *Service) backfillWithIDs(ctx context.Context) (bool, []int64, error) {
	if s.opts.Crawl == nil {
		return false, nil, nil
	}
	wished, owned, e := s.backfillTargets(ctx)
	if e != nil {
		return false, nil, e
	}
	if len(wished)+len(owned) == 0 {
		return false, nil, nil
	}
	started, e := s.startPrices(ctx, wished, owned)
	if !started || e != nil {
		return started, nil, e
	}
	all := make([]int64, 0, len(wished)+len(owned))
	all = append(all, wished...)
	all = append(all, owned...)
	return true, all, nil
}
func (s *Service) stampUncrawledMissing(ctx context.Context, appids []int64) (int, error) {
	ids := cleanPoolIDs(appids)
	if len(ids) == 0 {
		return 0, nil
	}
	nowText := s.now().In(wishlistZone).Format("2006-01-02 15:04:05.000000")
	wrote := 0
	err := s.pool.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		ph := make([]string, len(ids))
		args := make([]any, len(ids))
		for i, id := range ids {
			ph[i] = "?"
			args[i] = id
		}
		rows, err := conn.QueryContext(ctx, fmt.Sprintf(`SELECT appid FROM games WHERE appid IN (%s)`, strings.Join(ph, ",")), args...)
		if err != nil {
			return err
		}
		known := map[int64]bool{}
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			known[id] = true
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return err
		}
		rows.Close()
		for _, id := range ids {
			if known[id] {
				continue
			}
			res, err := conn.ExecContext(ctx, `INSERT OR IGNORE INTO game_current_prices
				(appid,region_code,currency,price,original_price,discount_percent,sub_id,price_status,fail_count,cny_fen,attempt_outcome,last_success_at,updated_at)
				VALUES(?,'','',NULL,NULL,0,NULL,'missing',1,NULL,'failed',NULL,?)`, id, nowText)
			if err != nil {
				return err
			}
			if n, _ := res.RowsAffected(); n > 0 {
				wrote++
			}
		}
		return nil
	})
	if err == nil && wrote > 0 {
		s.opts.Log("回补后仍取不到价格，已标记为缺失数据并转入补抓通道 │ 条数=%d", wrote)
	}
	return wrote, err
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
			started, ids, e := s.backfillWithIDs(ctx)
			if e != nil {
				s.opts.Log("Steam 新增游戏价格回补尚未完成：%v", e)
			} else if started && len(ids) > 0 {
				if _, se := s.stampUncrawledMissing(ctx, ids); se != nil {
					s.opts.Log("回补缺失状态标记未完成：%v", se)
				}
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
