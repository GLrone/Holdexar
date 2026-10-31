// Package schedlab：调度内核实验（G2 第三块）。
// 自研最小内核（interval+cron 混合、错拍合并、单实例互斥、重锚 SetNext、
// 启动错峰、停机不等）对照 robfig/cron/v3 同场景实测，输出裁决数据。
// 语义基准：Python 侧 APScheduler 全局 coalesce=True / max_instances=1 /
// misfire_grace_time 默认 1s，价格主轮 grace=None；job 体内改写下次触发
// 后到生效、不被触发器续排覆盖（interval 续排仅作兜底）。
package main

import (
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"sync"
	"time"
	_ "time/tzdata" // Windows 无系统 tzdata，内嵌保证时区可用

	cron "github.com/robfig/cron/v3"
)

var (
	locBJ  *time.Location
	locPac *time.Location
)

func init() {
	var err error
	locBJ, err = time.LoadLocation("Asia/Shanghai")
	if err != nil {
		panic(err)
	}
	locPac, err = time.LoadLocation("America/Los_Angeles")
	if err != nil {
		panic(err)
	}
}

// ---------------------------------------------------------------- 触发器

// Trigger：给定时刻，返回严格晚于它的下一次触发。
type Trigger interface {
	Next(after time.Time) time.Time
}

// IntervalTrigger：锚点 + 周期网格；步进锚定 Start，不随实际唤醒漂移。
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

// CronTrigger：每日固定时刻（单时区日粒度，覆盖生产 cron 注册集）。
type CronTrigger struct {
	Hour, Minute int
	Loc          *time.Location
}

func (ct CronTrigger) Next(after time.Time) time.Time {
	t := after.In(ct.Loc)
	cand := time.Date(t.Year(), t.Month(), t.Day(), ct.Hour, ct.Minute, 0, 0, ct.Loc)
	if !cand.After(after) {
		cand = cand.AddDate(0, 0, 1)
	}
	return cand
}

// ---------------------------------------------------------------- 事件与任务

type Event struct {
	Kind      string // fire / skip / missed / setnext / stop / panic
	JobID     string
	Scheduled time.Time // 原定触发时刻
	At        time.Time // 实际处理时刻
	Note      string
}

// RunCtx：job 运行体句柄——重锚与占用登记。
type RunCtx struct {
	JobID     string
	Scheduled time.Time // 原定触发时刻
	now       func() time.Time
	busy      func(time.Duration)
	setNext   func(time.Time)
}

func (rc *RunCtx) Now() time.Time { return rc.now() }

// BusyFor：登记运行体将持续占用（模拟模式按虚拟时长，供单实例互斥判定）。
func (rc *RunCtx) BusyFor(d time.Duration) {
	if rc.busy != nil {
		rc.busy(d)
	}
}

// SetNext：运行体内改写下次触发时刻（自续约重锚；后到生效，覆盖默认续排）。
func (rc *RunCtx) SetNext(t time.Time) { rc.setNext(t) }

type Job struct {
	ID           string
	Trig         Trigger
	MisfireGrace *time.Duration // nil=无限宽限：迟到多久都补最新一拍
	MaxInstances int
	Body         func(rc *RunCtx)

	next      time.Time
	running   int
	busyUntil time.Time // 模拟模式：BusyFor 登记的占用截止
}

func grace(d time.Duration) *time.Duration { return &d }

// ---------------------------------------------------------------- 内核

type Kernel struct {
	mu      sync.Mutex
	jobs    map[string]*Job
	order   []string
	sim     bool
	simNow  time.Time
	events  []Event
	stopped bool
	stopCh  chan struct{}
	stopOne sync.Once
	wake    chan struct{}
	OnEvent func(Event)
}

func NewKernel() *Kernel {
	return &Kernel{jobs: map[string]*Job{}, stopCh: make(chan struct{}), wake: make(chan struct{}, 1)}
}

func (k *Kernel) Add(j *Job) {
	if j.MaxInstances <= 0 {
		j.MaxInstances = 1
	}
	k.mu.Lock()
	k.jobs[j.ID] = j
	k.order = append(k.order, j.ID)
	k.mu.Unlock()
	k.poke()
}

func (k *Kernel) poke() {
	select {
	case k.wake <- struct{}{}:
	default:
	}
}

func (k *Kernel) now() time.Time {
	if k.sim {
		return k.simNow
	}
	return time.Now()
}

// emitLocked：持锁路径追加事件（驱动循环与模拟模式）。
func (k *Kernel) emitLocked(e Event) {
	k.events = append(k.events, e)
	if k.OnEvent != nil {
		k.OnEvent(e)
	}
}

// emit：无锁调用方（真实模式 job 运行体）路径。
func (k *Kernel) emit(e Event) {
	k.mu.Lock()
	k.emitLocked(e)
	k.mu.Unlock()
}

// StartSim：进入模拟模式并置当前时刻；未初始化的 job 以当前时刻推算首拍。
func (k *Kernel) StartSim(t time.Time) {
	k.mu.Lock()
	k.sim = true
	k.simNow = t
	for _, j := range k.jobs {
		if j.next.IsZero() {
			j.next = j.Trig.Next(t)
		}
	}
	k.mu.Unlock()
}

