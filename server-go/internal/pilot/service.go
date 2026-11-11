//go:build windows

// service.go 领航员服务运行态：依赖句柄、进程内会话态（在飞轮/在飞问）
// 与门面方法。编排主体（ask/agent 循环）见 ask.go。
package pilot

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"path/filepath"
	"sync"
)

// ToolBridge 工具执行桥：pilot 工具名 → 归一化结果（kind 键形状，与
// Python REGISTRY 数据面同构）。由 serverd 装配注入。
type ToolBridge func(ctx *CallCtx, name string, args map[string]any, guarded bool) map[string]any

// CallCtx 工具执行环境。
type CallCtx struct {
	Ctx context.Context
	Sid string
	DB  *sql.DB
	Mux *http.ServeMux
}

// Deps 服务装配依赖。
type Deps struct {
	DB      *sql.DB
	DataDir string
	Version string
	Mux     *http.ServeMux // 进程内 fetch 已迁移只读端点
	Log     func(string, ...any)
	// KV 桥：settings_service 语义（KV JSON 值 + secretbox 密文），serverd 注入。
	KVGet       func(key string) (json.RawMessage, bool)
	KVGetSecret func(key string) (string, bool)
	// ToolSpecs 工具表投影（serverd 从 agent 注册表注入）。
	ToolSpecs func() []map[string]any
	KVSet     func(ctx context.Context, values map[string]json.RawMessage, secrets map[string]string) error
}

// Service 领航员运行态。
type Service struct {
	deps      Deps
	mu        sync.Mutex
	running   map[string]bool
	inflight  map[string]int
	inflightQ map[string]string
	store     *SessionStore
	bridge    ToolBridge
}

// New 构造服务（路由装配时创建一次）。
func New(deps Deps, bridge ToolBridge) *Service {
	return &Service{
		deps: deps, bridge: bridge, running: map[string]bool{},
		inflight: map[string]int{}, inflightQ: map[string]string{},
	}
}

func (s *Service) SetToolSpecs(fn func() []map[string]any) {
	s.deps.ToolSpecs = fn
}

func (s *Service) log(format string, args ...any) {
	if s.deps.Log != nil {
		s.deps.Log(format, args...)
	}
}

// GetStore 会话存取（目录 = <data>/pilot/sessions）。
func (s *Service) GetStore() *SessionStore {
	s.mu.Lock()
	defer s.mu.Unlock()
	base := filepath.Join(s.deps.DataDir, "pilot", "sessions")
	if s.store == nil || s.store.Base() != base {
		s.store = NewSessionStore(base)
	}
	return s.store
}

// MarkSessionRunning ask 流受理登记（router 层挂载，断开必达）。
func (s *Service) MarkSessionRunning(sid string) {
	if sid == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.running[sid] = true
}

// ClearSessionRunning 流收口摘除登记。
func (s *Service) ClearSessionRunning(sid string) {
	if sid == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.running, sid)
}

// SessionRunning 本会话是否有流在途。
func (s *Service) SessionRunning(sid string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.running[sid]
}

// SessionBusy 会话写闸统一判据（rewind/fork/run_tool 共用）。
func (s *Service) SessionBusy(sid string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.inflight[sid] > 0 || s.running[sid]
}

// SetInflightQuestion 在飞轮的提问原文（刷新后据此补一屏进行中轮）。
func (s *Service) SetInflightQuestion(sid, question string) {
	if sid == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.inflightQ[sid] = question
}

// ClearInflightQuestion 摘除在飞问。
func (s *Service) ClearInflightQuestion(sid string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.inflightQ, sid)
}

// InflightQuestion 本会话在飞轮的提问；无则空串。
func (s *Service) InflightQuestion(sid string) string {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.running[sid] {
		return ""
	}
	return s.inflightQ[sid]
}

// acquireTurn 在飞轮登记（同会话第二轮直接 busy）；返回受理时的全局在飞数。
func (s *Service) acquireTurn(sid string) (bool, int) {
	if sid == "" {
		return true, 1
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.inflight[sid] > 0 {
		return false, s.inflight[sid]
	}
	s.inflight[sid]++
	total := 0
	for _, n := range s.inflight {
		total += n
	}
	return true, total
}

// releaseTurn 摘除在飞登记。
func (s *Service) releaseTurn(sid string) {
	if sid == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if left := s.inflight[sid] - 1; left <= 0 {
		delete(s.inflight, sid)
	} else {
		s.inflight[sid] = left
	}
}

// ToolExecute 工具执行入口（守卫/预算由桥接实现承载）。
func (s *Service) ToolExecute(ctx context.Context, name string, args map[string]any, guarded bool, sid string) map[string]any {
	if s.bridge == nil {
		return map[string]any{"kind": "failed", "tool": name, "note": "tools_unwired"}
	}
	return s.bridge(&CallCtx{Ctx: ctx, Sid: sid, DB: s.deps.DB, Mux: s.deps.Mux}, name, args, guarded)
}

// ToolNameLabel 工具显示名占位（tools.go 实装后由注册表 label 提供）。
func (s *Service) ToolNameLabel(name string) string { return name }
