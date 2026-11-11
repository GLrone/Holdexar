//go:build windows

package pilot

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestIntentRouting(t *testing.T) {
	cases := []struct {
		q    string
		want string
	}{
		{"这个游戏多少钱", IntentPriceAnalysis},
		{"帮我关注 Portal 2", IntentAddMonitor},
		{"低于 50 元提醒我", IntentCreateAlert},
		{"有什么便宜的策略游戏推荐", IntentFindGames},
		{"怎么设置提醒", IntentHowTo},
		{"打开设置", IntentNavigate},
		{"今天天气怎么样", IntentChat},
		{"值得关注的新游戏", IntentChat}, // 块词：评价语不是关注动作
		{"批量删除我的关注", IntentChat}, // 守卫词不进写意图
	}
	for _, tc := range cases {
		if got := Route(tc.q); got != tc.want {
			t.Fatalf("Route(%q)=%s 期望 %s", tc.q, got, tc.want)
		}
	}
	if IsGuarded("批量删除") != true || IsGuarded("加一个关注") != false {
		t.Fatal("守卫词判定错误")
	}
	if NavTarget("打开设置") != "settings" || NavTarget("设置在哪里") != "" {
		t.Fatal("导航直通判定错误")
	}
}

func TestModelWindows(t *testing.T) {
	if got := GuessContextWindow("deepseek-chat"); got != 131072 {
		t.Fatalf("deepseek 窗口识别错误：%d", got)
	}
	if got := GuessContextWindow("gpt-4o"); got != 128000 {
		t.Fatalf("gpt-4o 窗口识别错误：%d", got)
	}
	if got := GuessContextWindow("完全不认识的模型名"); got != 0 {
		t.Fatalf("表外模型应返回 0：%d", got)
	}
}

func TestSessionStoreLifecycle(t *testing.T) {
	base := filepath.Join(t.TempDir(), "sessions")
	store := NewSessionStore(base)
	sid := "abc123"
	if !ValidSID(sid) || ValidSID("../evil") {
		t.Fatal("sid 白名单错误")
	}
	// 落两轮 + 改名 + 置顶
	if !store.AppendTurn(sid, map[string]any{"t": "turn", "tid": "t1", "q": "Portal 2 现在多少钱？", "resp": map[string]any{"answer": "42 元", "source": "llm"}}) {
		t.Fatal("首轮落账失败")
	}
	if !store.AppendTurn(sid, map[string]any{"t": "turn", "tid": "t2", "q": "历史史低呢？", "resp": map[string]any{"answer": "19 元", "source": "llm"}}) {
		t.Fatal("次轮落账失败")
	}
	state := store.Load(sid)
	if state == nil || len(state.Turns) != 2 {
		t.Fatalf("折叠轮数错误：%v", state)
	}
	if state.Title == "" || !strings.Contains(state.Title, "Portal 2") {
		t.Fatalf("首问自动起名失败：%q", state.Title)
	}
	if !store.SetTitle(sid, "自定义标题") {
		t.Fatal("改名失败")
	}
	if !store.SetPinned(sid, true) {
		t.Fatal("置顶失败")
	}
	state = store.Load(sid)
	if state.Title != "自定义标题" || !state.Pinned {
		t.Fatalf("标题/置顶折叠错误：%v", state)
	}
	// feedback
	if reason := store.SetTurnFeedback(sid, "t1", "accepted"); reason != "" {
		t.Fatalf("评价写入失败：%s", reason)
	}
	if reason := store.SetTurnFeedback(sid, "gone", "accepted"); reason != "turn_gone" {
		t.Fatalf("不存在轮评价应 turn_gone：%s", reason)
	}
	// 列表投影（置顶在前）
	items := store.ListRecent(50, "")
	if len(items) != 1 || items[0]["pinned"] != true {
		t.Fatalf("列表投影错误：%v", items)
	}
	if items[0]["title"] != "自定义标题" {
		t.Fatalf("列表标题错误：%v", items[0]["title"])
	}
	// 检索命中回答正文
	items = store.ListRecent(50, "史低")
	if len(items) != 1 || items[0]["snippet"] == "" {
		t.Fatalf("检索投影错误：%v", items)
	}
	// 分叉
	newSid := "def456"
	if !store.Fork(sid, newSid, 1) {
		t.Fatal("分叉失败")
	}
	forked := store.Load(newSid)
	if forked == nil || len(forked.Turns) != 1 || forked.ForkedFrom != sid {
		t.Fatalf("分叉折叠错误：%v", forked)
	}
	if store.Fork(sid, newSid, 1) {
		t.Fatal("目标已存在应拒绝")
	}
	// 软截断
	if !store.Truncate(newSid, 0) {
		t.Fatal("截断失败")
	}
	forked = store.Load(newSid)
	if len(forked.Turns) != 0 {
		t.Fatalf("截断后轮数错误：%d", len(forked.Turns))
	}
	// 删除
	if reason := store.DeleteWithReason(sid); reason != "deleted" {
		t.Fatalf("删除失败：%s", reason)
	}
	if store.Load(sid) != nil {
		t.Fatal("删除后仍可载入")
	}
}

