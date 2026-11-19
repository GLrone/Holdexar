package pilot

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func sseChunk(chunks ...string) string {
	var b strings.Builder
	for _, c := range chunks {
		b.WriteString("data: ")
		b.WriteString(c)
		b.WriteString("\n\n")
	}
	b.WriteString("data: [DONE]\n\n")
	return b.String()
}

func TestTextGuardsAndVisualDetection(t *testing.T) {
	if !IsVisualRequest("帮我画一个购买决策流程图") {
		t.Fatal("视觉关键词未识别")
	}
	if IsVisualRequest("星露谷多少钱") {
		t.Fatal("普通问价不应误判视觉请求")
	}
	if !isSelfNarration("让我来帮你查一下星露谷的价格") {
		t.Fatal("自述垫话应识别")
	}
	if isSelfNarration("## 结论\n国区现价 ¥48.00，当前 0% 折扣。") {
		t.Fatal("含真实结论的正文不应误判为自述")
	}
	if !looksTruncated("价格为 48", 1, nil) {
		t.Fatal("以数字断尾应触发截断判定")
	}
	if looksTruncated("结论：值得等史低。", 1, nil) {
		t.Fatal("完整短句不应误判截断")
	}
	raw := "我需要检查一下参数\n<｜DSML｜tool_calls><｜DSML｜invoke name=\"x\"/></｜DSML｜tool_calls>\n\n最终结论：值得现在入手。"
	cleaned := stripLeakedThinking(raw)
	if strings.Contains(cleaned, "DSML") || strings.Contains(cleaned, "我需要检查") {
		t.Fatalf("外泄思考未剥离：%q", cleaned)
	}
	if !strings.Contains(cleaned, "最终结论：值得现在入手。") {
		t.Fatalf("有效结论被误删：%q", cleaned)
	}
}

func TestContextReplaysJSONLMessages(t *testing.T) {
	dir := t.TempDir()
	svc := newTestPilotServiceWithKV(t, filepath.Join(dir, "pilot.db"))
	st := svc.GetStore()
	sid := "replay01"
	turnMsgs := []map[string]any{
		{"role": "user", "content": "查一下星露谷"},
		{"role": "assistant", "content": nil, "tool_calls": []any{
			map[string]any{"id": "c1", "type": "function", "function": map[string]any{"name": "search_games", "arguments": `{"q":"星露谷"}`}},
		}},
		{"role": "tool", "tool_call_id": "c1", "content": `{"kind":"games","items":[{"appid":413150,"name":"Stardew Valley"}]}`},
		{"role": "assistant", "content": "星露谷目前 48 元。"},
	}
	if !st.AppendTurn(sid, map[string]any{
		"tid":      "t1",
		"q":        "查一下星露谷",
		"resp":     map[string]any{"answer": "星露谷目前 48 元。", "tool_catalog": []string{"read_logs"}},
		"messages": turnMsgs,
	}) {
		t.Fatal("写入轮次失败")
	}
	loaded := st.Load(sid)
	if loaded == nil {
		t.Fatal("读取会话失败")
	}
	expanded := sessionExpandedTools(loaded)
	if !expanded["read_logs"] {
		t.Fatalf("tool_catalog 未从 JSONL 恢复：%v", expanded)
	}
	assembled := assemble(loaded, "再看看日志", nil, 10000, 3000, nil, nil, nil)
	foundTool := false
	for _, m := range assembled {
		if m["role"] == "tool" && strings.Contains(stringOf(m["content"]), "413150") {
			foundTool = true
		}
	}
	if !foundTool {
		t.Fatalf("JSONL 重放未保留 tool 消息链：%v", assembled)
	}
}

