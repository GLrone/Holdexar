package crawler

import (
	"errors"
	"testing"
)

// ── 注册与重名 ───────────────────────────────────────────────────

func TestRouterRegisterDuplicate(t *testing.T) {
	r := NewRouter()
	if err := r.Register("app", func(*Context) error { return nil }); err != nil {
		t.Fatalf("首次注册失败: %v", err)
	}
	err := r.Register("app", func(*Context) error { return nil })
	if err == nil {
		t.Fatalf("重名注册应报错")
	}
	want := "任务类型 app 的处理器已注册"
	if err.Error() != want {
		t.Fatalf("重名报错应为「%s」，实际「%s」", want, err.Error())
	}
}

func TestRouterRegisterGuards(t *testing.T) {
	r := NewRouter()
	if err := r.Register("", func(*Context) error { return nil }); err == nil {
		t.Fatalf("空类型注册应报错")
	}
	if err := r.Register("x", nil); err == nil {
		t.Fatalf("空处理器注册应报错")
	}
}

// ── 分发与跳过 ───────────────────────────────────────────────────

func TestRouterDispatch(t *testing.T) {
	r := NewRouter()
	var got *Context
	if err := r.Register("app", func(ctx *Context) error {
		got = ctx
		return nil
	}); err != nil {
		t.Fatalf("注册失败: %v", err)
	}
	lane := &Lane{Client: "cli"}
	task := Task{ID: "t1", Type: "app", Body: "payload"}
	if err := r.Route(&Context{Task: task, Client: "cli", Lane: lane}); err != nil {
		t.Fatalf("路由失败: %v", err)
	}
	if got == nil || got.Task.ID != "t1" || got.Client != "cli" || got.Lane != lane {
		t.Fatalf("处理器收到的上下文不完整: %+v", got)
	}
}

func TestRouterHandlerErrorPropagates(t *testing.T) {
	r := NewRouter()
	sentinel := errors.New("任务处理失败")
	_ = r.Register("app", func(*Context) error { return sentinel })
	if err := r.Route(&Context{Task: Task{Type: "app"}}); err != sentinel {
		t.Fatalf("处理器错误应原样上抛，实际 %v", err)
	}
}

func TestRouterSkip(t *testing.T) {
	r := NewRouter()
	called := false
	_ = r.Register("app", func(*Context) error { called = true; return nil })
	var skips []string
	r.OnSkip = func(task Task, reason string) { skips = append(skips, reason) }

	// 缺类型
	if err := r.Route(&Context{Task: Task{ID: "a"}}); err != nil {
		t.Fatalf("缺类型应跳过而非报错: %v", err)
	}
	// 未知类型
	if err := r.Route(&Context{Task: Task{ID: "b", Type: "ghost"}}); err != nil {
		t.Fatalf("未知类型应跳过而非报错: %v", err)
	}
	if called {
		t.Fatalf("跳过任务不应触达处理器")
	}
	if len(skips) != 2 {
		t.Fatalf("应记录两次跳过观察，实际 %d", len(skips))
	}
}

// ── 队列收尾丢弃 ─────────────────────────────────────────────────

func TestContextEnqueueAfterClose(t *testing.T) {
	s := NewScheduler(NewRouter())
	s.closeQueue()
	if s.push(Task{ID: "x"}) {
		t.Fatalf("队列关闭后入队应被丢弃")
	}
	ctx := &Context{sched: s}
	if ctx.Enqueue(Task{ID: "y"}) {
		t.Fatalf("队列关闭后动态追加应被丢弃")
	}
}

func TestContextNilScheduler(t *testing.T) {
	ctx := &Context{}
	if ctx.Enqueue(Task{ID: "x"}) {
		t.Fatalf("无归属调度器的上下文不应能入队")
	}
	if ctx.StopChan() != nil {
		t.Fatalf("无归属调度器的上下文不应有停止信号")
	}
}
