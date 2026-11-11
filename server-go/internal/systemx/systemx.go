// Package systemx 提供系统信息和运行状态的只读端点。
package systemx

import (
	"context"
	"database/sql"
	"encoding/json"
	"math"
	"net/http"
	"os"
	"runtime"
	"time"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/crawl"
)

// githubRepo 发布仓库（owner/repo）：前端拼发布页与问题反馈链接的唯一来源。
// config 包尚未收录该常量，暂在此声明，与品牌常量同源。
const githubRepo = "GLrone/Holdexar"

// dbProbeTimeout 数据库探活查询超时：readiness 不被慢库无限挂住。
const dbProbeTimeout = 2 * time.Second

// resolveDataDir 数据目录判定，Register 时调用一次；测试可替换。
var resolveDataDir = func() string {
	dir, err := config.ResolveDataDir()
	if err != nil {
		return ""
	}
	return dir
}

// distDirReady 前端构建产物目录存在性判定；测试可替换。
var distDirReady = func() bool {
	dir, err := config.WebDistDir()
	if err != nil {
		return false
	}
	_, statErr := os.Stat(dir)
	return statErr == nil
}

// System 系统面只读端点的依赖。
type System struct {
	DB        *sql.DB
	StartedAt time.Time
	DataDir   string
	Readiness func(context.Context) (crawl.Readiness, error)
}

// Register 装配系统面只读端点：/api/v1/info 与 /api/v1/system/readiness。
func Register(mux *http.ServeMux, db *sql.DB, readiness func(context.Context) (crawl.Readiness, error)) {
	s := &System{DB: db, StartedAt: time.Now(), DataDir: resolveDataDir(), Readiness: readiness}
	mux.HandleFunc("/api/v1/info", s.handleInfo)
	mux.HandleFunc("/api/v1/system/readiness", s.handleReadiness)
}

// infoResponse 运行信息，字段名与 Python 侧逐字段一致。
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

// probeDB 数据库可达性：轻量只读查询，失败返回人话原因。
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
	return map[string]any{"detail": nil}
}

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
