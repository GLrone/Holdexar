package pilot

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func TestSameOriginHelper(t *testing.T) {
	cases := []struct {
		a, b     string
		expected bool
	}{
		{"http://127.0.0.1:8000", "http://127.0.0.1:8000/v1", true},
		{"https://api.openai.com/v1", "https://api.openai.com/v2", true},
		{"https://api.openai.com", "http://api.openai.com", false},
		{"https://api.openai.com", "https://evil.example.com", false},
		{"http://127.0.0.1:8000", "http://127.0.0.1:8001", false},
		{"https://api.openai.com:443", "https://api.openai.com", true},
		{"http://localhost:80", "http://localhost", true},
		{"", "https://api.openai.com", false},
	}
	for _, tc := range cases {
		if got := sameOrigin(tc.a, tc.b); got != tc.expected {
			t.Errorf("sameOrigin(%q, %q) = %v, expected %v", tc.a, tc.b, got, tc.expected)
		}
	}
}

func newTestPilotServiceWithKV(t *testing.T, dbPath string) *Service {
	t.Helper()
	db, e := openPilotTestDB(dbPath)
	if e != nil {
		t.Fatal(e)
	}
	t.Cleanup(func() { db.Close() })

	var mu sync.Mutex
	values := make(map[string]json.RawMessage)
	secrets := make(map[string]string)

	deps := Deps{
		DB:      db,
		DataDir: t.TempDir(),
		KVGet: func(key string) (json.RawMessage, bool) {
			mu.Lock()
			defer mu.Unlock()
			v, ok := values[key]
			return v, ok
		},
		KVGetSecret: func(key string) (string, bool) {
			mu.Lock()
			defer mu.Unlock()
			s, ok := secrets[key]
			return s, ok
		},
		KVSet: func(ctx context.Context, vals map[string]json.RawMessage, secs map[string]string) error {
			mu.Lock()
			defer mu.Unlock()
			for k, v := range vals {
				values[k] = v
			}
			for k, s := range secs {
				if s == "" {
					delete(secrets, k)
					delete(values, k)
				} else {
					secrets[k] = s
				}
			}
			return nil
		},
	}
	service := New(deps, nil)
	service.SetGate(store.NewWriteGate(db, dbPath))
	return service
}

func TestPilotDetectCrossOriginKeyLeakRejected(t *testing.T) {
	dir := t.TempDir()
	svc := newTestPilotServiceWithKV(t, filepath.Join(dir, "pilot.db"))

	err := svc.SaveConfig(context.Background(), map[string]any{
		"base_url": "https://api.openai.com/v1",
		"api_key":  "sk-test-secret-key-12345",
		"protocol": "openai",
		"model":    "gpt-4o",
	})
	if err != nil {
		t.Fatalf("SaveConfig failed: %v", err)
	}

	mux := http.NewServeMux()
	Register(mux, svc)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/api/v1/pilot/detect", strings.NewReader(`{"base_url":"https://attacker.example.com/v1"}`))
	mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("跨源探测未传密钥应 422 拒绝，实际 %d: %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), keyScopeHint) {
		t.Fatalf("错误文案不符：%s", rec.Body.String())
	}

	recTest := httptest.NewRecorder()
	evilBase := "https://attacker.example.com/v1"
	reqTest := httptest.NewRequest("POST", "/api/v1/pilot/test", strings.NewReader(`{"base_url":"`+evilBase+`"}`))
	mux.ServeHTTP(recTest, reqTest)
	if recTest.Code != http.StatusUnprocessableEntity {
		t.Fatalf("跨源连通性测试未传密钥应 422 拒绝，实际 %d: %s", recTest.Code, recTest.Body.String())
	}
	if !strings.Contains(recTest.Body.String(), keyScopeHint) {
		t.Fatalf("错误文案不符：%s", recTest.Body.String())
	}
}

func TestSameOriginRejectsUnsafeSchemesAndInvalidURLs(t *testing.T) {
	bad := [][2]string{
		{"", "https://api.openai.com"},
		{"https://api.openai.com", ""},
		{"ftp://api.openai.com", "ftp://api.openai.com"},
		{"javascript:alert(1)", "javascript:alert(1)"},
		{"api.openai.com", "api.openai.com"},
	}
	for _, tc := range bad {
		if sameOrigin(tc[0], tc[1]) {
			t.Fatalf("sameOrigin(%q, %q) 应拒绝非 http/https 或空地址", tc[0], tc[1])
		}
	}
}

