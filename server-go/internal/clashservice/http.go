package clashservice

import (
	"encoding/json"
	"errors"
	"net/http"

	"holdexar/server-go/internal/httpx"
)

func reply(w http.ResponseWriter, status int, value any) {
	httpx.WriteJSON(w, status, value)
}

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

// readSubID 解析 {"subscriptionId": int}；required=false 允许缺省/<=0 返回 nil，required=true 返回 422。
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
