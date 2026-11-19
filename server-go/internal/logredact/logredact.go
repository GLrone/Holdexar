package logredact

import (
	"net/url"
	"regexp"
	"strings"
)

var proxyLink = regexp.MustCompile(`(?i)\b(vless|vmess|trojan|ssr?|hysteria2?|tuic|wireguard)://([^\s"'<>]+)`)

var urlUserinfo = regexp.MustCompile(`(?i)\b([a-z][a-z0-9+.\-]*)://([^/\s:@]+):([^/\s@]+)@`)

var querySecret = regexp.MustCompile(`(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token` +
	`|token|secret|password|passwd|pwd|signature|sign|key|sessionid` +
	`|steamloginsecure|steamlogin|authorization)=([^&\s;"'<>]+)`)

// 键名须含凭据词根，避免误抹 {"key": "games"} 等普通字段。
var jsonSecret = regexp.MustCompile(
	`(?i)"([A-Za-z0-9_.\-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token` +
		`|auth[_-]?token|token|secret|password|passwd|pwd|signature|cookie|credential)` +
		`[A-Za-z0-9_.\-]*)"(\s*:\s*)"([^"]*)"`)

var authHeader = regexp.MustCompile(`(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=\-]{8,})`)

// 仅匹配全大写含凭据词根的变量名，避免误伤普通参数赋值。
var envAssign = regexp.MustCompile(
	`\b([A-Z][A-Z0-9_]*` +
		`(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|COOKIE|CREDENTIAL|SIGNATURE)` +
		`[A-Z0-9_]*)(\s*=\s*)([^\s,;"'<>]+)`)

// Redact 按固定规则与路径型 token 兜底抹除一行日志内的凭据值（幂等）。
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

// RedactURL 将 URL 路径与查询打码为 /***（畸形输入返回 ***）。
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

var urlInLine = regexp.MustCompile(`(?i)https?://[^\s"'<>│]+`)
var tokenSegment = regexp.MustCompile(`^[A-Za-z0-9_-]{16,}$`)

// segmentIsToken 仅匹配 ≥16 位字母数字混合整段（排除纯数字/词语/带点文件名）。
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
