// 局域网访问模式的状态持有层：开关、本机地址集、解锁限速。
//
// 信任模型：默认关闭＝纯回环（行为与历史版本一致）。开启后 Host 闸放行
// 本机网卡地址，局域网浏览器凭访问密码自助换会话（/session/unlock）。
// 本文件只持内存态与纯计算，持久化（设置 KV）与凭据读写归 settings/systemx。
package access

import (
	"crypto/rand"
	"encoding/base64"
	"net"
	"os"
	"sort"
	"strings"
	"sync"
	"time"
)

// 环境变量通道：HOLDEXAR_ACCESS=lan 强制开启（NAS 无窗口场景先于设置页存在）；
// 访问密码值本身只从环境读取，绝不入源码。
const (
	EnvAccess         = "HOLDEXAR_ACCESS"
	EnvAccessPassword = "HOLDEXAR_ACCESS_PASSWORD"
)

// 解锁失败限速：连续失败达阈值后全局锁定一段时间（局域网信任假设下的
// 防穷举底线；成功一次即清零）。
const (
	unlockMaxFails    = 5
	unlockLockSeconds = 30.0
)

var (
	lanMu       sync.Mutex
	lanEnabled  bool
	extraHosts  map[string]struct{}
	unlockFails int
	unlockUntil time.Time
)

// hostname 取 Host 头的主机名部分（剥端口；IPv6 字面量剥方括号）。
func hostname(host string) string {
	host = strings.ToLower(strings.TrimSpace(host))
	if host == "" {
		return ""
	}
	if strings.HasPrefix(host, "[") {
		end := strings.Index(host, "]")
		if end < 0 {
			return ""
		}
		return host[1:end]
	}
	if strings.Count(host, ":") > 1 {
		return host // 裸 IPv6 字面量（该形态本就不带端口）
	}
	if i := strings.LastIndex(host, ":"); i >= 0 {
		return host[:i]
	}
	return host
}

// IsLoopbackHost Host 是否指向本机回环（默认模式唯一放行面）。
func IsLoopbackHost(host string) bool {
	switch hostname(host) {
	case "127.0.0.1", "localhost", "::1":
		return true
	}
	return false
}

// SetExtraHosts 更新局域网模式放行的本机地址集合；空集合＝回环-only 默认模式。
func SetExtraHosts(hosts []string) {
	lanMu.Lock()
	defer lanMu.Unlock()
	normalized := make(map[string]struct{}, len(hosts))
	for _, host := range hosts {
		if name := hostname(host); name != "" {
			normalized[name] = struct{}{}
		}
	}
	extraHosts = normalized
}

// EnvForced 环境变量是否强制开启（区别于设置 KV 通道）。
func EnvForced() bool {
	return strings.ToLower(strings.TrimSpace(os.Getenv(EnvAccess))) == "lan"
}

// EnvPassword 无窗口场景由环境变量直接指定访问密码；未设置返回空串。
func EnvPassword() string {
	return strings.TrimSpace(os.Getenv(EnvAccessPassword))
}

// IsEnabled 局域网模式是否生效（环境变量强制开启优先于设置 KV 的缓存结果）。
func IsEnabled() bool {
	return EnvForced() || lanEnabled
}

// SetEnabled 更新设置 KV 通道的缓存开关；返回生效状态（环境变量开启时恒真）。
func SetEnabled(enabled bool) bool {
	lanMu.Lock()
	lanEnabled = enabled
	lanMu.Unlock()
	return IsEnabled()
}

// LocalIPs 本机全部网卡地址（字面量集合，供 Host 闸放行局域网直连）。
// 变量形态只为测试替身；生产路径恒为真实探测。
var LocalIPs = func() []string {
	ips := map[string]bool{"127.0.0.1": true, "::1": true, "localhost": true}
	if host, err := os.Hostname(); err == nil {
		if addrs, err := net.LookupIP(host); err == nil {
			for _, addr := range addrs {
				ips[addr.String()] = true
			}
		}
	}
	// 路由表探测：向外连接的本机源地址（多网卡时补齐默认出口地址）
	if conn, err := net.DialTimeout("udp", "223.5.5.5:53", 500*time.Millisecond); err == nil {
		if addr, ok := conn.LocalAddr().(*net.UDPAddr); ok && addr.IP != nil {
			ips[addr.IP.String()] = true
		}
		_ = conn.Close()
	}
	out := make([]string, 0, len(ips))
	for ip := range ips {
		if ip != "" && !strings.HasPrefix(ip, "169.254.") {
			out = append(out, ip)
		}
	}
	sort.Strings(out)
	return out
}

// RefreshExtraHosts 按当前开关同步 Host 闸白名单：开＝本机地址集，关＝回环-only。
func RefreshExtraHosts() {
	if IsEnabled() {
		SetExtraHosts(LocalIPs())
		return
	}
	SetExtraHosts(nil)
}

// UnlockLockedSeconds 解锁通道剩余锁定秒数（0＝可尝试）。
func UnlockLockedSeconds() float64 {
	lanMu.Lock()
	defer lanMu.Unlock()
	if unlockUntil.IsZero() {
		return 0
	}
	remaining := time.Until(unlockUntil).Seconds()
	if remaining <= 0 {
		return 0
	}
	return remaining
}

// UnlockRecordFailure 记录一次解锁失败；连续失败达阈值进入锁定窗口。
func UnlockRecordFailure() {
	lanMu.Lock()
	defer lanMu.Unlock()
	unlockFails++
	if unlockFails >= unlockMaxFails {
		unlockUntil = time.Now().Add(unlockLockSeconds * time.Second)
		unlockFails = 0
	}
}

// UnlockRecordSuccess 解锁成功清零失败计数与锁定。
func UnlockRecordSuccess() {
	lanMu.Lock()
	defer lanMu.Unlock()
	unlockFails = 0
	unlockUntil = time.Time{}
}

// ResetUnlockState 清空解锁限速状态（测试隔离用）。
func ResetUnlockState() {
	UnlockRecordSuccess()
}

// GeneratePassword 生成 12 位随机访问密码（URL 安全字符，手机可输入）。
func GeneratePassword() string {
	buf := make([]byte, 9)
	if _, err := rand.Read(buf); err != nil {
		panic("访问密码生成失败：" + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(buf)
}

// ResetLANState 清空局域网模式全部内存态（测试隔离用）。
func ResetLANState() {
	lanMu.Lock()
	defer lanMu.Unlock()
	lanEnabled = false
	extraHosts = nil
	unlockFails = 0
	unlockUntil = time.Time{}
}
