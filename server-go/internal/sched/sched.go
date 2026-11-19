// Package sched 进程内周期任务调度器（默认合并漏拍、1s 宽限、单实例互斥；interval 网格锚定 Start）。
package sched

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

const (
	EventFire    = "fire"
	EventSkip    = "skip"
	EventMissed  = "missed"
	EventSetNext = "setnext"
	EventStop    = "stop"
	EventPanic   = "panic"
)

// Event 调度事件；setnext 事件中 Scheduled 携带新目标时刻。
type Event struct {
	Kind      string
	JobID     string
	Label     string
	Scheduled time.Time
	At        time.Time
	Note      string
}

type RunCtx struct {
	JobID     string
	Label     string
	Scheduled time.Time

	s *Scheduler
	j *job
}

func (rc *RunCtx) Now() time.Time { return rc.s.now() }

// BusyFor 仅在模拟模式下登记占用时长，供单实例互斥判定。
func (rc *RunCtx) BusyFor(d time.Duration) {
	if rc.s.sim {
		rc.j.busyUntil = rc.s.simNow.Add(d)
	}
}

// SetNext 覆盖默认续排并重锚下次触发；零值取消触发，过去时刻进入常规到期/宽限判定。
func (rc *RunCtx) SetNext(t time.Time) {
	if rc.s.sim {
		rc.s.applySetNextLocked(rc.j, t)
		return
	}
	rc.s.setNextExternal(rc.j, t)
}

// SetJobNext：运行体重锚指定任务，模拟运行体复用已持有的调度锁。
func (rc *RunCtx) SetJobNext(id string, t time.Time) error {
	if !rc.s.sim {
		return rc.s.SetNext(id, t)
	}
	j, ok := rc.s.jobs[id]
	if !ok {
		return fmt.Errorf("任务 %s 未注册", id)
	}
	if rc.s.stopped {
		return errors.New("调度器已停止，无法重锚")
	}
	rc.s.applySetNextLocked(j, t)
	return nil
}

type job struct {
	id       string
	label    string
	trig     Trigger
	body     func(*RunCtx)
	coalesce bool
	maxInst  int
	grace    *time.Duration // nil = 无限宽限

	next      time.Time
	running   int
	busyUntil time.Time
}

type JobOption func(*job)

func WithLabel(label string) JobOption { return func(j *job) { j.label = label } }

func WithCoalesce(on bool) JobOption { return func(j *job) { j.coalesce = on } }

func WithMaxInstances(n int) JobOption {
	return func(j *job) {
		if n > 0 {
			j.maxInst = n
		}
	}
}

func WithMisfireUnlimited() JobOption { return func(j *job) { j.grace = nil } }

func WithMisfireGrace(d time.Duration) JobOption { return func(j *job) { g := d; j.grace = &g } }

func defaultMisfireGrace() *time.Duration { g := time.Second; return &g }

type SchedulerOption func(*Scheduler)

// WithOnEvent 回调在内部锁外执行，可安全重入 Scheduler；并发事件时序以 At 为准。
func WithOnEvent(fn func(Event)) SchedulerOption { return func(s *Scheduler) { s.onEvent = fn } }

type Scheduler struct {
	mu      sync.Mutex
	jobs    map[string]*job
	order   []string
	onEvent func(Event)
	pending []Event // 锁内暂存，锁外经 flush 派发，避免在内部锁内执行回调

	started bool
	sim     bool
	simNow  time.Time
	stopped bool
	stopCh  chan struct{}
	stopOne sync.Once
	wake    chan struct{}
}

func NewScheduler(opts ...SchedulerOption) *Scheduler {
	s := &Scheduler{
		jobs:   map[string]*job{},
		stopCh: make(chan struct{}),
		wake:   make(chan struct{}, 1),
	}
	for _, o := range opts {
		o(s)
	}
	return s
}

func (s *Scheduler) Running() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.started && !s.stopped
}

func (s *Scheduler) Register(id string, trig Trigger, body func(*RunCtx), opts ...JobOption) error {
	if id == "" {
		return errors.New("任务 ID 为空")
	}
	if trig == nil {
		return errors.New("任务触发器为空")
	}
	if body == nil {
		return errors.New("任务运行体为空")
	}
	j := &job{id: id, coalesce: true, maxInst: 1, grace: defaultMisfireGrace()}
	for _, o := range opts {
		o(j)
	}
	j.trig = trig
	j.body = body
	s.mu.Lock()
	if s.stopped {
		s.mu.Unlock()
		return errors.New("调度器已停止，无法注册任务")
	}
	if _, dup := s.jobs[id]; dup {
		s.mu.Unlock()
		return fmt.Errorf("任务 %s 已注册", id)
	}
	if s.started {
		j.next = j.trig.Next(s.nowLocked())
	}
	s.jobs[id] = j
	s.order = append(s.order, id)
	s.mu.Unlock()
	s.poke()
	return nil
}