func TestAskLoopDynamicToolsParallelNudgeAndTelemetry(t *testing.T) {
	var round atomic.Int32
	var seenTools [][]string
	var seenMsgs []map[string]any
	var parallelCount atomic.Int32
	var maxConcurrent atomic.Int32

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		names := []string{}
		for _, raw := range anySlice(body["tools"]) {
			if m, ok := raw.(map[string]any); ok {
				if fn, ok := m["function"].(map[string]any); ok {
					names = append(names, stringOf(fn["name"]))
				}
			}
		}
		seenTools = append(seenTools, names)
		if msgs, ok := body["messages"].([]any); ok && round.Load() == 2 {
			for _, raw := range msgs {
				if m, ok := raw.(map[string]any); ok {
					seenMsgs = append(seenMsgs, m)
				}
			}
		}

		w.Header().Set("Content-Type", "text/event-stream")
		cur := round.Add(1)
		switch cur {
		case 1:
			_, _ = w.Write([]byte(sseChunk(
				`{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"epic_free","arguments":"{}"}},{"index":1,"id":"c2","function":{"name":"steam_free","arguments":"{}"}}]}}]}`,
				`{"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":600,"completion_tokens":20}}`,
			)))
		case 2:
			_, _ = w.Write([]byte(sseChunk(
				`{"choices":[{"delta":{"content":""},"finish_reason":"stop"}],"usage":{"prompt_tokens":650,"completion_tokens":5}}`,
			)))
		case 3:
			_, _ = w.Write([]byte(sseChunk(
				`{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c3","function":{"name":"list_tools","arguments":"{}"}}]}}]}`,
				`{"choices":[{"delta":{},"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":700,"completion_tokens":15}}`,
			)))
		default:
			_, _ = w.Write([]byte(sseChunk(
				`{"choices":[{"delta":{"content":"我需要先确认参数\n\n本周喜加一已查到 1 款免费游戏。"},"finish_reason":"stop"}],"usage":{"prompt_tokens":720,"completion_tokens":30}}`,
			)))
		}
	}))
	defer srv.Close()

	svc := newTestPilotServiceWithKV(t, filepath.Join(t.TempDir(), "pilot.db"))
	svc.SetToolSpecs(func() []map[string]any {
		return []map[string]any{
			{"type": "function", "function": map[string]any{"name": "epic_free", "description": "e", "parameters": map[string]any{"type": "object"}}},
			{"type": "function", "function": map[string]any{"name": "steam_free", "description": "s", "parameters": map[string]any{"type": "object"}}},
			{"type": "function", "function": map[string]any{"name": "list_tools", "description": "l", "parameters": map[string]any{"type": "object"}}},
			{"type": "function", "function": map[string]any{"name": "read_logs", "description": "logs", "parameters": map[string]any{"type": "object"}}},
		}
	})
	svc.bridge = func(ctx *CallCtx, name string, args map[string]any, guarded bool) map[string]any {
		if name == "epic_free" || name == "steam_free" {
			cur := parallelCount.Add(1)
			for {
				prev := maxConcurrent.Load()
				if cur <= prev || maxConcurrent.CompareAndSwap(prev, cur) {
					break
				}
			}
			time.Sleep(40 * time.Millisecond)
			parallelCount.Add(-1)
			if name == "epic_free" {
				return map[string]any{"kind": "epic", "offers": []any{map[string]any{"title": "FreeGame"}}}
			}
			return map[string]any{"kind": "rows", "titleKey": "steamFree", "rows": []any{map[string]any{"k": "SF", "v": "0"}}}
		}
		if name == "list_tools" {
			return map[string]any{"kind": "tool_catalog", "tools": []any{map[string]any{"name": "read_logs"}}, "count": 1}
		}
		return map[string]any{"kind": "empty"}
	}

	ctx := context.Background()
	if err := svc.SaveConfig(ctx, map[string]any{
		"enabled": true, "protocol": "openai", "base_url": srv.URL + "/v1", "model": "gpt-4o", "api_key": "sk-test",
	}); err != nil {
		t.Fatal(err)
	}

	ch, err := svc.AskStream(ctx, AskRequest{
		Question:  "画个流程图看看本周免费",
		SessionID: "sess_parity_01",
	})
	if err != nil {
		t.Fatal(err)
	}
	var done map[string]any
	for ev := range ch {
		if ev.Type == "done" {
			done = ev.Payload
		}
	}
	if done == nil {
		t.Fatal("未收到 done 事件")
	}
	if maxConcurrent.Load() < 2 {
		t.Fatalf("两只读工具未真正并行执行：maxConcurrent=%d", maxConcurrent.Load())
	}
	if len(seenTools) < 1 || containsString(seenTools[0], "read_logs") {
		t.Fatalf("首轮工具表泄露非核心工具：%v", seenTools)
	}
	sawNudge, sawSVG := false, false
	for _, m := range seenMsgs {
		c := stringOf(m["content"])
		if strings.Contains(c, "你刚才没有输出任何内容") {
			sawNudge = true
		}
		if strings.Contains(c, "SVG 矢量图形设计规范") {
			sawSVG = true
		}
	}
	if !sawNudge || !sawSVG {
		t.Fatalf("nudge=%t svg=%t 未进入模型消息", sawNudge, sawSVG)
	}
	if !strings.Contains(stringOf(done["answer"]), "本周喜加一已查到") || strings.Contains(stringOf(done["answer"]), "我需要先确认") {
		t.Fatalf("终答未剥离外泄思考：%q", done["answer"])
	}
	unlocked := stringOrAnyList(done["tool_catalog"])
	if !containsString(unlocked, "read_logs") {
		t.Fatalf("done.tool_catalog 应包含解锁工具 read_logs：%v", done["tool_catalog"])
	}
	if _, ok := done["phases"].([]any); !ok {
		t.Fatalf("done 缺少 phases[]: %v", done["phases"])
	}
	if done["first_in"] != 600 || jsonInt(done["ctx_budget"]) <= 0 || jsonInt(done["ctx_tokens"]) <= 0 {
		t.Fatalf("telemetry 缺失: first_in=%v ctx_budget=%v ctx_tokens=%v", done["first_in"], done["ctx_budget"], done["ctx_tokens"])
	}

	round.Store(10)
	ch2, err := svc.AskStream(ctx, AskRequest{Question: "接着查日志", SessionID: "sess_parity_01"})
	if err != nil {
		t.Fatal(err)
	}
	for range ch2 {
	}
	if !containsString(seenTools[len(seenTools)-1], "read_logs") {
		t.Fatalf("次轮会话应自动恢复解锁工具 read_logs：%v", seenTools[len(seenTools)-1])
	}
}