// RunSim：离散事件驱动——先把当前时刻的所有到期拍处理完，再跳到最早的
// 下次触发，直至 end 或停机。
func (k *Kernel) RunSim(end time.Time) {
	k.mu.Lock()
	defer k.mu.Unlock()
	for !k.stopped {
		k.processAllDueLocked(k.simNow)
		nw := k.nextWakeLocked()
		if nw.IsZero() || nw.After(end) {
			return
		}
		k.simNow = nw
	}
}

// SimJump：冻结期注入——时钟直接跳到 t，期间到点的拍按错过处理。
func (k *Kernel) SimJump(t time.Time) {
	k.mu.Lock()
	k.simNow = t
	k.mu.Unlock()
}

// SimStop：停机不等在跑 job（wait=false 语义）：登记停止与在跑数，
// 已排触发作废，立即返回。
func (k *Kernel) SimStop() {
	k.mu.Lock()
	defer k.mu.Unlock()
	if k.stopped {
		return
	}
	k.stopped = true
	inflight := 0
	for _, j := range k.jobs {
		if j.running > 0 {
			inflight++
		}
	}
	k.emitLocked(Event{Kind: "stop", At: k.simNow, Note: fmt.Sprintf("in-flight=%d", inflight)})
}

// RunReal：真实时钟驱动——定时器到点处理 due，SetNext/Add 唤醒重排。
// 调用方 goroutine 阻塞至 Stop。
func (k *Kernel) RunReal() {
	k.mu.Lock()
	k.sim = false
	for _, j := range k.jobs {
		if j.next.IsZero() {
			j.next = j.Trig.Next(time.Now())
		}
	}
	k.mu.Unlock()
	for {
		k.mu.Lock()
		if k.stopped {
			k.mu.Unlock()
			return
		}
		nw := k.nextWakeLocked()
		k.mu.Unlock()
		if nw.IsZero() {
			select {
			case <-k.stopCh:
				return
			case <-k.wake:
			}
			continue
		}
		d := time.Until(nw)
		if d < 0 {
			d = 0
		}
		t := time.NewTimer(d)
		select {
		case <-k.stopCh:
			t.Stop()
			return
		case <-k.wake:
			t.Stop()
		case now := <-t.C:
			k.mu.Lock()
			if !k.stopped {
				k.processAllDueLocked(now)
			}
			k.mu.Unlock()
		}
	}
}

// Stop：停机不等在跑 job（wait=false 语义）：已排触发作废、立即返回，
// 在跑运行体不等待（计数登记在 stop 事件里）。
func (k *Kernel) Stop() {
	k.stopOne.Do(func() {
		k.mu.Lock()
		k.stopped = true
		inflight := 0
		for _, j := range k.jobs {
			if j.running > 0 {
				inflight++
			}
		}
		e := Event{Kind: "stop", At: time.Now(), Note: fmt.Sprintf("in-flight=%d", inflight)}
		k.emitLocked(e)
		k.mu.Unlock()
		close(k.stopCh)
	})
}

func (k *Kernel) nextWakeLocked() time.Time {
	var min time.Time
	for _, j := range k.jobs {
		if j.next.IsZero() {
			continue
		}
		if min.IsZero() || j.next.Before(min) {
			min = j.next
		}
	}
	return min
}

func (k *Kernel) processAllDueLocked(now time.Time) {
	for pass := 0; pass < 10; pass++ {
		due := false
		for _, id := range k.order {
			j := k.jobs[id]
			if j.next.IsZero() {
				continue
			}
			for n := 0; !j.next.After(now) && !k.stopped && n < 1000; n++ {
				k.processDueLocked(j, now)
				due = true
			}
		}
		if !due {
			return
		}
	}
}

// processDueLocked：单拍到点处理——coalesce 坍缩、宽限判定、单实例互斥、
// 默认续排后执行运行体（SetNext 后到生效）。
func (k *Kernel) processDueLocked(j *Job, now time.Time) {
	s := j.next
	for {
		nx := j.Trig.Next(s)
		if nx.After(now) || !nx.After(s) {
			break
		}
		s = nx
	}
	if j.MisfireGrace != nil && now.Sub(s) > *j.MisfireGrace {
		k.emitLocked(Event{Kind: "missed", JobID: j.ID, Scheduled: s, At: now})
		j.next = j.Trig.Next(s)
		return
	}
	if !j.busyUntil.IsZero() && !j.busyUntil.After(now) {
		j.busyUntil = time.Time{}
		j.running = 0
	}
	if j.running >= j.MaxInstances {
		k.emitLocked(Event{Kind: "skip", JobID: j.ID, Scheduled: s, At: now,
			Note: fmt.Sprintf("上轮未完成 busyUntil=%s", ft(j.busyUntil))})
		j.next = j.Trig.Next(s)
		return
	}
	j.running++
	// 默认续排在运行体之前生效；运行体内 SetNext 随后覆盖——
	// 重锚主导，interval 续排仅作重锚失败时的兜底。
	j.next = j.Trig.Next(s)
	k.emitLocked(Event{Kind: "fire", JobID: j.ID, Scheduled: s, At: now})
	rc := &RunCtx{
		JobID:     j.ID,
		Scheduled: s,
		now:       k.now,
		busy:      func(d time.Duration) { j.busyUntil = k.now().Add(d) },
		setNext:   func(t time.Time) { k.applySetNext(j, t) },
	}
	if k.sim {
		k.runBodyLocked(j, rc, now)
		if j.busyUntil.IsZero() || !j.busyUntil.After(now) {
			j.running--
		}
		return
	}
	go func() {
		k.runBody(j, rc, time.Now())
		k.mu.Lock()
		j.running--
		k.mu.Unlock()
	}()
}

