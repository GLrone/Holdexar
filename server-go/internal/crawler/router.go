// 任务路由：按任务类型把任务派给注册的处理器。
package crawler

import (
	"fmt"
	"sync"
)

// Handler：任务处理器。返回错误即任务失败（计入失败计数与错误台账）。
type Handler func(ctx *Context) error

// Router：任务类型到处理器的路由表。注册集中在启动期完成，重名即错。
type Router struct {
	mu       sync.RWMutex
	handlers map[string]Handler

	// OnSkip：路由跳过观察点。缺类型/未知类型的任务按跳过处理——不计失败、
	// 按完成计数，仅在此处留下观察（nil 时静默跳过）。
	OnSkip func(task Task, reason string)
}

func NewRouter() *Router {
	return &Router{handlers: make(map[string]Handler)}
}

// Register：注册任务类型的处理器；重名返回错误，注册集中在启动期、
// 不与路由并发时无锁争用。
func (r *Router) Register(taskType string, h Handler) error {
	if taskType == "" {
		return fmt.Errorf("任务类型不能为空")
	}
	if h == nil {
		return fmt.Errorf("任务类型 %s 的处理器不能为空", taskType)
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if _, dup := r.handlers[taskType]; dup {
		return fmt.Errorf("任务类型 %s 的处理器已注册", taskType)
	}
	r.handlers[taskType] = h
	return nil
}

// Route：按任务类型分发。缺类型/未知类型：记 OnSkip 并返回 nil（跳过任务
// 不计失败、按完成计数）；处理器错误原样上抛。
func (r *Router) Route(ctx *Context) error {
	taskType := ctx.Task.Type
	if taskType == "" {
		if r.OnSkip != nil {
			r.OnSkip(ctx.Task, "任务缺少类型字段")
		}
		return nil
	}
	r.mu.RLock()
	h, ok := r.handlers[taskType]
	r.mu.RUnlock()
	if !ok {
		if r.OnSkip != nil {
			r.OnSkip(ctx.Task, "没有能处理该类型的处理器")
		}
		return nil
	}
	return h(ctx)
}
