//go:build windows

// routes.go 系统更新端点装配（对齐 app/domains/system/router.py 的 update-*）：
// GET update-check / POST update-download / GET update-progress /
// GET update-pending / POST update-ack / POST update-cancel / POST update-clear。
package updater

import (
	"context"
	"encoding/json"
	"net/http"
	"sync"
)

// Deps 端点装配依赖。
type Deps struct {
	DataDir     string
	InstallRoot string
	Version     string
	// Channels 候选下载通道（内核代理/本地混合端口/直连），每次下载现取。
	Channels func() []Channel
	// Log 关键事件一行式记录（可空）。
	Log func(string, ...any)
}

var (
	singleton *Service
	once      sync.Once
)

// Get 进程级单例（下载进度是模块级单例语义）。
func Get(deps Deps) *Service {
	once.Do(func() {
		singleton = &Service{
			DataDir:     deps.DataDir,
			InstallRoot: deps.InstallRoot,
			Version:     deps.Version,
			Channels:    deps.Channels,
			Log:         deps.Log,
		}
	})
	return singleton
}

// Register 装配系统更新端点（路径与 Python 路由逐条对齐）。
func Register(mux *http.ServeMux, deps Deps) {
	service := Get(deps)
	writeJSON := func(w http.ResponseWriter, status int, payload any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(payload)
	}
	mux.HandleFunc("GET /api/v1/system/update-check", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, service.CheckUpdate(nil))
	})
	mux.HandleFunc("POST /api/v1/system/update-download", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Tag    string `json:"tag"`
			SHA256 string `json:"sha256"`
			Asset  string `json:"asset"`
			Size   int64  `json:"size"`
			Notes  string `json:"notes"`
		}
		_ = json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&req)
		// 校验值必填：缺失即 400 拒绝——缺它就没有可信完整性锚点。
		if req.SHA256 == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "更新源未提供校验值，已拒绝下载"})
			return
		}
		if req.Tag == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "更新目标版本缺失，已拒绝下载"})
			return
		}
		if !service.MarkDownloadStarting(req.Tag, req.Size) {
			writeJSON(w, http.StatusConflict, map[string]string{"detail": "已有更新下载进行中"})
			return
		}
		// fire-and-forget：下载在后台跑，前端轮询 /system/update-progress。
		// 不得携带请求上下文——handler 返回即取消，下载协程会秒死于通道探测。
		go func() {
			_ = service.Download(context.Background(), req.Tag, req.SHA256, req.Asset, req.Size, req.Notes)
		}()
		writeJSON(w, http.StatusOK, map[string]any{"started": true})
	})
	mux.HandleFunc("GET /api/v1/system/update-progress", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, service.Progress())
	})
	mux.HandleFunc("GET /api/v1/system/update-pending", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, service.PendingStatus())
	})
	mux.HandleFunc("POST /api/v1/system/update-ack", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, service.AcknowledgeUpdate())
	})
	mux.HandleFunc("POST /api/v1/system/update-cancel", func(w http.ResponseWriter, r *http.Request) {
		// 下载中即中止（保留 .part 续传基线，下次接着下），不清暂存；
		// 不从下载中直接拒绝——下载 125MB 时用户唯一的出路会变成等它失败。
		if service.progressRunning() {
			result := service.CancelDownload()
			result["cleared"] = false
			writeJSON(w, http.StatusOK, result)
			return
		}
		writeJSON(w, http.StatusOK, service.ClearStaging())
	})
}

// progressRunning 下载是否进行中。
func (s *Service) progressRunning() bool {
	s.progress.mu.Lock()
	defer s.progress.mu.Unlock()
	return s.progress.data.running
}
