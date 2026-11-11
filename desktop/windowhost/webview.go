//go:build windows

package main

import (
	"encoding/json"
	"fmt"
	"net/url"
	"strings"
)

// splashHTML 本地等待页（零网络依赖）：后端就绪前窗口呈现的启动画面。
func splashHTML(appName string) string {
	return "<!doctype html><html><head><meta charset='utf-8'>" +
		"<title>" + appName + "</title>" +
		"<style>html,body{height:100%;margin:0}" +
		"body{display:flex;align-items:center;justify-content:center;" +
		"background:#1b2838;color:#c7d5e0;" +
		"font:15px/1.8 'Segoe UI',system-ui,sans-serif}" +
		".box{text-align:center}" +
		".spin{width:34px;height:34px;margin:0 auto 16px;border-radius:50%;" +
		"border:3px solid rgba(199,213,224,.2);border-top-color:#66c0f4;" +
		"animation:r .9s linear infinite}" +
		"@keyframes r{to{transform:rotate(360deg)}}" +
		"</style></head><body><div class='box'><div class='spin'></div>" +
		appName + " 启动中，请稍候…</div></body></html>"
}

// bridgeBootstrapScript 文档创建即注入：合成前端依赖的 window.pywebview 桥
// （restart_app）并回报主题；宿主经 postMessage 应答。
const bridgeBootstrapScript = `(function () {
  if (window.__hlxHostBridge) return;
  window.__hlxHostBridge = 1;
  var pending = new Map();
  var seq = 0;
  var post = function (m) { window.chrome.webview.postMessage(JSON.stringify(m)); };
  var call = function (name, args) {
    return new Promise(function (resolve, reject) {
      var id = 'c' + (++seq);
      pending.set(id, { resolve: resolve, reject: reject });
      post({ jsonrpc: '2.0', id: id, method: name, params: args || [] });
      setTimeout(function () {
        if (pending.has(id)) { pending.delete(id); reject(new Error(name + ' 超时')); }
      }, 60000);
    });
  };
  window.addEventListener('message', function (e) {
    var m = e.data;
    if (typeof m === 'string') { try { m = JSON.parse(m); } catch (_) { return; } }
    if (m && m.id !== undefined && pending.has(m.id)) {
      var p = pending.get(m.id);
      pending.delete(m.id);
      if (m.error) { p.reject(new Error(m.error.message || String(m.error))); }
      else { p.resolve(m.result); }
    }
  });
  window.pywebview = {
    api: new Proxy({}, { get: function (_t, name) { return function () { return call(String(name), [].slice.call(arguments)); }; } }),
    _internal: { _call: call }
  };
  var sendTheme = function () {
    post({ theme: document.documentElement.classList.contains('dark') ? 'dark' : 'light' });
  };
  document.addEventListener('DOMContentLoaded', sendTheme);
  new MutationObserver(sendTheme).observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });
})();`

// onNavigationStarting 导航白名单：仅放行本实例确切本机源；外链交系统浏览器。
func (a *app) onNavigationStarting(uri string) bool {
	if uri == "" || strings.HasPrefix(uri, "data:") || uri == "about:blank" {
		return true
	}
	scheme, host := urlParts(uri)
	if scheme == "" {
		return false
	}
	a.navMu.Lock()
	origin := a.navOrigin
	a.navMu.Unlock()
	if strings.EqualFold(scheme+"://"+host, origin) && origin != "" {
		a.navMu.Lock()
		a.currentPath = urlPathWithQuery(uri)
		a.navMu.Unlock()
		return true
	}
	if scheme == "http" || scheme == "https" {
		a.log.write("外链转交系统浏览器：" + urlRedacted(uri))
		if a.openExternal != nil {
			a.openExternal(uri)
		}
	}
	return false
}

// buildAppURL 应用地址：开发显式 DEV_URL（Vite）优先，否则本机服务端口。
func buildAppURL(cfg *options) string {
	if cfg.devWatch && cfg.devURL != "" {
		return strings.TrimRight(cfg.devURL, "/")
	}
	return fmt.Sprintf("http://127.0.0.1:%d", cfg.serverPort)
}

// buildEntryURL 引导入口：票据换会话 Cookie；next 仅同源路径且非首页时携带。
func buildEntryURL(appURL, nonce, next string) string {
	entry := appURL + "/api/v1/session/bootstrap?nonce=" + url.QueryEscape(nonce)
	if next != "" && strings.HasPrefix(next, "/") && !strings.HasPrefix(next, "//") && next != "/" {
		entry += "&next=" + url.QueryEscape(next)
	}
	return entry
}

func urlParts(raw string) (scheme, host string) {
	parsed, err := url.Parse(raw)
	if err != nil {
		return "", ""
	}
	return strings.ToLower(parsed.Scheme), strings.ToLower(parsed.Host)
}

func urlOrigin(raw string) string {
	scheme, host := urlParts(raw)
	if scheme == "" || host == "" {
		return ""
	}
	return scheme + "://" + host
}

// urlPathWithQuery 取路径+查询+片段（同源回跳目标）。
func urlPathWithQuery(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	target := parsed.Path
	if parsed.RawQuery != "" {
		target += "?" + parsed.RawQuery
	}
	if parsed.Fragment != "" {
		target += "#" + parsed.Fragment
	}
	return target
}

// urlRedacted 票据 nonce 不进日志：查询串整体脱敏。
func urlRedacted(raw string) string {
	parsed, err := url.Parse(raw)
	if err != nil || parsed.RawQuery == "" {
		return raw
	}
	parsed.RawQuery = "…"
	return parsed.String()
}

func parseJSONMap(text string) (map[string]any, bool) {
	var decoded map[string]any
	if err := json.Unmarshal([]byte(text), &decoded); err != nil {
		return nil, false
	}
	return decoded, true
}

func bridgeResult(id string, result map[string]any) string {
	payload := map[string]any{"jsonrpc": "2.0", "id": id, "result": result}
	encoded, _ := json.Marshal(payload)
	return string(encoded)
}

func bridgeError(id string, code int, message string) string {
	payload := map[string]any{
		"jsonrpc": "2.0", "id": id,
		"error": map[string]any{"code": code, "message": message},
	}
	encoded, _ := json.Marshal(payload)
	return string(encoded)
}
