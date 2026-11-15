// http.go clash 订阅内核控制面 9 条端点（对齐 Python domains/proxies/router.py
// :207-392 的 /proxies/clash/* 面）：状态/安装/进度/启动/切换/点选/测速/测速
// 进度/停止。错误映射：*StatusError 直接映射状态码与 detail，其余 500。
package clashservice

import (
	"encoding/json"
	"errors"
	"net/http"
)

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

// replyService 统一服务调用应答：*StatusError → 对应状态码+detail，其余 500。
func (s *Service) replyService(w http.ResponseWriter, r *http.Request, call func() (any, error)) {
	result, err := call()
	if err != nil {
		var status *StatusError
		if errors.As(err, &status) {
			reply(w, status.Status, map[string]any{"detail": status.Detail})
			return
		}
		reply(w, 500, map[string]any{"detail": err.Error()})
		return
	}
	reply(w, 200, result)
}

// readSubID 解析 {"subscriptionId": int}：required=false 时缺省/0 → nil（缺省
// 用最近点选）；required=true 时缺失/非法 → FastAPI 422 形状（loc body）。
func readSubID(w http.ResponseWriter, r *http.Request, required bool) (*int64, bool) {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16<<10))
	var body struct {
		SubscriptionID *int64 `json:"subscriptionId"`
	}
	if err := decoder.Decode(&body); err != nil && err.Error() != "EOF" {
		reply(w, 422, map[string]any{"detail": "订阅参数无效"})
		return nil, false
	}
	if body.SubscriptionID == nil || *body.SubscriptionID <= 0 {
		if required {
			reply(w, 422, map[string]any{"detail": "订阅参数无效"})
			return nil, false
		}
		return nil, true
	}
	return body.SubscriptionID, true
}

// Register 装配 9 条控制面端点。固定字面量路径先注册，动态段歧义由 Go 1.22
// ServeMux 精确字面量优先解决（Python router.py:203-204 的顺序要求不受影响）。
func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/proxies/clash", func(w http.ResponseWriter, r *http.Request) {
		reply(w, 200, s.StatusPayload(r.Context()))
	})
	mux.HandleFunc("POST /api/v1/proxies/clash/install", func(w http.ResponseWriter, r *http.Request) {
		result := s.InstallKernel(r.Context())
		if result.OK && result.Path != nil {
			version := s.KernelVersion(*result.Path)
			result.Version = &version
		}
		reply(w, 200, result)
	})
	mux.HandleFunc("GET /api/v1/proxies/clash/install/progress", func(w http.ResponseWriter, r *http.Request) {
		reply(w, 200, s.InstallProgress())
	})
	mux.HandleFunc("POST /api/v1/proxies/clash/start", func(w http.ResponseWriter, r *http.Request) {
		id, ok := readSubID(w, r, false)
		if !ok {
			return
		}
		s.replyService(w, r, func() (any, error) { return s.StartClash(r.Context(), id) })
	})
	mux.HandleFunc("POST /api/v1/proxies/clash/switch", func(w http.ResponseWriter, r *http.Request) {
		id, ok := readSubID(w, r, true)
		if !ok {
			return
		}
		s.replyService(w, r, func() (any, error) { return s.SwitchClash(r.Context(), *id) })
	})
	mux.HandleFunc("POST /api/v1/proxies/clash/select", func(w http.ResponseWriter, r *http.Request) {
		id, ok := readSubID(w, r, true)
		if !ok {
			return
		}
		s.replyService(w, r, func() (any, error) {
			selected, err := s.SelectClash(r.Context(), *id)
			if err != nil {
				return nil, err
			}
			return map[string]any{"selected": selected}, nil
		})
	})
	mux.HandleFunc("POST /api/v1/proxies/clash/test", func(w http.ResponseWriter, r *http.Request) {
		reply(w, 200, s.TestStart(r.Context()))
	})
	mux.HandleFunc("GET /api/v1/proxies/clash/test/progress", func(w http.ResponseWriter, r *http.Request) {
		reply(w, 200, s.TestProgress(r.Context()))
	})
	mux.HandleFunc("POST /api/v1/proxies/clash/stop", func(w http.ResponseWriter, r *http.Request) {
		reply(w, 200, s.StopClash())
	})
}
