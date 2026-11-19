package sched

import (
	"time"
	_ "time/tzdata" // Windows 无系统时区库，内嵌保证 DefaultLoc 可加载
)

var DefaultLoc = mustLoc("Asia/Shanghai")

func mustLoc(name string) *time.Location {
	loc, err := time.LoadLocation(name)
	if err != nil {
		panic(err)
	}
	return loc
}

// Trigger：给定时刻，返回严格晚于它的下一次触发；零值表示不再触发。
type Trigger interface {
	Next(after time.Time) time.Time
}

type IntervalTrigger struct {
	Start  time.Time
	Period time.Duration
}

func (it IntervalTrigger) Next(after time.Time) time.Time {
	if it.Period <= 0 {
		return time.Time{}
	}
	if it.Start.After(after) {
		return it.Start
	}
	k := after.Sub(it.Start)/it.Period + 1
	return it.Start.Add(time.Duration(k) * it.Period)
}

type DailyTrigger struct {
	Hour, Minute int
	Loc          *time.Location
}

func (ct DailyTrigger) Next(after time.Time) time.Time {
	loc := ct.Loc
	if loc == nil {
		loc = DefaultLoc
	}
	t := after.In(loc)
	cand := time.Date(t.Year(), t.Month(), t.Day(), ct.Hour, ct.Minute, 0, 0, loc)
	if !cand.After(after) {
		cand = cand.AddDate(0, 0, 1)
	}
	return cand
}
