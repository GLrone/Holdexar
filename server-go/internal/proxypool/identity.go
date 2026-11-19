package proxypool

// 节点主键身份 = subscription_tag + 规范化配置 JSON sha256，改语义即改主键。

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

var ignoreForFingerprint = map[string]bool{
	"name": true, "display-name": true,
	"udp": true, "tfo": true, "mptcp": true, "smux": true,
	"uot": true, "xudp": true,
	"dialer-proxy": true, "interface-name": true, "routing-mark": true,
}

// portOnlyKeys 仅含 type/server/port 时端口参与节点身份。
var portOnlyKeys = map[string]bool{"type": true, "server": true, "port": true}

func PortIsIdentity(config map[string]any) bool {
	for k := range config {
		if !ignoreForFingerprint[k] && !portOnlyKeys[k] {
			return false
		}
	}
	return true
}

func SubscriptionTag(url string) string {
	text := pyTrimSpace(url)
	sum := sha256.Sum256([]byte(text))
	digest := hex.EncodeToString(sum[:])[:8]
	host := urlHostname(text)
	var parts []string
	for _, p := range strings.Split(host, ".") {
		if p != "" {
			parts = append(parts, p)
		}
	}
	label := host
	if len(parts) >= 2 {
		label = strings.Join(parts[len(parts)-2:], ".")
	}
	if label == "" {
		return digest
	}
	return digest + "." + label
}

func NodeFingerprint(config map[string]any, tag string) (string, error) {
	portIdentity := PortIsIdentity(config)
	payload := make(map[string]any, len(config))
	for k, v := range config {
		if ignoreForFingerprint[k] {
			continue
		}
		if k == "port" && !portIdentity {
			continue
		}
		payload[k] = v
	}
	blob, err := pyJSONEncode(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256([]byte(tag + "|" + blob))
	return hex.EncodeToString(sum[:]), nil
}

// 首次登记铸造后绝不重算（重算会让已绑定条目指向不存在的名字）。
func MakeRuntimeName(subscriptionID int64, originalName string, taken map[string]bool) string {
	base := fmt.Sprintf("%d|%s", subscriptionID, originalName)
	candidate, suffix := base, 1
	for taken[candidate] {
		suffix++
		candidate = fmt.Sprintf("%s#%d", base, suffix)
	}
	return candidate
}

var infoNamePatterns = []string{
	"剩余", "到期", "过期", "官网", "套餐", "重置",
	"有效期", "可用设备", "防失联", "失联",
}

func IsInfoPlaceholder(name string) bool {
	for _, p := range infoNamePatterns {
		if strings.Contains(name, p) {
			return true
		}
	}
	return false
}

func SubscriptionURLIdentity(url string) string {
	sum := sha256.Sum256([]byte(url))
	return hex.EncodeToString(sum[:])
}

func urlHostname(text string) string {
	rest := text
	if i := strings.Index(rest, "://"); i >= 0 {
		rest = rest[i+3:]
	}
	if i := strings.IndexAny(rest, "/?#"); i >= 0 {
		rest = rest[:i]
	}
	if i := strings.LastIndex(rest, "@"); i >= 0 {
		rest = rest[i+1:]
	}
	if strings.HasPrefix(rest, "[") {
		if i := strings.Index(rest, "]"); i >= 0 {
			return strings.ToLower(rest[1:i])
		}
		return strings.ToLower(rest)
	}
	if i := strings.LastIndex(rest, ":"); i >= 0 {
		rest = rest[:i]
	}
	return strings.ToLower(rest)
}
