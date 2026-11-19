package agent

import (
	"context"
	"strings"
	"testing"
)

func TestPilotAdapterSharedSpecsAndExecute(t *testing.T) {
	specs, err := SharedSpecs()
	if err != nil {
		t.Fatal(err)
	}
	if len(specs) != 80 {
		t.Fatalf("SharedSpecs 数量=%d want 80", len(specs))
	}
	if len(CoreToolNames()) != 31 {
		t.Fatalf("CoreToolNames 数量=%d want 31", len(CoreToolNames()))
	}
	if StepLabel("get_price_briefing") != "price" || StepLabel("audit_follows_workflow") != "auditFollowsWorkflow" {
		t.Fatalf("StepLabel 映射异常")
	}
	if !IsReadTool("epic_free") || IsReadTool("add_follow") || !IsWriteTool("add_follow") {
		t.Fatalf("工具分组判定异常")
	}
	if CardKindHints()["get_price_briefing"] != "price" || !IsCardHintRowsTool("proxy_exit_detail") || len(CardTools()) != 21 {
		t.Fatalf("卡片工具映射异常：%d", len(CardTools()))
	}

	mux, db, _, _ := newTestSurface(t, false)
	runner, err := NewPilotRunner(mux, db, nil)
	if err != nil {
		t.Fatal(err)
	}
	res, ok := runner.Execute(context.Background(), "search_games", map[string]any{"q": "星露"}, false)
	if !ok {
		t.Fatal("共享工具应命中")
	}
	if res["kind"] != "games" {
		t.Fatalf("结果形状异常：%v", res)
	}
	if _, ok := runner.Execute(context.Background(), "not_in_shared_registry", nil, false); ok {
		t.Fatal("不存在的工具不应命中")
	}

	if bad, ok := runner.Execute(context.Background(), "search_games", map[string]any{}, false); !ok || bad["kind"] != "failed" {
		t.Fatalf("缺少必填参数应返回 failed：%v", bad)
	}
	if badType, ok := runner.Execute(context.Background(), "search_games", map[string]any{"q": 123}, false); !ok || badType["kind"] != "failed" {
		t.Fatalf("参数类型错误应返回 failed：%v", badType)
	}
	if badCard, ok := runner.Execute(context.Background(), "search_games", map[string]any{"q": "星露", "emit_card": "false"}, false); !ok || badCard["kind"] != "failed" {
		t.Fatalf("emit_card 非布尔值应返回 failed：%v", badCard)
	}
	if okCard, ok := runner.Execute(context.Background(), "search_games", map[string]any{"q": "星露", "emit_card": false}, false); !ok || okCard["kind"] != "games" {
		t.Fatalf("合法 emit_card=false 应放行：%v", okCard)
	}
}

func TestPilotRunnerValidationShortCircuitsHandler(t *testing.T) {
	called := 0
	reg := registerOne(t, &ToolSpec{
		Name: "get_price_briefing", Group: GroupRead, Risk: "low", TimeoutS: 5,
		Parameters: `{"type":"object","properties":{"appid":{"type":"integer"},"ratio":{"type":"number"}},"required":["appid"]}`,
		Handler: func(c *CallContext) (*ToolResult, error) {
			called++
			out := newObj()
			out.set("kind", "price")
			return &ToolResult{Status: StatusOK, Data: out}, nil
		},
	})
	runner := &PilotRunner{registry: reg}

	bad, ok := runner.Execute(context.Background(), "get_price_briefing", map[string]any{"appid": "730"}, false)
	if !ok || bad["kind"] != "failed" || !strings.Contains(bad["note"].(string), "参数校验失败") {
		t.Fatalf("非法 appid 应回结构化 failed：%v", bad)
	}
	if called != 0 {
		t.Fatalf("校验拦截后不应进入 handler，called=%d", called)
	}

	good, ok := runner.Execute(context.Background(), "get_price_briefing", map[string]any{"appid": 730, "ratio": 12.5, "emit_card": false}, false)
	if !ok || good["kind"] != "price" || called != 1 {
		t.Fatalf("合法整数与浮点入参应直达 handler：ok=%t good=%v called=%d", ok, good, called)
	}
}
