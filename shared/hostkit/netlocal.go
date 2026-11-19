package hostkit

import (
	"net"
	"net/netip"
	"slices"
	"strconv"
	"strings"
)

// LocalIPs returns sorted, non-loopback, non-link-local IP addresses from active network interfaces.
func LocalIPs() []string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil
	}
	seen := map[string]struct{}{}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagUp == 0 || iface.Flags&net.FlagLoopback != 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, rawAddr := range addrs {
			var ip net.IP
			switch v := rawAddr.(type) {
			case *net.IPNet:
				ip = v.IP
			case *net.IPAddr:
				ip = v.IP
			}
			if len(ip) == 0 {
				continue
			}
			addr, ok := netip.AddrFromSlice(ip)
			if !ok {
				continue
			}
			addr = addr.Unmap().WithZone("")
			if !addr.IsValid() || addr.IsUnspecified() || addr.IsLoopback() ||
				addr.IsMulticast() || addr.IsLinkLocalUnicast() || addr.IsLinkLocalMulticast() {
				continue
			}
			seen[addr.String()] = struct{}{}
		}
	}
	out := make([]string, 0, len(seen))
	for ip := range seen {
		out = append(out, ip)
	}
	slices.Sort(out)
	return out
}

func formatLocalServiceURLs(port int, ips []string) []string {
	portStr := strconv.Itoa(port)
	out := make([]string, 0, 1+len(ips))
	seen := map[string]struct{}{}
	addHost := func(host string) {
		clean := strings.TrimSuffix(strings.TrimPrefix(strings.TrimSpace(host), "["), "]")
		if clean == "" {
			return
		}
		url := "http://" + net.JoinHostPort(clean, portStr)
		if _, dup := seen[url]; dup {
			return
		}
		seen[url] = struct{}{}
		out = append(out, url)
	}
	addHost("127.0.0.1")
	sorted := append([]string(nil), ips...)
	slices.Sort(sorted)
	for _, ip := range sorted {
		addHost(ip)
	}
	return out
}

// LocalServiceURLs returns http://127.0.0.1:<port> followed by properly bracketed http://<ip>:<port> URLs for LocalIPs().
func LocalServiceURLs(port int) []string {
	return formatLocalServiceURLs(port, LocalIPs())
}
