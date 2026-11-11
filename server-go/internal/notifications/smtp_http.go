package notifications

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
)

func decodeSMTP(w http.ResponseWriter, r *http.Request) (*SMTPConfig, bool) {
	cfg := &SMTPConfig{Port: 465, UseSSL: true}
	if e := json.NewDecoder(r.Body).Decode(&cfg); e != nil || cfg == nil {
		writeJSON(w, 422, map[string]any{"detail": "邮件配置必须是有效对象"})
		return nil, false
	}
	return cfg, true
}
func (h *handler) registerSMTP(mux *http.ServeMux) {
	service := h.options.Delivery
	mux.HandleFunc("GET /api/v1/alerts/smtp", func(w http.ResponseWriter, r *http.Request) {
		v, e := service.SMTPView(r.Context())
		if e != nil {
			writeJSON(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		writeJSON(w, 200, v)
	})
	mux.HandleFunc("PUT /api/v1/alerts/smtp", func(w http.ResponseWriter, r *http.Request) {
		cfg, ok := decodeSMTP(w, r)
		if !ok {
			return
		}
		if e := service.SaveSMTP(r.Context(), *cfg); e != nil {
			writeJSON(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		v, e := service.SMTPView(r.Context())
		if e != nil {
			writeJSON(w, 500, map[string]any{"detail": e.Error()})
			return
		}
		service.options.Log("邮件配置已更新")
		writeJSON(w, 200, v)
	})
	mux.HandleFunc("POST /api/v1/alerts/smtp/test", func(w http.ResponseWriter, r *http.Request) {
		cfg, ok := decodeSMTP(w, r)
		if !ok {
			return
		}
		if e := service.TestMail(r.Context(), *cfg); e != nil {
			writeJSON(w, 400, map[string]any{"detail": e.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	mux.HandleFunc("POST /api/v1/notifications/test", func(w http.ResponseWriter, r *http.Request) {
		cfg, e := service.ReadSMTP(r.Context())
		if e == nil {
			e = service.TestMail(r.Context(), cfg)
		}
		if e != nil {
			writeJSON(w, 400, map[string]any{"detail": e.Error()})
			return
		}
		writeJSON(w, 200, map[string]any{"ok": true})
	})
}
func (s *DeliveryService) TestMail(ctx context.Context, cfg SMTPConfig) error {
	if cfg.Password == "" {
		saved, e := s.ReadSMTP(ctx)
		if e != nil {
			return e
		}
		cfg.Password = saved.Password
	}
	missing := []string{}
	for _, field := range []struct{ name, value string }{{"SMTP 服务器", cfg.Host}, {"发件账户", cfg.User}, {"授权码/密码", cfg.Password}, {"收件邮箱", cfg.ToAddr}} {
		if field.value == "" {
			missing = append(missing, field.name)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("请先填写：%s", strings.Join(missing, "、"))
	}
	body, e := s.testMailHTML(cfg)
	if e != nil {
		return e
	}
	ok, problem := s.options.Send(ctx, cfg, "欢迎使用 Holdexar · 邮箱连通性测试", body)
	if ok {
		s.options.Log("邮箱连通性测试成功")
		return nil
	}
	if strings.HasPrefix(problem, "SMTPAuthenticationError: ") {
		return fmt.Errorf("%s", strings.TrimPrefix(problem, "SMTPAuthenticationError: "))
	}
	if deliveryUnknown(problem) {
		return fmt.Errorf("邮件发送结果无法确认，请先检查收件箱再决定是否重新测试")
	}
	return fmt.Errorf("连接失败：%s", problem)
}
