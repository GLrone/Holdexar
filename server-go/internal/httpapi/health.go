package httpapi

import (
	"net/http"
	"os"
	"path/filepath"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/updateguard"
)

// healthResponse 健康响应：更新交接字段按环境变量按需出现（指针区分缺省与 false）。
// 未鉴权远端仅返回 {"status":"ok"}；本机路径与 PID 仅回环对端可见。
type healthResponse struct {
	Status         string `json:"status"`
	App            string `json:"app,omitempty"`
	Version        string `json:"version,omitempty"`
	PID            int    `json:"pid,omitempty"`
	DataDir        string `json:"dataDir,omitempty"`
	Executable     string `json:"executable,omitempty"`
	SchedulerReady *bool  `json:"schedulerReady,omitempty"`

	UpdateAttemptID    *string `json:"updateAttemptId,omitempty"`
	UpdateDataProtocol *int    `json:"updateDataProtocol,omitempty"`
	WriteProtected     *bool   `json:"writeProtected,omitempty"`
	PageReady          *bool   `json:"pageReady,omitempty"`
}

func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		writeJSON(w, http.StatusMethodNotAllowed, detailNull())
		return
	}
	loopback := access.PeerIsLoopback(r.RemoteAddr)
	authorized := s.Gate != nil && s.Gate.Authorized(r)
	if !loopback && !authorized {
		writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
		return
	}
	schedulerReady := s.SchedulerReady
	if s.SchedulerReadyFn != nil {
		schedulerReady = s.SchedulerReadyFn()
	}
	resp := healthResponse{
		Status:         "ok",
		App:            s.App,
		Version:        s.Version,
		SchedulerReady: &schedulerReady,
	}
	if loopback {
		resp.PID = os.Getpid()
		resp.DataDir = s.DataDir
		resp.Executable = executablePath()
		if attemptID, ok := os.LookupEnv(config.EnvPrefix + "UPDATE_ATTEMPT_ID"); ok {
			resp.UpdateAttemptID = &attemptID
			protocol := 1
			resp.UpdateDataProtocol = &protocol
			protected := s.UpdateWriteProtected
			if s.UpdateWriteProtectedFn != nil {
				protected = s.UpdateWriteProtectedFn()
			}
			resp.WriteProtected = &protected
			// pageReady 由 update-ready 端点翻位（updateguard 内部状态），读取须取现值
			pageReady := updateguard.PageReady()
			resp.PageReady = &pageReady
		}
	}
	writeJSON(w, http.StatusOK, resp)
}

func executablePath() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	if abs, err := filepath.Abs(exe); err == nil {
		return abs
	}
	return exe
}
