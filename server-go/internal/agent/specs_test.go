package agent

import "testing"

// TestBudgetForTiers 预算分档表钉住（等价 Python projection.py budget_for：
// 同步 240 / 任务 60 / 单工具覆盖 40-180 / 默认 20）。
func TestBudgetForTiers(t *testing.T) {
	cases := []struct {
		name string
		want float64
	}{
		// 网络往返档 40
		{"search_steam", 40}, {"web_search", 40}, {"hb_monthly", 40}, {"epic_free", 40},
		{"steam_free", 40}, {"calendar_events", 40}, {"rates_overview", 40},
		// 重扫/聚合档 60
		{"diagnose_price", 60}, {"bills_summary", 60}, {"achievements_summary", 60},
		{"level_profile", 60}, {"challenges_overview", 60}, {"list_family_library", 60},
		{"list_owned", 60}, {"top_games", 60}, {"price_drops", 60}, {"list_bundles", 60},
		{"read_session", 60}, {"achievements_near_completion", 60}, {"bills_spend_query", 60},
		// 流水线巡检档 90
		{"find_deletables", 90}, {"audit_follows_workflow", 90},
		// 全量出口检测档 180
		{"proxy_latency_test", 180},
		// 同步刷新档 240（写闸排队为主，短预算误判失败）
		{"sync_wallet", 240}, {"sync_library", 240}, {"sync_achievements", 240},
		{"sync_bills", 240}, {"sync_family", 240}, {"sync_subscriptions", 240},
		{"subscription_sync", 240}, {"refresh_rates", 240}, {"ingest_appids", 240},
		{"refresh_game_price", 240}, {"notify_test", 240},
		// 运行面调度档 60
		{"list_tasks", 60}, {"start_task", 60}, {"cancel_task", 60}, {"crawl_stop", 60},
		// 默认档 20（已注册工具与未知工具同径）
		{"search_games", 20}, {"game_detail", 20}, {"no_such_tool", 20},
	}
	for _, tc := range cases {
		if got := budgetFor(tc.name); got != tc.want {
			t.Errorf("budgetFor(%s)=%v want %v", tc.name, got, tc.want)
		}
	}
}

func TestApplyDefaultBudgetKeepsPreset(t *testing.T) {
	preset := &ToolSpec{Name: "preset", TimeoutS: 60}
	applyDefaultBudget(preset)
	if preset.TimeoutS != 60 {
		t.Fatalf("预置预算被覆盖：%v", preset.TimeoutS)
	}
	fallback := &ToolSpec{Name: "fallback"}
	applyDefaultBudget(fallback)
	if fallback.TimeoutS != budgetDefaultS {
		t.Fatalf("缺省预算=%v want %v", fallback.TimeoutS, budgetDefaultS)
	}
}

func TestBuildRegistryAppliesDefaultBudget(t *testing.T) {
	r, err := buildRegistry()
	if err != nil {
		t.Fatal(err)
	}
	// 全表预算统一按 Python 分档表（projection.py budget_for）——
	// 阶段 6 起缺省档 20 只是分档表的兜底值。
	for _, spec := range r.all() {
		if spec.TimeoutS != budgetFor(spec.Name) {
			t.Fatalf("%s 预算=%v want %v", spec.Name, spec.TimeoutS, budgetFor(spec.Name))
		}
	}
}
