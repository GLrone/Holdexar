package agent

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"
)

func TestPostJSONDecodesAndReportsStatus(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /echo", func(w http.ResponseWriter, r *http.Request) {
		if ct := r.Header.Get("Content-Type"); ct != "application/json" {
			t.Errorf("Content-Type=%q", ct)
		}
		var in map[string]any
		if err := json.NewDecoder(r.Body).Decode(&in); err != nil {
			t.Errorf("请求体解码失败: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"ok":true,"n":` + func() string {
			b, _ := json.Marshal(in["n"])
			return string(b)
		}() + `}`))
	})
	mux.HandleFunc("POST /busy", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"已有任务在跑"}`))
	})
	c := &CallContext{Ctx: context.Background(), post: newMuxPoster(mux)}

	var out map[string]any
	if err := c.postJSON("/echo", map[string]any{"n": 3}, &out); err != nil {
		t.Fatal(err)
	}
	if out["ok"] != true || out["n"] != json.Number("3") {
		t.Fatalf("解码结果=%v", out)
	}

	err := c.postJSON("/busy", nil, &out)
	if err == nil || !strings.Contains(err.Error(), "409") || !strings.Contains(err.Error(), "已有任务在跑") {
		t.Fatalf("非 200 应带状态码与响应体：%v", err)
	}
}

func TestPostJSONWithoutChannelFails(t *testing.T) {
	c := &CallContext{Ctx: context.Background()}
	if err := c.postJSON("/x", nil, nil); err == nil || !strings.Contains(err.Error(), "POST") {
		t.Fatalf("未注入 POST 通道应报错：%v", err)
	}
}

func TestPostJSONSkipsDecodeWhenOutNil(t *testing.T) {
	mux := http.NewServeMux()
	mux.HandleFunc("POST /ok", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"done":true}`))
	})
	c := &CallContext{Ctx: context.Background(), post: newMuxPoster(mux)}
	if err := c.postJSON("/ok", map[string]any{}, nil); err != nil {
		t.Fatal(err)
	}
}
