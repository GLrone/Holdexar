// Package logredact：运行日志统一脱敏链（对齐 Python core/logging.py 的
// 渲染后置 redact）。六条规则全部按「保留字段名、抹掉值」替换：日志的
// 可读性与排障价值留在字段名上；只抹值不删行。纯函数、无副作用、幂等
// ——已抹过的行再抹一次结果不变（替换后的 *** 不再匹配任何规则）。
// 散点事前掩码保留，本链是 Ring 与日志出口共用的兜底网。
package logredact

import (
	"net/url"
	"regexp"
	"strings"
)

// 代理链接：值本身就是凭据（vless/vmess 的 UUID 嵌在 URL 里，ss 整条是 base64）。
var proxyLink = regexp.MustCompile(`(?i)\b(vless|vmess|trojan|ssr?|hysteria2?|tuic|wireguard)://([^\s"'<>]+)`)

// URL 用户信息：scheme://user:pass@host → 抹掉 user:pass，保留 host
//（host 用于判断打到哪）。
var urlUserinfo = regexp.MustCompile(`(?i)\b([a-z][a-z0-9+.\-]*)://([^/\s:@]+):([^/\s@]+)@`)

// 查询串/表单里的凭据参数。key 一项即 Steam Web API；sessionid/steamLogin* 是
// Steam 会话 Cookie。
var querySecret = regexp.MustCompile(`(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token` +
	`|token|secret|password|passwd|pwd|signature|sign|key|sessionid` +
	`|steamloginsecure|steamlogin|authorization)=([^&\s;"'<>]+)`)

// JSON 形态：`{"smtp.password": "…"}`。键名要求**包含**凭据词根
//（smtp.password、account.steam_cookies），故裸 key 不在其中——那会把
// `{"key": "games"}` 这类正常调试输出一起抹掉，得不偿失。
var jsonSecret = regexp.MustCompile(
	`(?i)"([A-Za-z0-9_.\-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token` +
		`|auth[_-]?token|token|secret|password|passwd|pwd|signature|cookie|credential)` +
		`[A-Za-z0-9_.\-]*)"(\s*:\s*)"([^"]*)"`)

// Authorization 头。
var authHeader = regexp.MustCompile(`(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=\-]{8,})`)

// 环境变量式赋值：HOLDEXAR_SMTP_PASSWORD=xxx / STEAM_API_KEY=xxx。
// 要求变量名含凭据词根且全大写，避免误伤 max_length=100 这类普通赋值。
var envAssign = regexp.MustCompile(
	`\b([A-Z][A-Z0-9_]*` +
		`(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|COOKIE|CREDENTIAL|SIGNATURE)` +
		`[A-Z0-9_]*)(\s*=\s*)([^\s,;"'<>]+)`)

// Redact 抹掉一行文本里的凭据值，保留字段名。六规则顺序应用后再做路径型
// token 的第二道网。
func Redact(text string) string {
	if text == "" {
		return text
	}
	text = proxyLink.ReplaceAllString(text, "$1://***")
	text = urlUserinfo.ReplaceAllString(text, "$1://***:***@")
	text = querySecret.ReplaceAllString(text, "$1=***")
	text = jsonSecret.ReplaceAllString(text, `"$1"$2"***"`)
	text = authHeader.ReplaceAllString(text, "$1 ***")
	text = envAssign.ReplaceAllString(text, "${1}${2}***")
	return maskTokenURLs(text)
}

// RedactURL 订阅类 URL 的展示形态（日志与响应体共用）：保留 scheme+host，
// 路径与查询一律打码。订阅地址的身份凭据在路径段里的 token——逐行 redact
// 的正则对路径型 token 天然失明，所以落日志前在这里先降级成主机形态。
// 打码函数自身不得抛异常：畸形 URL 宁可整体掩码。
func RedactURL(rawURL string) string {
	if rawURL == "" {
		return ""
	}
	parts, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil {
		return "***"
	}
	if parts.Host == "" {
		return "***"
	}
	netloc := parts.Host
	if parts.User != nil {
		netloc = parts.User.String() + "@" + parts.Host
	}
	return parts.Scheme + "://" + netloc + "/***"
}

// ── 路径型 token 的第二道网 ───────────────────────────────────────
// 订阅凭据常嵌在 URL 路径段里（/api/v1/pq/<32 位随机串>），行级正则对它
// 失明，只能靠调用点先过 RedactURL——而调用点是纪律不是机制，漏写即泄漏。
// 路径里出现「整段长随机串」时整条 URL 降级为主机形态，堵住这一缺口。
// 锚定整段控制误伤面：带点的发布资产文件名、纯数字段（SteamID64）、纯字母
// 词（API 路径段）都不命中，普通 URL 的路径排障价值保留。残余盲区（短
// token、纯字母或纯数字的 token）与普通路径形状不可区分，仍依赖调用点降级。

var urlInLine = regexp.MustCompile(`(?i)https?://[^\s"'<>│]+`)
var tokenSegment = regexp.MustCompile(`^[A-Za-z0-9_-]{16,}$`)

// segmentIsToken 路径段是否形如随机凭据：整段长度达标且字母数字混合
//（排除词语段与纯数字序号段；锚定整段，带点段不命中）。
func segmentIsToken(segment string) bool {
	if !tokenSegment.MatchString(segment) {
		return false
	}
	hasDigit, hasAlpha := false, false
	for _, r := range segment {
		switch {
		case r >= '0' && r <= '9':
			hasDigit = true
		case r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z':
			hasAlpha = true
		}
	}
	return hasDigit && hasAlpha
}

// maskTokenURLs 整行内的 URL 扫描：任一路径段形如 token → 整条降级主机形态。
func maskTokenURLs(text string) string {
	return urlInLine.ReplaceAllStringFunc(text, func(match string) string {
		parts, err := url.Parse(match)
		if err != nil {
			return match
		}
		for _, seg := range strings.Split(parts.EscapedPath(), "/") {
			if segmentIsToken(seg) {
				return parts.Scheme + "://" + parts.Host + "/***"
			}
		}
		return match
	})
}