func (k *Kernel) runBodyLocked(j *Job, rc *RunCtx, at time.Time) {
	defer func() {
		if r := recover(); r != nil {
			k.emitLocked(Event{Kind: "panic", JobID: j.ID, Scheduled: rc.Scheduled, At: at, Note: fmt.Sprint(r)})
		}
	}()
	j.Body(rc)
}

func (k *Kernel) runBody(j *Job, rc *RunCtx, at time.Time) {
	defer func() {
		if r := recover(); r != nil {
			k.emit(Event{Kind: "panic", JobID: j.ID, Scheduled: rc.Scheduled, At: at, Note: fmt.Sprint(r)})
		}
	}()
	j.Body(rc)
}

func (k *Kernel) applySetNext(j *Job, t time.Time) {
	if k.sim {
		j.next = t
		k.emitLocked(Event{Kind: "setnext", JobID: j.ID, Scheduled: t, At: k.simNow})
		return
	}
	k.mu.Lock()
	j.next = t
	k.mu.Unlock()
	k.emit(Event{Kind: "setnext", JobID: j.ID, Scheduled: t, At: time.Now()})
	k.poke()
}

func (k *Kernel) eventsOf(kind, id string) []Event {
	k.mu.Lock()
	defer k.mu.Unlock()
	var out []Event
	for _, e := range k.events {
		if e.Kind == kind && (id == "" || e.JobID == id) {
			out = append(out, e)
		}
	}
	return out
}

func (k *Kernel) countOf(kind string) int { return len(k.eventsOf(kind, "")) }

func (k *Kernel) snapshot() []Event {
	k.mu.Lock()
	defer k.mu.Unlock()
	return append([]Event(nil), k.events...)
}

func firstAt(f []Event) time.Time {
	if len(f) == 0 {
		return time.Time{}
	}
	return f[0].At
}

// ---------------------------------------------------------------- 价格网格（job 侧，external_time 同构）

// anchorHour：Steam 折扣刷新的北京锚点小时——太平洋夏令时 01:00 / 冬令时 02:00。
func anchorHour(now time.Time) int {
	if now.In(locPac).IsDST() {
		return 1
	}
	return 2
}

// gridNext：锚点 + step 网格中严格晚于 now 的下一时刻。
func gridNext(now time.Time, step time.Duration) time.Time {
	b := now.In(locBJ)
	base := time.Date(b.Year(), b.Month(), b.Day(), anchorHour(now), 0, 0, 0, locBJ)
	for !base.After(now) {
		base = base.Add(step)
	}
	return base
}

// priceBody：价格主轮运行体——按运行时刻重算网格并自续约。
func priceBody(rc *RunCtx) {
	rc.SetNext(gridNext(rc.Now(), 6*time.Hour))
}

// ---------------------------------------------------------------- 断言与输出

var failed bool

func checkf(ok bool, format string, args ...any) {
	mark := "PASS"
	if !ok {
		mark = "FAIL"
		failed = true
	}
	fmt.Printf("  [%s] %s\n", mark, fmt.Sprintf(format, args...))
}

func ft(t time.Time) string {
	if t.IsZero() {
		return "零值"
	}
	return t.In(locBJ).Format("01-02 15:04:05.000")
}

func ftShort(t time.Time) string { return t.In(locBJ).Format("2006-01-02 15:04") }

func parseBJ(s string) time.Time {
	t, err := time.ParseInLocation("2006-01-02 15:04", s, locBJ)
	if err != nil {
		panic(err)
	}
	return t
}

// ---------------------------------------------------------------- 场景 1：48h 价格网格 + DST 切换

