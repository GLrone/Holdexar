// Package httpapi：net/http 原生服务骨架（健康端点、SPA 静态托管、统一 404）。
package httpapi

import (
	"encoding/json"
	"net/http"
)

// Server 服务骨架的路由与依赖。
type Server struct {
	App     string
	Version string
	DataDir string
	// DistDir 前端构建产物目录；index.html 缺失时按「仅 API」运行，全部路径回 404。
	DistDir string
	// SchedulerReady 调度器就绪状态；调度器接入后由启动链赋值。
	SchedulerReady bool
	// UpdateWriteProtected / UpdatePageReady 更新验收期间的运行保护位。
	UpdateWriteProtected bool
	UpdatePageReady      bool
}

// Handler 装配全部路由。
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/health", s.handleHealth)
	mux.HandleFunc("/api/", s.handleAPINotFound)
	mux.HandleFunc("/", s.handleStatic)
	return mux
}

// handleAPINotFound 未匹配的 API 路径统一回 404。
func (s *Server) handleAPINotFound(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusNotFound, detailNull())
}

// detailNull 错误响应体：{"detail": null}。
func detailNull() map[string]any {
	return map[string]any{"detail": nil}
}

// writeJSON JSON 响应体不带尾随换行。
func writeJSON(w http.ResponseWriter, status int, payload any) {
	body, err := json.Marshal(payload)
	if err != nil {
		http.Error(w, "响应序列化失败", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}
