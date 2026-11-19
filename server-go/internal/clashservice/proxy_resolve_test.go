package clashservice

import (
	"context"
	"errors"
	"testing"

	"holdexar/server-go/internal/clashkernel"
)

func TestResolveOutboundProxy(t *testing.T) {
	ctx := context.Background()
	runningKernel := func(kernel *stubRuntime) {
		kernel.mu.Lock()
		kernel.st = clashkernel.Status{Running: true, Port: 18347}
		kernel.mu.Unlock()
	}
	stoppedKernel := func(kernel *stubRuntime) {
		kernel.mu.Lock()
		kernel.st = clashkernel.Status{}
		kernel.mu.Unlock()
	}
	systemProxy := func(url string, err error) func(context.Context) (string, error) {
		return func(context.Context) (string, error) { return url, err }
	}

	t.Run("direct_first 恒直连", func(t *testing.T) {
		svc, kernel, _ := newFixture(t)
		runningKernel(kernel)
		svc.PortListeningFn = func(int) bool { return true }
		svc.SystemProxy = systemProxy("http://127.0.0.1:7890", nil)
		if err := seedStrategy(t, svc, "direct_first"); err != nil {
			t.Fatal(err)
		}
		if url, ok := svc.ResolveOutboundProxy(ctx); ok || url != "" {
			t.Fatalf("direct_first 应直连：%q %v", url, ok)
		}
	})

	t.Run("direct_only 系统代理三态", func(t *testing.T) {
		svc, _, _ := newFixture(t)
		svc.SystemProxy = systemProxy("http://127.0.0.1:7890", nil)
		if url, ok := svc.ResolveOutboundProxy(ctx); !ok || url != "http://127.0.0.1:7890" {
			t.Fatalf("有系统代理应返回：%q %v", url, ok)
		}
		svc.SystemProxy = systemProxy("", nil)
		if url, ok := svc.ResolveOutboundProxy(ctx); ok || url != "" {
			t.Fatalf("无系统代理应直连：%q %v", url, ok)
		}
		svc.SystemProxy = systemProxy("", errors.New("boom"))
		if url, ok := svc.ResolveOutboundProxy(ctx); ok || url != "" {
			t.Fatalf("解析失败应直连不抛错：%q %v", url, ok)
		}
	})

	t.Run("缺省策略 direct_only", func(t *testing.T) {
		svc, _, _ := newFixture(t)
		svc.SystemProxy = systemProxy("http://127.0.0.1:7890", nil)
		if url, ok := svc.ResolveOutboundProxy(ctx); !ok || url != "http://127.0.0.1:7890" {
			t.Fatalf("无 KV 行应走 direct_only：%q %v", url, ok)
		}
	})

	t.Run("proxy_first 内核端口优先失败回直连", func(t *testing.T) {
		svc, kernel, _ := newFixture(t)
		if err := seedStrategy(t, svc, "proxy_first"); err != nil {
			t.Fatal(err)
		}
		svc.PortListeningFn = func(int) bool { return true }
		runningKernel(kernel)
		if url, ok := svc.ResolveOutboundProxy(ctx); !ok || url != "http://127.0.0.1:18347" {
			t.Fatalf("内核端口可用应返回：%q %v", url, ok)
		}
		svc.PortListeningFn = func(int) bool { return false }
		svc.LocalFallback = func() int { return 0 }
		if url, ok := svc.ResolveOutboundProxy(ctx); ok || url != "" {
			t.Fatalf("端口未听应直连：%q %v", url, ok)
		}
		stoppedKernel(kernel)
		svc.PortListeningFn = func(int) bool { return true }
		if url, ok := svc.ResolveOutboundProxy(ctx); ok || url != "" {
			t.Fatalf("内核未运行应直连：%q %v", url, ok)
		}
		svc.LocalFallback = func() int { return 7897 }
		if url, ok := svc.ResolveOutboundProxy(ctx); !ok || url != "http://127.0.0.1:7897" {
			t.Fatalf("应回落 7897 兜底口：%q %v", url, ok)
		}
		if _, err := svc.DB.Exec(`INSERT INTO proxies(scheme,host,port,enabled,status,consecutive_failures) VALUES('http','10.1.2.3',8888,1,'ok',0)`); err != nil {
			t.Fatal(err)
		}
		if url, ok := svc.ResolveOutboundProxy(ctx); !ok || url != "http://10.1.2.3:8888" {
			t.Fatalf("proxy_first 无内核时应优先取池节点：%q %v", url, ok)
		}
	})

	t.Run("proxy_only 失败回直连不抛错", func(t *testing.T) {
		svc, kernel, _ := newFixture(t)
		if err := seedStrategy(t, svc, "proxy_only"); err != nil {
			t.Fatal(err)
		}
		stoppedKernel(kernel)
		if url, ok := svc.ResolveOutboundProxy(ctx); ok || url != "" {
			t.Fatalf("proxy_only 无端口应直连：%q %v", url, ok)
		}
		runningKernel(kernel)
		svc.PortListeningFn = func(int) bool { return true }
		if url, ok := svc.ResolveOutboundProxy(ctx); !ok || url != "http://127.0.0.1:18347" {
			t.Fatalf("proxy_only 端口可用应返回：%q %v", url, ok)
		}
	})

	t.Run("未知策略按内核端口兜底", func(t *testing.T) {
		svc, kernel, _ := newFixture(t)
		if err := seedStrategy(t, svc, "weird_strategy"); err != nil {
			t.Fatal(err)
		}
		runningKernel(kernel)
		svc.PortListeningFn = func(int) bool { return true }
		if url, ok := svc.ResolveOutboundProxy(ctx); !ok || url != "http://127.0.0.1:18347" {
			t.Fatalf("未知策略应走内核端口：%q %v", url, ok)
		}
	})
}

func seedStrategy(t *testing.T, svc *Service, strategy string) error {
	t.Helper()
	_, err := svc.DB.Exec(`INSERT INTO app_settings(key,value_json) VALUES('proxy.strategy', ?)`,
		`"`+strategy+`"`)
	return err
}