func scenarioGridDST() {
	fmt.Println("== 场景 1：48h 价格网格，锚点在第 24 小时经历 DST 切换（2026-11-01 秋季回拨，北京锚点 01:00→02:00）")
	start := time.Date(2026, 10, 31, 17, 0, 0, 0, locBJ)
	end := start.Add(48 * time.Hour)
	k := NewKernel()
	first := gridNext(start, 6*time.Hour)
	k.Add(&Job{ID: "price_refresh", Trig: IntervalTrigger{Start: first, Period: 6 * time.Hour},
		MisfireGrace: nil, MaxInstances: 1, Body: priceBody})
	k.StartSim(start)
	k.RunSim(end)

	fires := k.eventsOf("fire", "price_refresh")
	expect := []string{
		"2026-10-31 19:00", "2026-11-01 01:00", "2026-11-01 07:00", "2026-11-01 13:00",
		"2026-11-01 19:00", "2026-11-01 20:00", "2026-11-02 02:00", "2026-11-02 08:00", "2026-11-02 14:00",
	}
	fmt.Printf("  %-4s %-21s %-21s %-8s %-12s\n", "#", "预期(北京)", "实际(北京)", "偏差", "距上一拍")
	allOK := len(fires) == len(expect)
	prev := start
	for i, e := range fires {
		exp := "-"
		if i < len(expect) {
			exp = expect[i]
		}
		gap := e.Scheduled.Sub(prev)
		deltaMS := e.At.Sub(e.Scheduled).Milliseconds()
		fmt.Printf("  #%-3d %-21s %-21s %-6dms %-12s\n", i+1, exp, ftShort(e.Scheduled), deltaMS, gap)
		prev = e.Scheduled
		if i < len(expect) && !e.Scheduled.Equal(parseBJ(expect[i])) {
			allOK = false
		}
	}
	if len(fires) != len(expect) {
		allOK = false
	}
	checkf(allOK, "触发序列 %d/%d 全对，偏差全部 0ms（离散事件内核精确到拍）", len(fires), len(expect))
	if len(fires) == 9 {
		checkf(fires[5].Scheduled.Sub(fires[4].Scheduled) == time.Hour,
			"DST 重锚跨步：11-01 19:00 → 20:00 = 1h 追赶拍（秋季锚点 01:00→02:00，间隔偏短）")
		checkf(fires[6].Scheduled.Sub(fires[5].Scheduled) == 6*time.Hour,
			"追赶拍后恢复 6h 步进（11-02 02:00 起为新网格 2/8/14/20）")
	}
	fmt.Println("  setnext 事件（自续约改写）:", k.countOf("setnext"), "次；interval 兜底续排被重锚覆盖")
}

// ---------------------------------------------------------------- 场景 2：执行 > 周期（max_instances=1 跳拍）

func scenarioBusy() {
	fmt.Println("== 场景 2：job 执行 14h > 周期 6h（max_instances=1 跳拍语义）")
	start := time.Date(2026, 10, 31, 17, 0, 0, 0, locBJ)
	k := NewKernel()
	first := gridNext(start, 6*time.Hour)
	k.Add(&Job{ID: "heavy", Trig: IntervalTrigger{Start: first, Period: 6 * time.Hour},
		MisfireGrace: grace(time.Second), MaxInstances: 1,
		Body: func(rc *RunCtx) { rc.BusyFor(14 * time.Hour) }})
	k.StartSim(start)
	k.RunSim(start.Add(24 * time.Hour))

	var seq []string
	for _, e := range k.events {
		if e.JobID != "heavy" {
			continue
		}
		seq = append(seq, fmt.Sprintf("%s@原定%s", e.Kind, ftShort(e.Scheduled)))
	}
	got := ""
	for i, s := range seq {
		if i > 0 {
			got += "  "
		}
		got += s
	}
	fmt.Println("  事件序列:", got)
	want := "fire@原定2026-10-31 19:00  skip@原定2026-11-01 01:00  skip@原定2026-11-01 07:00  fire@原定2026-11-01 13:00"
	checkf(got == want, "上轮未完成时到点即跳拍并产生 skip 信号（含原定时刻），错过的拍不重排队")
	checkf(len(k.eventsOf("skip", "heavy")) == 2, "skip 事件数 = 2，每次跳拍一条")
}

// ---------------------------------------------------------------- 场景 3：冻结期错过多拍（coalesce + grace 对照）

func scenarioMiss() {
	fmt.Println("== 场景 3：冻结 10h 错过 2 拍——无限宽限补最新一拍 vs 有限宽限丢拍")
	start := time.Date(2026, 10, 31, 17, 0, 0, 0, locBJ)
	k := NewKernel()
	first := gridNext(start, 6*time.Hour)
	k.Add(&Job{ID: "price", Trig: IntervalTrigger{Start: first, Period: 6 * time.Hour},
		MisfireGrace: nil, MaxInstances: 1, Body: priceBody})
	k.Add(&Job{ID: "limited", Trig: IntervalTrigger{Start: first, Period: 6 * time.Hour},
		MisfireGrace: grace(time.Hour), MaxInstances: 1, Body: func(rc *RunCtx) {}})
	k.StartSim(start)
	k.RunSim(time.Date(2026, 11, 1, 7, 30, 0, 0, locBJ)) // 07:00 拍跑完后冻结
	k.SimJump(time.Date(2026, 11, 1, 17, 30, 0, 0, locBJ))
	k.RunSim(time.Date(2026, 11, 2, 3, 0, 0, 0, locBJ))

	price := k.eventsOf("fire", "price")
	var ps []string
	for _, e := range price {
		ps = append(ps, fmt.Sprintf("%s(原定%s)", ftShort(e.At), ftShort(e.Scheduled)))
	}
	fmt.Println("  price（grace=None）  触发:", ps)
	checkf(len(price) == 6 &&
		price[3].At.Equal(parseBJ("2026-11-01 17:30").In(locBJ)) &&
		price[3].Scheduled.Equal(parseBJ("2026-11-01 13:00")) &&
		price[4].Scheduled.Equal(parseBJ("2026-11-01 20:00")) &&
		price[5].Scheduled.Equal(parseBJ("2026-11-02 02:00")),
		"恢复瞬间补跑最新一拍（原定 13:00 @17:30 执行）只补一拍，随后重锚出追赶拍 20:00 并落位新网格 02:00")
	lim := k.eventsOf("fire", "limited")
	miss := k.eventsOf("missed", "limited")
	checkf(len(lim) == 5 && len(miss) == 1 && miss[0].Scheduled.Equal(parseBJ("2026-11-01 13:00")),
		"grace=1h：错过拍超宽限 → missed 信号 1 条 + 该拍不补，interval 网格 19:00 起恢复（共 %d 拍）", len(lim))
}