func (s *Scheduler) RegisterInterval(id string, start time.Time, period time.Duration, body func(*RunCtx), opts ...JobOption) error {
	if period <= 0 {
		return errors.New("周期必须为正数")
	}
	return s.Register(id, IntervalTrigger{Start: start, Period: period}, body, opts...)
}

func (s *Scheduler) RegisterDaily(id string, hour, minute int, body func(*RunCtx), opts ...JobOption) error {
	if hour < 0 || hour > 23 || minute < 0 || minute > 59 {
		return fmt.Errorf("每日时刻 %d:%02d 越界", hour, minute)
	}
	return s.Register(id, DailyTrigger{Hour: hour, Minute: minute}, body, opts...)
}

func (s *Scheduler) SetNext(id string, t time.Time) error {
	s.mu.Lock()
	j, ok := s.jobs[id]
	if !ok {
		s.mu.Unlock()
		return fmt.Errorf("任务 %s 未注册", id)
	}
	if s.stopped {
		s.mu.Unlock()
		return errors.New("调度器已停止，无法重锚")
	}
	s.mu.Unlock()
	s.setNextExternal(j, t)
	return nil
}

func (s *Scheduler) Start() {
	s.mu.Lock()
	if s.started || s.stopped {
		s.mu.Unlock()
		return
	}
	s.started = true
	for _, j := range s.jobs {
		if j.next.IsZero() {
			j.next = j.trig.Next(s.nowLocked())
		}
	}
	s.mu.Unlock()
	go s.runReal()
}

func (s *Scheduler) StartSim(t time.Time) {
	s.mu.Lock()
	s.started = true
	s.sim = true
	s.simNow = t
	for _, j := range s.jobs {
		if j.next.IsZero() {
			j.next = j.trig.Next(t)
		}
	}
	s.mu.Unlock()
}

func (s *Scheduler) RunSim(end time.Time) {
	s.mu.Lock()
	if !s.sim {
		s.mu.Unlock()
		panic("调度器未进入模拟模式，需先 StartSim")
	}
	for !s.stopped {
		s.processAllDueLocked(s.simNow)
		nw := s.nextWakeLocked()
		if nw.IsZero() || nw.After(end) {
			break
		}
		s.simNow = nw
	}
	s.mu.Unlock()
	s.flush()
}

func (s *Scheduler) SimJump(t time.Time) {
	s.mu.Lock()
	s.simNow = t
	s.mu.Unlock()
}

// Stop：停机，不等在跑任务：已排触发作废、立即返回，在跑数量记录在 stop 事件。
func (s *Scheduler) Stop() {
	s.stopOne.Do(func() {
		s.mu.Lock()
		s.stopped = true
		inflight := 0
		for _, j := range s.jobs {
			if j.running > 0 {
				inflight++
			}
		}
		s.emitLocked(Event{Kind: EventStop, At: s.nowLocked(), Note: fmt.Sprintf("in-flight=%d", inflight)})
		s.mu.Unlock()
		close(s.stopCh)
		s.flush()
	})
}

func (s *Scheduler) now() time.Time {
	if s.sim {
		return s.simNow // 模拟模式调用方均持锁内
	}
	return time.Now()
}

func (s *Scheduler) nowLocked() time.Time {
	if s.sim {
		return s.simNow
	}
	return time.Now()
}

