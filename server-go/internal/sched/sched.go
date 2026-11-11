// Package sched：进程内周期任务调度内核。
//
// 语义约定：
//   - 触发器只回答「严格晚于某时刻的下一次触发」：interval 网格锚定注册时刻不漂移，
//     每日任务为单时区日粒度；
//   - 错过多拍默认合并为一拍补跑（取最新原定时刻），可改逐拍补跑（WithCoalesce(false)）；
//   - 迟到宽限默认 1s，超宽限放弃该拍并出 missed 事件；需迟到必补的任务（价格主轮）
//     用 WithMisfireUnlimited 无限宽限；
//   - 单实例互斥默认开启：上轮未完成时到点即跳拍并出 skip 事件；
//   - 运行体内 SetNext 重锚下次触发，后到生效并覆盖触发器默认续排，续排仅作兜底；
//   - Stop 停机不等在跑任务：已排触发作废、立即返回，在跑数量记录在 stop 事件。
//
// 时区：包内嵌 tzdata，Windows 无系统时区库时 DefaultLoc 仍可用。
// 测试：StartSim/RunSim/SimJump 提供离散事件模拟驱动，确定性验证语义，不睡真实时间。
package sched

import (
	"errors"
	"fmt"
	"sync"
	"time"
)

// 事件类别。
const (
	EventFire    = "fire"    // 到点执行
	EventSkip    = "skip"    // 上轮未完成，跳拍
	EventMissed  = "missed"  // 迟到超宽限，放弃该拍
	EventSetNext = "setnext" // 重锚生效
	EventStop    = "stop"    // 停机
	EventPanic   = "panic"   // 运行体崩溃，已 recover
)

// Event：调度事件（运行账挂点）。At 为实际处理时刻，Scheduled 为原定触发时刻
// （setnext 事件中 Scheduled 携带新目标时刻）。
type Event struct {
	Kind      string
	JobID     string
	Label     string
	Scheduled time.Time
	At        time.Time
	Note      string
}

// RunCtx：任务运行体句柄。
type RunCtx struct {
	JobID     string
	Label     string
	Scheduled time.Time // 原定触发时刻

	s *Scheduler
	j *job
}

// Now：调度器当前时刻（模拟模式为模拟时钟）。
func (rc *RunCtx) Now() time.Time { return rc.s.now() }

// BusyFor：模拟模式登记运行体将持续占用 d（供单实例互斥判定）；
// 真实模式占用以运行体实际返回为准，此调用无效果。
func (rc *RunCtx) BusyFor(d time.Duration) {
	if rc.s.sim {
		rc.j.busyUntil = rc.s.simNow.Add(d)
	}
}

// SetNext：运行中重锚下次触发，后到生效并覆盖触发器默认续排；
// 零值取消已排触发；过去时刻按普通迟到拍处理（受宽限政策约束）。
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
	busyUntil time.Time // 模拟模式占用截止
}

// JobOption：任务注册选项。
type JobOption func(*job)

// WithLabel：任务标签（运行账展示名，随事件下发）。
func WithLabel(label string) JobOption { return func(j *job) { j.label = label } }

// WithCoalesce：错过多拍是否合并为一拍（默认开；关闭则逐拍补跑）。
func WithCoalesce(on bool) JobOption { return func(j *job) { j.coalesce = on } }

// WithMaxInstances：并发实例上限（默认 1；非正值保持默认）。
func WithMaxInstances(n int) JobOption {
	return func(j *job) {
		if n > 0 {
			j.maxInst = n
		}
	}
}

// WithMisfireUnlimited：无限宽限——迟到多久都补跑最新一拍（价格主轮）。
func WithMisfireUnlimited() JobOption { return func(j *job) { j.grace = nil } }

// WithMisfireGrace：迟到超过宽限即放弃该拍（出 missed 事件）。
func WithMisfireGrace(d time.Duration) JobOption { return func(j *job) { g := d; j.grace = &g } }

