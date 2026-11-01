package access

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

func newTicketFile(t *testing.T) *BootstrapFile {
	t.Helper()
	b := NewBootstrapFile(filepath.Join(t.TempDir(), "server-bootstrap.ticket"))
	return b
}

func TestPublishWritesNonceOnly(t *testing.T) {
	b := newTicketFile(t)
	if err := b.Publish("nonce-abc"); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	raw, err := os.ReadFile(b.path)
	if err != nil {
		t.Fatalf("读票据文件: %v", err)
	}
	if string(raw) != "nonce-abc" {
		t.Fatalf("文件内容必须是 nonce 本体，得到 %q", string(raw))
	}
	if runtime.GOOS != "windows" {
		info, err := os.Stat(b.path)
		if err != nil {
			t.Fatalf("Stat: %v", err)
		}
		if perm := info.Mode().Perm(); perm != 0o600 {
			t.Fatalf("文件权限应为 0600，得到 %o", perm)
		}
	}
}

func TestPublishMissingParentFails(t *testing.T) {
	b := NewBootstrapFile(filepath.Join(t.TempDir(), "缺目录", "server-bootstrap.ticket"))
	if err := b.Publish("nonce"); err == nil {
		t.Fatal("父目录不存在时必须报错")
	}
}

func TestConsumedRemovesFile(t *testing.T) {
	b := newTicketFile(t)
	if err := b.Publish("nonce"); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	b.Consumed()
	if _, err := os.Stat(b.path); !os.IsNotExist(err) {
		t.Fatalf("消费后文件必须被删除，Stat 错误：%v", err)
	}
}

func TestExpiryCleanupRemovesFile(t *testing.T) {
	b := newTicketFile(t)
	b.ttl = 60 * time.Millisecond
	if err := b.Publish("nonce"); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(b.path); os.IsNotExist(err) {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("过期后文件未被清理")
}

func TestMiddlewareConsumesOn303Only(t *testing.T) {
	newChain := func(next http.Handler) (*BootstrapFile, http.Handler) {
		b := newTicketFile(t)
		if err := b.Publish("nonce"); err != nil {
			t.Fatalf("Publish: %v", err)
		}
		return b, b.Middleware(next)
	}

	// 303（会话发放成功）→ 文件删除
	b, chain := newChain(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusSeeOther)
	}))
	rec := httptest.NewRecorder()
	chain.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, bootstrapEndpoint+"?nonce=nonce", nil))
	if rec.Code != http.StatusSeeOther {
		t.Fatalf("下游状态应原样透传，得到 %d", rec.Code)
	}
	if _, err := os.Stat(b.path); !os.IsNotExist(err) {
		t.Fatalf("303 后文件必须被删除，Stat 错误：%v", err)
	}

	// 401（无效/过期）→ 文件保留，交给过期计时
	b, chain = newChain(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusUnauthorized)
	}))
	rec = httptest.NewRecorder()
	chain.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, bootstrapEndpoint+"?nonce=nonce", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("下游状态应原样透传，得到 %d", rec.Code)
	}
	if _, err := os.Stat(b.path); err != nil {
		t.Fatalf("401 后文件必须保留，Stat 错误：%v", err)
	}

	// 其他路径不触发清理
	b, chain = newChain(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusSeeOther)
	}))
	rec = httptest.NewRecorder()
	chain.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/api/v1/health", nil))
	if _, err := os.Stat(b.path); err != nil {
		t.Fatalf("非引导端点不得触发清理，Stat 错误：%v", err)
	}
}

func TestCloseRemovesLeftover(t *testing.T) {
	b := newTicketFile(t)
	if err := b.Publish("nonce"); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	b.Close()
	if _, err := os.Stat(b.path); !os.IsNotExist(err) {
		t.Fatalf("退出兜底必须删除仍在场的文件，Stat 错误：%v", err)
	}
}

func TestPublishResetsExpiryTimer(t *testing.T) {
	b := newTicketFile(t)
	b.ttl = 80 * time.Millisecond
	if err := b.Publish("first"); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	time.Sleep(40 * time.Millisecond)
	if err := b.Publish("second"); err != nil {
		t.Fatalf("Publish: %v", err)
	}
	time.Sleep(60 * time.Millisecond) // 距第一次写入已超时，距第二次未超时
	if _, err := os.Stat(b.path); err != nil {
		t.Fatalf("重复发布应重置过期计时，Stat 错误：%v", err)
	}
	raw, _ := os.ReadFile(b.path)
	if string(raw) != "second" {
		t.Fatalf("文件应被最新票据覆盖，得到 %q", string(raw))
	}
}

func TestBootstrapEndpointInPublicPaths(t *testing.T) {
	if !PublicPaths[bootstrapEndpoint] {
		t.Fatal("引导端点必须在公开路径白名单内")
	}
}
