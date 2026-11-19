package crawler

import (
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"time"
)

const (
	DefaultWorkers = 30
	MaxWorkers     = 60
	LaneCooldown   = 60 * time.Second

	speedWindow = 10 * time.Second
)

type Lane struct {
	Client any

	flag atomic.Bool
}

func (l *Lane) MarkUnhealthy() { l.flag.Store(true) }

func (l *Lane) MarkHealthy() { l.flag.Store(false) }

func (l *Lane) Unhealthy() bool { return l.flag.Load() }

type Task struct {
	ID   string
	Type string
	Body any
}

type Progress struct {
	Done   int
	Ok     int
	Fail   int
	Total  int
	Queued int
	Speed  float64
}

type Context struct {
	Task   Task
	Client any
	Lane   *Lane

	sched *Scheduler
}

func (c *Context) Enqueue(t Task) bool {
	if c.sched == nil {
		return false
	}
	return c.sched.push(t)
}

func (c *Context) StopChan() <-chan struct{} {
	if c.sched == nil {
		return nil
	}
	return c.sched.stopCh
}

type Scheduler struct {
	router     *Router
	workers    int
	laneCount  int
	laneFact   func(i int) *Lane
	shared     any
	onProgress func(Progress)
	onError    func(task Task, err error)
	clock      Clock
	laneWait   time.Duration

	stopOnce     sync.Once
	stopCh       chan struct{}
	stopExternal atomic.Bool

	qmu    sync.Mutex
	qcond  *sync.Cond
	queue  []Task
	qclose bool

	taskWG   sync.WaitGroup
	workerWG sync.WaitGroup

	laneMu        sync.Mutex
	lanes         []*Lane
	cooldownLanes []*Lane
	cooldownUntil map[*Lane]time.Time
	poolReady     bool

	mu           sync.Mutex
	totalTarget  int
	totalDone    int
	failCount    int
	completedIDs map[string]struct{}
	speedStamps  []time.Time
}

type Option func(*Scheduler)

func WithWorkers(n int) Option {
	return func(s *Scheduler) {
		if n > 0 {
			s.workers = n
		}
	}
}

func WithLaneCount(n int) Option {
	return func(s *Scheduler) { s.laneCount = n }
}

func WithLaneFactory(f func(i int) *Lane) Option {
	return func(s *Scheduler) { s.laneFact = f }
}

func WithSharedClient(c any) Option {
	return func(s *Scheduler) { s.shared = c }
}

// WithProgress：进度回调。必须同步投递（返回即送达），不得转异步——
// 完成标记以进度回调先返回为前缀（见 finishTask）。回调自身崩溃被吞掉。
func WithProgress(fn func(Progress)) Option {
	return func(s *Scheduler) { s.onProgress = fn }
}

func WithErrorSink(fn func(task Task, err error)) Option {
	return func(s *Scheduler) { s.onError = fn }
}

func WithClock(c Clock) Option {
	return func(s *Scheduler) {
		if c != nil {
			s.clock = c
		}
	}
}

func NewScheduler(router *Router, opts ...Option) *Scheduler {
	if router == nil {
		panic("爬取调度器缺少任务路由表")
	}
	s := &Scheduler{
		router:        router,
		stopCh:        make(chan struct{}),
		completedIDs:  make(map[string]struct{}),
		cooldownUntil: make(map[*Lane]time.Time),
		clock:         systemClock{},
		laneWait:      time.Second,
	}
	s.qcond = sync.NewCond(&s.qmu)
	for _, opt := range opts {
		opt(s)
	}
	if s.workers <= 0 {
		s.workers = DefaultWorkers
	}
	if s.workers > MaxWorkers {
		s.workers = MaxWorkers
	}
	return s
}