func (s *Scheduler) poke() {
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func (s *Scheduler) emitLocked(e Event) {
	s.pending = append(s.pending, e)
}

func (s *Scheduler) emit(e Event) {
	s.mu.Lock()
	s.emitLocked(e)
	s.mu.Unlock()
	s.flush()
}

func (s *Scheduler) flush() {
	for {
		s.mu.Lock()
		if len(s.pending) == 0 {
			s.mu.Unlock()
			return
		}
		evs := s.pending
		s.pending = nil
		s.mu.Unlock()
		for _, e := range evs {
			if s.onEvent != nil {
				s.onEvent(e)
			}
		}
	}
}

func (s *Scheduler) runReal() {
	for {
		s.mu.Lock()
		if s.stopped {
			s.mu.Unlock()
			return
		}
		nw := s.nextWakeLocked()
		s.mu.Unlock()
		if nw.IsZero() {
			select {
			case <-s.stopCh:
				return
			case <-s.wake:
			}
			continue
		}
		d := time.Until(nw)
		if d < 0 {
			d = 0
		}
		t := time.NewTimer(d)
		select {
		case <-s.stopCh:
			t.Stop()
			return
		case <-s.wake:
			t.Stop()
		case now := <-t.C:
			s.mu.Lock()
			if !s.stopped {
				s.processAllDueLocked(now)
			}
			s.mu.Unlock()
			s.flush()
		}
	}
}

func (s *Scheduler) nextWakeLocked() time.Time {
	var min time.Time
	for _, j := range s.jobs {
		if j.next.IsZero() {
			continue
		}
		if min.IsZero() || j.next.Before(min) {
			min = j.next
		}
	}
	return min
}

// processAllDueLocked：反复消化到期拍（含运行体重排产生的当拍），双层上限防病态触发器。
func (s *Scheduler) processAllDueLocked(now time.Time) {
	for pass := 0; pass < 10; pass++ {
		due := false
		for _, id := range s.order {
			j := s.jobs[id]
			if j.next.IsZero() {
				continue
			}
			for n := 0; !j.next.IsZero() && !j.next.After(now) && !s.stopped && n < 1000; n++ {
				s.processDueLocked(j, now)
				due = true
			}
		}
		if !due {
			return
		}
	}
}

func (s *Scheduler) processDueLocked(j *job, now time.Time) {
	slot := j.next
	if j.coalesce {
		for {
			nx := j.trig.Next(slot)
			if nx.After(now) || !nx.After(slot) {
				break
			}
			slot = nx
		}
	}
	if j.grace != nil && now.Sub(slot) > *j.grace {
		s.emitLocked(Event{Kind: EventMissed, JobID: j.id, Label: j.label, Scheduled: slot, At: now})
		j.next = j.trig.Next(slot)
		return
	}
	if !j.busyUntil.IsZero() && !j.busyUntil.After(now) {
		j.busyUntil = time.Time{}
		j.running = 0
	}
	if j.running >= j.maxInst {
		s.emitLocked(Event{Kind: EventSkip, JobID: j.id, Label: j.label, Scheduled: slot, At: now, Note: "上轮未完成"})
		j.next = j.trig.Next(slot)
		return
	}
	j.running++
	// 默认续排先落位作兜底，运行体内调用 SetNext 时覆盖。
	j.next = j.trig.Next(slot)
	s.emitLocked(Event{Kind: EventFire, JobID: j.id, Label: j.label, Scheduled: slot, At: now})
	rc := &RunCtx{JobID: j.id, Label: j.label, Scheduled: slot, s: s, j: j}
	if s.sim {
		s.runBodyLocked(j, rc, now)
		if j.busyUntil.IsZero() || !j.busyUntil.After(now) {
			j.running--
		}
		return
	}
	go func() {
		s.runBody(j, rc, time.Now())
		s.mu.Lock()
		j.running--
		s.mu.Unlock()
	}()
}

func (s *Scheduler) runBodyLocked(j *job, rc *RunCtx, at time.Time) {
	defer func() {
		if r := recover(); r != nil {
			s.emitLocked(Event{Kind: EventPanic, JobID: j.id, Label: j.label, Scheduled: rc.Scheduled, At: at, Note: fmt.Sprint(r)})
		}
	}()
	j.body(rc)
}

func (s *Scheduler) runBody(j *job, rc *RunCtx, at time.Time) {
	defer func() {
		if r := recover(); r != nil {
			s.emit(Event{Kind: EventPanic, JobID: j.id, Label: j.label, Scheduled: rc.Scheduled, At: at, Note: fmt.Sprint(r)})
		}
	}()
	j.body(rc)
}

func (s *Scheduler) applySetNextLocked(j *job, t time.Time) {
	j.next = t
	s.emitLocked(Event{Kind: EventSetNext, JobID: j.id, Label: j.label, Scheduled: t, At: s.nowLocked()})
}

func (s *Scheduler) setNextExternal(j *job, t time.Time) {
	s.mu.Lock()
	j.next = t
	s.emitLocked(Event{Kind: EventSetNext, JobID: j.id, Label: j.label, Scheduled: t, At: s.nowLocked()})
	s.mu.Unlock()
	s.flush()
	s.poke()
}
