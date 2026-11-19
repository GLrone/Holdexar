package hostkit

import (
	"net/netip"
	"sort"
	"strings"
	"testing"
)

func TestLocalIPsExcludesLoopbackAndLinkLocal(t *testing.T) {
	ips := LocalIPs()
	if !sort.StringsAreSorted(ips) {
		t.Fatalf("LocalIPs not sorted: %v", ips)
	}
	for _, raw := range ips {
		addr, err := netip.ParseAddr(raw)
		if err != nil {
			t.Fatalf("invalid IP %q: %v", raw, err)
		}
		if addr.IsLoopback() || addr.IsLinkLocalUnicast() || addr.IsLinkLocalMulticast() || addr.IsUnspecified() {
			t.Fatalf("unexpected non-LAN IP in LocalIPs: %q", raw)
		}
	}
}

func TestLocalServiceURLsIPv4AndIPv6Bracketing(t *testing.T) {
	urls := formatLocalServiceURLs(28765, []string{
		"2001:db8::1",
		"192.168.1.50",
		"[fd00::abcd]",
		"127.0.0.1",
	})
	want := []string{
		"http://127.0.0.1:28765",
		"http://192.168.1.50:28765",
		"http://[2001:db8::1]:28765",
		"http://[fd00::abcd]:28765",
	}
	if len(urls) != len(want) {
		t.Fatalf("urls = %v, want %v", urls, want)
	}
	for i := range want {
		if urls[i] != want[i] {
			t.Fatalf("urls[%d] = %q, want %q", i, urls[i], want[i])
		}
	}

	live := LocalServiceURLs(28765)
	if len(live) == 0 || live[0] != "http://127.0.0.1:28765" {
		t.Fatalf("LocalServiceURLs missing loopback head: %v", live)
	}
	for _, u := range live {
		if strings.Contains(u, "[[") || strings.Contains(u, "]]") {
			t.Fatalf("double-bracketed IPv6 URL: %q", u)
		}
	}
}
