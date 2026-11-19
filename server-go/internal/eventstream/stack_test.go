package eventstream

import (
	"bufio"
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/events"
)

func TestStackSessionGatesStream(t *testing.T) {
	gate := access.New()
	bus := events.NewBus()
	ring := NewRing(100)

	mux := http.NewServeMux()
	Register(mux, bus, ring)
	mux.HandleFunc("/api/", func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "Not Found", http.StatusNotFound)
	})
	srv := httptest.NewServer(gate.Middleware(mux))
	defer srv.Close()

	for _, path := range []string{"/api/v1/events/stream", "/api/v1/system/logs/stream"} {
		resp, err := http.Get(srv.URL + path)
		if err != nil {
			t.Fatalf("%s 请求失败: %v", path, err)
		}
		if resp.StatusCode != http.StatusUnauthorized {
			resp.Body.Close()
			t.Fatalf("%s 无会话状态码 = %d, want 401", path, resp.StatusCode)
		}
		resp.Body.Close()
	}

	rec := httptest.NewRecorder()
	gate.Issue(rec)
	cookie := rec.Result().Cookies()[0]

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	req2, _ := http.NewRequestWithContext(ctx, "GET", srv.URL+"/api/v1/events/stream", nil)
	req2.AddCookie(cookie)
	resp, err := http.DefaultClient.Do(req2)
	if err != nil {
		t.Fatalf("带会话连接失败: %v", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("带会话状态码 = %d, want 200", resp.StatusCode)
	}
	body := bufio.NewReader(resp.Body)
	line, err := body.ReadString('\n')
	if err != nil || line != ": connected\n" {
		t.Fatalf("带会话首行 = %q (err=%v), want connected 注释帧", line, err)
	}
	if bus.Len() != 1 {
		t.Fatalf("流式期间订阅数 = %d, want 1", bus.Len())
	}
}