// 动态追加的任务（Context.Enqueue）一并等待完成。不可并发重入。
func (s *Scheduler) Run(initial []Task) {
	s.mu.Lock()
	s.totalTarget = len(initial)
	s.mu.Unlock()
	s.materializeLanes()
	for _, t := range initial {
		s.push(t)
	}
	for i := 0; i < s.workers; i++ {
		s.workerWG.Add(1)
		go s.worker()
	}

	done := make(chan struct{})
	go func() {
		s.taskWG.Wait()
		close(done)
	}()
	select {
	case <-done:
	case <-s.stopCh:
	}
	s.closeQueue()
	if s.stopExternal.Load() {
		s.drainQueue()
	}
	s.workerWG.Wait()
}

func (s *Scheduler) Stop() {
	s.stopExternal.Store(true)
	s.stopOnce.Do(func() { close(s.stopCh) })
}

func (s *Scheduler) Counts() (done, ok, fail int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.derivedLocked()
}

func (s *Scheduler) derivedLocked() (done, ok, fail int) {
	done = len(s.completedIDs)
	fail = s.failCount
	ok = done - fail
	if ok < 0 {
		ok = 0
	}
	return done, ok, fail
}

func (s *Scheduler) push(t Task) bool {
	s.qmu.Lock()
	if s.qclose {
		s.qmu.Unlock()
		return false
	}
	s.taskWG.Add(1)
	s.queue = append(s.queue, t)
	s.qmu.Unlock()
	s.qcond.Broadcast()
	return true
}

func (s *Scheduler) pop() (Task, bool) {
	s.qmu.Lock()
	defer s.qmu.Unlock()
	for {
		if s.qclose || s.stopExternal.Load() {
			return Task{}, false
		}
		if len(s.queue) > 0 {
			t := s.queue[0]
			s.queue[0] = Task{}
			s.queue = s.queue[1:]
			return t, true
		}
		s.qcond.Wait()
	}
}

func (s *Scheduler) closeQueue() {
	s.qmu.Lock()
	s.qclose = true
	s.qmu.Unlock()
	s.qcond.Broadcast()
}

func (s *Scheduler) drainQueue() {
	s.qmu.Lock()
	rest := s.queue
	s.queue = nil
	s.qmu.Unlock()
	for range rest {
		s.taskWG.Done()
	}
}

func (s *Scheduler) queued() int {
	s.qmu.Lock()
	defer s.qmu.Unlock()
	return len(s.queue)
}

func (s *Scheduler) materializeLanes() {
	s.laneMu.Lock()
	defer s.laneMu.Unlock()
	if s.laneFact == nil || s.poolReady {
		return
	}
	count := s.laneCount
	if count <= 0 {
		count = s.workers
	}
	for i := 0; i < count; i++ {
		if lane := s.laneFact(i); lane != nil {
			s.lanes = append(s.lanes, lane)
		}
	}
	s.poolReady = true
}

// acquireLane 取一条可用 lane；冷却到期 lane 以 probe=true 返回供任务探路，
// 调用方不得在验证前再次隔离（防空转死锁），失败由归还清扫重入冷却。
func (s *Scheduler) acquireLane() (lane *Lane, probe bool, ok bool) {
	s.laneMu.Lock()
	defer s.laneMu.Unlock()
	s.sweepUnhealthyLocked()
	if len(s.lanes) > 0 {
		lane = s.lanes[0]
		s.lanes = s.lanes[1:]
		return lane, false, true
	}
	now := s.clock.Now()
	for i, l := range s.cooldownLanes {
		if !now.Before(s.cooldownUntil[l]) {
			delete(s.cooldownUntil, l)
			s.cooldownLanes = append(s.cooldownLanes[:i], s.cooldownLanes[i+1:]...)
			return l, true, true
		}
	}
	return nil, false, false
}

// sweepUnhealthyLocked 将池内不健康 lane 移入冷却区，使其可触发到期探测。
func (s *Scheduler) sweepUnhealthyLocked() {
	kept := s.lanes[:0]
	for _, l := range s.lanes {
		if l.Unhealthy() {
			s.toCooldownLocked(l)
		} else {
			kept = append(kept, l)
		}
	}
	s.lanes = kept
}