func TestSessionFoldTruncateCleansDerivatives(t *testing.T) {
	base := filepath.Join(t.TempDir(), "sessions")
	store := NewSessionStore(base)
	sid := "trunc01"
	store.AppendTurn(sid, map[string]any{"t": "turn", "tid": "t1", "q": "问题一", "resp": map[string]any{"answer": "答"}})
	store.AppendTurn(sid, map[string]any{"t": "turn", "tid": "t2", "q": "问题二", "resp": map[string]any{"answer": "答"}})
	store.AppendSummary(sid, "要点存档", 2, "auto", 1000, 200)
	store.AppendProposal(sid, map[string]any{"pid": "p1", "action": "add_follow", "items": []any{}, "state": "pending"})
	state := store.Load(sid)
	if state.SummaryThrough != 2 || state.PendingProposal == nil {
		t.Fatalf("前置折叠错误：%+v", state)
	}
	store.Truncate(sid, 1)
	state = store.Load(sid)
	if len(state.Turns) != 1 {
		t.Fatalf("截断轮数错误：%d", len(state.Turns))
	}
	if state.SummaryThrough != 1 {
		t.Fatalf("压缩边界未随轮数收紧：%d", state.SummaryThrough)
	}
	if state.PendingProposal != nil {
		t.Fatal("截断后待确认提议应清空")
	}
	for _, marker := range state.Markers {
		if after, _ := marker["after_turn"].(int); after > len(state.Turns) {
			t.Fatalf("压缩边界越界：%v", marker)
		}
	}
}

func TestLLMOllamaParse(t *testing.T) {
	var events []LlmEvent
	handle := func(ev LlmEvent) { events = append(events, ev) }
	OllamaParse(handle, `{"message":{"role":"assistant","content":"答案"}}`)
	OllamaParse(handle, `{"message":{"role":"assistant","thinking":"思考中"}}`)
	OllamaParse(handle, `{"message":{"tool_calls":[{"function":{"name":"search_games","arguments":{"q":"portal"}}}],"content":""}}`)
	OllamaParse(handle, `{"done":true,"prompt_eval_count":100,"eval_count":50}`)
	if len(events) != 5 {
		t.Fatalf("事件数错误：%d", len(events))
	}
	if events[0].Kind != "answer" || events[0].Text != "答案" {
		t.Fatalf("answer 事件错误：%+v", events[0])
	}
	_ = events
	if events[2].Kind != "tool_calls" || len(events[2].Calls) != 1 || events[2].Calls[0]["name"] != "search_games" {
		t.Fatalf("tool_calls 事件错误：%+v", events)
	}
	if events[3].Kind != "usage" || events[3].UsageIn != 100 || events[3].UsageOut != 50 {
		t.Fatalf("usage 事件错误：%+v", events[4])
	}
}

