package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
)

type ExportedSpec struct {
	Name           string
	Description    string
	ParametersJSON string
	Group          string
	Risk           string
	TimeoutS       float64
}

// SharedSpecs 导出共享工具注册表的全部工具定义。
func SharedSpecs() ([]ExportedSpec, error) {
	reg, err := buildRegistry()
	if err != nil {
		return nil, err
	}
	all := reg.all()
	out := make([]ExportedSpec, 0, len(all))
	for _, spec := range all {
		out = append(out, ExportedSpec{
			Name:           spec.Name,
			Description:    spec.Description,
			ParametersJSON: spec.Parameters,
			Group:          string(spec.Group),
			Risk:           string(spec.Risk),
			TimeoutS:       spec.TimeoutS,
		})
	}
	return out, nil
}

func BudgetFor(name string) float64 {
	return budgetFor(name)
}

func CoreToolNames() map[string]bool {
	out := make(map[string]bool, len(coreTools))
	for name, on := range coreTools {
		out[name] = on
	}
	return out
}

func StepLabel(name string) string {
	if label, ok := stepLabels[name]; ok {
		return label
	}
	return name
}

func IsReadTool(name string) bool { return pilotReadTools[name] }

func IsSyncTool(name string) bool { return syncBudgetTools[name] }

func IsTaskTool(name string) bool { return taskBudgetTools[name] }

func IsWriteTool(name string) bool { return pilotWriteTools[name] }

func CardKindHints() map[string]string {
	out := make(map[string]string, len(cardKindHints))
	for k, v := range cardKindHints {
		out[k] = v
	}
	return out
}

func IsCardHintRowsTool(name string) bool {
	return name == "proxy_exit_detail" || name == "proxy_latency_test"
}

// CardTools 返回可注入 emit_card 参数的工具名称集合。
func CardTools() map[string]bool {
	out := make(map[string]bool, len(cardKindHints)+2)
	for name := range cardKindHints {
		out[name] = true
	}
	out["proxy_exit_detail"] = true
	out["proxy_latency_test"] = true
	return out
}

// PilotRunner 在进程内复用 agent 注册表执行工具调用，避免第二套孤岛实现。
type PilotRunner struct {
	registry *ToolRegistry
	fetcher  muxFetcher
	poster   muxPoster
	db       *sql.DB
	deps     *Deps
}

// NewPilotRunner 构造共享工具执行器；未传 deps 字段时继承 Default.deps 能力。
func NewPilotRunner(mux *http.ServeMux, db *sql.DB, deps *Deps) (*PilotRunner, error) {
	reg, err := buildRegistry()
	if err != nil {
		return nil, err
	}
	merged := &Deps{}
	if Default != nil && Default.deps != nil {
		*merged = *Default.deps
	}
	if deps != nil {
		if deps.LogRing != nil {
			merged.LogRing = deps.LogRing
		}
		if deps.WriteGate != nil {
			merged.WriteGate = deps.WriteGate
		}
		if deps.ResolveProxy != nil {
			merged.ResolveProxy = deps.ResolveProxy
		}
		if deps.ImportAppids != nil {
			merged.ImportAppids = deps.ImportAppids
		}
		if deps.AppIDFreshness != nil {
			merged.AppIDFreshness = deps.AppIDFreshness
		}
	}
	var fetcher muxFetcher
	var poster muxPoster
	if mux != nil {
		fetcher = newMuxFetcher(mux)
		poster = newMuxPoster(mux)
	} else if Default != nil {
		fetcher = Default.fetcher
		poster = Default.poster
	}
	if db == nil && Default != nil {
		db = Default.db
	}
	return &PilotRunner{
		registry: reg,
		fetcher:  fetcher,
		poster:   poster,
		db:       db,
		deps:     merged,
	}, nil
}

func (r *PilotRunner) Execute(ctx context.Context, name string, args map[string]any, guarded bool) (map[string]any, bool) {
	if r == nil || r.registry == nil {
		return nil, false
	}
	spec := r.registry.get(name)
	if spec == nil {
		return nil, false
	}
	if ctx == nil {
		ctx = context.Background()
	}
	if args == nil {
		args = map[string]any{}
	}
	res := Execute(&CallContext{
		Ctx:   ctx,
		Args:  args,
		fetch: r.fetcher,
		post:  r.poster,
		db:    r.db,
		deps:  r.deps,
	}, spec, guarded)
	if res == nil || res.Data == nil {
		return map[string]any{}, true
	}
	out, _ := objToAny(res.Data).(map[string]any)
	if out == nil {
		return map[string]any{}, true
	}
	return out, true
}

func objToAny(value any) any {
	switch v := value.(type) {
	case *Obj:
		if v == nil {
			return map[string]any{}
		}
		out := make(map[string]any, len(v.keys))
		for i, k := range v.keys {
			out[k] = objToAny(v.vals[i])
		}
		return out
	case []*Obj:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = objToAny(item)
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			out[i] = objToAny(item)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(v))
		for k, item := range v {
			out[k] = objToAny(item)
		}
		return out
	case nil, string, bool, int, int64, float64:
		return v
	default:
		raw, err := json.Marshal(v)
		if err != nil {
			return v
		}
		var parsed any
		if json.Unmarshal(raw, &parsed) == nil {
			return parsed
		}
		return v
	}
}

