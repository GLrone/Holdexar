package netenv

import (
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

func TestProxyCandidatesEnvironmentPrecedenceAndRegistryForms(t *testing.T) {
	for _, tc := range []struct {
		environment []string
		enabled     bool
		server      string
		want        []string
	}{
		{[]string{"HTTPS_PROXY=127.0.0.1:7897", "HTTP_PROXY=http://127.0.0.1:7897"}, true, "different:80", []string{"http://127.0.0.1:7897"}},
		{[]string{"HTTP_PROXY=upper:80", "http_proxy=lower:81"}, false, "", []string{"http://lower:81"}},
		{[]string{"HTTP_PROXY=upper:80", "REQUEST_METHOD=GET"}, true, "registry:82", []string{"http://registry:82"}},
		{[]string{"HTTP_PROXY=upper:80", "REQUEST_METHOD=GET", "http_proxy=lower:81"}, true, "registry:82", []string{"http://lower:81"}},
		{[]string{"NO_PROXY=localhost"}, true, "registry:82", nil},
		{[]string{"ALL_PROXY=socks5://host:1080"}, true, "registry:82", nil},
		{nil, true, "127.0.0.1:7897", []string{"http://127.0.0.1:7897"}},
		{nil, true, "http=http:80;https=https://secure:81;socks=socks:1080", []string{"https://secure:81", "http://http:80"}},
		{nil, true, "socks=127.0.0.1:1080", nil},
		{nil, false, "127.0.0.1:7897", nil},
		{[]string{"https_proxy=socks5://host:1080", "http_proxy=http://good:81"}, false, "", []string{"http://good:81"}},
		{[]string{"HTTP_PROXY=upper:80", "http_proxy="}, true, "registry:82", []string{"http://registry:82"}},
	} {
		proxies := environmentProxies(tc.environment)
		if len(proxies) == 0 {
			proxies = registryProxies(tc.enabled, tc.server)
		}
		if got := proxyCandidates(proxies); !reflect.DeepEqual(got, tc.want) {
			t.Fatalf("candidates=%v want=%v", got, tc.want)
		}
	}
}

func pipeConnection() net.Conn { a, b := net.Pipe(); _ = b.Close(); return a }

func TestResolverOrderedProbeAndPositiveNegativeCache(t *testing.T) {
	now := time.Date(2026, 11, 6, 0, 0, 0, 0, time.UTC)
	env := []string{"https_proxy=dead:7897", "http_proxy=alive:7890"}
	var probed []string
	r := resolver{environment: func() []string { return env }, registry: func() (bool, string) { t.Fatal("environment must suppress registry"); return false, "" }, now: func() time.Time { return now }, dial: func(_ context.Context, network, address string) (net.Conn, error) {
		probed = append(probed, address)
		if address == "alive:7890" {
			return pipeConnection(), nil
		}
		return nil, errors.New("unreachable")
	}}
	if got, err := r.resolve(context.Background()); err != nil || got != "http://alive:7890" || !reflect.DeepEqual(probed, []string{"dead:7897", "alive:7890"}) {
		t.Fatal(got, err, probed)
	}
	env = []string{"http_proxy=dead:7897"}
	if got, err := r.resolve(context.Background()); err != nil || got != "http://alive:7890" || len(probed) != 2 {
		t.Fatal(got, err, probed)
	}
	now = now.Add(30 * time.Second)
	if got, err := r.resolve(context.Background()); err != nil || got != "" || len(probed) != 3 {
		t.Fatal(got, err, probed)
	}
	if got, err := r.resolve(context.Background()); err != nil || got != "" || len(probed) != 3 {
		t.Fatal(got, err, probed)
	}
}

func TestResolverTCPListenerAndHTTPProxyCredentials(t *testing.T) {
	var requests atomic.Int32
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests.Add(1)
		if r.URL.Host != "steam.invalid" || r.URL.Path != "/items" || r.Header.Get("Proxy-Authorization") == "" {
			t.Error("proxy request or authorization missing")
		}
		_, _ = io.WriteString(w, "proxy response")
	}))
	defer proxy.Close()
	u, err := url.Parse(proxy.URL)
	if err != nil {
		t.Fatal(err)
	}
	u.User = url.UserPassword("test-user", "test-password")
	r := resolver{environment: func() []string { return []string{"HTTPS_PROXY=" + u.String()} }, registry: func() (bool, string) { return false, "" }, dial: (&net.Dialer{Timeout: time.Second}).DialContext, now: time.Now}
	got, err := r.resolve(context.Background())
	if err != nil || got != u.String() {
		t.Fatal("proxy resolution failed", err)
	}
	if requests.Load() != 0 {
		t.Fatal("resolver sent an egress probe instead of testing listener")
	}
	resolved, err := url.Parse(got)
	if err != nil {
		t.Fatal(err)
	}
	transport := &http.Transport{Proxy: http.ProxyURL(resolved)}
	defer transport.CloseIdleConnections()
	client := &http.Client{Transport: transport, Timeout: time.Second}
	resp, err := client.Get("http://steam.invalid/items")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil || string(body) != "proxy response" || requests.Load() != 1 {
		t.Fatal("configured proxy request failed", err)
	}
}

func TestResolverMalformedCandidatesAndCancellation(t *testing.T) {
	for _, candidate := range []string{"http://missing-port", "http://host:70000", "http://host:no", "socks5://host:1080", "http://:80"} {
		r := resolver{environment: func() []string { return []string{"https_proxy=" + candidate} }, registry: func() (bool, string) { return false, "" }, now: time.Now, dial: func(context.Context, string, string) (net.Conn, error) {
			t.Fatal("invalid candidate dialed")
			return nil, nil
		}}
		if got, err := r.resolve(context.Background()); err != nil || got != "" {
			t.Fatal(got, err)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	r := resolver{}
	if _, err := r.resolve(ctx); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	ctx, cancel = context.WithCancel(context.Background())
	r = resolver{environment: func() []string { return []string{"http_proxy=host:80"} }, registry: func() (bool, string) { return false, "" }, now: time.Now, dial: func(context.Context, string, string) (net.Conn, error) { cancel(); return nil, context.Canceled }}
	if _, err := r.resolve(ctx); !errors.Is(err, context.Canceled) || r.cached {
		t.Fatal("cancellation cached", err)
	}
}

func TestResolverConcurrentCallersShareProbe(t *testing.T) {
	var probes atomic.Int32
	r := resolver{environment: func() []string { return []string{"https_proxy=[::1]:7897"} }, registry: func() (bool, string) { return false, "" }, now: time.Now, dial: func(_ context.Context, _ string, address string) (net.Conn, error) {
		probes.Add(1)
		if address != "[::1]:7897" {
			t.Error(address)
		}
		return pipeConnection(), nil
	}}
	var callers sync.WaitGroup
	for range 16 {
		callers.Go(func() {
			if got, err := r.resolve(context.Background()); err != nil || got != "http://[::1]:7897" {
				t.Error(got, err)
			}
		})
	}
	callers.Wait()
	if probes.Load() != 1 {
		t.Fatal(probes.Load())
	}
}