func TestLLMOpenAIParseToolAccumulation(t *testing.T) {
	var events []LlmEvent
	handle := func(ev LlmEvent) { events = append(events, ev) }
	acc := newOpenaiAcc()
	acc.feed(`{"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"search_games","arguments":"{\"q\":"}}]}}]}`, handle)
	acc.feed(`{"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\"portal\"}"}}]}}],"finish_reason":null}`, handle)
	acc.feed(`{"choices":[{"delta":{},"finish_reason":"tool_calls"}]}`, handle)
	calls := 0
	var assembled []map[string]any
	for _, ev := range events {
		if ev.Kind == "tool_calls" {
			calls++
			assembled = ev.Calls
		}
	}
	if calls != 1 || len(assembled) != 1 {
		t.Fatalf("工具调用组装错误：%v", events)
	}
	args, _ := assembled[0]["arguments"].(map[string]any)
	if args["q"] != "portal" {
		t.Fatalf("参数组装错误：%v", args)
	}
}

func TestAttachmentsLifecycle(t *testing.T) {
	dataDir := t.TempDir()
	sid := "att123"
	ref, ok := SaveAttachment(dataDir, sid, strings.Repeat("行内容\n", 100), "测试附件")
	if !ok || ref.ID == "" || ref.Lines != 101 {
		t.Fatalf("附件落盘失败：%+v %v", ref, ok)
	}
	if len(ref.ID) != 12 || !ValidAID(ref.ID) {
		t.Fatalf("附件 id 形态错误：%s", ref.ID)
	}
	items := ListAttachments(dataDir, sid)
	if len(items) != 1 || items[0].Name != "测试附件" {
		t.Fatalf("附件清单错误：%v", items)
	}
	got := ReadRange(dataDir, sid, ref.ID, 1, 10)
	if got == nil || got["end_line"] != 10 {
		t.Fatalf("区间读取错误：%v", got)
	}
	if got["truncated"] != false {
		t.Fatal("短附件不应截断")
	}
	if _, ok := SaveAttachment(dataDir, "../evil", "x", ""); ok {
		t.Fatal("非法 sid 应拒绝")
	}
	if _, ok := SaveAttachment(dataDir, sid, strings.Repeat("x", MaxAttachChars+1), ""); ok {
		t.Fatal("超上限应拒绝")
	}
}

func TestFlywheelStatsAndPurge(t *testing.T) {
	base := filepath.Join(t.TempDir(), "sessions")
	store := NewSessionStore(base)
	store.AppendTurn("fw01", map[string]any{"t": "turn", "tid": "f1", "q": "问题", "resp": map[string]any{"answer": "答", "source": "llm"}})
	_ = store.SetTurnFeedback("fw01", "f1", "accepted") // 明确评价 → labeled
	entry := map[string]any{"ts": nowISO(), "question": "问题", "intent": "price_analysis",
		"mode": "agent", "source": "llm", "tid": "f1", "sid": "fw01"}
	if !AppendDecision(base, entry) {
		t.Fatal("决策日志写入失败")
	}
	if _, err := os.Stat(decisionsPath(base)); err != nil {
		t.Fatalf("decisions.jsonl 未落盘：%v", err)
	}
	stats := FlywheelStats(base, store.RawRecords)
	if stats["total"] != 1 {
		t.Fatalf("飞轮总轮数错误：%v", stats["total"])
	}
	labeled, _ := stats["labeled"].(int)
	if labeled != 1 {
		t.Fatalf("反馈关联计数错误：%v", stats["labeled"])
	}
	// purge：删除会话时决策日志同步清理
	if !purgeFlywheelSession(base, "fw01") {
		t.Fatal("飞轮清理失败")
	}
	if stats := FlywheelStats(base, nil); stats["total"] != 0 {
		t.Fatalf("清理后应空：%v", stats["total"])
	}
}

