// steam.go 商店端点出网：POST ajaxregisterkey（激活页同款内部接口）。
// 登录态整 jar 注入、代理优先 + SSL 证书降级重试一次；响应解释与文案
// 逐字对齐 Python domains/redeem/service.py。
package redeem

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// registerKeyURL 出网端点；包级变量供测试打桩。
var registerKeyURL = "https://store.steampowered.com/account/ajaxregisterkey/"

const (
	registerTimeout = 20 * time.Second
	registerReferer = "https://store.steampowered.com/account/registerkey/"
	storeUA         = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
	acceptLanguage  = "zh-CN,zh;q=0.9,en;q=0.8"
)

// postRegisterKey 商店端点 POST：代理优先 + SSL 证书降级重试（经 Clash vless
// 链路的既有规则）。返回状态码与响应体原文；传输失败按错误上抛。
func (s *Service) postRegisterKey(ctx context.Context, cookie, sessionID, productKey string) (int, string, error) {
	proxy := ""
	if s.opts.Proxy != nil {
		// 代理解析失败按直连处理（拿不到代理也要把这次激活跑完）
		if resolved, e := s.opts.Proxy(ctx); e == nil {
			proxy = resolved
		}
	}
	form := url.Values{"product_key": {productKey}, "sessionid": {sessionID}}
	attempt := func(insecure bool) (int, string, error) {
		transport := http.DefaultTransport.(*http.Transport).Clone()
		transport.Proxy = nil
		if proxy != "" {
			u, e := url.Parse(proxy)
			if e != nil {
				return 0, "", fmt.Errorf("代理地址无效: %w", e)
			}
			transport.Proxy = http.ProxyURL(u)
		}
		if insecure {
			if transport.TLSClientConfig == nil {
				transport.TLSClientConfig = &tls.Config{InsecureSkipVerify: true}
			} else {
				transport.TLSClientConfig.InsecureSkipVerify = true
			}
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
			// 响应头已收到、读体中断：Python 侧同型（raw 留空按非 JSON 处理）
			return resp.StatusCode, "", nil
		}
		return resp.StatusCode, string(body), nil
	}
	status, body, err := attempt(false)
	if err == nil || !isSSLError(err) {
		return status, body, err
	}
	s.opts.Log("商店端点证书校验失败（经代理场景），降级跳过校验重试")
	return attempt(true)
}

// isSSLError TLS 证书校验类错误（降级重试仅此一类）。
func isSSLError(err error) bool {
	var verify *tls.CertificateVerificationError
	if errors.As(err, &verify) {
		return true
	}
	var unknown x509.UnknownAuthorityError
	var invalid x509.CertificateInvalidError
	var hostname x509.HostnameError
	return errors.As(err, &unknown) || errors.As(err, &invalid) || errors.As(err, &hostname)
}

// interpretRegisterKey 激活响应解释：401/429/非 JSON 三类页面层分支 + JSON
// 回执映射（success==1 → ok；错误码 9 → own；其余 → fail）。
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
		// store 页面层可能对脚本客户端返回登录页 HTML（钱包模块同款行为），
		// 或节点级 429 页面——都归为「页面层未认登录态」，指引换节点/稍后再试。
		if statusCode == http.StatusTooManyRequests {
			return Result{Status: "fail", Detail: "激活节点被限流（429），请稍后再试或切换 Clash 节点", Raw: raw}
		}
		return Result{Status: "fail",
			Detail: "Steam 页面层未认登录态（返回非 JSON）——可能节点被限流或会话校验拒绝，" +
				"请切换 Clash 节点重试；持续失败参考「完整账单」同款页面层限制", Raw: raw}
	}
	// Python data.get("success") == 1：True == 1 亦成立，一并镜像
	if data["success"] == float64(1) || data["success"] == true {
		subID, subName := receiptSummary(data["purchase_receipt_info"])
		return Result{Status: "ok", Detail: "激活成功", SubID: subID, SubName: subName, Raw: raw}
	}
	// 错误码永远取 purchase_result_details（真 key 回执里 success=2 是「失败」
	// 标记不是错误码——{"success":2,"purchase_result_details":53}）；receipt
	// 的 result_detail 兜底。失败/已拥有回执若带 line_items（失败分支同样提取），
	// 一并展示 subId/版本名——「已拥有」能看到是哪个 sub 是原脚本一直有的信息。
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

// receiptSummary 从 purchase_receipt_info 提取 (subId, subName)；
// 空 line_items 返回 "",""。
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

// codeFrom 对齐 Python int() 语义的错误码提取（不可转换返回 0）。
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

// pyString 对齐 Python str() 的回执字段渲染（None → ""）。
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

// pyStr 对齐 Python str() 的兜底渲染（错误码 0 分支 detail 原文，None → "None"）。
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
