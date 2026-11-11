//go:build windows

// pilot_specs.go 领航员工具表投影（agent 注册表 → OpenAI function 格式）。
package main

import "encoding/json"

// agentToolSpecs 投影 agent 注册表的工具为 OpenAI function 格式。
// 每个工具的 description 和 parameters 来自 agent ToolSpec（与 MCP 面同源）。
func agentToolSpecs() []map[string]any {
	toolDefs := []struct {
		name        string
		description string
		parameters  string
	}{
		{"search_games", "在本地游戏目录按名称检索游戏（中英文均可），返回候选（含 appid、现价、折扣、好评率）",
			`{"type":"object","properties":{"q":{"type":"string","description":"游戏名称关键词"}},"required":["q"]}`},
		{"recommend_games", "按条件从用户库内挑游戏：打折中 / 好评率下限 / 价格上限（元）",
			`{"type":"object","properties":{"only_discounted":{"type":"boolean"},"min_rating":{"type":"integer","description":"好评率下限0-100"},"max_price_yuan":{"type":"number","description":"现价上限元"}}}`},
		{"get_price_briefing", "取某游戏的完整价格简报：现价、原价、折扣、史低价。问某游戏什么情况/值不值得买时调用",
			`{"type":"object","properties":{"appid":{"type":"integer","description":"游戏AppID"}},"required":["appid"]}`},
		{"get_region_prices", "取某游戏在各区服的现价（全区比价）。问哪个区最便宜时调用",
			`{"type":"object","properties":{"appid":{"type":"integer","description":"游戏AppID"}},"required":["appid"]}`},
		{"add_follow", "添加游戏到关注（监控池）。用户说『帮我关注 X』时调用",
			`{"type":"object","properties":{"appid":{"type":"integer","description":"游戏AppID"}},"required":["appid"]}`},
		{"create_price_alert", "设置价格提醒。用户说『低于N元提醒我』或『史低提醒我』时调用",
			`{"type":"object","properties":{"appid":{"type":"integer","description":"游戏AppID"},"target_type":{"type":"string","description":"historic_low或price"},"target_value_fen":{"type":"integer","description":"提醒阈值（分），史低类省略"}},"required":["appid"]}`},
		{"list_follows", "查看当前关注清单",
			`{"type":"object","properties":{}}`},
		{"list_alerts", "查看已设提醒清单",
			`{"type":"object","properties":{}}`},
		{"epic_free", "查看 Epic 免费游戏（本周/下周白嫖）",
			`{"type":"object","properties":{}}`},
		{"rates_overview", "查看各区服汇率概览",
			`{"type":"object","properties":{}}`},
		{"navigate", "页面导航：控制前端跳转到指定页面",
			`{"type":"object","properties":{"target":{"type":"string","description":"页面标识：dashboard/library/follows/alerts/settings等"}},"required":["target"]}`},
		{"list_tools", "查看完整工具清单（解锁诊断与同步类工具）",
			`{"type":"object","properties":{}}`},
		{"propose_bulk", "批量提议：批量关注/提醒等写操作提交给用户确认",
			`{"type":"object","properties":{"action":{"type":"string","description":"add_follow或create_price_alert"},"items":{"type":"array","description":"目标列表"}},"required":["action","items"]}`},
	}
	out := make([]map[string]any, 0, len(toolDefs))
	for _, def := range toolDefs {
		var params any
		_ = json.Unmarshal([]byte(def.parameters), &params)
		out = append(out, map[string]any{
			"type": "function",
			"function": map[string]any{
				"name":        def.name,
				"description": def.description,
				"parameters":  params,
			},
		})
	}
	return out
}