func TestProviderKeyOriginFailClosedAndDeleteCleanup(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "pilot.db")
	svc := newTestPilotServiceWithKV(t, dbPath)
	ctx := context.Background()

	created, err := svc.CreateProvider(ctx, map[string]any{
		"name":     "OpenAI",
		"protocol": "openai",
		"base_url": "https://api.openai.com/v1",
		"api_key":  "sk-origin-bound",
	})
	if err != nil {
		t.Fatal(err)
	}
	if !created.HasKey || created.KeyOrigin != "https://api.openai.com:443" {
		t.Fatalf("新建供应商应记录 key_origin 且 has_key=true: %+v", created)
	}
	if got := svc.ProviderKey(created.ID); got != "sk-origin-bound" {
		t.Fatalf("同源应读回密钥: %q", got)
	}

	updated, err := svc.UpdateProvider(ctx, created.ID, map[string]any{
		"base_url": "https://evil.example.com/v1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if updated.HasKey || updated.KeyOrigin != "" {
		t.Fatalf("跨源改址后应清空 key_origin 且 has_key=false: %+v", updated)
	}
	if got := svc.ProviderKey(created.ID); got != "" {
		t.Fatalf("跨源改址后不得泄露旧密钥: %q", got)
	}
	if got, _ := svc.deps.KVGetSecret(providerKeyName(created.ID)); got != "" {
		t.Fatalf("跨源改址后应立即删除存量密文密钥: %q", got)
	}
	reverted, err := svc.UpdateProvider(ctx, created.ID, map[string]any{
		"base_url": "https://api.openai.com/v1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if reverted.HasKey || svc.ProviderKey(created.ID) != "" {
		t.Fatalf("改回原地址不得复活旧密钥: %+v", reverted)
	}

	if err := svc.SaveConfig(ctx, map[string]any{
		"base_url": "https://api.openai.com/v1",
		"api_key":  "sk-save-config",
		"protocol": "openai",
	}); err != nil {
		t.Fatal(err)
	}
	activeID := svc.LoadConfig().Active
	if got := svc.ProviderKey(activeID); got != "sk-save-config" {
		t.Fatalf("SaveConfig 应保存密钥: %q", got)
	}
	if err := svc.SaveConfig(ctx, map[string]any{
		"base_url": "https://evil.example.com/v1",
	}); err != nil {
		t.Fatal(err)
	}
	if err := svc.SaveConfig(ctx, map[string]any{
		"base_url": "https://api.openai.com/v1",
	}); err != nil {
		t.Fatal(err)
	}
	if got := svc.ProviderKey(activeID); got != "" {
		t.Fatalf("SaveConfig 跨源再改回原地址不得复活旧密钥: %q", got)
	}

	legacy, err := svc.CreateProvider(ctx, map[string]any{
		"name":     "Legacy",
		"protocol": "openai",
		"base_url": "https://legacy.example.com/v1",
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := svc.deps.KVSet(ctx, nil, map[string]string{providerKeyName(legacy.ID): "sk-legacy"}); err != nil {
		t.Fatal(err)
	}
	if got := svc.ProviderKey(legacy.ID); got != "" {
		t.Fatalf("无 key_origin 存量密钥不得返回: %q", got)
	}

	if err := svc.DeleteProvider(ctx, created.ID); err != nil {
		t.Fatal(err)
	}
	if _, existsInKV := svc.deps.KVGet(providerKeyName(created.ID)); existsInKV {
		t.Fatalf("DeleteProvider 不应在 values 写入空串残留")
	}
	if got, _ := svc.deps.KVGetSecret(providerKeyName(created.ID)); got != "" {
		t.Fatalf("DeleteProvider 应清除密文密钥: %q", got)
	}
}

func TestHttpClientRejectsCrossOriginAndSchemeDowngradeRedirects(t *testing.T) {
	var evilAuth string
	evil := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		evilAuth = r.Header.Get("Authorization")
		w.WriteHeader(http.StatusOK)
	}))
	defer evil.Close()

	origin := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/same" {
			http.Redirect(w, r, "/ok", http.StatusFound)
			return
		}
		if r.URL.Path == "/ok" {
			w.WriteHeader(http.StatusOK)
			return
		}
		http.Redirect(w, r, evil.URL+"/steal", http.StatusFound)
	}))
	defer origin.Close()

	client := HttpClient(origin.URL)
	defer client.CloseIdleConnections()

	reqCross, _ := http.NewRequest(http.MethodGet, origin.URL+"/cross", nil)
	reqCross.Header.Set("Authorization", "Bearer sk-secret")
	if resp, err := client.Do(reqCross); err == nil {
		resp.Body.Close()
		t.Fatal("跨源重定向应被拦截")
	}
	if evilAuth != "" {
		t.Fatalf("跨源重定向泄露了 Authorization 头: %q", evilAuth)
	}

	reqSame, _ := http.NewRequest(http.MethodGet, origin.URL+"/same", nil)
	respSame, err := client.Do(reqSame)
	if err != nil {
		t.Fatalf("同源重定向应放行: %v", err)
	}
	respSame.Body.Close()

	origReq, _ := http.NewRequest(http.MethodGet, "https://api.example.com/v1/models", nil)
	downgradeReq, _ := http.NewRequest(http.MethodGet, "http://api.example.com/v1/models", nil)
	if err := sameOriginRedirect(downgradeReq, []*http.Request{origReq}); err == nil {
		t.Fatal("HTTPS->HTTP 降级重定向应被拦截")
	}
}

type roundTripRecorder struct {
	called bool
	inner  http.RoundTripper
}

func (r *roundTripRecorder) RoundTrip(req *http.Request) (*http.Response, error) {
	r.called = true
	return r.inner.RoundTrip(req)
}

func TestChatStreamReusesProvidedClient(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/event-stream")
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		time.Sleep(80 * time.Millisecond)
		_, _ = w.Write([]byte("data: {\"choices\":[{\"delta\":{\"content\":\"ok\"}}]}\n\ndata: [DONE]\n\n"))
	}))
	defer srv.Close()

	rec := &roundTripRecorder{inner: srv.Client().Transport}
	customClient := &http.Client{Transport: rec, Timeout: 50 * time.Millisecond}

	svc := New(Deps{DataDir: t.TempDir()}, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	ch, err := svc.ChatStream(ctx, "openai", srv.URL, "sk-test", "m",
		[]map[string]any{{"role": "user", "content": "hi"}}, nil, 16, customClient)
	if err != nil {
		t.Fatal(err)
	}
	var got string
	for ev := range ch {
		if ev.Kind == "answer" {
			got += ev.Text
		}
	}
	if !rec.called || got != "ok" {
		t.Fatalf("ChatStream 未使用传入的 client: called=%t got=%q", rec.called, got)
	}
}
