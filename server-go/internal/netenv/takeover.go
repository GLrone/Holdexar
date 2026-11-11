package netenv

import (
	"context"
	"net"
	"net/netip"
	"sync"
	"time"
)

const TakeoverReason = "本机网络被其他软件接管，为避免误判已暂停相关动作，关闭后会自动继续"

var takeover = &takeoverProbe{lookup: net.DefaultResolver.LookupNetIP, now: time.Now}
var fakeNetwork = netip.MustParsePrefix("198.18.0.0/16")

type takeoverProbe struct {
	mu     sync.Mutex
	lookup func(context.Context, string, string) ([]netip.Addr, error)
	now    func() time.Time
	at     time.Time
	value  bool
}

func NetworkTakenOver(ctx context.Context) bool {
	return takeover.takenOver(ctx)
}

func (p *takeoverProbe) takenOver(ctx context.Context) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	now := p.now()
	if now.Sub(p.at) < 30*time.Second {
		return p.value
	}
	query, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	addresses, err := p.lookup(query, "ip", "www.baidu.com")
	if ctx.Err() != nil {
		return false
	}
	p.at, p.value = now, false
	if err == nil {
		for _, addr := range addresses {
			if fakeNetwork.Contains(addr.Unmap()) {
				p.value = true
				break
			}
		}
	}
	return p.value
}
