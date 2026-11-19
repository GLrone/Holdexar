// Package systemx 提供系统信息、会话发放、诊断与数据维护 HTTP 端点。
package systemx

import (
	"context"
	"database/sql"
	"math"
	"net/http"
	"os"
	"runtime"
	"time"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/crawl"
	"holdexar/server-go/internal/httpx"
)

// githubRepo 默认发布仓库标识（/api/v1/info 的 repo 字段输出）。
const githubRepo = "GLrone/Holdexar"

const dbProbeTimeout = 2 * time.Second

var resolveDataDir = func() string {
	dir, err := config.ResolveDataDir()
	if err != nil {
		return ""
	}
	return dir
}

var distDirReady = func() bool {
	dir, err := config.WebDistDir()
	if err != nil {
		return false
	}
	_, statErr := os.Stat(dir)
	return statErr == nil
}

type System struct {
	DB        *sql.DB
	StartedAt time.Time
	DataDir   string
	Readiness func(context.Context) (crawl.Readiness, error)
}

func Register(mux *http.ServeMux, db *sql.DB, readiness func(context.Context) (crawl.Readiness, error)) {
	s := &System{DB: db, StartedAt: time.Now(), DataDir: resolveDataDir(), Readiness: readiness}
	mux.HandleFunc("/api/v1/info", s.handleInfo)
	mux.HandleFunc("/api/v1/system/readiness", s.handleReadiness)
}

// python / platform 字段名沿用既有前端契约，值改为 Go 运行时版本与平台串。
type infoResponse struct {
	App           string  `json:"app"`
	Version       string  `json:"version"`
	Python        string  `json:"python"`
	Platform      string  `json:"platform"`
	DataDir       string  `json:"data_dir"`
	WebDistReady  bool    `json:"web_dist_ready"`
	UptimeSeconds float64 `json:"uptime_seconds"`
	Repo          string  `json:"repo"`
}

type readinessResponse struct {
	HTTPReady      bool `json:"httpReady"`
	WriteProtected bool `json:"writeProtected"`
	crawl.Readiness
}

func (s *System) handleInfo(w http.ResponseWriter, r *http.Request) {
	if !isReadMethod(r) {
		w.Header().Set("Allow", "GET, HEAD")
		writeJSON(w, http.StatusMethodNotAllowed, detailNull())
		return
	}
	resp := infoResponse{
		App:           config.AppName(),
		Version:       config.Version(),
		Python:        runtime.Version(),
		Platform:      runtime.GOOS + "-" + runtime.GOARCH,
		DataDir:       s.DataDir,
		WebDistReady:  distDirReady(),
		UptimeSeconds: math.Round(time.Since(s.StartedAt).Seconds()*10) / 10,
		Repo:          githubRepo,
	}
	writeJSON(w, http.StatusOK, resp)
}

func (s *System) handleReadiness(w http.ResponseWriter, r *http.Request) {
	if !isReadMethod(r) {
		w.Header().Set("Allow", "GET, HEAD")
		writeJSON(w, http.StatusMethodNotAllowed, detailNull())
		return
	}
	resp := readinessResponse{HTTPReady: true, Readiness: crawl.Readiness{Strategy: "proxy_first"}}
	if s.Readiness != nil {
		ctx, cancel := context.WithTimeout(r.Context(), dbProbeTimeout)
		defer cancel()
		var err error
		resp.Readiness, err = s.Readiness(ctx)
		if err != nil {
			resp.Reason = "读取价格更新状态失败：" + err.Error()
		}
	} else if ok, failure := probeDB(s.DB); !ok {
		resp.Reason = failure
	}
	writeJSON(w, http.StatusOK, resp)
}

func probeDB(db *sql.DB) (bool, string) {
	if db == nil {
		return false, "数据库不可达：连接未就绪"
	}
	ctx, cancel := context.WithTimeout(context.Background(), dbProbeTimeout)
	defer cancel()
	var one int
	if err := db.QueryRowContext(ctx, "SELECT 1").Scan(&one); err != nil {
		return false, "数据库不可达：" + err.Error()
	}
	return true, ""
}

func isReadMethod(r *http.Request) bool {
	return r.Method == http.MethodGet || r.Method == http.MethodHead
}

func detailNull() map[string]any {
	return httpx.DetailNull()
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	httpx.WriteJSON(w, status, payload)
}