func (s *Scheduler) toCooldownLocked(l *Lane) {
	for _, c := range s.cooldownLanes {
		if c == l {
			return
		}
	}
	s.cooldownLanes = append(s.cooldownLanes, l)
	s.cooldownUntil[l] = s.clock.Now().Add(LaneCooldown)
}

func (s *Scheduler) quarantineLane(lane *Lane) {
	s.laneMu.Lock()
	defer s.laneMu.Unlock()
	for i, l := range s.lanes {
		if l == lane {
			s.lanes = append(s.lanes[:i], s.lanes[i+1:]...)
			break
		}
	}
	s.toCooldownLocked(lane)
}

func (s *Scheduler) releaseLane(lane *Lane) {
	s.laneMu.Lock()
	defer s.laneMu.Unlock()
	inLanes, inCooldown := false, false
	for _, l := range s.lanes {
		if l == lane {
			inLanes = true
			break
		}
	}
	if !inLanes {
		for _, l := range s.cooldownLanes {
			if l == lane {
				inCooldown = true
				break
			}
		}
	}
	if !inLanes && !inCooldown {
		s.lanes = append(s.lanes, lane)
	}
	s.sweepUnhealthyLocked()
}

func (s *Scheduler) worker() {
	defer s.workerWG.Done()
	multi := s.laneFact != nil
	var lane *Lane
	probe := false
	for {
		if multi {
			l, pr, ok := s.acquireLane()
			if !ok {
				select {
				case <-s.stopCh:
					return
				case <-time.After(s.laneWait):
				}
				continue
			}
			lane, probe = l, pr
			if !probe && lane.Unhealthy() {
				s.quarantineLane(lane)
				continue
			}
		}
		task, ok := s.pop()
		if !ok {
			if multi {
				s.releaseLane(lane)
			}
			return
		}
		err := s.runTask(task, lane)
		s.finishTask(task, err, lane, multi)
	}
}

func (s *Scheduler) runTask(task Task, lane *Lane) (err error) {
	defer func() {
		if r := recover(); r != nil {
			err = fmt.Errorf("任务执行崩溃: %v", r)
		}
	}()
	client := s.shared
	if lane != nil {
		client = lane.Client
	}
	return s.router.Route(&Context{Task: task, Client: client, Lane: lane, sched: s})
}

// finishTask 任务收尾唯一出口，强制顺序：计数更新 → 同步进度回调（吞掉 panic）→ taskWG.Done，
// 确保末条进度先于 Run 返回送达，且回调崩溃不挂死 Run。
func (s *Scheduler) finishTask(task Task, err error, lane *Lane, multi bool) {
	if multi && lane != nil {
		s.releaseLane(lane)
	}
	id := task.ID
	if id == "" {
		id = "unknown"
	}
	s.mu.Lock()
	s.totalDone++
	s.completedIDs[id] = struct{}{}
	if err != nil {
		s.failCount++
	}
	done, ok, fail := s.derivedLocked()
	total := s.totalTarget
	if total <= 0 {
		total = done
	}
	speed := s.recordSpeedLocked()
	s.mu.Unlock()

	if err != nil && s.onError != nil {
		func() {
			defer func() { _ = recover() }()
			s.onError(task, err)
		}()
	}
	if s.onProgress != nil {
		func() {
			defer func() { _ = recover() }()
			s.onProgress(Progress{
				Done:   done,
				Ok:     ok,
				Fail:   fail,
				Total:  total,
				Queued: s.queued(),
				Speed:  math.Round(speed*100) / 100,
			})
		}()
	}
	s.taskWG.Done()
}

func (s *Scheduler) recordSpeedLocked() float64 {
	now := s.clock.Now()
	s.speedStamps = append(s.speedStamps, now)
	cutoff := now.Add(-speedWindow)
	i := 0
	for i < len(s.speedStamps) && s.speedStamps[i].Before(cutoff) {
		i++
	}
	s.speedStamps = s.speedStamps[i:]
	return float64(len(s.speedStamps)) / speedWindow.Seconds()
}
