package notifications

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"

	"holdexar/server-go/internal/plugins"
)

func (s *DeliveryService) sendNotification(ctx context.Context, recipient, subject, body string) (bool, string) {
	if strings.HasPrefix(recipient, "plugin:") {
		return s.sendPlugin(ctx, recipient, subject, body)
	}
	cfg, e := s.ReadSMTP(ctx)
	if e != nil {
		return false, "读取邮件配置失败"
	}
	cfg.ToAddr = recipient
	return s.options.Send(ctx, cfg, subject, body)
}

func (s *DeliveryService) sendPlugin(ctx context.Context, recipient, subject, body string) (bool, string) {
	if s.options.Plugins == nil {
		return false, "DeliveryNotStarted: 通知扩展未配置"
	}
	active, e := s.options.Plugins.Active(ctx)
	if e != nil || active == nil {
		return false, "DeliveryNotStarted: 通知扩展未启用"
	}
	if plugins.RecipientFor(*active) != recipient {
		return false, "DeliveryNotStarted: 通知目标已变化"
	}
	data, headers, e := plugins.Render(active.Manifest, active.Config, subject, body)
	if e != nil {
		return false, "DeliveryNotStarted: 通知扩展消息格式无效"
	}
	request, e := http.NewRequestWithContext(ctx, http.MethodPost, active.Manifest.Endpoint, bytes.NewReader(data))
	if e != nil {
		return false, "DeliveryNotStarted: 通知扩展地址无效"
	}
	request.Header.Set("Content-Type", "application/json")
	for key, value := range headers {
		request.Header.Set(key, value)
	}
	client := s.options.PluginHTTP
	if client == nil {
		client = plugins.NewSafeClient()
	}
	response, e := client.Do(request)
	if e != nil {
		return false, "PluginDeliveryUnknown: 请求未得到确认"
	}
	defer response.Body.Close()
	_, _ = io.Copy(io.Discard, io.LimitReader(response.Body, 4096))
	if response.StatusCode >= 200 && response.StatusCode < 300 {
		return true, ""
	}
	if response.StatusCode == 429 || response.StatusCode >= 500 {
		return false, fmt.Sprintf("PluginDeliveryUnknown: HTTP %d", response.StatusCode)
	}
	return false, fmt.Sprintf("PluginRejected: %d", response.StatusCode)
}

func (s *DeliveryService) TestPlugin(ctx context.Context, id string) error {
	if s.options.Plugins == nil {
		return fmt.Errorf("通知扩展未配置")
	}
	active, e := s.options.Plugins.Active(ctx)
	if e != nil || active == nil || active.Manifest.ID != id {
		return fmt.Errorf("请先配置并启用这个通知扩展")
	}
	recipient := plugins.RecipientFor(*active)
	ok, problem := s.sendPlugin(ctx, recipient, "Holdexar · 通知测试", "这是一条测试通知。This is a test notification.")
	if !ok {
		s.options.Log("通知扩展测试未成功 │ 扩展=%s，原因=%s", id, problem)
		return fmt.Errorf("%s", problem)
	}
	s.options.Log("通知扩展测试成功 │ 扩展=%s", id)
	return nil
}