// ---------------------------------------------------------------- 场景 4：混合注册 48h + 错峰 + 停机

func scenarioMix() {
	fmt.Println("== 场景 4：混合注册 48h（interval 全家族 + 每日 cron + 启动错峰 + 停机不等）")
	start := time.Date(2026, 10, 31, 17, 0, 0, 0, locBJ)
	end := start.Add(48 * time.Hour)
	k := NewKernel()
	first := gridNext(start, 6*time.Hour)
	k.Add(&Job{ID: "wallet_sync", Trig: IntervalTrigger{Start: start.Add(5 * time.Second), Period: time.Minute},
		MisfireGrace: grace(time.Second), Body: func(rc *RunCtx) {}})
	k.Add(&Job{ID: "price_repair", Trig: IntervalTrigger{Start: start.Add(9 * time.Second), Period: 5 * time.Minute},
		MisfireGrace: grace(time.Second), Body: func(rc *RunCtx) {}})
	k.Add(&Job{ID: "subscription_refresh", Trig: IntervalTrigger{Start: start.Add(14 * time.Second), Period: 30 * time.Minute},
		MisfireGrace: grace(time.Second), Body: func(rc *RunCtx) {}})
	k.Add(&Job{ID: "price_refresh", Trig: IntervalTrigger{Start: first, Period: 6 * time.Hour},
		MisfireGrace: nil, Body: priceBody})
	k.Add(&Job{ID: "board_popularnew", Trig: IntervalTrigger{Start: start.Add(30 * time.Second), Period: 24 * time.Hour},
		MisfireGrace: grace(time.Second), Body: func(rc *RunCtx) {}})
	k.Add(&Job{ID: "fx_refresh", Trig: CronTrigger{Hour: 3, Minute: 0, Loc: locBJ},
		MisfireGrace: grace(time.Second), Body: func(rc *RunCtx) {}})
	k.Add(&Job{ID: "wal_truncate", Trig: CronTrigger{Hour: 4, Minute: 30, Loc: locBJ},
		MisfireGrace: grace(time.Second), Body: func(rc *RunCtx) {}})
	k.Add(&Job{ID: "steam_events_sync", Trig: CronTrigger{Hour: 5, Minute: 0, Loc: locBJ},
		MisfireGrace: grace(time.Second), Body: func(rc *RunCtx) {}})
	k.StartSim(start)
	k.RunSim(end)

	firsts := map[string]string{
		"wallet_sync": "10-31 17:00:05", "price_repair": "10-31 17:00:09",
		"subscription_refresh": "10-31 17:00:14", "board_popularnew": "10-31 17:00:30",
	}
	for id, want := range firsts {
		f := k.eventsOf("fire", id)
		checkf(len(f) > 0 && firstAt(f).In(locBJ).Format("01-02 15:04:05") == want,
			"%s 启动错峰首拍 = 启动+%s（实测 %s，共 %d 拍）", id, want[6:], ft(firstAt(f)), len(f))
	}
	cadence := map[string]time.Duration{"wallet_sync": time.Minute, "price_repair": 5 * time.Minute, "subscription_refresh": 30 * time.Minute}
	for id, p := range cadence {
		ok := true
		f := k.eventsOf("fire", id)
		if len(f) == 0 {
			ok = false
		}
		for i := 1; i < len(f); i++ {
			if f[i].Scheduled.Sub(f[i-1].Scheduled) != p {
				ok = false
			}
		}
		checkf(ok, "%s 全部拍距 = %s（错峰首拍后网格锚定不漂移，共 %d 拍）", id, p, len(f))
	}
	for id, hm := range map[string]string{"fx_refresh": "03:00:00", "wal_truncate": "04:30:00", "steam_events_sync": "05:00:00"} {
		f := k.eventsOf("fire", id)
		ok := len(f) == 2
		for _, e := range f {
			if e.At.In(locBJ).Format("15:04:05") != hm {
				ok = false
			}
		}
		checkf(ok, "%s 每日 cron 恰好 2 拍且均在 %s（11-01 与 11-02）", id, hm)
	}
	price := k.eventsOf("fire", "price_refresh")
	checkf(len(price) == 9 && price[5].Scheduled.Equal(parseBJ("2026-11-01 20:00")),
		"混合注册下价格主轮仍完成 DST 重锚（11-01 20:00 追赶拍，随后 11-02 02:00 新网格）")
	checkf(k.countOf("skip") == 0 && k.countOf("missed") == 0 && k.countOf("panic") == 0,
		"48h 全窗无 skip / missed / panic 事件")

	// 停机不等：job 在跑时停机，立即返回且不再有事件
	k2 := NewKernel()
	k2.Add(&Job{ID: "longrun", Trig: IntervalTrigger{Start: start.Add(10 * time.Minute), Period: time.Hour},
		MisfireGrace: grace(time.Second), Body: func(rc *RunCtx) { rc.BusyFor(3 * time.Hour) }})
	k2.StartSim(start)
	k2.RunSim(start.Add(11 * time.Minute)) // 首拍 fire 且进入 3h 占用
	k2.SimStop()
	before := len(k2.events)
	k2.RunSim(start.Add(20 * time.Hour))
	stops := k2.eventsOf("stop", "")
	checkf(len(stops) == 1 && stops[0].Note == "in-flight=1" && len(k2.events) == before,
		"在跑时停机：stop 事件登记 in-flight=1，停机后 0 新事件、立即返回")
}

