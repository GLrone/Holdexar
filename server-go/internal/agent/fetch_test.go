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

func TestMuxFetcherAndPosterLoopbackRemoteAddr(t *testing.T) {
	var getPeer, postPeer string
	mux := http.NewServeMux()
	mux.HandleFunc("GET /ping", func(w http.ResponseWriter, r *http.Request) {
		getPeer = r.RemoteAddr
	})
	mux.HandleFunc("POST /ping", func(w http.ResponseWriter, r *http.Request) {
		postPeer = r.RemoteAddr
	})

	status, _, err := newMuxFetcher(mux)(context.Background(), "/ping")
	if err != nil || status != http.StatusOK || getPeer != "127.0.0.1:0" {
		t.Fatalf("GET status=%d peer=%q err=%v", status, getPeer, err)
	}
	status, _, err = newMuxPoster(mux)(context.Background(), "/ping", []byte(`{}`))
	if err != nil || status != http.StatusOK || postPeer != "127.0.0.1:0" {
		t.Fatalf("POST status=%d peer=%q err=%v", status, postPeer, err)
	}
}

func TestQueryEncodingEscapesDelimiters(t *testing.T) {
	var gotSearch, gotTxType, gotSid string
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/bills/imports/1/game-txs", func(w http.ResponseWriter, r *http.Request) {
		gotSearch = r.URL.Query().Get("search")
		gotTxType = r.URL.Query().Get("txType")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"rows":[]}`))
	})
	mux.HandleFunc("GET /api/v1/achievements/challenges", func(w http.ResponseWriter, r *http.Request) {
		gotSid = r.URL.Query().Get("steamid")
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"summary":{},"challenges":[]}`))
	})

	want := "Command & Conquer #1 ?x=2"
	res := executeTool(t, mux, nil, "bills_spend_query", `{"import_id":1,"mode":"game_txs","search":"Command & Conquer #1 ?x=2","tx_type":"Purchase & Gift #2"}`)
	if gotSearch != want || gotTxType != "Purchase & Gift #2" {
		t.Fatalf("bills query search=%q txType=%q (res=%v)", gotSearch, gotTxType, res)
	}
	res = executeTool(t, mux, nil, "challenges_overview", `{"steam_id":"Command & Conquer #1 ?x=2"}`)
	if gotSid != want {
		t.Fatalf("sidQuery steam_id=%q want=%q (res=%v)", gotSid, want, res)
	}
}

func TestDisabledModulesReturnDisabledKind(t *testing.T) {
	_, db, _, _ := newTestSurface(t, false)
	mux := http.NewServeMux()
	mux.HandleFunc("GET /api/v1/metadata/epic/offers", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"Epic 免费游戏功能已停用"}`))
	})
	mux.HandleFunc("GET /api/v1/metadata/hb/offers", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"months":[]}`))
	})
	mux.HandleFunc("GET /api/v1/achievements/summary", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"成就功能已停用"}`))
	})
	mux.HandleFunc("POST /api/v1/achievements/sync", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail":"成就功能已停用，可在设置 → 插件中开启"}`))
	})

	if _, err := db.Exec(`INSERT INTO app_settings (key, value_json, updated_at) VALUES ('module.humble_choice.enabled', 'false', '2026-01-01 00:00:00')`); err != nil {
		t.Fatal(err)
	}

	for _, tool := range []string{"epic_free", "hb_monthly", "achievements_summary"} {
		out := executeTool(t, mux, db, tool, `{}`)
		if out["kind"] != "disabled" || out["status"] != "empty" {
			t.Fatalf("%s disabled shape=%v", tool, out)
		}
	}

	spec := buildRegistryOrDie().get("sync_achievements")
	res := Execute(&CallContext{
		Ctx:   context.Background(),
		Args:  map[string]any{},
		fetch: newMuxFetcher(mux),
		post:  newMuxPoster(mux),
		db:    db,
	}, spec, false)
	if res.Status != StatusEmpty || res.Data.get("kind") != "disabled" {
		t.Fatalf("sync_achievements disabled=%s %s", res.Status, encodeJSON(res.Data))
	}
}