func TestDeriveTitleStripsQuoteMarkers(t *testing.T) {
	title := DeriveTitle("【引用1】这个游戏的价格走势如何")
	if strings.Contains(title, "【") {
		t.Fatalf("引用标号未剥离：%q", title)
	}
	if !strings.Contains(title, "价格走势") {
		t.Fatalf("标题内容缺失：%q", title)
	}
	long := DeriveTitle(strings.Repeat("长", 100))
	if len([]rune(long)) > titleAutoChars+3 {
		t.Fatalf("标题未截断：%d", len([]rune(long)))
	}
}

func TestConfigPayloadJSONShape(t *testing.T) {
	// PilotConfigPayload 的 JSON 形状自检（前端契约：字段名与类型）
	payload := map[string]any{
		"protocol": "openai", "enabled": false, "base_url": "", "model": "",
		"has_api_key": false, "monthly_cap": 0, "context_window": nil,
		"tool_content_limit": 3000, "usage_inp": 0, "usage_out": 0,
		"usage_calls": 0, "usage_total": 0, "active": "", "providers": []any{},
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	var back map[string]any
	if json.Unmarshal(raw, &back) != nil {
		t.Fatal("payload 序列化往返失败")
	}
	if back["context_window"] != nil {
		t.Fatal("context_window 应可空")
	}
}

// anthropicConvert：消息转换与连续角色合并（append 切片头曾丢失致 messages 恒空）。
func TestAnthropicConvertShapes(t *testing.T) {
	msgs := []map[string]any{
		{"role": "system", "content": "S1"},
		{"role": "system", "content": "S2"},
		{"role": "user", "content": "推荐几个打折的游戏"},
		{"role": "assistant", "content": "让我查。",
			"tool_calls": []any{map[string]any{"id": "c1", "type": "function",
				"function": map[string]any{"name": "recommend_games", "arguments": `{"only_discount":true}`}}}},
		{"role": "tool", "tool_call_id": "c1", "content": `{"kind":"games","items":[]}`},
		{"role": "tool", "tool_call_id": "c2", "content": `{"kind":"games","items":[]}`},
	}
	system, out := anthropicConvert(msgs)
	if system != "S1\nS2" {
		t.Fatalf("system 合并不符: %q", system)
	}
	if len(out) != 3 {
		t.Fatalf("应合并 tool 结果进同一 user 消息: %d", len(out))
	}
	if out[0]["role"] != "user" {
		t.Fatalf("首消息应为 user: %v", out[0])
	}
	first := out[0]["content"].([]map[string]any)
	if len(first) != 1 || first[0]["type"] != "text" || first[0]["text"] != "推荐几个打折的游戏" {
		t.Fatalf("user 文本块不符: %v", first)
	}
	second := out[1]["content"].([]map[string]any)
	if len(second) != 2 || second[0]["type"] != "text" || second[1]["type"] != "tool_use" {
		t.Fatalf("assistant 块不符: %v", second)
	}
	toolUse := second[1]
	if toolUse["id"] != "c1" || toolUse["name"] != "recommend_games" {
		t.Fatalf("tool_use 块不符: %v", toolUse)
	}
	inputMap, _ := toolUse["input"].(map[string]any)
	if inputMap["only_discount"] != true {
		t.Fatalf("tool_use input 解析不符: %v", inputMap)
	}
	third := out[2]["content"].([]map[string]any)
	if len(third) != 2 || third[0]["type"] != "tool_result" || third[0]["tool_use_id"] != "c1" {
		t.Fatalf("tool 结果块不符: %v", third)
	}
	if third[1]["tool_use_id"] != "c2" {
		t.Fatalf("第二条 tool 结果应并入同 user 消息: %v", third)
	}
}
