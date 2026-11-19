package updater

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"regexp"
	"strings"
	"sync"
)

var releaseNameRe = regexp.MustCompile(`^[A-Za-z0-9._-]+$`)

func validReleaseName(name string) bool {
	return releaseNameRe.MatchString(name) && !strings.Contains(name, "..") && name != "."
}

type Deps struct {
	DataDir     string
	InstallRoot string
	Version     string
	Channels    func() []Channel
	Log         func(string, ...any)
}

var (
	singleton *Service
	once      sync.Once
)

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
			Size   *int64 `json:"size"`
			Notes  string `json:"notes"`
		}
		if r.Body == nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "请求体无效"})
			return
		}
		dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
		if err := dec.Decode(&req); err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "请求体无效"})
			return
		}
		var trailing any
		if err := dec.Decode(&trailing); err != io.EOF {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "请求体无效"})
			return
		}
		if !validReleaseName(req.Tag) || (req.Asset != "" && !validReleaseName(req.Asset)) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "更新标识包含非法字符"})
			return
		}
		if req.SHA256 != "" && !hex64Re.MatchString(strings.ToLower(req.SHA256)) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "校验值格式无效"})
			return
		}
		if !versionGT(versionFromTag(req.Tag), versionFromTag(service.Version)) {
			writeJSON(w, http.StatusBadRequest, map[string]string{"detail": "目标版本不高于当前版本"})
			return
		}
		if !service.MarkDownloadStarting(req.Tag, 0) {
			writeJSON(w, http.StatusConflict, map[string]string{"detail": "已有更新下载进行中"})
			return
		}
		var expectedSize int64
		if req.Size != nil && *req.Size > 0 {
			expectedSize = *req.Size
		}
		// 后台下载必须传 context.Background()，避免 HTTP handler 返回取消请求上下文。
		go func() {
			_ = service.Download(context.Background(), req.Tag, req.SHA256, req.Asset, expectedSize, req.Notes)
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
		// 下载进行中只置取消保留 .part 续传，仅在空闲时清暂存目录。
		if service.progressRunning() {
			result := service.CancelDownload()
			result["cleared"] = false
			writeJSON(w, http.StatusOK, result)
			return
		}
		writeJSON(w, http.StatusOK, service.ClearStaging())
	})
}

func (s *Service) progressRunning() bool {
	s.progress.mu.Lock()
	defer s.progress.mu.Unlock()
	return s.progress.data.running
}
