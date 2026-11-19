package httpapi

import (
	"net/http"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/httpx"
)

type Server struct {
	App     string
	Version string
	DataDir string
	// DistDir 前端构建产物目录；index.html 缺失时静态路由回 404。
	DistDir          string
	SchedulerReady   bool
	SchedulerReadyFn func() bool
	// UpdateWriteProtected 更新验收期写保护位（UpdateWriteProtectedFn 优先；UpdatePageReady 未启用，实际读取 updateguard.PageReady）。
	UpdateWriteProtected   bool
	UpdateWriteProtectedFn func() bool
	UpdatePageReady        bool
	Gate                   *access.Gate
	Register               func(mux *http.ServeMux)
}

func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	if s.Register != nil {
		s.Register(mux)
	}
	mux.HandleFunc("/api/v1/health", s.handleHealth)
	mux.HandleFunc("/api/", s.handleAPINotFound)
	mux.HandleFunc("/", s.handleStatic)
	return mux
}

func (s *Server) handleAPINotFound(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusNotFound, map[string]any{"detail": "Not Found"})
}

func detailNull() map[string]any {
	return map[string]any{"detail": nil}
}

// writeJSON JSON 响应体不带尾随换行，禁用 HTML 转义。
func writeJSON(w http.ResponseWriter, status int, payload any) {
	httpx.WriteJSON(w, status, payload)
}
