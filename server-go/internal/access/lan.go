// 局域网访问模式的状态持有层：开关、本机地址集、解锁限速（对齐 Python
// core/lan_access.py）。信任模型：默认关闭＝纯回环（行为与历史版本一致）。
// 开启后 Host 闸放行本机网卡地址，局域网浏览器凭访问密码自助换会话
// （/api/v1/session/unlock）。本文件只持内存态与纯计算；持久化（设置 KV）
// 与凭据读写归 settings 域。
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

// 环境变量：HOLDEXAR_ACCESS=lan 强制开启（先于设置页存在的 NAS 无窗口
// 场景）；密码值本身只从环境读取，绝不入源码。
const (
	EnvAccess         = "HOLDEXAR_ACCESS"
	EnvAccessPassword = "HOLDEXAR_ACCESS_PASSWORD"
)

// 解锁失败限速：连续失败达阈值后全局锁定一段时间（局域网信任假设下的
// 防穷举底线；成功一次即清零）。锁定窗过期后再次穷举的，锁时按轮次加倍
// （渐进式升级，防「过期-再锁」循环维持无限解锁拒绝）。
const (
	unlockMaxFails = 5
	unlockLockTime = 30 * time.Second
	unlockLockMax  = 10 * time.Minute
)

var (
	lanMu           sync.Mutex
	lanEnabled      bool
	unlockFailCount int
	unlockLockUntil time.Time
	unlockLockCycle int // 连续锁定轮次（锁窗过期后再次触发则递增）
)

// EnvForced 环境变量是否强制开启（区别于设置 KV 通道）。
func EnvForced() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(EnvAccess)), "lan")
}

// LANEnabled 局域网模式是否生效（环境变量强制开启优先于设置 KV 的缓存结果）。
func LANEnabled() bool {
	lanMu.Lock()
	defer lanMu.Unlock()
	return lanEnabled || EnvForced()
}

// SetLANEnabled 更新设置 KV 通道的缓存开关；返回生效状态（环境变量开启时恒真）。
func SetLANEnabled(enabled bool) bool {
	lanMu.Lock()
	lanEnabled = enabled
	lanMu.Unlock()
	return LANEnabled()
}

// EnvPassword 无窗口场景由环境变量直接指定访问密码；未设置返回空串。
func EnvPassword() string {
	return strings.TrimSpace(os.Getenv(EnvAccessPassword))
}

// GeneratePassword 生成 12 位随机访问密码（URL 安全字符，手机可输入；
// Python token_urlsafe(9) 同参同形）。
func GeneratePassword() string {
	buf := make([]byte, 9)
	if _, err := rand.Read(buf); err != nil {
		panic("访问密码生成失败：" + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(buf)
}

// LocalIPs 本机全部网卡地址（字面量集合，供 Host 闸放行局域网直连）：
// 回环集 + 主机名解析 + UDP connect 探测的默认出口源地址，滤链路本地段。
func LocalIPs() []string {
	ips := map[string]struct{}{"127.0.0.1": {}, "::1": {}, "localhost": {}}
	if host, err := os.Hostname(); err == nil {
		if addrs, err := net.LookupHost(host); err == nil {
			for _, addr := range addrs {
				ips[addr] = struct{}{}
			}
		}
	}
	// 路由表探测：向外连接的本机源地址（多网卡时补齐默认出口地址）
	if conn, err := net.DialTimeout("udp", "223.5.5.5:53", 500*time.Millisecond); err == nil {
		if addr, ok := conn.LocalAddr().(*net.UDPAddr); ok && addr.IP != nil {
			ips[addr.IP.String()] = struct{}{}
		}
		conn.Close()
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
	if LANEnabled() {
		SetExtraHosts(LocalIPs())
		return
	}
	SetExtraHosts(nil)
}

// UnlockLockedSeconds 解锁通道剩余锁定秒数（0＝可尝试）。
func UnlockLockedSeconds() float64 {
	lanMu.Lock()
	defer lanMu.Unlock()
	if unlockLockUntil.IsZero() {
		return 0
	}
	remaining := time.Until(unlockLockUntil)
	if remaining <= 0 {
		return 0
	}
	return remaining.Seconds()
}

// RecordUnlockFailure 记录一次解锁失败；连续失败达阈值进入锁定窗口。
// 锁窗过期后再次穷举的，锁时按连续轮次加倍（上限 10 分钟）。
func RecordUnlockFailure() {
	lanMu.Lock()
	defer lanMu.Unlock()
	unlockFailCount++
	if unlockFailCount >= unlockMaxFails {
		lock := unlockLockTime
		for i := 0; i < unlockLockCycle && lock < unlockLockMax; i++ {
			lock *= 2
		}
		if lock > unlockLockMax {
			lock = unlockLockMax
		}
		unlockLockUntil = time.Now().Add(lock)
		unlockFailCount = 0
		unlockLockCycle++
	}
}

// RecordUnlockSuccess 解锁成功清零失败计数、锁定与升级轮次。
func RecordUnlockSuccess() {
	lanMu.Lock()
	defer lanMu.Unlock()
	unlockFailCount = 0
	unlockLockUntil = time.Time{}
	unlockLockCycle = 0
}

// ResetUnlockState 测试隔离：清空限速状态。
func ResetUnlockState() {
	lanMu.Lock()
	defer lanMu.Unlock()
	unlockFailCount = 0
	unlockLockUntil = time.Time{}
}
