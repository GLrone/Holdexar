package pilot

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strconv"
	"strings"
	"sync"

	"holdexar/server-go/internal/agent"
	"holdexar/server-go/internal/modulectl"
	"holdexar/server-go/internal/store"
)

type ToolBridge func(ctx *CallCtx, name string, args map[string]any, guarded bool) map[string]any

type CallCtx struct {
	Ctx     context.Context
	Sid     string
	DB      *sql.DB
	Mux     *http.ServeMux
	DataDir string
	Store   *SessionStore
}

type Deps struct {
	DB          *sql.DB
	DataDir     string
	Version     string
	Mux         *http.ServeMux // 进程内调用主 mux 端点（含删除提议与桥接写端点）
	Log         func(string, ...any)
	KVGet       func(key string) (json.RawMessage, bool)
	KVGetSecret func(key string) (string, bool)
	ToolSpecs   func() []map[string]any
	KVSet       func(ctx context.Context, values map[string]json.RawMessage, secrets map[string]string) error
}

type Service struct {
	deps        Deps
	gate        *store.WriteGate
	ctl         *modulectl.Control
	mu          sync.Mutex
	running     map[string]bool
	inflight    map[string]int
	inflightQ   map[string]string
	compactFail map[string]int
	store       *SessionStore
	bridge      ToolBridge
}

func New(deps Deps, bridge ToolBridge) *Service {
	return &Service{
		deps: deps, bridge: bridge, ctl: modulectl.New(ModuleEnabledKey),
		running:     map[string]bool{},
		inflight:    map[string]int{},
		inflightQ:   map[string]string{},
		compactFail: map[string]int{},
	}
}

func (s *Service) SetToolSpecs(fn func() []map[string]any) {
	s.deps.ToolSpecs = fn
}

func (s *Service) SetGate(gate *store.WriteGate) { s.gate = gate }

func (s *Service) inflightTotal() int {
	s.mu.Lock()
	defer s.mu.Unlock()
	total := 0
	for _, n := range s.inflight {
		total += n
	}
	return total
}

func (s *Service) log(format string, args ...any) {
	if s.deps.Log != nil {
		s.deps.Log(format, args...)
	}
}

func (s *Service) GetStore() *SessionStore {
	s.mu.Lock()
	defer s.mu.Unlock()
	base := filepath.Join(s.deps.DataDir, "pilot", "sessions")
	if s.store == nil || s.store.Base() != base {
		s.store = NewSessionStore(base)
	}
	return s.store
}

// MarkSessionRunning 将会话标记为运行中（测试注入用；AskStream 内部维护运行态）。
func (s *Service) MarkSessionRunning(sid string) {
	if sid == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.running[sid] = true
}

func (s *Service) ClearSessionRunning(sid string) {
	if sid == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.running, sid)
}

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

func (s *Service) SetInflightQuestion(sid, question string) {
	if sid == "" {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.inflightQ[sid] = question
}

func (s *Service) ClearInflightQuestion(sid string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.inflightQ, sid)
}

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

func (s *Service) ToolExecute(ctx context.Context, name string, args map[string]any, guarded bool, sid string) map[string]any {
	if s.bridge == nil {
		return map[string]any{"kind": "failed", "tool": name, "note": "tools_unwired"}
	}
	var st *SessionStore
	if s.deps.DataDir != "" {
		st = s.GetStore()
	}
	return s.bridge(&CallCtx{
		Ctx:     ctx,
		Sid:     sid,
		DB:      s.deps.DB,
		Mux:     s.deps.Mux,
		DataDir: s.deps.DataDir,
		Store:   st,
	}, name, args, guarded)
}

func (s *Service) ToolNameLabel(name string) string { return agent.StepLabel(name) }