// ---------------------------------------------------------------- 场景 5：真实时钟重叠对照（自研 vs robfig）

type concTracker struct {
	mu  sync.Mutex
	cur int
	max int
}

func (c *concTracker) enter() {
	c.mu.Lock()
	c.cur++
	if c.cur > c.max {
		c.max = c.cur
	}
	c.mu.Unlock()
}

func (c *concTracker) leave() {
	c.mu.Lock()
	c.cur--
	c.mu.Unlock()
}

func (c *concTracker) peak() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.max
}

// fixedDelaySchedule：@every 语义的亚秒等价——robfig 的 Every 把亚秒间隔
// 舍入到整秒（constantdelay.go），实验用自定义 Schedule 才能取到 200ms 节拍。
type fixedDelaySchedule struct{ period time.Duration }

func (d fixedDelaySchedule) Next(t time.Time) time.Time { return t.Add(d.period) }

func scenarioReal() {
	fmt.Println("== 场景 5：真实时钟——执行 500ms > 周期 200ms，三配置对照（窗口 3s）")

	runSelf := func() (fires, skips []int, peak int, inflight string) {
		k := NewKernel()
		t0 := time.Now()
		tr := &concTracker{}
		k.Add(&Job{ID: "overlap", Trig: IntervalTrigger{Start: t0.Add(200 * time.Millisecond), Period: 200 * time.Millisecond},
			MisfireGrace: grace(time.Second), MaxInstances: 1,
			Body: func(rc *RunCtx) {
				tr.enter()
				time.Sleep(500 * time.Millisecond)
				tr.leave()
			}})
		go k.RunReal()
		time.Sleep(3 * time.Second)
		k.Stop()
		time.Sleep(800 * time.Millisecond) // 收尾在跑运行体，保证计数完整
		for _, e := range k.snapshot() {
			off := e.At.Sub(t0).Milliseconds()
			switch e.Kind {
			case "fire":
				fires = append(fires, int(off))
			case "skip":
				skips = append(skips, int(off))
			case "stop":
				inflight = e.Note
			}
		}
		return fires, skips, tr.peak(), inflight
	}
	fires, skips, peak, inflight := runSelf()
	fmt.Printf("  自研内核   fires(ms)=%v\n             skips(ms)=%v\n             并发峰值=%d  %s\n", fires, skips, peak, inflight)
	checkf(peak == 1, "自研内核：运行体并发峰值 = 1（单实例互斥）")
	checkf(len(skips) > 0, "自研内核：重叠拍产生 skip 信号 %d 条（含原定时刻）", len(skips))
	checkf(len(fires) >= 3 && len(fires) <= 9, "自研内核：实际执行 %d 次 ≈ 周期让位后的节奏", len(fires))

	countRobfig := func(chain bool) (offsets []int, peak int) {
		c := cron.New()
		if chain {
			c = cron.New(cron.WithChain(cron.SkipIfStillRunning(cron.PrintfLogger(log.New(io.Discard, "", 0)))))
		}
		t0 := time.Now()
		tr := &concTracker{}
		var mu sync.Mutex
		c.Schedule(fixedDelaySchedule{period: 200 * time.Millisecond}, cron.FuncJob(func() {
			mu.Lock()
			offsets = append(offsets, int(time.Since(t0).Milliseconds()))
			mu.Unlock()
			tr.enter()
			time.Sleep(500 * time.Millisecond)
			tr.leave()
		}))
		c.Start()
		time.Sleep(3 * time.Second)
		<-c.Stop().Done()
		return offsets, tr.peak()
	}
	defOffs, defPeak := countRobfig(false)
	fmt.Printf("  robfig 默认 runs=%d 并发峰值=%d\n", len(defOffs), defPeak)
	checkf(len(defOffs) >= 12 && defPeak >= 2, "robfig 默认：运行体并发峰值 = %d（无 max_instances 语义，重叠并行执行，%d 次）", defPeak, len(defOffs))

	chnOffs, chnPeak := countRobfig(true)
	fmt.Printf("  robfig+SkipIfStillRunning runs=%d 并发峰值=%d\n", len(chnOffs), chnPeak)
	checkf(chnPeak == 1 && len(chnOffs) < len(defOffs), "robfig+Skip 链：可串行化（峰值=%d，执行 %d 次），但跳过的拍无任何信号可订阅", chnPeak, len(chnOffs))
}

