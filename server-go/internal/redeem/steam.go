package redeem

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

var registerKeyURL = "https://store.steampowered.com/account/ajaxregisterkey/"

const (
	registerTimeout = 20 * time.Second
	registerReferer = "https://store.steampowered.com/account/registerkey/"
	storeUA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
	acceptLanguage  = "zh-CN,zh;q=0.9,en;q=0.8"
)

// postRegisterKey 代理优先 POST；严格校验证书防 Cookie 泄露。
func (s *Service) postRegisterKey(ctx context.Context, cookie, sessionID, productKey string) (int, string, error) {
	proxy := ""
	if s.opts.Proxy != nil {
		if resolved, e := s.opts.Proxy(ctx); e == nil {
			proxy = resolved
		}
	}
	form := url.Values{"product_key": {productKey}, "sessionid": {sessionID}}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = nil
	if proxy != "" {
		u, e := url.Parse(proxy)
		if e != nil {
			return 0, "", fmt.Errorf("代理地址无效: %w", e)
		}
		transport.Proxy = http.ProxyURL(u)
	}
	client := &http.Client{Transport: transport, Timeout: registerTimeout}
	req, e := http.NewRequestWithContext(ctx, http.MethodPost, registerKeyURL, strings.NewReader(form.Encode()))
	if e != nil {
		return 0, "", e
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("User-Agent", storeUA)
	req.Header.Set("Accept-Language", acceptLanguage)
	req.Header.Set("Referer", registerReferer)
	req.Header.Set("Cookie", cookie)
	resp, e := client.Do(req)
	if e != nil {
		return 0, "", e
	}
	defer resp.Body.Close()
	body, e := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if e != nil {
		return resp.StatusCode, "", nil
	}
	return resp.StatusCode, string(body), nil
}

func interpretRegisterKey(statusCode int, body string) Result {
	raw := truncateRunes(body, rawDisplayMax)
	if statusCode == http.StatusUnauthorized {
		return Result{Status: "fail", Detail: "Cookie 已失效，请重新绑定", Raw: raw}
	}
	if statusCode == http.StatusTooManyRequests {
		return Result{Status: "fail", Detail: errorCodes[14], Raw: raw}
	}
	var data map[string]any
	if err := json.Unmarshal([]byte(body), &data); err != nil {
		// 非 JSON 响应（如登录页 HTML）归为页面层未认登录态（注：HTTP 429 已在上方按 errorCodes[14] 提前返回）。
		if statusCode == http.StatusTooManyRequests {
			return Result{Status: "fail", Detail: "激活节点被限流（429），请稍后再试或切换 Clash 节点", Raw: raw}
		}
		return Result{Status: "fail",
			Detail: "Steam 页面层未认登录态（返回非 JSON）——可能节点被限流或会话校验拒绝，" +
				"请切换 Clash 节点重试；持续失败参考「完整账单」同款页面层限制", Raw: raw}
	}
	if data["success"] == float64(1) || data["success"] == true {
		subID, subName := receiptSummary(data["purchase_receipt_info"])
		return Result{Status: "ok", Detail: "激活成功", SubID: subID, SubName: subName, Raw: raw}
	}
	// 错误码取 purchase_result_details（兜底 result_detail），success=2 仅为失败标志；
	// 失败或已拥有回执若含 line_items 仍提取 subId/版本名。
	detail := data["purchase_result_details"]
	receipt, _ := data["purchase_receipt_info"].(map[string]any)
	code := codeFrom(detail)
	if code == 0 {
		code = codeFrom(receipt["result_detail"])
	}
	subID, subName := receiptSummary(receipt)
	if code == 9 {
		return Result{Status: "own", Detail: "已拥有", SubID: subID, SubName: subName, Raw: raw}
	}
	if zh, ok := errorCodes[code]; ok {
		return Result{Status: "fail", Detail: zh, SubID: subID, SubName: subName, Raw: raw}
	}
	if code != 0 {
		return Result{Status: "fail", Detail: fmt.Sprintf("激活失败（错误码 %d）", code), SubID: subID, SubName: subName, Raw: raw}
	}
	text := truncateRunes(pyStr(detail), 120)
	if text == "" {
		text = "激活失败"
	}
	return Result{Status: "fail", Detail: text, SubID: subID, SubName: subName, Raw: raw}
}

func receiptSummary(receipt any) (string, string) {
	m, _ := receipt.(map[string]any)
	if m == nil {
		return "", ""
	}
	items, _ := m["line_items"].([]any)
	if len(items) == 0 {
		return "", ""
	}
	first, _ := items[0].(map[string]any)
	return pyString(first["packageid"]), pyString(first["line_item_description"])
}

func codeFrom(v any) int {
	switch t := v.(type) {
	case float64:
		return int(t)
	case bool:
		if t {
			return 1
		}
		return 0
	case string:
		n, err := strconv.Atoi(strings.TrimSpace(t))
		if err != nil {
			return 0
		}
		return n
	default:
		return 0
	}
}

func pyString(v any) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case float64:
		if t == math.Trunc(t) && math.Abs(t) < 1e15 {
			return strconv.FormatInt(int64(t), 10)
		}
		return strconv.FormatFloat(t, 'g', -1, 64)
	default:
		return pyStr(t)
	}
}

func pyStr(v any) string {
	switch t := v.(type) {
	case nil:
		return "None"
	case string:
		return t
	case bool:
		if t {
			return "True"
		}
		return "False"
	case float64:
		if t == math.Trunc(t) && math.Abs(t) < 1e15 {
			return strconv.FormatInt(int64(t), 10)
		}
		return strconv.FormatFloat(t, 'g', -1, 64)
	default:
		raw, err := json.Marshal(t)
		if err != nil {
			return fmt.Sprintf("%v", t)
		}
		return string(raw)
	}
}
