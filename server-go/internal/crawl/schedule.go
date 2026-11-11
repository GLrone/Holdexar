package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"time"

	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

func nextPriceGrid(now time.Time, hours int) time.Time {
	if hours < 1 {
		hours = 1
	}
	if hours > 72 {
		hours = 72
	}
	pacific, _ := time.LoadLocation("America/Los_Angeles")
	hour := 2
	if now.In(pacific).IsDST() {
		hour = 1
	}
	local := now.In(beijing)
	next := time.Date(local.Year(), local.Month(), local.Day(), hour, 0, 0, 0, beijing)
	for !next.After(now) {
		next = next.Add(time.Duration(hours) * time.Hour)
	}
	return next
}

func (r *Runtime) interval(ctx context.Context) int {
	v, e := r.setting(ctx, "crawl.price_interval_hours", 6)
	if e != nil {
		return 6
	}
	var n int
	switch value := v.(type) {
	case float64:
		n = int(value)
	case int:
		n = value
	case string:
		var e error
		n, e = strconv.Atoi(value)
		if e != nil {
			return 6
		}
	default:
		return 6
	}
	return max(1, min(72, n))
}

func (r *Runtime) AutoEnabled(ctx context.Context) (bool, error) {
	v, e := r.setting(ctx, "crawl.auto_price", true)
	if e != nil {
		return false, e
	}
	switch x := v.(type) {
	case nil:
		return false, nil
	case bool:
		return x, nil
	case float64:
		return x != 0, nil
	case string:
		return x != "", nil
	default:
		return true, nil
	}
}

func (r *Runtime) RegisterSchedules(ctx context.Context, s *sched.Scheduler) error {
	now := r.now()
	if err := s.RegisterInterval("price_refresh", nextPriceGrid(now, r.interval(ctx)), 6*time.Hour, func(rc *sched.RunCtx) {
		rc.SetNext(nextPriceGrid(r.now(), r.interval(ctx)))
		on, e := r.AutoEnabled(ctx)
		if e != nil || !on {
			if e != nil {
				r.opts.Log("价格设置读取失败：%v", e)
			}
			return
		}
		specs, e := r.planner.DefaultSpecs(ctx, false)
		if e == nil {
			_, e = r.Run(ctx, "scheduled", specs)
		}
		if e != nil && !errors.Is(e, ErrBusy) {
			r.opts.Log("自动价格更新未完成：%v", e)
		}
	}, sched.WithMisfireUnlimited()); err != nil {
		return err
	}
	err := s.RegisterInterval("price_repair", now.Add(10*time.Second), 5*time.Minute, func(rc *sched.RunCtx) {
		defer func() {
			if ctx.Err() == nil && r.opts.AfterRepair != nil {
				if e := r.opts.AfterRepair(ctx); e != nil {
					r.opts.Log("本次待发提醒尚未处理完成：%v", e)
				}
			}
		}()
		on, e := r.AutoEnabled(ctx)
		if e != nil {
			r.opts.Log("补抓设置读取失败：%v", e)
			return
		}
		if !on || occupancy.CrawlerBusy() {
			return
		}
		if e = rc.SetJobNext("price_refresh", nextPriceGrid(r.now(), r.interval(ctx))); e != nil {
			r.opts.Log("价格排程校准失败：%v", e)
		}
		if e = r.kickStale(ctx, rc.SetJobNext); e != nil {
			r.opts.Log("价格时效检查失败：%v", e)
		}
		if e = r.RunRepair(ctx, 4*time.Minute); e != nil && !errors.Is(e, ErrBusy) {
			r.opts.Log("自动补抓未完成：%v", e)
		}
	}, sched.WithMisfireUnlimited())
	if err == nil {
		err = r.registerRechecks(ctx, s)
	}
	if err == nil {
		r.mu.Lock()
		r.scheduler = s
		r.mu.Unlock()
	}
	return err
}

func (r *Runtime) SettingsChanged(ctx context.Context, keys, kicks []string) {
	r.mu.Lock()
	scheduler := r.scheduler
	r.mu.Unlock()
	if scheduler == nil {
		return
	}
	for _, key := range keys {
		if key == "crawl.price_interval_hours" {
			next := nextPriceGrid(r.now(), r.interval(ctx))
			if e := scheduler.SetNext("price_refresh", next); e != nil {
				r.opts.Log("价格更新排程尚未调整：%v", e)
			} else {
				r.opts.Log("价格更新间隔已生效 │ 下轮=%s", next.Format("2006-01-02 15:04:05"))
			}
		}
		if key == "crawl.auto_price" {
			enabled, e := r.AutoEnabled(ctx)
			if e != nil {
				r.opts.Log("自动更新设置读取失败：%v", e)
			} else if enabled {
				if e = r.kickStale(ctx, scheduler.SetNext); e != nil {
					r.opts.Log("自动价格更新尚未提前安排：%v", e)
				}
			}
		}
	}
	for _, job := range kicks {
		if e := scheduler.SetNext(job, r.now().Add(time.Second)); e != nil {
			r.opts.Log("内容来源设置已保存，对应自动执行尚未接通 │ 来源=%s，原因=%v", job, e)
		}
	}
}

