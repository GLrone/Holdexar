package agent

import (
	"context"
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
}
