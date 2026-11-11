package contentsched

import (
	"context"
	"database/sql"
	"slices"
	"sync"
	"sync/atomic"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
)

type cadence struct {
	daily sched.DailyTrigger
	days  int
}

func (c cadence) next(after time.Time) time.Time {
	next := c.daily.Next(after)
	for (next.Day()-1)%c.days != 0 {
		next = next.AddDate(0, 0, 1)
	}
	return next
}

type trigger struct {
	cadence atomic.Pointer[cadence]
}

func (t *trigger) Next(after time.Time) time.Time {
	return t.cadence.Load().next(after)
}

type Automation struct {
	Job        string
	Label      string
	Field      string
	Key        string
	Source     string
	Daily      sched.DailyTrigger
	Interval   time.Duration
	FirstAfter time.Duration
	DB         *sql.DB
	Run        func(context.Context) (map[string]any, error)
	Recorder   *jobrun.Recorder
	Log        func(string, ...any)
	Now        func() time.Time

	updateMu sync.Mutex
	clock    *sched.Scheduler
	trigger  *trigger

	runMu   sync.Mutex
	closed  bool
	runDone chan struct{}
}

func (a *Automation) readCadence(ctx context.Context) (*cadence, error) {
	if a.Field == "" {
		return &cadence{daily: a.Daily, days: 1}, nil
	}
	hour, minute, days, err := settings.ContentSchedule(ctx, a.DB, a.Field)
	return &cadence{daily: sched.DailyTrigger{Hour: hour, Minute: minute}, days: days}, err
}

func (a *Automation) Register(ctx context.Context, clock *sched.Scheduler) error {
	a.updateMu.Lock()
	defer a.updateMu.Unlock()
	cadence, err := a.readCadence(ctx)
	if ctx.Err() != nil {
		return ctx.Err()
	}
	if err != nil && a.Log != nil {
		a.Log("%s 自动刷新节奏暂未读取，采用默认时刻：%v", a.Label, err)
	}
	trigger := &trigger{}
	trigger.cadence.Store(cadence)
	now := time.Now()
	if a.Now != nil {
		now = a.Now()
	}
	var next sched.Trigger = trigger
	if a.Interval > 0 {
		first := a.FirstAfter
		if first <= 0 {
			first = a.Interval
		}
		next = sched.IntervalTrigger{Start: now.Add(first), Period: a.Interval}
	}
	if err := clock.Register(a.Job, next, func(*sched.RunCtx) { a.runScheduled(ctx) }, sched.WithLabel(a.Label)); err != nil {
		return err
	}
	if a.FirstAfter > 0 {
		if err := clock.SetNext(a.Job, now.Add(a.FirstAfter)); err != nil {
			return err
		}
	}
	a.clock, a.trigger = clock, trigger
	return nil
}

func (a *Automation) SettingsChanged(ctx context.Context, keys, kicks []string) {
	reanchor := slices.Contains(keys, a.Key)
	kick := slices.Contains(kicks, a.Job)
	if !reanchor && !kick {
		return
	}
	a.updateMu.Lock()
	defer a.updateMu.Unlock()
	if a.clock == nil || ctx.Err() != nil {
		return
	}
	if reanchor {
		cadence, err := a.readCadence(ctx)
		if err == nil {
			a.trigger.cadence.Store(cadence)
		} else if a.Log != nil {
			a.Log("%s 自动刷新节奏读取失败：%v", a.Label, err)
		}
		if err != nil && !kick {
			return
		}
	}
	now := time.Now()
	if a.Now != nil {
		now = a.Now()
	}
	next := now
	if !kick {
		next = a.trigger.Next(now)
	}
	if err := a.clock.SetNext(a.Job, next); err != nil && a.Log != nil {
		a.Log("%s 自动刷新排程尚未调整：%v", a.Label, err)
	}
}

func (a *Automation) runScheduled(ctx context.Context) {
	a.runMu.Lock()
	if a.closed || ctx.Err() != nil {
		a.runMu.Unlock()
		return
	}
	a.runDone = make(chan struct{})
	a.runMu.Unlock()
	defer func() {
		a.runMu.Lock()
		close(a.runDone)
		a.runDone = nil
		a.runMu.Unlock()
	}()

	body := func() (map[string]any, error) {
		source := a.Source
		if source == "" && a.Field != "" {
			source = a.Job
		}
		if source != "" {
			on, err := settings.ContentEnabled(ctx, a.DB, source)
			if err != nil {
				return nil, err
			}
			if !on {
				return map[string]any{"skipped": a.Label + "自动更新已关闭"}, nil
			}
		}
		return a.Run(ctx)
	}
	var err error
	if a.Recorder != nil {
		_, err = a.Recorder.Run(ctx, a.Job, body)
	} else {
		_, err = body()
	}
	if err != nil && a.Log != nil {
		a.Log("%s 本轮未完成：%v", a.Label, err)
	}
}

func (a *Automation) Close(ctx context.Context) error {
	a.runMu.Lock()
	a.closed = true
	done := a.runDone
	a.runMu.Unlock()
	if done == nil {
		return nil
	}
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