func TestConfirmProposalExecutesBulkActionsAndDelete(t *testing.T) {
	svc := newTestPilotServiceWithKV(t, filepath.Join(t.TempDir(), "pilot.db"))
	mux := http.NewServeMux()
	var deletedAlert bool
	var deleteRemoteAddr string
	mux.HandleFunc("DELETE /api/v1/alerts/9", func(w http.ResponseWriter, r *http.Request) {
		deletedAlert = true
		deleteRemoteAddr = r.RemoteAddr
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"removed":true}`))
	})
	svc.deps.Mux = mux

	var followed []int
	svc.bridge = func(ctx *CallCtx, name string, args map[string]any, guarded bool) map[string]any {
		if name == "add_follow" {
			id := jsonInt(args["appid"])
			followed = append(followed, id)
			return map[string]any{"action": "monitor_add", "appid": id, "name": fmt.Sprintf("Game %d", id)}
		}
		return map[string]any{"kind": "failed"}
	}

	sid := "prop01"
	st := svc.GetStore()
	st.AppendProposal(sid, map[string]any{
		"pid": "p1", "action": "add_follow",
		"items": []any{map[string]any{"appid": 413150, "name": "Stardew"}, map[string]any{"appid": 620, "name": "Portal 2"}},
		"args":  map[string]any{}, "state": "pending",
	})
	res := svc.ConfirmProposal(context.Background(), sid, "p1", true)
	if res["ok"] != true || res["state"] != "confirmed" || res["done"] != 2 || len(followed) != 2 {
		t.Fatalf("批量确认执行异常：%v followed=%v", res, followed)
	}

	st.AppendProposal(sid, map[string]any{
		"pid": "d1", "action": "delete",
		"items": []any{map[string]any{"key": "alert:9", "name": "旧提醒"}},
		"args":  map[string]any{}, "state": "pending",
	})
	delRes := svc.ConfirmProposal(context.Background(), sid, "d1", true)
	if delRes["ok"] != true || delRes["done"] != 1 || !deletedAlert || deleteRemoteAddr != "127.0.0.1:0" {
		t.Fatalf("删除提议执行异常：%v deletedAlert=%t remoteAddr=%q", delRes, deletedAlert, deleteRemoteAddr)
	}
}

func TestTransientNudgesNotPersistedAndNarrationExcludedAndTruncatedContinued(t *testing.T) {
	ctx := context.Background()

	t.Run("narration discarded and transient nudges excluded from JSONL", func(t *testing.T) {
		var round atomic.Int32
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "text/event-stream")
			switch round.Add(1) {
			case 1:
				_, _ = w.Write([]byte(sseChunk(
					`{"choices":[{"delta":{"content":"让我来帮你查一下这款游戏的价格。"},"finish_reason":"stop"}]}`,
				)))
			case 2:
				_, _ = w.Write([]byte(sseChunk(
					`{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"search_games","arguments":"{\"q\":\"missing\"}"}}]}}]}`,
					`{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}`,
				)))
			case 3:
				_, _ = w.Write([]byte(sseChunk(
					`{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c2","function":{"name":"search_games","arguments":"{\"q\":\"missing\"}"}}]}}]}`,
					`{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}`,
				)))
			case 4:
				_, _ = w.Write([]byte(sseChunk(
					`{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c3","function":{"name":"search_games","arguments":"{\"q\":\"missing\"}"}}]}}]}`,
					`{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}`,
				)))
			default:
				_, _ = w.Write([]byte(sseChunk(
					`{"choices":[{"delta":{"content":"没有查到相关数据。"},"finish_reason":"stop"}]}`,
				)))
			}
		}))
		defer srv.Close()

		svc := newTestPilotServiceWithKV(t, filepath.Join(t.TempDir(), "pilot.db"))
		svc.SetToolSpecs(func() []map[string]any {
			return []map[string]any{
				{"type": "function", "function": map[string]any{"name": "search_games", "description": "s", "parameters": map[string]any{"type": "object"}}},
			}
		})
		svc.bridge = func(ctx *CallCtx, name string, args map[string]any, guarded bool) map[string]any {
			return map[string]any{"kind": "games", "items": []any{}}
		}
		if err := svc.SaveConfig(ctx, map[string]any{
			"enabled": true, "protocol": "openai", "base_url": srv.URL + "/v1", "model": "gpt-4o", "api_key": "sk-test",
		}); err != nil {
			t.Fatal(err)
		}

		ch, err := svc.AskStream(ctx, AskRequest{Question: "未知游戏多少钱", SessionID: "nudge_clean_01"})
		if err != nil {
			t.Fatal(err)
		}
		var done map[string]any
		for ev := range ch {
			if ev.Type == "done" {
				done = ev.Payload
			}
		}
		if got := stringOf(done["answer"]); got != "没有查到相关数据。" {
			t.Fatalf("废弃垫话未从最终答案剔除: %q", got)
		}
		loaded := svc.GetStore().Load("nudge_clean_01")
		for _, m := range turnMessages(loaded.Turns[0]) {
			c := stringOf(m["content"])
			if strings.Contains(c, "（系统提示）") {
				t.Fatalf("瞬态重试提示词不应落盘进会话 JSONL: %q", c)
			}
		}
	})

	t.Run("truncated step sends assistant prefix and user continuation prompt", func(t *testing.T) {
		var round atomic.Int32
		var round3Msgs []map[string]any
		srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			var body map[string]any
			_ = json.NewDecoder(r.Body).Decode(&body)
			cur := round.Add(1)
			if cur == 3 {
				for _, raw := range anySlice(body["messages"]) {
					if m, ok := raw.(map[string]any); ok {
						round3Msgs = append(round3Msgs, m)
					}
				}
			}
			w.Header().Set("Content-Type", "text/event-stream")
			switch cur {
			case 1:
				_, _ = w.Write([]byte(sseChunk(
					`{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"get_price_briefing","arguments":"{\"appid\":413150}"}}]}}]}`,
					`{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}`,
				)))
			case 2:
				_, _ = w.Write([]byte(sseChunk(
					`{"choices":[{"delta":{"content":"国区现价为 48"},"finish_reason":"stop"}]}`,
				)))
			default:
				_, _ = w.Write([]byte(sseChunk(
					`{"choices":[{"delta":{"content":" 元，处于平史低。"},"finish_reason":"stop"}]}`,
				)))
			}
		}))
		defer srv.Close()

		svc := newTestPilotServiceWithKV(t, filepath.Join(t.TempDir(), "pilot.db"))
		svc.SetToolSpecs(func() []map[string]any {
			return []map[string]any{
				{"type": "function", "function": map[string]any{"name": "get_price_briefing", "description": "p", "parameters": map[string]any{"type": "object"}}},
			}
		})
		svc.bridge = func(ctx *CallCtx, name string, args map[string]any, guarded bool) map[string]any {
			return map[string]any{"kind": "price", "appid": 413150, "name": "Stardew Valley"}
		}
		if err := svc.SaveConfig(ctx, map[string]any{
			"enabled": true, "protocol": "openai", "base_url": srv.URL + "/v1", "model": "gpt-4o", "api_key": "sk-test",
		}); err != nil {
			t.Fatal(err)
		}

		ch, err := svc.AskStream(ctx, AskRequest{Question: "今天天气", SessionID: "nudge_trunc_01"})
		if err != nil {
			t.Fatal(err)
		}
		var done map[string]any
		for ev := range ch {
			if ev.Type == "done" {
				done = ev.Payload
			}
		}
		if got := stringOf(done["answer"]); got != "国区现价为 48 元，处于平史低。" {
			t.Fatalf("续写拼接结果不符: %q", got)
		}
		if len(round3Msgs) < 2 {
			t.Fatalf("第 3 轮消息过短: %v", round3Msgs)
		}
		prev := round3Msgs[len(round3Msgs)-2]
		last := round3Msgs[len(round3Msgs)-1]
		if prev["role"] != "assistant" || prev["content"] != "国区现价为 48" {
			t.Fatalf("续写前应携带 assistant 半截输出: %v", prev)
		}
		if last["role"] != "user" || !strings.Contains(stringOf(last["content"]), "没写完") {
			t.Fatalf("续写提示应作为 user 消息跟在末尾: %v", last)
		}
		loaded := svc.GetStore().Load("nudge_trunc_01")
		for _, m := range turnMessages(loaded.Turns[0]) {
			if strings.Contains(stringOf(m["content"]), "（系统提示）") {
				t.Fatalf("续写提示不应落盘: %v", m)
			}
		}
	})
}

func containsString(list []string, target string) bool {
	for _, s := range list {
		if s == target {
			return true
		}
	}
	return false
}