// ---------------------------------------------------------------- 场景 6：robfig 探针——运行中改写 / 错过拍

// overridableSchedule：带 SetNext 的自定义 Schedule（等价「重锚」的最贴近写法）。
type overridableSchedule struct {
	mu     sync.Mutex
	target time.Time
	period time.Duration
}

func (o *overridableSchedule) Next(t time.Time) time.Time {
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.target.After(t) {
		return o.target
	}
	return t.Add(o.period)
}

func (o *overridableSchedule) SetTarget(t time.Time) {
	o.mu.Lock()
	o.target = t
	o.mu.Unlock()
}

// oneShotSchedule：Next 返回零值即被 robfig 移除。
type oneShotSchedule struct{ at time.Time }

func (s oneShotSchedule) Next(t time.Time) time.Time {
	if s.at.After(t) {
		return s.at
	}
	return time.Time{}
}

// anchoredStallSchedule：锚定网格 + 第 stallCall 次调用拖住循环 stallDur。
type anchoredStallSchedule struct {
	start     time.Time
	period    time.Duration
	calls     int
	stallCall int
	stallDur  time.Duration

	mu       sync.Mutex
	declared []time.Time
}

func (g *anchoredStallSchedule) Next(t time.Time) time.Time {
	g.mu.Lock()
	g.calls++
	g.mu.Unlock()
	if g.calls == g.stallCall {
		time.Sleep(g.stallDur)
	}
	var nx time.Time
	if t.Before(g.start) {
		nx = g.start
	} else {
		k := t.Sub(g.start)/g.period + 1
		nx = g.start.Add(time.Duration(k) * g.period)
	}
	g.mu.Lock()
	g.declared = append(g.declared, nx)
	g.mu.Unlock()
	return nx
}

func (g *anchoredStallSchedule) declaredSlots() []time.Time {
	g.mu.Lock()
	defer g.mu.Unlock()
	return append([]time.Time(nil), g.declared...)
}

// delayStallSchedule：@every 语义等价（Next=t+period）+ 同款拖住。
type delayStallSchedule struct {
	period    time.Duration
	calls     int
	stallCall int
	stallDur  time.Duration

	mu       sync.Mutex
	declared []time.Time
}

func (d *delayStallSchedule) Next(t time.Time) time.Time {
	d.mu.Lock()
	d.calls++
	d.mu.Unlock()
	if d.calls == d.stallCall {
		time.Sleep(d.stallDur)
	}
	nx := t.Add(d.period)
	d.mu.Lock()
	d.declared = append(d.declared, nx)
	d.mu.Unlock()
	return nx
}

func (d *delayStallSchedule) declaredSlots() []time.Time {
	d.mu.Lock()
	defer d.mu.Unlock()
	return append([]time.Time(nil), d.declared...)
}

