package main

import (
	"context"
	"net/http"
	"strings"
	"testing"

	"holdexar/server-go/internal/agent"
	"holdexar/server-go/internal/pilot"
)

func TestPilotExtraToolSchemasResolve(t *testing.T) {
	for _, def := range pilotExtraToolDefs {
		compiled := agent.CompileToolSchema(def.name, def.parameters)
		if !compiled.Resolved() {
			t.Fatalf("扩展工具 %s schema 解析失败", def.name)
		}
	}
}

func TestPilotBridgeExtraToolsValidation(t *testing.T) {
	bridge := pilotToolBridge(nil, nil, http.NewServeMux(), nil)
	ctx := &pilot.CallCtx{Ctx: context.Background()}

	cases := []struct {
		name string
		tool string
		args map[string]any
		want string
	}{
		{
			name: "start_task 枚举非法",
			tool: "start_task",
			args: map[string]any{"kind": "invalid"},
			want: "failed",
		},
		{
			name: "create_price_alert 缺必填字段",
			tool: "create_price_alert",
			args: map[string]any{"appid": 730},
			want: "failed",
		},
		{
			name: "create_price_alert 类型非法",
			tool: "create_price_alert",
			args: map[string]any{"appid": "730", "target_type": "historic_low"},
			want: "failed",
		},
		{
			name: "propose_bulk 数组项类型非法",
			tool: "propose_bulk",
			args: map[string]any{"action": "add_follow", "appids": []any{"abc"}},
			want: "failed",
		},
		{
			name: "present_ui 缺 data",
			tool: "present_ui",
			args: map[string]any{"component_id": "metric-grid"},
			want: "failed",
		},
		{
			name: "present_ui 密度枚举非法",
			tool: "present_ui",
			args: map[string]any{"component_id": "metric-grid", "data": map[string]any{}, "density": "huge"},
			want: "failed",
		},
		{
			name: "present_ui 未知组件保持 denied",
			tool: "present_ui",
			args: map[string]any{"component_id": "unknown-x", "data": map[string]any{}, "emit_card": true},
			want: "denied",
		},
		{
			name: "present_ui emit_card 非布尔报错",
			tool: "present_ui",
			args: map[string]any{"component_id": "metric-grid", "data": map[string]any{}, "emit_card": "yes"},
			want: "failed",
		},
		{
			name: "acknowledge_update 拒绝多余键",
			tool: "acknowledge_update",
			args: map[string]any{"extra": 1},
			want: "failed",
		},
		{
			name: "navigate 合法跳转",
			tool: "navigate",
			args: map[string]any{"target": "dashboard", "emit_card": false},
			want: "navigate",
		},
	}

	for _, tc := range cases {
		res := bridge(ctx, tc.tool, tc.args, false)
		if res["kind"] != tc.want {
			t.Errorf("%s: kind=%v want %s (res=%+v)", tc.name, res["kind"], tc.want, res)
		}
	}

	validAlert := bridge(ctx, "create_price_alert", map[string]any{
		"appid":             730,
		"target_type":       "price",
		"target_value_yuan": 12.5,
	}, false)
	if note, _ := validAlert["note"].(string); strings.Contains(note, "参数校验失败") {
		t.Fatalf("合法 create_price_alert 入参不应触发 schema 拦截：%v", validAlert)
	}

	guardedRes := bridge(ctx, "add_follow", map[string]any{}, true)
	if guardedRes["kind"] != "denied" {
		t.Fatalf("受保护写工具应先触发 bulk_guard，实际：%v", guardedRes)
	}
}
