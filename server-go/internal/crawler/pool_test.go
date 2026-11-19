package crawler

import (
	"errors"
	"strconv"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func runFor(t *testing.T, s *Scheduler, initial []Task) (returned chan struct{}) {
	t.Helper()
	returned = make(chan struct{})
	go func() {
		s.Run(initial)
		close(returned)
	}()
	return returned
}

func mustReturn(t *testing.T, returned chan struct{}, what string) {
	t.Helper()
	select {
	case <-returned:
	case <-time.After(5 * time.Second):
		t.Fatalf("Run 未收敛：%s", what)
	}
}

func TestPoolBasic(t *testing.T) {
	r := NewRouter()
	var handled atomic.Int64
	_ = r.Register("work", func(*Context) error { handled.Add(1); return nil })
	s := NewScheduler(r, WithWorkers(4))

	initial := make([]Task, 50)
	for i := range initial {
		initial[i] = Task{ID: strconv.Itoa(i), Type: "work"}
	}
	mustReturn(t, runFor(t, s, initial), "50 个任务 4 worker")

	done, ok, fail := s.Counts()
	if done != 50 || ok != 50 || fail != 0 {
		t.Fatalf("计数应为 (50, 50, 0)，实际 (%d, %d, %d)", done, ok, fail)
	}
	if handled.Load() != 50 {
		t.Fatalf("处理器应执行 50 次，实际 %d", handled.Load())
	}
}

func TestPoolFailCountsAndSink(t *testing.T) {
	r := NewRouter()
	sentinel := errors.New("任务处理失败")
	_ = r.Register("work", func(ctx *Context) error {
		if ctx.Task.ID[0]%2 == 0 {
			return sentinel
		}
		return nil
	})
	var sinkErrs atomic.Int64
	s := NewScheduler(r, WithWorkers(3), WithErrorSink(func(task Task, err error) {
		if !errors.Is(err, sentinel) {
			t.Errorf("错误台账应收到原始错误，实际 %v", err)
		}
		sinkErrs.Add(1)
	}))
	initial := make([]Task, 10)
	for i := range initial {
		initial[i] = Task{ID: strconv.Itoa(i), Type: "work"}
	}
	mustReturn(t, runFor(t, s, initial), "10 个任务带失败")

	done, ok, fail := s.Counts()
	if done != 10 || fail != 5 || ok != 5 {
		t.Fatalf("计数应为 (10, 5, 5)，实际 (%d, %d, %d)", done, ok, fail)
	}
	if sinkErrs.Load() != 5 {
		t.Fatalf("错误台账应记 5 次，实际 %d", sinkErrs.Load())
	}
}

func TestPoolHandlerPanicCountsFail(t *testing.T) {
	r := NewRouter()
	_ = r.Register("boom", func(*Context) error { panic("处理器崩溃") })
	_ = r.Register("work", func(*Context) error { return nil })
	s := NewScheduler(r, WithWorkers(2), WithErrorSink(func(task Task, err error) {
		if task.Type == "boom" && err == nil {
			t.Errorf("崩溃应转为错误")
		}
	}))
	mustReturn(t, runFor(t, s, []Task{
		{ID: "a", Type: "boom"},
		{ID: "b", Type: "work"},
	}), "崩溃任务隔离")
	done, ok, fail := s.Counts()
	if done != 2 || fail != 1 || ok != 1 {
		t.Fatalf("崩溃应计失败不拖垮 worker (2, 1, 1)，实际 (%d, %d, %d)", done, ok, fail)
	}
}

func TestPoolDoneDedupByID(t *testing.T) {
	r := NewRouter()
	var calls atomic.Int64
	_ = r.Register("work", func(*Context) error { calls.Add(1); return nil })
	s := NewScheduler(r, WithWorkers(2))
	initial := []Task{
		{ID: "x", Type: "work"},
		{ID: "x", Type: "work"},
		{ID: "y", Type: "work"},
	}
	mustReturn(t, runFor(t, s, initial), "重复 ID 任务")
	if calls.Load() != 3 {
		t.Fatalf("重复投递应逐次执行，实际 %d", calls.Load())
	}
	done, ok, fail := s.Counts()
	if done != 2 || ok != 2 || fail != 0 {
		t.Fatalf("完成数应按 ID 去重为 (2, 2, 0)，实际 (%d, %d, %d)", done, ok, fail)
	}
}

func TestPoolRouterSkipCountsAsDone(t *testing.T) {
	r := NewRouter()
	var skipReason atomic.Value
	r.OnSkip = func(task Task, reason string) { skipReason.Store(reason) }
	s := NewScheduler(r, WithWorkers(1))
	mustReturn(t, runFor(t, s, []Task{{ID: "a", Type: "ghost"}}), "未知类型任务")
	done, ok, fail := s.Counts()
	if done != 1 || ok != 1 || fail != 0 {
		t.Fatalf("跳过任务应计完成不计失败 (1, 1, 0)，实际 (%d, %d, %d)", done, ok, fail)
	}
	if skipReason.Load() == nil {
		t.Fatalf("跳过任务应有观察记录")
	}
}

func TestPoolDynamicEnqueue(t *testing.T) {
	r := NewRouter()
	_ = r.Register("work", func(ctx *Context) error {
		if ctx.Task.ID == "root" {
			ctx.Enqueue(Task{ID: "c1", Type: "work"})
			ctx.Enqueue(Task{ID: "c2", Type: "work"})
		}
		return nil
	})
	s := NewScheduler(r, WithWorkers(2))
	mustReturn(t, runFor(t, s, []Task{{ID: "root", Type: "work"}}), "动态追加任务")
	done, _, _ := s.Counts()
	if done != 3 {
		t.Fatalf("动态追加应一并等待完成（3 个），实际 %d", done)
	}
}

func TestPoolStopDrainsQueue(t *testing.T) {
	r := NewRouter()
	release := make(chan struct{})
	var started, finished atomic.Int64
	_ = r.Register("work", func(*Context) error {
		started.Add(1)
		<-release
		finished.Add(1)
		return nil
	})
	s := NewScheduler(r, WithWorkers(2))
	initial := make([]Task, 20)
	for i := range initial {
		initial[i] = Task{ID: strconv.Itoa(i), Type: "work"}
	}
	returned := runFor(t, s, initial)
	waitFor(t, "两个 worker 各领一题", func() bool { return started.Load() == 2 })

	s.Stop()
	close(release)
	mustReturn(t, returned, "停止后排干收敛")

	if started.Load() != 2 || finished.Load() != 2 {
		t.Fatalf("在跑任务应跑完计数，实际开始 %d 完成 %d", started.Load(), finished.Load())
	}
	done, ok, fail := s.Counts()
	if done != 2 || ok != 2 || fail != 0 {
		t.Fatalf("停止后计数应为在跑的 (2, 2, 0)，实际 (%d, %d, %d)", done, ok, fail)
	}
}

func TestPoolStopBeforeRun(t *testing.T) {
	r := NewRouter()
	var called atomic.Int64
	_ = r.Register("work", func(*Context) error { called.Add(1); return nil })
	s := NewScheduler(r, WithWorkers(2))
	s.Stop()
	mustReturn(t, runFor(t, s, []Task{{ID: "a", Type: "work"}}), "先停后跑")
	if called.Load() != 0 {
		t.Fatalf("停止后启动不应执行任务，实际 %d", called.Load())
	}
	if done, _, _ := s.Counts(); done != 0 {
		t.Fatalf("排干任务不计数，实际 %d", done)
	}
}

func TestProgressBeforeDoneMark(t *testing.T) {
	r := NewRouter()
	_ = r.Register("work", func(*Context) error { return nil })
	var (
		mu       sync.Mutex
		lastSeen Progress
		seenAll  atomic.Bool
	)
	s := NewScheduler(r, WithWorkers(2), WithProgress(func(p Progress) {
		mu.Lock()
		lastSeen = p
		mu.Unlock()
		if p.Done == 10 {
			seenAll.Store(true)
		}
	}))
	initial := make([]Task, 10)
	for i := range initial {
		initial[i] = Task{ID: strconv.Itoa(i), Type: "work"}
	}
	mustReturn(t, runFor(t, s, initial), "10 个任务带进度回调")

	if !seenAll.Load() {
		t.Fatalf("Run 返回前末条进度未送达")
	}
	mu.Lock()
	last := lastSeen
	mu.Unlock()
	if last.Done != 10 || last.Ok != 10 || last.Fail != 0 || last.Total != 10 {
		t.Fatalf("末条进度口径不符: %+v", last)
	}
	if last.Queued != 0 {
		t.Fatalf("收尾时排队应为 0，实际 %d", last.Queued)
	}
}

func TestProgressTotalFixed(t *testing.T) {
	r := NewRouter()
	_ = r.Register("work", func(ctx *Context) error {
		if ctx.Task.ID == "root" {
			ctx.Enqueue(Task{ID: "c1", Type: "work"})
		}
		return nil
	})
	var lastTotal atomic.Int64
	s := NewScheduler(r, WithWorkers(1), WithProgress(func(p Progress) {
		lastTotal.Store(int64(p.Total))
	}))
	mustReturn(t, runFor(t, s, []Task{{ID: "root", Type: "work"}}), "动态追加")
	if lastTotal.Load() != 1 {
		t.Fatalf("Total 应固定为初始任务量 1，实际 %d", lastTotal.Load())
	}
}

func TestLaneRentalSkipsUnhealthy(t *testing.T) {
	r := NewRouter()
	var (
		mu     sync.Mutex
		served []any
	)
	_ = r.Register("work", func(ctx *Context) error {
		mu.Lock()
		served = append(served, ctx.Lane.Client)
		mu.Unlock()
		return nil
	})
	var lanes []*Lane
	s := NewScheduler(r,
		WithWorkers(2),
		WithLaneCount(2),
		WithLaneFactory(func(i int) *Lane {
			l := &Lane{Client: i}
			lanes = append(lanes, l)
			return l
		}),
	)
	s.materializeLanes()
	lanes[0].MarkUnhealthy()

	initial := make([]Task, 8)
	for i := range initial {
		initial[i] = Task{ID: strconv.Itoa(i), Type: "work"}
	}
	mustReturn(t, runFor(t, s, initial), "不健康 lane 隔离")

	mu.Lock()
	defer mu.Unlock()
	if len(served) != 8 {
		t.Fatalf("应完成 8 个任务，实际 %d", len(served))
	}
	for _, c := range served {
		if c.(int) == 0 {
			t.Fatalf("不健康 lane 不应接到任务")
		}
	}
	s.laneMu.Lock()
	inCooldown := len(s.cooldownLanes) == 1 && s.cooldownLanes[0] == lanes[0]
	s.laneMu.Unlock()
	if !inCooldown {
		t.Fatalf("不健康 lane 应被隔离进冷却区")
	}
}

func TestLaneCooldownProbeFlow(t *testing.T) {
	r := NewRouter()
	var (
		mu    sync.Mutex
		order []int
	)
	_ = r.Register("work", func(ctx *Context) error {
		if ctx.Task.ID == "heal" {
			ctx.Lane.MarkHealthy()
		}
		mu.Lock()
		order = append(order, ctx.Lane.Client.(int))
		mu.Unlock()
		return nil
	})
	var lanes []*Lane
	s := NewScheduler(r,
		WithWorkers(1),
		WithLaneCount(2),
		WithLaneFactory(func(i int) *Lane {
			l := &Lane{Client: i}
			lanes = append(lanes, l)
			return l
		}),
	)
	s.laneWait = time.Millisecond

	s.materializeLanes()
	lanes[0].MarkUnhealthy()
	lanes[1].MarkUnhealthy()
	returned := runFor(t, s, []Task{
		{ID: "t1", Type: "work"},
		{ID: "t2", Type: "work"},
		{ID: "heal", Type: "work"},
	})

	waitFor(t, "两条 lane 进冷却区", func() bool {
		s.laneMu.Lock()
		defer s.laneMu.Unlock()
		return len(s.cooldownLanes) == 2
	})
	expire := func(l *Lane) {
		s.laneMu.Lock()
		s.cooldownUntil[l] = s.clock.Now()
		s.laneMu.Unlock()
	}
	waitForOrder := func(n, cooldownN int) {
		waitFor(t, "任务推进到 "+strconv.Itoa(n), func() bool {
			mu.Lock()
			defer mu.Unlock()
			return len(order) >= n
		})
		waitFor(t, "冷却区回落到目标态", func() bool {
			s.laneMu.Lock()
			defer s.laneMu.Unlock()
			return len(s.cooldownLanes) == cooldownN
		})
	}

	expire(lanes[0])
	waitForOrder(1, 2)
	mu.Lock()
	first := order[0]
	mu.Unlock()
	if first != 0 {
		t.Fatalf("t1 应落在探路位 lane0，实际 %v", orderSnapshot(&mu, &order))
	}
	s.laneMu.Lock()
	fresh := s.cooldownUntil[lanes[0]].Sub(s.clock.Now()) > LaneCooldown/2
	s.laneMu.Unlock()
	if !fresh {
		t.Fatalf("探路失败 lane 应在归还清扫时进冷却区并刷新到期时刻")
	}

	expire(lanes[1])
	waitForOrder(2, 2)
	mu.Lock()
	second := order[1]
	mu.Unlock()
	if second != 1 {
		t.Fatalf("t2 应落在探路位 lane1，实际 %v", orderSnapshot(&mu, &order))
	}

	expire(lanes[1])
	waitForOrder(3, 1)
	mustReturn(t, returned, "探路位流程收敛")

	mu.Lock()
	defer mu.Unlock()
	if len(order) != 3 || order[0] != 0 || order[1] != 1 || order[2] != 1 {
		t.Fatalf("任务落点序列应为 [0 1 1]，实际 %v", order)
	}
	s.laneMu.Lock()
	healthyKept := len(s.lanes) == 1 && s.lanes[0] == lanes[1]
	s.laneMu.Unlock()
	if !healthyKept {
		t.Fatalf("探测成功的 lane 应回到岗位池")
	}
	if done, _, _ := s.Counts(); done != 3 {
		t.Fatalf("完成数应为 3，实际 %d", done)
	}
}

func orderSnapshot(mu *sync.Mutex, order *[]int) []int {
	mu.Lock()
	defer mu.Unlock()
	out := make([]int, len(*order))
	copy(out, *order)
	return out
}

func TestLaneCountStandby(t *testing.T) {
	r := NewRouter()
	_ = r.Register("work", func(*Context) error { return nil })
	s := NewScheduler(r, WithWorkers(1), WithLaneCount(3),
		WithLaneFactory(func(i int) *Lane { return &Lane{Client: i} }))
	mustReturn(t, runFor(t, s, []Task{{ID: "a", Type: "work"}}), "待用岗位")
	s.laneMu.Lock()
	total := len(s.lanes) + len(s.cooldownLanes)
	s.laneMu.Unlock()
	if total != 3 {
		t.Fatalf("岗位池应物化 3 条 lane（含待用），实际 %d", total)
	}
}

func TestPoolWorkerClamp(t *testing.T) {
	if s := NewScheduler(NewRouter()); s.workers != DefaultWorkers {
		t.Fatalf("默认 worker 数应为 %d，实际 %d", DefaultWorkers, s.workers)
	}
	if s := NewScheduler(NewRouter(), WithWorkers(100)); s.workers != MaxWorkers {
		t.Fatalf("worker 数应截断到上限 %d，实际 %d", MaxWorkers, s.workers)
	}
	if s := NewScheduler(NewRouter(), WithWorkers(7)); s.workers != 7 {
		t.Fatalf("显式 worker 数应生效，实际 %d", s.workers)
	}
}
