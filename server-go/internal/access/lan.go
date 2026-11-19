package access

import (
	"crypto/rand"
	"encoding/base64"
	"net"
	"net/netip"
	"os"
	"slices"
	"strings"
	"sync"
	"time"

	"holdexar/hostkit"
)

// 环境变量名；HOLDEXAR_ACCESS=lan 强制开启局域网模式。
const (
	EnvAccess         = "HOLDEXAR_ACCESS"
	EnvAccessPassword = "HOLDEXAR_ACCESS_PASSWORD"
)

// 按对端分桶限速：连续 5 次失败锁定 30s，后续轮次翻倍至 10m 上限；桶上限 512，满时先清未锁定/已过期桶再随机驱逐。
const (
	unlockMaxFails = 5
	unlockLockTime = 30 * time.Second
	unlockLockMax  = 10 * time.Minute
	peerCap        = 512
)

type peerState struct {
	fails     int
	lockUntil time.Time
	cycle     int
}

var (
	lanMu      sync.Mutex
	lanEnabled bool
	peersMu    sync.Mutex
	peers      = make(map[string]*peerState)
)

func EnvForced() bool {
	return strings.EqualFold(strings.TrimSpace(os.Getenv(EnvAccess)), "lan")
}

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

func EnvPassword() string {
	return strings.TrimSpace(os.Getenv(EnvAccessPassword))
}

func GeneratePassword() string {
	buf := make([]byte, 9)
	if _, err := rand.Read(buf); err != nil {
		panic("访问密码生成失败：" + err.Error())
	}
	return base64.RawURLEncoding.EncodeToString(buf)
}

// LocalIPs 返回回环与本机活动网卡非链路本地地址集合。
func LocalIPs() []string {
	ips := map[string]struct{}{"127.0.0.1": {}, "::1": {}, "localhost": {}}
	for _, ip := range hostkit.LocalIPs() {
		ips[ip] = struct{}{}
	}
	out := make([]string, 0, len(ips))
	for ip := range ips {
		out = append(out, ip)
	}
	slices.Sort(out)
	return out
}

func RefreshExtraHosts() {
	if LANEnabled() {
		SetExtraHosts(LocalIPs())
		return
	}
	SetExtraHosts(nil)
}

// PeerKey 从对端地址提取分桶 IP；回环统一归入 "loopback"，空地址归入 "unknown"。
func PeerKey(remoteAddr string) string {
	remoteAddr = strings.TrimSpace(remoteAddr)
	if remoteAddr == "" {
		return "unknown"
	}
	host, _, err := net.SplitHostPort(remoteAddr)
	if err != nil {
		host = remoteAddr
	}
	host = strings.TrimSuffix(strings.TrimPrefix(host, "["), "]")
	if host == "" {
		return "unknown"
	}
	addr, perr := netip.ParseAddr(host)
	if perr != nil {
		return host
	}
	addr = addr.Unmap()
	if addr.IsLoopback() {
		return "loopback"
	}
	return addr.String()
}

func resolvePeer(peer ...string) string {
	if len(peer) > 0 && strings.TrimSpace(peer[0]) != "" {
		return strings.TrimSpace(peer[0])
	}
	return "loopback"
}

func getPeer(peer string) *peerState {
	if s, ok := peers[peer]; ok {
		return s
	}
	if len(peers) >= peerCap {
		now := time.Now()
		for k, v := range peers {
			if v.lockUntil.Before(now) {
				delete(peers, k)
				if len(peers) < peerCap {
					break
				}
			}
		}
		for len(peers) >= peerCap {
			for k := range peers {
				delete(peers, k)
				break
			}
		}
	}
	s := &peerState{}
	peers[peer] = s
	return s
}

func UnlockLockedSeconds(peer ...string) float64 {
	target := resolvePeer(peer...)
	peersMu.Lock()
	defer peersMu.Unlock()
	s, ok := peers[target]
	if !ok || s.lockUntil.IsZero() {
		return 0
	}
	remaining := time.Until(s.lockUntil)
	if remaining <= 0 {
		return 0
	}
	return remaining.Seconds()
}

func RecordUnlockFailure(peer ...string) {
	target := resolvePeer(peer...)
	peersMu.Lock()
	defer peersMu.Unlock()
	s := getPeer(target)
	s.fails++
	if s.fails >= unlockMaxFails {
		lock := unlockLockTime
		for i := 0; i < s.cycle && lock < unlockLockMax; i++ {
			lock *= 2
		}
		if lock > unlockLockMax {
			lock = unlockLockMax
		}
		s.lockUntil = time.Now().Add(lock)
		s.fails = 0
		s.cycle++
	}
}

func RecordUnlockSuccess(peer ...string) {
	target := resolvePeer(peer...)
	peersMu.Lock()
	defer peersMu.Unlock()
	delete(peers, target)
}

func RecordUnlockFailureFor(peer string) {
	RecordUnlockFailure(peer)
}

func RecordUnlockSuccessFor(peer string) {
	RecordUnlockSuccess(peer)
}

func ResetUnlockState() {
	peersMu.Lock()
	defer peersMu.Unlock()
	peers = make(map[string]*peerState)
}
