package pilot

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func routeFixture(t *testing.T) (*Service, func(method, path, body string) *httptest.ResponseRecorder) {
	t.Helper()
	service := New(Deps{DataDir: t.TempDir(), KVGet: func(string) (json.RawMessage, bool) { return nil, false }}, nil)
	mux := http.NewServeMux()
	Register(mux, service)
	do := func(method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}
	return service, do
}

func decodePilotBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON：%v（%s）", err, rec.Body.String())
	}
	return out
}

func seedTurns(t *testing.T, service *Service, sid string, count int) {
	t.Helper()
	for i := 0; i < count; i++ {
		if !service.GetStore().AppendTurn(sid, map[string]any{
			"tid": "t" + string(rune('a'+i)), "q": "问题", "resp": map[string]any{"answer": "答"},
			"ts": "2026-11-14T10:00:00",
		}) {
			t.Fatalf("轮次播种失败：%d", i)
		}
	}
}

func TestRewindValidation(t *testing.T) {
	_, do := routeFixture(t)
	cases := []struct {
		body string
		kind string
		ctx  map[string]any
	}{
		{`{}`, "missing", nil},
		{`{"keep_turns": -1}`, "greater_than_equal", map[string]any{"ge": float64(0)}},
		{`{"keep_turns": 1000}`, "less_than_equal", map[string]any{"le": float64(999)}},
		{`{"keep_turns": "abc"}`, "int_parsing", nil},
	}
	for _, tc := range cases {
		rec := do("POST", "/api/v1/pilot/sessions/probe0000000000000000sess01/rewind", tc.body)
		if rec.Code != 422 {
			t.Fatalf("%s 应 422：%d %s", tc.body, rec.Code, rec.Body.String())
		}
		var payload struct {
			Detail []struct {
				Type string         `json:"type"`
				Ctx  map[string]any `json:"ctx"`
			} `json:"detail"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
			t.Fatal(err)
		}
		if payload.Detail[0].Type != tc.kind {
			t.Fatalf("%s 错误类型应 %s：%s", tc.body, tc.kind, rec.Body.String())
		}
	}
}

func TestRewindNoSessionAndSuccess(t *testing.T) {
	service, do := routeFixture(t)
	sid := "probe0000000000000000sess01"
	rec := do("POST", "/api/v1/pilot/sessions/"+sid+"/rewind", `{"keep_turns": 3}`)
	if rec.Code != 200 {
		t.Fatalf("无会话应 200 机器码：%d %s", rec.Code, rec.Body.String())
	}
	body := decodePilotBody(t, rec)
	if body["ok"] != false || body["reason"] != "no_session" || body["turn_total"] != float64(0) {
		t.Fatal(body)
	}
	seedTurns(t, service, sid, 3)
	rec = do("POST", "/api/v1/pilot/sessions/"+sid+"/rewind", `{"keep_turns": 2}`)
	body = decodePilotBody(t, rec)
	if body["ok"] != true || body["turn_total"] != float64(2) || body["reason"] != nil {
		t.Fatalf("成功截断形状不符：%s", rec.Body.String())
	}
	if state := service.GetStore().Load(sid); len(state.Turns) != 2 {
		t.Fatalf("截断未生效：%d", len(state.Turns))
	}
	seedTurns(t, service, sid, 1)
	rec = do("POST", "/api/v1/pilot/sessions/"+sid+"/rewind", `{"keep_turns": 999}`)
	body = decodePilotBody(t, rec)
	if body["turn_total"] != float64(3) {
		t.Fatalf("越界夹取应等于现有轮数：%s", rec.Body.String())
	}
}

func TestRewindBusy(t *testing.T) {
	service, do := routeFixture(t)
	sid := "probe0000000000000000sess01"
	seedTurns(t, service, sid, 2)
	service.MarkSessionRunning(sid)
	rec := do("POST", "/api/v1/pilot/sessions/"+sid+"/rewind", `{"keep_turns": 1}`)
	body := decodePilotBody(t, rec)
	if body["ok"] != false || body["reason"] != "busy" || body["turn_total"] != float64(0) {
		t.Fatalf("busy 形状不符：%s", rec.Body.String())
	}
}

func TestCompactMachineCodes(t *testing.T) {
	_, do := routeFixture(t)
	rec := do("POST", "/api/v1/pilot/sessions/absent00000000000000000001/compact", "")
	if rec.Code != 404 || !strings.Contains(rec.Body.String(), "session not found") {
		t.Fatalf("无会话应 404：%d %s", rec.Code, rec.Body.String())
	}
	service, do := routeFixture(t)
	seedTurns(t, service, "probe0000000000000000sess01", 2)
	rec = do("POST", "/api/v1/pilot/sessions/probe0000000000000000sess01/compact", "")
	if rec.Code != 200 {
		t.Fatalf("llm_off 应 200 机器码：%d %s", rec.Code, rec.Body.String())
	}
	body := decodePilotBody(t, rec)
	if body["ok"] != false || body["reason"] != "llm_off" {
		t.Fatalf("llm_off 形状不符：%s", rec.Body.String())
	}

	service.MarkSessionRunning("probe0000000000000000sess01")
	busyRec := do("POST", "/api/v1/pilot/sessions/probe0000000000000000sess01/compact", "")
	if busyRec.Code != http.StatusConflict {
		t.Fatalf("运行中会话 compact 应返回 409：%d %s", busyRec.Code, busyRec.Body.String())
	}
}