func (s *Service) ConfirmProposal(ctx context.Context, sid, pid string, approve bool) map[string]any {
	st := s.GetStore()
	state := st.Load(sid)
	if state == nil {
		return map[string]any{"ok": false, "reason": "no_session"}
	}
	prop := state.PendingProposal
	if prop == nil || stringOf(prop["pid"]) != pid {
		return map[string]any{"ok": false, "reason": "proposal_gone"}
	}
	action := stringOf(prop["action"])
	args, _ := prop["args"].(map[string]any)
	if args == nil {
		args = map[string]any{}
	}
	items := anySlice(prop["items"])
	if !approve {
		_ = st.AppendProposal(sid, map[string]any{
			"pid": pid, "action": action, "items": items, "args": args, "state": "rejected",
		})
		return map[string]any{
			"ok": true, "state": "rejected", "action": action, "total": len(items), "done": 0,
		}
	}
	done := []map[string]any{}
	failed := []map[string]any{}
	if action == "delete" {
		for _, raw := range items {
			item, _ := raw.(map[string]any)
			key := stringOf(item["key"])
			kind, idStr, found := strings.Cut(key, ":")
			targetID, err := strconv.ParseInt(idStr, 10, 64)
			if !found || err != nil || targetID <= 0 {
				failed = append(failed, map[string]any{"key": key, "name": item["name"]})
				continue
			}
			okDone := s.executeProposalDelete(ctx, kind, targetID)
			if okDone {
				done = append(done, map[string]any{"key": key, "name": item["name"]})
			} else {
				failed = append(failed, map[string]any{"key": key, "name": item["name"]})
			}
		}
	} else {
		for _, raw := range items {
			item, _ := raw.(map[string]any)
			appid := jsonInt(item["appid"])
			if appid <= 0 {
				continue
			}
			var res map[string]any
			if action == "add_follow" {
				res = s.ToolExecute(ctx, "add_follow", map[string]any{"appid": appid}, false, sid)
			} else {
				toolArgs := map[string]any{
					"appid":       appid,
					"target_type": stringOr(args["target_type"], "historic_low"),
				}
				if v, ok := args["target_value_yuan"]; ok && v != nil {
					toolArgs["target_value_yuan"] = v
				} else if v, ok := args["target_value_fen"]; ok && v != nil {
					toolArgs["target_value_fen"] = v
				}
				res = s.ToolExecute(ctx, "create_price_alert", toolArgs, false, sid)
			}
			if res != nil && jsonInt(res["appid"]) > 0 && stringOf(res["kind"]) != "failed" {
				done = append(done, map[string]any{"appid": appid, "name": stringOr(res["name"], stringOf(item["name"]))})
			} else {
				failed = append(failed, map[string]any{"appid": appid, "name": item["name"]})
			}
		}
	}
	_ = st.AppendProposal(sid, map[string]any{
		"pid": pid, "action": action, "items": items, "args": args,
		"state": "confirmed", "done": len(done), "failed": failed,
	})
	return map[string]any{
		"ok": true, "state": "confirmed", "action": action,
		"total": len(items), "done": len(done), "failed": failed,
	}
}

func (s *Service) executeProposalDelete(ctx context.Context, kind string, targetID int64) bool {
	if s.deps.Mux == nil {
		return false
	}
	var path string
	switch kind {
	case "alert":
		path = "/api/v1/alerts/" + strconv.FormatInt(targetID, 10)
	case "bill_import":
		path = "/api/v1/bills/imports/" + strconv.FormatInt(targetID, 10)
	case "follow":
		path = "/api/v1/follows/" + strconv.FormatInt(targetID, 10)
	default:
		return false
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, "http://127.0.0.1"+path, nil)
	if err != nil {
		return false
	}
	req.RemoteAddr = "127.0.0.1:0"
	rec := &statusRecorder{header: make(http.Header), code: http.StatusOK}
	s.deps.Mux.ServeHTTP(rec, req)
	return rec.code >= 200 && rec.code < 300
}

type statusRecorder struct {
	header http.Header
	code   int
}

func (r *statusRecorder) Header() http.Header         { return r.header }
func (r *statusRecorder) Write(b []byte) (int, error) { return len(b), nil }
func (r *statusRecorder) WriteHeader(statusCode int)  { r.code = statusCode }