var pilotReadTools = map[string]bool{
	"list_follows": true, "list_alerts": true, "list_bundle_follows": true,
	"diagnose_price": true, "recent_job_failures": true, "proxy_pool_status": true,
	"proxy_exit_detail": true, "proxy_latency_test": true, "write_gate_status": true,
	"hb_monthly": true, "epic_free": true, "steam_free": true, "top_games": true,
	"price_drops": true, "rates_overview": true, "calendar_events": true,
	"list_accounts": true, "list_wishlist": true, "list_owned": true,
	"list_family_library": true, "achievements_summary": true, "level_profile": true,
	"challenges_overview": true, "bills_summary": true, "family_status": true,
	"redeem_quota": true, "list_bundles": true, "list_sessions": true,
	"read_session": true, "read_attachment": true, "web_search": true,
	"present_ui": true, "crawl_active": true, "price_freshness": true,
	"recent_cycles": true, "price_events": true, "subscriptions_list": true,
	"pool_health_history": true, "pool_job_runs": true, "account_status": true,
	"notifications_status": true, "notification_history": true,
	"achievements_near_completion": true, "achievements_game_detail": true,
	"bills_spend_query": true, "alert_history": true, "update_status": true,
	"agent_runs_list": true, "agent_run_events": true, "game_detail": true,
	"game_price_history": true, "game_versions": true, "linked_bundles": true,
	"bundle_detail": true, "rates_history": true, "regions_config": true,
}

var pilotWriteTools = map[string]bool{
	"acknowledge_update": true, "add_follow": true, "create_price_alert": true,
	"update_price_alert": true, "bundle_follow": true, "region_toggle": true,
	"retry_removed_game": true, "monitor_include": true,
}

var cardKindHints = map[string]string{
	"get_price_briefing":     "price",
	"get_region_prices":      "regions",
	"compare_games":          "compare",
	"search_games":           "games",
	"recommend_games":        "games",
	"search_steam":           "games",
	"top_games":              "games",
	"price_drops":            "games",
	"epic_free":              "epic",
	"list_wishlist":          "games",
	"list_owned":             "games",
	"list_family_library":    "games",
	"audit_follows_workflow": "stepper",
	"achievements_summary":   "achievements",
	"family_status":          "family",
	"propose_bulk":           "proposal",
	"propose_delete":         "proposal",
	"navigate":               "navigate",
	"present_ui":             "structured",
}

var stepLabels = map[string]string{
	"search_games": "search", "get_price_briefing": "price", "get_region_prices": "regionPrices",
	"compare_games": "compare", "recommend_games": "recommend", "add_follow": "follow",
	"create_price_alert": "alert", "propose_bulk": "propose", "navigate": "navigate",
	"list_follows": "follows", "list_alerts": "alerts", "list_bundle_follows": "bundleFollows",
	"diagnose_price": "diagnose", "recent_job_failures": "jobFailures", "proxy_pool_status": "proxyStatus",
	"proxy_exit_detail": "proxyExits", "proxy_latency_test": "proxyLatencyTest", "write_gate_status": "gateStatus",
	"list_tasks": "tasks", "start_task": "taskStart", "cancel_task": "taskCancel",
	"list_sessions": "sessions", "read_session": "sessionRead", "read_attachment": "attachmentRead",
	"hb_monthly": "hbMonthly", "epic_free": "epicFree", "steam_free": "steamFree",
	"top_games": "topGames", "price_drops": "priceDrops", "rates_overview": "rates",
	"calendar_events": "calendar", "list_accounts": "accounts", "list_wishlist": "wishlist",
	"list_owned": "owned", "list_family_library": "famLibrary", "achievements_summary": "achievements",
	"level_profile": "levelProfile", "challenges_overview": "challengesOverview", "bills_summary": "bills",
	"family_status": "family", "redeem_quota": "redeem", "list_bundles": "bundlesAll",
	"update_price_alert": "updateAlert", "bundle_follow": "bundleFollow", "region_toggle": "regionToggle",
	"sync_wallet": "syncWallet", "sync_library": "syncLibrary", "sync_achievements": "syncAchievements",
	"sync_bills": "syncBills", "sync_family": "syncFamily", "refresh_rates": "refreshRates",
	"search_steam": "steamSearch", "ingest_appids": "ingest", "web_search": "webSearch",
	"refresh_game_price": "refreshPrice", "retry_removed_game": "retryRemoved", "monitor_include": "monitorInclude",
	"notify_test": "notifyTest", "find_deletables": "deletables", "propose_delete": "proposeDelete",
	"get_user_preferences": "getPreferences", "set_user_preference": "setPreference",
	"audit_follows_workflow": "auditFollowsWorkflow", "read_logs": "readLogs", "system_diagnostics": "diagnostics",
	"sync_subscriptions": "syncSubs", "list_tools": "listTools", "present_ui": "presentUI",
	"crawl_active": "crawlActive", "price_freshness": "freshness", "recent_cycles": "cycles",
	"price_events": "priceEvents", "crawl_stop": "crawlStop", "subscriptions_list": "subsList",
	"subscription_sync": "subSync", "pool_health_history": "healthHistory", "pool_job_runs": "jobRuns",
	"account_status": "accountStatus", "notifications_status": "notifyStatus",
	"notification_history": "notifyHistory", "achievements_near_completion": "achNearDone",
	"achievements_game_detail": "achGame", "bills_spend_query": "billsQuery", "alert_history": "alertHistory",
	"update_status": "updateStatus", "acknowledge_update": "updateStatus",
	"agent_runs_list": "agentRuns", "agent_run_events": "agentEvents",
	"game_detail": "gameDetail", "game_price_history": "priceHistory", "game_versions": "gameVersions",
	"linked_bundles": "linkedBundles", "bundle_detail": "bundleDetail",
	"rates_history": "ratesHistory", "regions_config": "regionsConfig",
}