// defaultMisfireGrace：全局迟到宽限默认 1s。
func defaultMisfireGrace() *time.Duration { g := time.Second; return &g }

// SchedulerOption：调度器构造选项。
type SchedulerOption func(*Scheduler)

// WithOnEvent：事件回调（运行账挂点）。回调在调度器内部锁外执行，可安全调用
// Scheduler 方法；真实模式下并发事件到达顺序以 At 为准。
func WithOnEvent(fn func(Event)) SchedulerOption { return func(s *Scheduler) { s.onEvent = fn } }

// Scheduler：周期任务调度器。零值不可用，经 NewScheduler 构造。
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

// NewScheduler：构造调度器。
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

// Register：注册任务。重复 ID、空 ID、空运行体、调度器已停止均报错；
// 调度器已启动时注册即时初锚并唤醒驱动。
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

// RegisterInterval：注册锚点 + 周期网格任务；当前时刻早于 start 时首拍即 start。
func (s *Scheduler) RegisterInterval(id string, start time.Time, period time.Duration, body func(*RunCtx), opts ...JobOption) error {
	if period <= 0 {
		return errors.New("周期必须为正数")
	}
	return s.Register(id, IntervalTrigger{Start: start, Period: period}, body, opts...)
}

// RegisterDaily：注册每日固定时刻任务（DefaultLoc 时区）。
func (s *Scheduler) RegisterDaily(id string, hour, minute int, body func(*RunCtx), opts ...JobOption) error {
	if hour < 0 || hour > 23 || minute < 0 || minute > 59 {
		return fmt.Errorf("每日时刻 %d:%02d 越界", hour, minute)
	}
	return s.Register(id, DailyTrigger{Hour: hour, Minute: minute}, body, opts...)
}

// SetNext：重锚指定任务的下次触发，后到生效并覆盖触发器默认续排。
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

// Start：启动真实时钟驱动（非阻塞；停机用 Stop，不等在跑任务）。
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

// StartSim：进入模拟模式并置当前时刻；未初锚任务按当前时刻推算首拍。
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

// RunSim：模拟驱动——处理当前时刻全部到期拍，再跳至最早下次触发，直至 end 或停机。
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

// SimJump：模拟时钟直接跳到 t；期间到点的拍按错过拍处理。
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

// emitLocked：持锁路径暂存事件，派发统一走 flush。
func (s *Scheduler) emitLocked(e Event) {
	s.pending = append(s.pending, e)
}

// emit：无锁调用方（真实模式运行体）路径。
func (s *Scheduler) emit(e Event) {
	s.mu.Lock()
	s.emitLocked(e)
	s.mu.Unlock()
	s.flush()
}

// flush：锁外派发暂存事件；回调内产生的新事件继续排空。
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

// runReal：真实时钟驱动——定时器到点处理到期拍；注册/重锚经 wake 通道即时重排。
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

// processDueLocked：单拍到点处理——错过拍合并、宽限判定、单实例互斥、
// 默认续排先行落位作兜底，再执行运行体（运行体重锚后到生效）。
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
	// 默认续排在运行体之前生效；运行体内 SetNext 随后覆盖——
	// 重锚主导，触发器续排仅作重锚失败时的兜底。
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

// applySetNextLocked：模拟模式运行体路径（调用方已持锁，直接写入）。
func (s *Scheduler) applySetNextLocked(j *job, t time.Time) {
	j.next = t
	s.emitLocked(Event{Kind: EventSetNext, JobID: j.id, Label: j.label, Scheduled: t, At: s.nowLocked()})
}

// setNextExternal：外部与真实模式运行体路径——取锁写入、派发事件并唤醒驱动重算定时。
func (s *Scheduler) setNextExternal(j *job, t time.Time) {
	s.mu.Lock()
	j.next = t
	s.emitLocked(Event{Kind: EventSetNext, JobID: j.id, Label: j.label, Scheduled: t, At: s.nowLocked()})
	s.mu.Unlock()
	s.flush()
	s.poke()
}