func (r *Runtime) kickStale(ctx context.Context, setNext func(string, time.Time) error) error {
	tasks, err := r.planner.Missing(ctx, 0)
	if err != nil {
		return err
	}
	earliest := r.now().Add(120 * time.Second)
	if len(tasks) > 0 {
		return setNext("price_repair", earliest)
	}
	ids, err := r.planner.ResolveScope(ctx, "pool", nil)
	if err != nil {
		return err
	}
	regions, err := r.planner.Regions(ctx, nil)
	if err != nil {
		return err
	}
	var last sql.NullString
	if err = r.db.QueryRowContext(ctx, "SELECT MAX(started_at) FROM price_cycles WHERE kind='scheduled'").Scan(&last); err != nil {
		return err
	}
	previous := nextPriceGrid(r.now(), r.interval(ctx)).Add(-time.Duration(r.interval(ctx)) * time.Hour)
	missed := last.Valid && last.String < timestamp(previous)
	fresh := 0
	if len(ids) > 0 {
		rows, e := r.db.QueryContext(ctx, `SELECT appid FROM game_current_prices WHERE appid IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) AND region_code IN (SELECT upper(value) FROM json_each(?)) AND last_success_at>=? GROUP BY appid HAVING COUNT(DISTINCT region_code)=?`, jsonString(ids), jsonString(regionCodes(regions)), timestamp(r.now().Add(-24*time.Hour)), len(regions))
		if e != nil {
			return e
		}
		for rows.Next() {
			fresh++
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
	}
	if missed || (len(ids) > 0 && float64(fresh)/float64(len(ids)) < .5) {
		r.opts.Log("监控价格需要更新，已安排自动执行")
		return setNext("price_refresh", earliest)
	}
	return nil
}

func (r *Runtime) RunRepair(parent context.Context, cooldown time.Duration) error {
	ctx, release, err := r.begin(parent)
	if err != nil {
		return err
	}
	defer release()
	tasks, err := r.planner.Missing(ctx, cooldown)
	if err != nil || len(tasks) == 0 {
		return err
	}
	regions, err := r.planner.Regions(ctx, nil)
	if err != nil {
		return err
	}
	start := r.now()
	id, status, err := r.runJob(ctx, 0, "repair", "", "scheduled", tasks, regions, nil)
	if id > 0 && ctx.Err() == nil && status != "stopped" {
		r.finishStandalone(ctx, id, tasks, start)
	}
	return err
}

func (r *Runtime) Recover(ctx context.Context) error {
	now := r.now()
	cutoff := timestamp(now)
	err := r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		for _, statement := range []string{
			`UPDATE crawl_jobs SET status='failed',finished_at=?,error='进程重启中断' WHERE status='running' AND (started_at IS NULL OR started_at<?)`,
			`UPDATE price_cycles SET status='failed',finished_at=?,error='进程重启中断' WHERE status IN ('planning','running','repairing','finalizing') AND (started_at IS NULL OR started_at<?)`,
		} {
			if _, e := conn.ExecContext(ctx, statement, cutoff, cutoff); e != nil {
				return e
			}
		}
		return r.recoverPendingStages(ctx, conn)
	})
	r.recoverProxyRuns(ctx, timestamp(now.Local()))
	return err
}

func (r *Runtime) Close(ctx context.Context) error {
	r.mu.Lock()
	r.closing = true
	if r.interrupt != nil {
		r.interrupt()
	}
	if r.workCancel != nil {
		r.workCancel()
	}
	r.mu.Unlock()
	ticker := time.NewTicker(20 * time.Millisecond)
	defer ticker.Stop()
	for {
		r.mu.Lock()
		busy := r.busy || r.scheduled > 0
		r.mu.Unlock()
		if !busy {
			return nil
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("价格任务尚未退出：%w", ctx.Err())
		case <-ticker.C:
		}
	}
}

func decodeObject(raw string) map[string]any {
	var v map[string]any
	_ = json.Unmarshal([]byte(raw), &v)
	if v == nil {
		v = map[string]any{}
	}
	return v
}
