// Package crawler：爬取并发骨架——worker 池、岗位 lane 租借、滑窗限流、
// 429 熔断与任务路由。
//
// 机制约定：
//   - worker 池默认 30、上限 60；worker 进程内不销毁，逐任务从岗位池租借
//     健康 lane，没有岗位就等——死出口拖不住岗位，任务队列由健康岗位消化；
//   - lane 连续失败进入 60s 冷却；冷却到期以探路位返回，必须放一个真任务
//     验证通道，验证前不得再次隔离，否则形成「隔离→到期→再隔离」的空转死锁；
//   - 滑窗限流按出口键分账，内部故障放行（fail-open），见 ratelimit.go；
//   - 429 熔断冷却 5s 起步指数翻倍至 60s 封顶，成功复位，见 breaker.go；
//   - 停止语义：停止信号后 worker 停止领取新任务，在跑任务跑完并计数，
//     队列剩余任务排干且不计数，全部 worker 收敛后 Run 返回；
//   - 进度先于完成：任务收尾唯一出口 finishTask 内，进度回调（同步、返回即
//     送达）先于完成标记——完成标记放行 Run 的收尾路径，末条进度若压在其后，
//     客户端会把已收束的任务重新置回运行态，进度条卡死在 100%；
//   - 完成计数按任务 ID 去重：重试/退避把同一任务重复投递时 done 不虚高。
//
// 本包不接 HTTP 客户端与数据库：客户端经 WithLaneFactory / WithSharedClient
// 注入，处理器经 Router 注册。
package crawler

import (
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"time"
)

const (
	// DefaultWorkers：worker 数默认值。
	DefaultWorkers = 30
	// MaxWorkers：worker 数上限（单机容量标定口径）。
	MaxWorkers = 60
	// LaneCooldown：不健康 lane 的冷却时长，到期以探路位返回。
	LaneCooldown = 60 * time.Second

	speedWindow = 10 * time.Second // 完成速率统计窗
)

// Lane：岗位池中的一条出口岗位。Client 为该岗位绑定的客户端，由 lane 工厂
// 装配；Unhealthy 由绑定客户端在连续传输失败时置位、探测成功时复位。
type Lane struct {
	Client any

	flag atomic.Bool
}

// MarkUnhealthy：标记通道不健康，岗位池随后的清扫会把它隔离进冷却区。
func (l *Lane) MarkUnhealthy() { l.flag.Store(true) }

// MarkHealthy：探测成功后复位健康标记，lane 回到岗位池正常派活。
func (l *Lane) MarkHealthy() { l.flag.Store(false) }

// Unhealthy：当前健康标记。
func (l *Lane) Unhealthy() bool { return l.flag.Load() }

// Task：爬取任务。Type 决定路由，ID 用于完成去重，Body 为任务载荷
// （处理器自行解释）。
type Task struct {
	ID   string
	Type string
	Body any
}

// Progress：进度事件。Done 按任务 ID 去重；Ok 为 Done 减 Fail 的非负值；
// Total 固定为 Run 的初始任务量，不随动态追加与重投波动；Queued 为当前
// 排队数；Speed 为近 10s 完成速率（项/秒）。
type Progress struct {
	Done   int
	Ok     int
	Fail   int
	Total  int
	Queued int
	Speed  float64
}

// Context：传给任务处理器的运行上下文。
type Context struct {
	Task   Task
	Client any   // 当前岗位绑定的客户端；单入口形态为共享客户端
	Lane   *Lane // 当前租借的岗位；单入口形态为 nil

	sched *Scheduler
}

// Enqueue：动态追加任务（重试/补投）。队列已关闭（停止或收尾）时丢弃并
// 返回 false，被丢弃的任务不计入完成数。
func (c *Context) Enqueue(t Task) bool {
	if c.sched == nil {
		return false
	}
	return c.sched.push(t)
}

// StopChan：停止信号。长时间阻塞的处理器监听它提前退出；收尾后也会关闭。
func (c *Context) StopChan() <-chan struct{} {
	if c.sched == nil {
		return nil
	}
	return c.sched.stopCh
}

