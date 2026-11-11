package netenv

import (
	"context"
	"errors"
	"net/netip"
	"testing"
	"time"
)

func TestNetworkTakeoverCacheExpiresAndFailsOpenOnLookupError(t *testing.T) {
	now := time.Now()
	calls := 0
	probe := &takeoverProbe{
		now: func() time.Time { return now },
		lookup: func(_ context.Context, network, host string) ([]netip.Addr, error) {
			calls++
			if network != "ip" || host != "www.baidu.com" {
				t.Fatalf("unexpected lookup %s %s", network, host)
			}
			if calls > 1 {
				return nil, errors.New("DNS lookup failed")
			}
			return []netip.Addr{netip.MustParseAddr("::ffff:198.18.0.120")}, nil
		},
	}
	if !probe.takenOver(context.Background()) || !probe.takenOver(context.Background()) || calls != 1 {
		t.Fatalf("fake IP not recognized or reused: calls=%d", calls)
	}
	now = now.Add(31 * time.Second)
	if probe.takenOver(context.Background()) || calls != 2 {
		t.Fatalf("DNS error retained stale protection: calls=%d", calls)
	}
}
