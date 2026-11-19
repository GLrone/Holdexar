package crawler

import (
	"fmt"
	"sync"
)

type Handler func(ctx *Context) error

type Router struct {
	mu       sync.RWMutex
	handlers map[string]Handler

	// OnSkip 路由跳过回调：缺类型或未注册类型视为跳过完成（不计失败）。
	OnSkip func(task Task, reason string)
}

func NewRouter() *Router {
	return &Router{handlers: make(map[string]Handler)}
}

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
