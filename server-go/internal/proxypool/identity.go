package proxypool

// 节点身份（Python models.py 的逐字节对齐移植，改语义等于改主键）：
// 节点身份 = subscription_tag（订阅标识）+ 整份配置规范化 JSON 的 sha256。
// 订阅标识进身份是刻意的：同一份配置由两个订阅提供时是两个节点。

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"
)

// 不参与节点身份的键：命名与传输层开关不改变「这个节点是谁」。
var ignoreForFingerprint = map[string]bool{
	"name": true, "display-name": true,
	"udp": true, "tfo": true, "mptcp": true, "smux": true,
	"uot": true, "xudp": true,
	"dialer-proxy": true, "interface-name": true, "routing-mark": true,
}

// portOnlyKeys 除 type/server 外没有别的实质字段时，端口是唯一能分辨
// 两个节点的东西（本地代理链形态），此时它参与身份；机场节点带
// password/sni/uuid，动态端口每次拉取都换号，含它会让同一节点反复重登记。
var portOnlyKeys = map[string]bool{"type": true, "server": true, "port": true}

// PortIsIdentity 配置是否只能靠端口区分。
func PortIsIdentity(config map[string]any) bool {
	for k := range config {
		if !ignoreForFingerprint[k] && !portOnlyKeys[k] {
			return false
		}
	}
	return true
}

// SubscriptionTag 订阅标识：sha256(链接)[:8] + 域名后两段（排障可读）。
// 按订阅唯一而不是按机场：同一机场的两个套餐订阅各自记账。
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

// NodeFingerprint 节点指纹：tag + 剔除消费侧噪声后规范化 JSON 的 sha256。
// 键顺序不影响结果；未知键参与身份（宁可重登记一条，不可并成一条台账）。
// tag 是 SubscriptionTag(url) 的结果；空串只用于无订阅语义的纯配置比较。
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

// MakeRuntimeName 池内唯一名：<订阅>|原名，同订阅撞名追加 #2 #3。
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

// 机场信息占位节点名称特征：公告位（剩余流量/到期/官网等）不是代理端点，
// 且内容随订阅更新漂移，同名会反复重生。
var infoNamePatterns = []string{
	"剩余", "到期", "过期", "官网", "套餐", "重置",
	"有效期", "可用设备", "防失联", "失联",
}

// IsInfoPlaceholder 节点名是否为机场信息占位节点（不得进池）。
func IsInfoPlaceholder(name string) bool {
	for _, p := range infoNamePatterns {
		if strings.Contains(name, p) {
			return true
		}
	}
	return false
}

// SubscriptionURLIdentity 链接的不可逆缓存关联身份。
func SubscriptionURLIdentity(url string) string {
	sum := sha256.Sum256([]byte(url))
	return hex.EncodeToString(sum[:])
}

// urlHostname 提取 URL 主机名并对齐 Python urlsplit.hostname 的可见行为：
// 去 scheme/userinfo/端口/IPv6 方括号、小写化；解析不出返回空串。
func urlHostname(text string) string {
	rest := text
	if i := strings.Index(rest, "://"); i >= 0 {
		rest = rest[i+3:]
	}
	// authority 到路径/查询/片段边界为止
	if i := strings.IndexAny(rest, "/?#"); i >= 0 {
		rest = rest[:i]
	}
	// userinfo（@ 不出现在主机合法字符中，取最后一个）
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
