package httpapi

import (
	"net/http"
	"os"
	"path/filepath"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/updateguard"
)

// healthResponse 健康端点响应。可选字段仅在更新交接环境变量存在时出现，
// 出现时 false 值也必须在场，故用指针区分缺省与显式 false。
type healthResponse struct {
	Status         string `json:"status"`
	App            string `json:"app"`
	Version        string `json:"version"`
	PID            int    `json:"pid"`
	DataDir        string `json:"dataDir"`
	Executable     string `json:"executable"`
	SchedulerReady bool   `json:"schedulerReady"`

	UpdateAttemptID    *string `json:"updateAttemptId,omitempty"`
	UpdateDataProtocol *int    `json:"updateDataProtocol,omitempty"`
	WriteProtected     *bool   `json:"writeProtected,omitempty"`
	PageReady          *bool   `json:"pageReady,omitempty"`
}

// handleHealth 健康端点：进程、数据目录与运行保护状态快照。
func (s *Server) handleHealth(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		w.Header().Set("Allow", "GET, HEAD")
		writeJSON(w, http.StatusMethodNotAllowed, detailNull())
		return
	}
	resp := healthResponse{
		Status:         "ok",
		App:            s.App,
		Version:        s.Version,
		PID:            os.Getpid(),
		DataDir:        s.DataDir,
		Executable:     executablePath(),
		SchedulerReady: s.SchedulerReady,
	}
	if attemptID, ok := os.LookupEnv(config.EnvPrefix + "UPDATE_ATTEMPT_ID"); ok {
		resp.UpdateAttemptID = &attemptID
		protocol := 1
		resp.UpdateDataProtocol = &protocol
		protected := s.UpdateWriteProtected
		resp.WriteProtected = &protected
		// pageReady 由 update-ready 端点翻位（updateguard 内部状态），读取须取现值
		pageReady := updateguard.PageReady()
		resp.PageReady = &pageReady
	}
	writeJSON(w, http.StatusOK, resp)
}

// executablePath 可执行文件绝对路径；无法确定时为空串。
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