func scenarioRobfig() {
	fmt.Println("== 场景 6：robfig 探针——运行中改写下次触发 / 错过拍行为")

	// 6a. 自定义 Schedule 的 SetNext：已在排的下一拍能否被提前/顺延
	func() {
		c := cron.New()
		t0 := time.Now()
		sched := &overridableSchedule{target: t0.Add(500 * time.Millisecond), period: 200 * time.Millisecond}
		var mu sync.Mutex
		var offs []int
		c.Schedule(sched, cron.FuncJob(func() {
			mu.Lock()
			offs = append(offs, int(time.Since(t0).Milliseconds()))
			mu.Unlock()
		}))
		c.Start()
		time.Sleep(100 * time.Millisecond)
		sched.SetTarget(t0.Add(200 * time.Millisecond))  // 提前重锚尝试
		time.Sleep(500 * time.Millisecond)               // 越过 fire1（≈500ms）
		sched.SetTarget(t0.Add(1400 * time.Millisecond)) // 顺延
		time.Sleep(1400 * time.Millisecond)
		<-c.Stop().Done()
		fmt.Printf("  6a SetNext 探针 fires(ms)=%v（期望看到 ~500/~700/~1400，而非 200）\n", offs)
		okEarly := true
		for _, o := range offs {
			if o < 400 {
				okEarly = false
			}
		}
		checkf(len(offs) >= 2 && okEarly, "已在排的下一拍不可提前：100ms 处 SetNext(200ms) 未生效")
		okPostpone := false
		for _, o := range offs {
			if o >= 1250 && o <= 1650 {
				okPostpone = true
			}
		}
		checkf(okPostpone, "顺延在循环重算后生效：1400ms 处出现触发（SetTarget 于 600ms 写入）")
	}()

	// 6a2. 立即重锚唯一支持路径：Remove + 重挂
	func() {
		c := cron.New()
		t0 := time.Now()
		var mu sync.Mutex
		fired := map[cron.EntryID][]int{}
		id1 := c.Schedule(oneShotSchedule{at: t0.Add(500 * time.Millisecond)}, cron.FuncJob(func() {
			mu.Lock()
			fired[0] = append(fired[0], int(time.Since(t0).Milliseconds()))
			mu.Unlock()
		}))
		c.Start()
		time.Sleep(100 * time.Millisecond)
		c.Remove(id1)
		id2 := c.Schedule(oneShotSchedule{at: t0.Add(200 * time.Millisecond)}, cron.FuncJob(func() {
			mu.Lock()
			fired[1] = append(fired[1], int(time.Since(t0).Milliseconds()))
			mu.Unlock()
		}))
		time.Sleep(time.Second)
		<-c.Stop().Done()
		fmt.Printf("  6a2 Remove+重挂 旧ID=%d(触发%d次) 新ID=%d(触发%v)\n", id1, len(fired[0]), id2, fired[1])
		checkf(len(fired[0]) == 0 && id2 != id1, "Remove+重挂可立即改下次触发：旧身份作废（ID %d→%d），原定 500ms 拍未执行", id1, id2)
		checkf(len(fired[1]) == 1 && fired[1][0] >= 150 && fired[1][0] <= 400, "新挂单次排程在 ~200ms 触发（实测 %v）", fired[1])
	}()

	// 6b. 锚定网格 + 循环被拖住 900ms：错过拍怎么办
	func() {
		c := cron.New()
		t0 := time.Now()
		sched := &anchoredStallSchedule{start: t0.Add(300 * time.Millisecond), period: 300 * time.Millisecond, stallCall: 3, stallDur: 900 * time.Millisecond}
		var mu sync.Mutex
		var fires []int
		c.Schedule(sched, cron.FuncJob(func() {
			mu.Lock()
			fires = append(fires, int(time.Since(t0).Milliseconds()))
			mu.Unlock()
		}))
		c.Start()
		time.Sleep(2500 * time.Millisecond)
		<-c.Stop().Done()
		var decl []string
		for _, s := range sched.declaredSlots() {
			decl = append(decl, fmt.Sprintf("%d", s.Sub(t0).Milliseconds()))
		}
		fmt.Printf("  6b 锚定网格声明序列(ms)=%v  实际 fires(ms)=%v\n", decl, fires)
		found1200 := false
		for _, s := range sched.declaredSlots() {
			if s.Sub(t0) == 1200*time.Millisecond {
				found1200 = true
			}
		}
		checkf(!found1200 && len(fires) < 7, "拖住循环 900ms：错过的网格拍（如 1200ms）不再出现于声明序列——不补跑、无信号")
	}()

	// 6c. @every 语义等价 + 循环被拖住：相位漂移
	func() {
		c := cron.New()
		t0 := time.Now()
		sched := &delayStallSchedule{period: 400 * time.Millisecond, stallCall: 3, stallDur: 950 * time.Millisecond}
		var mu sync.Mutex
		var fires []int
		c.Schedule(sched, cron.FuncJob(func() {
			mu.Lock()
			fires = append(fires, int(time.Since(t0).Milliseconds()))
			mu.Unlock()
		}))
		c.Start()
		time.Sleep(2600 * time.Millisecond)
		<-c.Stop().Done()
		fmt.Printf("  6c @every 等价声明序列(ms)=%v  实际 fires(ms)=%v\n", durList(sched.declaredSlots(), t0), fires)
		if len(fires) >= 3 {
			checkf(fires[2] >= 2000 && fires[2] <= 2450,
				"拖住 950ms 后下一拍实测 %dms（无拖住应为 ~1200ms）：定时器按拖住前旧时刻计算，相位全额漂移", fires[2])
		}
	}()
}

func durList(ts []time.Time, t0 time.Time) []int {
	out := make([]int, 0, len(ts))
	for _, t := range ts {
		out = append(out, int(t.Sub(t0).Milliseconds()))
	}
	return out
}

// ---------------------------------------------------------------- 入口

func main() {
	mode := flag.String("mode", "all", "grid | busy | miss | mix | real | robfig | all")
	flag.Parse()
	switch *mode {
	case "grid":
		scenarioGridDST()
	case "busy":
		scenarioBusy()
	case "miss":
		scenarioMiss()
	case "mix":
		scenarioMix()
	case "real":
		scenarioReal()
	case "robfig":
		scenarioRobfig()
	case "all":
		scenarioGridDST()
		scenarioBusy()
		scenarioMiss()
		scenarioMix()
		scenarioReal()
		scenarioRobfig()
	default:
		fmt.Println("未知 -mode:", *mode)
		os.Exit(2)
	}
	if failed {
		fmt.Println("== 存在断言失败")
		os.Exit(1)
	}
	fmt.Println("== 全部断言通过")
}