// Scheduler：生产者-消费者任务调度器（worker 池 + 岗位池）。
type Scheduler struct {
	router     *Router
	workers    int
	laneCount  int
	laneFact   func(i int) *Lane
	shared     any
	onProgress func(Progress)
	onError    func(task Task, err error)
	clock      Clock
	laneWait   time.Duration // 岗位全空时的重试轮询间隔

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

// WithWorkers：worker 数；非正值取默认 30，超过上限按上限截断。
func WithWorkers(n int) Option {
	return func(s *Scheduler) {
		if n > 0 {
			s.workers = n
		}
	}
}

// WithLaneCount：岗位池 lane 数，默认等于 worker 数；大于 worker 数时
// 多出的岗位为待用出口，被隔离的岗位由它们顶替。
func WithLaneCount(n int) Option {
	return func(s *Scheduler) { s.laneCount = n }
}

// WithLaneFactory：多入口形态的岗位工厂，按序号装配 lane（含客户端）。
// 注册后进入岗位池模式：worker 逐任务租借 lane。返回 nil 的序号被忽略。
func WithLaneFactory(f func(i int) *Lane) Option {
	return func(s *Scheduler) { s.laneFact = f }
}

// WithSharedClient：单入口形态的共享客户端（未注册岗位工厂时生效）。
func WithSharedClient(c any) Option {
	return func(s *Scheduler) { s.shared = c }
}

// WithProgress：进度回调。必须同步投递（返回即送达），不得转异步——
// 完成标记以进度回调先返回为前缀（见 finishTask）。回调自身崩溃被吞掉。
func WithProgress(fn func(Progress)) Option {
	return func(s *Scheduler) { s.onProgress = fn }
}

// WithErrorSink：任务失败分类回调（作业台账记账用）；nil 为不记账。
// 回调自身崩溃被吞掉，不拖垮 worker。
func WithErrorSink(fn func(task Task, err error)) Option {
	return func(s *Scheduler) { s.onError = fn }
}

// WithClock：注入时钟（lane 冷却到期、速率窗口）；默认系统时钟。
func WithClock(c Clock) Option {
	return func(s *Scheduler) {
		if c != nil {
			s.clock = c
		}
	}
}

// NewScheduler：构造调度器。router 必须提供。
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

// Run：把初始任务入队、启动 worker，阻塞到全部任务完成或收到停止信号。
//
// 停止语义：Stop 置位后 worker 停止领取新任务，在跑任务跑完并正常计数；
// 队列剩余任务排干、不计数（对齐队列账目一致）；全部 worker 收敛后返回。
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

// Stop：请求停止。幂等；Run 进行中调用即触发停止语义，Run 之前调用则
// Run 启动后立即排空返回。
func (s *Scheduler) Stop() {
	s.stopExternal.Store(true)
	s.stopOnce.Do(func() { close(s.stopCh) })
}

// Counts：任务收尾统计（与进度事件同一口径）：完成数按任务 ID 去重，
// 成功数 = 完成数 - 失败数的非负值。
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

// push：入队一个任务并计入待完成数；队列已关闭时丢弃并返回 false。
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

// pop：取下一个任务；停止信号或队列关闭后一律返回 false
// （停止后不再领新任务，剩余任务由排干处理）。
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

// drainQueue：排干剩余队列。被丢弃任务只冲销待完成数，不进任何计数。
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

// materializeLanes：Run 开始时装配岗位池，一次运行只装配一次。
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

// acquireLane：从岗位池取一条 lane，返回 (lane, 是否探路位, 是否取到)。
//
// 冷却到期的 lane 以探路位返回（probe=true）：它此刻仍带不健康标记，但
// 正因如此才要放一个真任务去验证通道——调用方不得在验证前再次隔离它，
// 否则形成「隔离→到期→再隔离」的空转死锁；验证失败的 lane 会在归还清扫
// 时带着新的完整冷却期回到冷却区。无可用岗位返回 ok=false，调用方稍后重试。
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

// sweepUnhealthyLocked：把池里已带不健康标记的 lane 扫进冷却区——否则它们
// 既不派活也拿不到冷却复活探测，永久卡死在岗位池里。
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

// toCooldownLocked：lane 进入冷却区；已在冷却区的不刷新到期时刻。
func (s *Scheduler) toCooldownLocked(l *Lane) {
	for _, c := range s.cooldownLanes {
		if c == l {
			return
		}
	}
	s.cooldownLanes = append(s.cooldownLanes, l)
	s.cooldownUntil[l] = s.clock.Now().Add(LaneCooldown)
}

// quarantineLane：lane 连续失败 → 移出岗位池进冷却区，到期自动回来接受探测。
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

// releaseLane：任务结束后归还 lane，随后清扫不健康标记。
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
				// 全部岗位在隔离/租出：等岗位回来或停止
				select {
				case <-s.stopCh:
					return
				case <-time.After(s.laneWait):
				}
				continue
			}
			lane, probe = l, pr
			// 探路位跳过健康复查：必须放真任务验证通道（见 acquireLane 契约）
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

// runTask：执行单个任务。处理器崩溃（panic）转为错误，不拖垮 worker。
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

// finishTask：任务收尾唯一出口，顺序不变量在此强制：
//
//	计数更新 → 进度发布（同步回调，返回即送达）→ 完成标记。
//
// 完成标记放行 Run 的收尾路径——若末条进度压在其后出站，客户端会把已
// 收束的任务重新置回运行态，进度条卡死在 100%。进度回调崩溃被吞掉，
// 保证完成标记必然执行（漏发会挂死 Run）。
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
			defer func() { _ = recover() }() // 记账回调崩溃不得拖垮 worker
			s.onError(task, err)
		}()
	}
	if s.onProgress != nil {
		func() {
			defer func() { _ = recover() }() // 进度回调崩溃不得拖垮收尾
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

// recordSpeedLocked：记录一次完成并返回近 10s 完成速率（项/秒）。
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
