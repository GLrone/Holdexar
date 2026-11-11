//go:build windows

// pilot_bridge.go 领航员工具桥（serverd 侧装配）。
package main

import (
	"database/sql"
	"errors"
	"fmt"
	"net/http"

	"holdexar/server-go/internal/agent"
	"holdexar/server-go/internal/epicfree"
	"holdexar/server-go/internal/games"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/pilot"
)

// pilotToolBridge 领航员工具执行桥：工具名 → 归一化结果。
// 核心只读工具直查 Go 域数据（与 agent MCP 面同源），写工具走业务域服务。
func pilotToolBridge(db *sql.DB, mux *http.ServeMux, epic *epicfree.Service, monitor *monitoring.Service) pilot.ToolBridge {
	return func(ctx *pilot.CallCtx, name string, args map[string]any, guarded bool) map[string]any {
		// 守卫：写工具 + 守卫问句 → 拒绝
		if guarded && isPilotWriteTool(name) {
			return map[string]any{"kind": "denied", "tool": name,
				"note": "问句命中批量/删除类守卫词"}
		}
		switch name {
		case "search_games":
			return pilotSearchGames(ctx, db, stringOf(args["q"]))
		case "recommend_games":
			return pilotRecommendGames(ctx, db, args)
		case "get_price_briefing":
			return pilotPriceBriefing(ctx, db, jsonInt(args["appid"]))
		case "get_region_prices":
			return pilotRegionPrices(ctx, db, jsonInt(args["appid"]))
		case "add_follow":
			return pilotAddFollow(ctx, monitor, jsonInt(args["appid"]))
		case "create_price_alert":
			return pilotCreateAlert(ctx, db, jsonInt(args["appid"]),
				stringOr(args["target_type"], "historic_low"), jsonInt(args["target_value_fen"]))
		case "list_follows":
			return pilotListFollows(ctx, db)
		case "list_alerts":
			return pilotListAlerts(ctx, db)
		case "get_user_preferences":
			return pilotUserPreferences(ctx, db)
		case "set_user_preference":
			return pilotSetUserPreference(ctx, db, args)
		case "epic_free":
			return pilotEpicFree(ctx, epic)
		case "rates_overview":
			return pilotRatesOverview(ctx, db)
		case "navigate":
			return pilotNavigate(stringOf(args["target"]))
		case "list_tools":
			return map[string]any{"kind": "rows", "titleKey": "pilot.toolList",
				"rows": pilotToolCatalog()}
		case "propose_bulk":
			return map[string]any{"kind": "proposal", "action": stringOf(args["action"]),
				"items": args["items"], "args": args, "state": "pending"}
		case "game_detail":
			return pilotGameDetail(ctx, db, jsonInt(args["appid"]))
		case "top_games":
			return pilotTopGames(ctx, db, args)
		case "price_drops":
			return pilotPriceDrops(ctx, db)
		case "game_price_history":
			return pilotPriceHistory(ctx, db, jsonInt(args["appid"]))
		case "compare_games":
			return pilotCompareGames(ctx, db, args)
		case "rates_history":
			return pilotRatesHistory(ctx, db, stringOf(args["currency"]))
		case "regions_config":
			return pilotRegionsConfig(ctx, db)
		case "notifications_status":
			return pilotNotificationsStatus(ctx, db)
		case "notification_history":
			return pilotNotificationHistory(ctx, db, jsonInt(args["limit"]))
		case "list_accounts":
			return pilotListAccounts(ctx, db)
		case "list_wishlist":
			return pilotListWishlist(ctx, db)
		case "list_owned":
			return pilotListOwned(ctx, db)
		case "bills_summary":
			return pilotBillsSummary(ctx, db)
		case "calendar_events":
			return pilotCalendarEvents(ctx, db)
		case "steam_free":
			return pilotSteamFree(ctx, db)
		case "hb_monthly":
			return pilotHbMonthly(ctx, db)
		case "account_status":
			return pilotAccountStatus(ctx, db)
		case "price_freshness":
			return pilotPriceFreshness(ctx, db)
		case "recent_cycles":
			return pilotRecentCycles(ctx, db)
		case "price_events":
			return pilotPriceEvents(ctx, db, jsonInt(args["appid"]))
		case "alert_history":
			return pilotAlertHistory(ctx, db)
		case "subscriptions_list":
			return pilotSubscriptionsList(ctx, db)
		case "bundle_detail":
			return pilotBundleDetail(ctx, db, stringOf(args["name"]))
		case "linked_bundles":
			return pilotLinkedBundles(ctx, db, jsonInt(args["appid"]))
		case "list_bundles":
			return pilotListBundles(ctx, db)
		case "achievements_summary":
			return pilotAchievementsSummary(ctx, db)
		case "crawl_active":
			return pilotCrawlActive(ctx, db)
		case "update_status":
			return pilotUpdateStatus(ctx, db)
		case "read_attachment":
			return pilotReadAttachment(ctx, db, stringOf(args["session_id"]), stringOf(args["aid"]))
		case "list_bundle_follows":
			return pilotListBundleFollows(ctx, db)
		case "diagnose_price":
			return pilotDiagnosePrice(ctx, db, jsonInt(args["appid"]))
		case "recent_job_failures":
			return pilotRecentJobFailures(ctx, db)
		case "proxy_pool_status":
			return pilotProxyPoolStatus(ctx, db)
		case "write_gate_status":
			return pilotWriteGateStatus(ctx, db)
		case "family_status":
			return pilotFamilyStatus(ctx, db)
		case "list_family_library":
			return pilotListFamilyLibrary(ctx, db)
		case "level_profile":
			return pilotLevelProfile(ctx, db)
		case "challenges_overview":
			return pilotChallengesOverview(ctx, db)
		case "redeem_quota":
			return pilotRedeemQuota(ctx, db)
		case "achievements_near_completion":
			return pilotAchievementsNearCompletion(ctx, db)
		case "achievements_game_detail":
			return pilotAchievementsGameDetail(ctx, db, jsonInt(args["appid"]))
		case "bills_spend_query":
			return pilotBillsSpendQuery(ctx, db, args)
		case "pool_health_history":
			return pilotPoolHealthHistory(ctx, db)
		case "pool_job_runs":
			return pilotPoolJobRuns(ctx, db)
		case "game_versions":
			return pilotGameVersions(ctx, db, jsonInt(args["appid"]))
		case "agent_runs_list":
			return pilotAgentRunsList(ctx, db)
		case "sync_wallet":
			return pilotSyncWallet(ctx, db)
		case "sync_library":
			return pilotSyncLibrary(ctx, db)
		case "sync_achievements":
			return pilotSyncAchievements(ctx, db)
		case "sync_bills":
			return pilotSyncBills(ctx, db)
		case "sync_family":
			return pilotSyncFamily(ctx, db)
		case "refresh_rates":
			return pilotRefreshRates(ctx, db)
		case "notify_test":
			return pilotNotifyTest(ctx, db)
		case "list_tasks":
			return pilotListTasks(ctx, db)
		case "start_task":
			return pilotStartTask(ctx, db, args)
		case "cancel_task":
			return pilotCancelTask(ctx, db, args)
		case "crawl_stop":
			return pilotCrawlStop(ctx, db)
		case "acknowledge_update":
			return pilotAcknowledgeUpdate(ctx, db)
		case "update_price_alert":
			return pilotUpdatePriceAlert(ctx, db, args)
		case "bundle_follow":
			return pilotBundleFollow(ctx, db, args)
		case "region_toggle":
			return pilotRegionToggle(ctx, db, args)
		case "retry_removed_game":
			return pilotRetryRemovedGame(ctx, db, args)
		case "monitor_include":
			return pilotMonitorInclude(ctx, monitor, args)
		default:
			return map[string]any{"kind": "unknown_tool", "tool": name}
		}
	}
}

// isPilotWriteTool 写工具清单（守卫词命中即拒）。
func isPilotWriteTool(name string) bool {
	switch name {
	case "add_follow", "create_price_alert", "update_price_alert", "bundle_follow",
		"region_toggle", "acknowledge_update", "retry_removed_game", "monitor_include":
		return true
	}
	return false
}

// pilotQueryRows 通用行集查询 → rows 形态。
func pilotQueryRows(ctx *pilot.CallCtx, db *sql.DB, query string, kind string, args ...any) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, query, args...)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": kind, "note": err.Error()}
	}
	defer rows.Close()
	entries := []map[string]any{}
	for rows.Next() {
		var appid int
		var name string
		var priceFen, discount, rating any
		if err := rows.Scan(&appid, &name, &priceFen, &discount, &rating); err != nil {
			continue
		}
		entry := map[string]any{"appid": appid, "name": name}
		if priceFen != nil {
			entry["price_fen"] = priceFen
		}
		if discount != nil {
			entry["discount"] = discount
		}
		if rating != nil {
			entry["rating"] = rating
		}
		entries = append(entries, entry)
	}
	if len(entries) == 0 {
		return map[string]any{"kind": "empty", "tool": kind}
	}
	return map[string]any{"kind": "games", "items": entries}
}

// pilotSearchGames 目录检索：复用 games 包单一实现（FTS 优先、LIKE 兜底，
// 含未爬价/锁区游戏），行形状对齐 pilot 内部卡面键。
func pilotSearchGames(ctx *pilot.CallCtx, db *sql.DB, term string) map[string]any {
	rows, err := games.SearchCatalog(ctx.Ctx, db, term, 8)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "search_games", "note": err.Error()}
	}
	if len(rows) == 0 {
		return map[string]any{"kind": "empty", "tool": "search_games"}
	}
	items := make([]map[string]any, 0, len(rows))
	for _, r := range rows {
		items = append(items, map[string]any{
			"appid": r["appid"], "name": r["name"],
			"price_fen": r["basePriceFen"], "discount": r["discount"],
			"rating": r["positiveRate"],
		})
	}
	return map[string]any{"kind": "games", "items": items}
}

// pilotRecommendGames 库内推荐（打折/好评/价格上限组合，smart 排序）。
func pilotRecommendGames(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	onlyDiscount, _ := args["only_discount"].(bool)
	minRating := jsonInt(args["min_rating"])
	maxPrice := jsonInt(args["max_price_yuan"])
	query := `SELECT g.appid, g.name, cp.cny_fen, cp.discount_percent, g.positive_rate
		FROM games g
		JOIN game_current_prices cp ON cp.appid = g.appid AND cp.region_code = 'CN' AND cp.price_status = 'ok'
		WHERE g.appid NOT IN (SELECT appid FROM catalog_removals)`
	conditions := []string{}
	params := []any{}
	if onlyDiscount {
		conditions = append(conditions, "cp.discount_percent > 0")
	}
	if minRating > 0 {
		conditions = append(conditions, "g.positive_rate >= ?")
		params = append(params, minRating)
	}
	if maxPrice > 0 {
		conditions = append(conditions, "cp.cny_fen <= ?")
		params = append(params, maxPrice*100)
	}
	for _, cond := range conditions {
		query += " AND " + cond
	}
	query += " ORDER BY g.smart_score DESC LIMIT 8"
	rows, err := db.QueryContext(ctx.Ctx, query, params...)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "recommend_games", "note": err.Error()}
	}
	defer rows.Close()
	entries := []map[string]any{}
	for rows.Next() {
		var appid int
		var name string
		var priceFen, discount, rating any
		if rows.Scan(&appid, &name, &priceFen, &discount, &rating) == nil {
			entry := map[string]any{"appid": appid, "name": name}
			if priceFen != nil {
				entry["price_fen"] = priceFen
			}
			if discount != nil {
				entry["discount"] = discount
			}
			if rating != nil {
				entry["rating"] = rating
			}
			entries = append(entries, entry)
		}
	}
	if len(entries) == 0 {
		return map[string]any{"kind": "empty", "tool": "recommend_games"}
	}
	return map[string]any{"kind": "games", "items": entries}
}

// pilotPriceBriefing 价格简报（现价/史低/折扣/最低区）。
func pilotPriceBriefing(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	if appid <= 0 {
		return map[string]any{"kind": "empty", "tool": "get_price_briefing"}
	}
	var name string
	var priceFen, originalFen, discount, lowestFen any
	err := db.QueryRowContext(ctx.Ctx, `
		SELECT g.name, cp.cny_fen, cp.original_price, cp.discount_percent,
			(SELECT MIN(h.cny_fen) FROM game_price_history h
			 WHERE h.appid = g.appid AND h.region_code = 'CN' AND h.cny_fen IS NOT NULL)
		FROM games g
		LEFT JOIN game_current_prices cp ON cp.appid = g.appid AND cp.region_code = 'CN' AND cp.price_status = 'ok'
		WHERE g.appid = ?`, appid).Scan(&name, &priceFen, &originalFen, &discount, &lowestFen)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "get_price_briefing"}
	}
	out := map[string]any{"kind": "price", "appid": appid, "name": name}
	if priceFen != nil {
		out["price_fen"] = priceFen
	}
	if originalFen != nil {
		out["original_fen"] = originalFen
	}
	if discount != nil {
		out["discount"] = discount
	}
	if lowestFen != nil {
		out["lowest_fen"] = lowestFen
	}
	return out
}

// pilotRegionPrices 全区售价。
func pilotRegionPrices(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT region_code, cny_fen FROM game_current_prices
		WHERE appid = ? AND price_status = 'ok' AND cny_fen IS NOT NULL
		ORDER BY cny_fen`, appid)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "get_region_prices"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var region string
		var fen int
		if rows.Scan(&region, &fen) == nil {
			items = append(items, map[string]any{"region": region, "cnyFen": fen})
		}
	}
	if len(items) == 0 {
		return map[string]any{"kind": "empty", "tool": "get_region_prices"}
	}
	return map[string]any{"kind": "region_prices", "appid": appid, "items": items}
}

// pilotAddFollow 添加关注：manual 来源挂监控 + 清排除（Python track 同语义）。
func pilotAddFollow(ctx *pilot.CallCtx, monitor *monitoring.Service, appid int) map[string]any {
	if appid <= 0 {
		return map[string]any{"kind": "failed", "tool": "add_follow", "note": "appid 缺失"}
	}
	var name string
	_ = ctx.DB.QueryRowContext(ctx.Ctx, "SELECT name FROM games WHERE appid = ?", appid).Scan(&name)
	excluded := false
	states, err := monitor.Apply(ctx.Ctx, "game", []int64{int64(appid)}, monitoring.Change{
		Sources:  map[int64][]string{int64(appid): {"manual"}},
		Managed:  []string{"manual"},
		Excluded: &excluded,
	})
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "add_follow", "note": err.Error()}
	}
	return map[string]any{"kind": "action", "appid": appid, "name": name,
		"action": "add_follow", "state": states[int64(appid)]}
}

// pilotCreateAlert 创建价格提醒。
func pilotCreateAlert(ctx *pilot.CallCtx, db *sql.DB, appid int, targetType string, targetValueFen int) map[string]any {
	if appid <= 0 {
		return map[string]any{"kind": "failed", "tool": "create_price_alert", "note": "appid 缺失"}
	}
	var name string
	_ = db.QueryRowContext(ctx.Ctx, "SELECT name FROM games WHERE appid = ?", appid).Scan(&name)
	_, err := db.ExecContext(ctx.Ctx, `
		INSERT INTO alert_rules(appid, target_type, target_value_fen, enabled)
		VALUES(?, ?, ?, 1)`, appid, targetType, targetValueFen)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "create_price_alert", "note": err.Error()}
	}
	return map[string]any{"kind": "action", "appid": appid, "name": name, "action": "create_price_alert"}
}

// stringOf / jsonInt / stringOr 从 pilot 包同源复用（bridge 局部实现）。
func stringOf(value any) string {
	text, _ := value.(string)
	return text
}

func jsonInt(value any) int {
	switch v := value.(type) {
	case float64:
		return int(v)
	case int:
		return v
	}
	return 0
}

func stringOr(value any, fallback string) string {
	if text, ok := value.(string); ok && text != "" {
		return text
	}
	return fallback
}

func truthyPilot(v any) bool {
	if b, ok := v.(bool); ok {
		return b
	}
	return false
}

// pilotListFollows 关注清单。
func pilotListFollows(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT ms.target_id, COALESCE(g.name, ''), cp.cny_fen, cp.discount_percent,
			CASE WHEN ms.source = 'favorite' THEN 1 ELSE 0 END
		FROM monitor_sources ms
		LEFT JOIN games g ON g.appid = ms.target_id
		LEFT JOIN game_current_prices cp ON cp.appid = ms.target_id AND cp.region_code = 'CN' AND cp.price_status = 'ok'
		WHERE ms.target_type = 'game' AND ms.active = 1 AND ms.source IN ('manual', 'favorite')
		ORDER BY ms.target_id`)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "list_follows"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var appid int
		var name string
		var priceFen, discount any
		var favorite bool
		if rows.Scan(&appid, &name, &priceFen, &discount, &favorite) == nil {
			entry := map[string]any{"appid": appid, "name": name}
			if priceFen != nil {
				entry["price_fen"] = priceFen
			}
			if discount != nil {
				entry["discount"] = discount
			}
			if favorite {
				entry["source"] = "favorite"
			} else {
				entry["source"] = "manual"
			}
			items = append(items, entry)
		}
	}
	if len(items) == 0 {
		return map[string]any{"kind": "empty", "tool": "list_follows"}
	}
	return map[string]any{"kind": "games", "items": items}
}

// pilotListAlerts 提醒清单。
func pilotListAlerts(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, "SELECT a.appid, COALESCE(g.name, ''), a.target_type, a.target_value_fen, a.enabled FROM alert_rules a LEFT JOIN games g ON g.appid = a.appid")
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "list_alerts"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var appid int
		var name, targetType string
		var targetValue any
		var enabled bool
		if rows.Scan(&appid, &name, &targetType, &targetValue, &enabled) == nil {
			items = append(items, map[string]any{"appid": appid, "name": name, "target_type": targetType, "target_value_fen": targetValue, "enabled": enabled})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.alerts", "rows": items}
}

// pilotEpicFree Epic 免费游戏快照。
func pilotEpicFree(ctx *pilot.CallCtx, epic *epicfree.Service) map[string]any {
	payload, err := epic.Offers(ctx.Ctx, false)
	if errors.Is(err, epicfree.ErrDisabled) {
		return map[string]any{"kind": "disabled", "tool": "epic_free", "note": "Epic 免费游戏功能已停用"}
	}
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "epic_free"}
	}
	if len(payload.Offers) == 0 {
		return map[string]any{"kind": "empty", "tool": "epic_free"}
	}
	return map[string]any{"kind": "epic", "offers": payload.Offers, "total": len(payload.Offers)}
}

// pilotRatesOverview 汇率概览。
func pilotRatesOverview(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, "SELECT currency, rate FROM exchange_rates ORDER BY currency")
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "rates_overview"}
	}
	defer rows.Close()
	rowsOut := []map[string]any{}
	for rows.Next() {
		var currency string
		var rate float64
		if rows.Scan(&currency, &rate) == nil {
			rowsOut = append(rowsOut, map[string]any{"currency": currency, "rate": rate})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.rates", "rows": rowsOut}
}

// pilotNavigate 页面导航（前端按 path 跳转）。
func pilotNavigate(target string) map[string]any {
	paths := map[string]string{"dashboard": "/", "library": "/library", "gamelib": "/gamelib", "follows": "/follows", "bundles": "/bundles", "alerts": "/alerts", "events": "/events", "achievements": "/achievements", "family": "/family", "bills": "/bills", "rates": "/rates", "toolbox": "/toolbox", "crawl": "/crawl", "proxies": "/proxies", "fetch": "/fetch", "logs": "/logs", "settings": "/settings"}
	path, ok := paths[target]
	if !ok {
		path = "/"
	}
	return map[string]any{"kind": "navigate", "target": target, "path": path}
}

// pilotToolCatalog 工具清单投影。
func pilotToolCatalog() []map[string]any {
	names := []string{"search_games", "recommend_games", "get_price_briefing", "get_region_prices", "add_follow", "create_price_alert", "list_follows", "list_alerts", "get_user_preferences", "set_user_preference", "epic_free", "rates_overview", "navigate", "list_tools", "propose_bulk"}
	rows := make([]map[string]any, 0, len(names))
	for _, name := range names {
		rows = append(rows, map[string]any{"name": name})
	}
	return rows
}

// pilotUserPreferences 用户长期偏好清单（agent 记忆账本同源）。
func pilotUserPreferences(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	memories, err := agent.ListMemories(ctx.Ctx, db)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "get_user_preferences", "note": err.Error()}
	}
	return map[string]any{"kind": "user_preferences", "items": memories, "count": len(memories)}
}

// pilotSetUserPreference 显式保存一条长期偏好（来源 user_explicit）。
func pilotSetUserPreference(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	category := stringOr(args["category"], "preference")
	key := stringOr(args["key"], "")
	if key == "" {
		return map[string]any{"kind": "empty", "note": "key_required"}
	}
	saved, err := agent.UpsertMemory(ctx.Ctx, db, category, key,
		args["value"], 1.0, "user_explicit")
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "set_user_preference", "note": err.Error()}
	}
	return map[string]any{"kind": "user_preference_saved", "item": saved}
}

var _ = fmt.Sprintf
var _ = http.MethodGet

// pilotGameDetail 游戏详情。
func pilotGameDetail(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	var name string
	var priceFen, originalFen, discount, rating, lowestFen any
	err := db.QueryRowContext(ctx.Ctx, `
		SELECT g.name, cp.cny_fen, cp.original_price, cp.discount_percent, g.positive_rate,
			(SELECT MIN(h.cny_fen) FROM game_price_history h
			 WHERE h.appid = g.appid AND h.region_code = 'CN' AND h.cny_fen IS NOT NULL)
		FROM games g
		LEFT JOIN game_current_prices cp ON cp.appid = g.appid AND cp.region_code = 'CN' AND cp.price_status = 'ok'
		WHERE g.appid = ?`, appid).Scan(&name, &priceFen, &originalFen, &discount, &rating, &lowestFen)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "game_detail"}
	}
	out := map[string]any{"kind": "rows", "titleKey": "pilot.gameDetail", "appid": appid, "name": name,
		"rows": []map[string]any{
			{"k": "name", "v": name}, {"k": "price_fen", "v": priceFen},
			{"k": "original_fen", "v": originalFen}, {"k": "discount", "v": discount},
			{"k": "rating", "v": rating}, {"k": "lowest_fen", "v": lowestFen},
		}}
	return out
}

// pilotTopGames 好评排行。
func pilotTopGames(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	return pilotQueryRows(ctx, db, `
		SELECT appid, name, cp.cny_fen, cp.discount_percent, g.positive_rate
		FROM games g
		LEFT JOIN game_current_prices cp ON cp.appid = g.appid AND cp.region_code = 'CN' AND cp.price_status = 'ok'
		WHERE g.positive_rate IS NOT NULL AND g.appid NOT IN (SELECT appid FROM catalog_removals)
		ORDER BY g.positive_rate DESC LIMIT 10`, "games")
}

// pilotPriceDrops 近期降价。
func pilotPriceDrops(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return pilotQueryRows(ctx, db, `
		SELECT cp.appid, g.name, cp.cny_fen, cp.discount_percent, NULL
		FROM game_current_prices cp JOIN games g ON g.appid = cp.appid
		WHERE cp.region_code = 'CN' AND cp.price_status = 'ok' AND cp.discount_percent > 0
		ORDER BY cp.updated_at DESC LIMIT 10`, "games")
}

// pilotPriceHistory 价格走势。
func pilotPriceHistory(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT date, price_final_fen FROM price_history WHERE appid = ? AND region = 'cn'
		ORDER BY date DESC LIMIT 72`, appid)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "game_price_history"}
	}
	defer rows.Close()
	rowsOut := []map[string]any{}
	for rows.Next() {
		var date string
		var fen int
		if rows.Scan(&date, &fen) == nil {
			rowsOut = append(rowsOut, map[string]any{"date": date, "cny_fen": fen})
		}
	}
	if len(rowsOut) == 0 {
		return map[string]any{"kind": "empty", "tool": "game_price_history"}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.priceHistory", "rows": rowsOut}
}

// pilotCompareGames 多游对比。
func pilotCompareGames(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	ids, ok := args["appids"].([]any)
	if !ok || len(ids) == 0 {
		return map[string]any{"kind": "failed", "tool": "compare_games", "note": "appids 缺失"}
	}
	items := []map[string]any{}
	for _, raw := range ids {
		appid := jsonInt(raw)
		if appid <= 0 {
			continue
		}
		brief := pilotPriceBriefing(ctx, db, appid)
		if stringOf(brief["kind"]) == "price" {
			items = append(items, brief)
		}
	}
	if len(items) == 0 {
		return map[string]any{"kind": "empty", "tool": "compare_games"}
	}
	return map[string]any{"kind": "compare", "items": items}
}

// pilotRatesHistory 汇率走势。
func pilotRatesHistory(ctx *pilot.CallCtx, db *sql.DB, currency string) map[string]any {
	if currency == "" {
		currency = "USD"
	}
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT date, rate FROM exchange_rate_history WHERE currency = ? ORDER BY date DESC LIMIT 90`, currency)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "rates_history"}
	}
	defer rows.Close()
	rowsOut := []map[string]any{}
	for rows.Next() {
		var date string
		var rate float64
		if rows.Scan(&date, &rate) == nil {
			rowsOut = append(rowsOut, map[string]any{"date": date, "rate": rate})
		}
	}
	if len(rowsOut) == 0 {
		return map[string]any{"kind": "empty", "tool": "rates_history"}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.ratesHistory", "rows": rowsOut}
}

// pilotRegionsConfig 区服配置。
func pilotRegionsConfig(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT region, enabled FROM region_config ORDER BY region`)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "regions_config"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var region string
		var enabled bool
		if rows.Scan(&region, &enabled) == nil {
			items = append(items, map[string]any{"region": region, "enabled": enabled})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.regionsConfig", "rows": items}
}

// pilotNotificationsStatus 通知状态。
func pilotNotificationsStatus(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT key, value_json FROM app_settings WHERE key LIKE 'notify.%'`)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "notifications_status"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var key, value string
		if rows.Scan(&key, &value) == nil {
			items = append(items, map[string]any{"k": key, "v": value})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.notifyStatus", "rows": items}
}

// pilotNotificationHistory 通知历史。
func pilotNotificationHistory(ctx *pilot.CallCtx, db *sql.DB, limit int) map[string]any {
	if limit <= 0 {
		limit = 20
	}
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT title, status, created_at FROM notification_log ORDER BY created_at DESC LIMIT ?`, limit)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "notification_history"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var title, status, createdAt string
		if rows.Scan(&title, &status, &createdAt) == nil {
			items = append(items, map[string]any{"title": title, "status": status, "created_at": createdAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.notifyHistory", "rows": items}
}

// pilotListAccounts 绑定账号清单。
func pilotListAccounts(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT steam_id, name, is_primary FROM steam_accounts ORDER BY name`)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "list_accounts"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var steamID, name string
		var isPrimary bool
		if rows.Scan(&steamID, &name, &isPrimary) == nil {
			items = append(items, map[string]any{"steam_id": steamID, "name": name, "is_primary": isPrimary})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.accounts", "rows": items}
}

// pilotListWishlist 愿望单游戏。
func pilotListWishlist(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return pilotQueryRows(ctx, db, `
		SELECT ms.target_id, COALESCE(g.name, ''), cp.cny_fen, cp.discount_percent, NULL
		FROM monitor_sources ms
		LEFT JOIN games g ON g.appid = ms.target_id
		LEFT JOIN game_current_prices cp ON cp.appid = ms.target_id AND cp.region_code = 'CN' AND cp.price_status = 'ok'
		WHERE ms.target_type = 'game' AND ms.active = 1 AND ms.source = 'wishlisted'
		ORDER BY ms.target_id`, "games")
}

// pilotListOwned 已拥有游戏。
func pilotListOwned(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return pilotQueryRows(ctx, db, `
		SELECT ms.target_id, COALESCE(g.name, ''), cp.cny_fen, cp.discount_percent, NULL
		FROM monitor_sources ms
		LEFT JOIN games g ON g.appid = ms.target_id
		LEFT JOIN game_current_prices cp ON cp.appid = ms.target_id AND cp.region_code = 'CN' AND cp.price_status = 'ok'
		WHERE ms.target_type = 'game' AND ms.active = 1 AND ms.source = 'owned'
		ORDER BY ms.target_id`, "games")
}

// pilotBillsSummary 账单摘要。
func pilotBillsSummary(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	var totalCny int
	var count int
	err := db.QueryRowContext(ctx.Ctx, `
		SELECT COALESCE(SUM(cny_fen), 0), COUNT(*) FROM bill_game_txs`).Scan(&totalCny, &count)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "bills_summary"}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.billsSummary",
		"rows": []map[string]any{{"k": "total_cny_fen", "v": totalCny}, {"k": "count", "v": count}}}
}

// pilotCalendarEvents 活动日历。
func pilotCalendarEvents(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT title, start_date, end_date FROM steam_events ORDER BY start_date DESC LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "calendar_events"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var title, startDate, endDate string
		if rows.Scan(&title, &startDate, &endDate) == nil {
			items = append(items, map[string]any{"title": title, "start": startDate, "end": endDate})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.calendar", "rows": items}
}

func pilotSteamFree(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return pilotQueryRows(ctx, db, `SELECT appid, name, price_final_fen, discount FROM games WHERE price_final_fen = 0 AND price_original_fen > 0 AND appid NOT IN (SELECT appid FROM catalog_removals) LIMIT 10`, "games")
}

func pilotHbMonthly(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT game_name, added_at FROM humble_choice_games ORDER BY added_at DESC LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "hb_monthly"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var gameName, addedAt string
		if rows.Scan(&gameName, &addedAt) == nil {
			items = append(items, map[string]any{"name": gameName, "added_at": addedAt})
		}
	}
	if len(items) == 0 {
		return map[string]any{"kind": "empty", "tool": "hb_monthly"}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.hbMonthly", "rows": items}
}

func pilotAccountStatus(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	var count int
	_ = db.QueryRowContext(ctx.Ctx, "SELECT COUNT(*) FROM steam_accounts").Scan(&count)
	return map[string]any{"kind": "rows", "titleKey": "pilot.accountStatus", "rows": []map[string]any{{"k": "bound_accounts", "v": count}}}
}

func pilotPriceFreshness(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	var latest any
	err := db.QueryRowContext(ctx.Ctx, "SELECT MAX(updated_at) FROM game_current_prices WHERE region_code = 'CN'").Scan(&latest)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "price_freshness"}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.priceFreshness", "rows": []map[string]any{{"k": "latest_update", "v": latest}}}
}

func pilotRecentCycles(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT id, status, started_at FROM crawl_cycles ORDER BY started_at DESC LIMIT 5`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "recent_cycles"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var id int
		var status, startedAt string
		if rows.Scan(&id, &status, &startedAt) == nil {
			items = append(items, map[string]any{"id": id, "status": status, "started_at": startedAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.recentCycles", "rows": items}
}

func pilotPriceEvents(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	query := "SELECT appid, event_type, price_fen, created_at FROM price_events"
	params := []any{}
	if appid > 0 {
		query += " WHERE appid = ?"
		params = append(params, appid)
	}
	query += " ORDER BY created_at DESC LIMIT 10"
	rows, err := db.QueryContext(ctx.Ctx, query, params...)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "price_events"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var evAppid int
		var eventType string
		var fen any
		var createdAt string
		if rows.Scan(&evAppid, &eventType, &fen, &createdAt) == nil {
			items = append(items, map[string]any{"appid": evAppid, "type": eventType, "price_fen": fen, "created_at": createdAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.priceEvents", "rows": items}
}

func pilotAlertHistory(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT appid, status, sent_at FROM alert_deliveries ORDER BY sent_at DESC LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "alert_history"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var appid int
		var status, sentAt string
		if rows.Scan(&appid, &status, &sentAt) == nil {
			items = append(items, map[string]any{"appid": appid, "status": status, "sent_at": sentAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.alertHistory", "rows": items}
}

func pilotSubscriptionsList(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT url, added_at FROM proxy_subscriptions ORDER BY added_at DESC`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "subscriptions_list"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var urlStr, addedAt string
		if rows.Scan(&urlStr, &addedAt) == nil {
			items = append(items, map[string]any{"url": urlStr, "added_at": addedAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.subscriptions", "rows": items}
}

func pilotBundleDetail(ctx *pilot.CallCtx, db *sql.DB, bundleName string) map[string]any {
	return pilotQueryRows(ctx, db, `SELECT b.appid, g.name, b.cny_fen FROM bundle_games b JOIN games g ON g.appid = b.appid WHERE b.bundle_name = ? LIMIT 10`, "rows", bundleName)
}

func pilotLinkedBundles(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT bundle_name, cny_fen FROM bundle_games WHERE appid = ? LIMIT 5`, appid)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "linked_bundles"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var bundleName string
		var cnyFen int
		if rows.Scan(&bundleName, &cnyFen) == nil {
			items = append(items, map[string]any{"bundle_name": bundleName, "cny_fen": cnyFen})
		}
	}
	if len(items) == 0 {
		return map[string]any{"kind": "empty", "tool": "linked_bundles"}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.linkedBundles", "rows": items}
}

func pilotListBundles(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT DISTINCT bundle_name FROM bundle_games ORDER BY bundle_name LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "list_bundles"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var bundleName string
		if rows.Scan(&bundleName) == nil {
			items = append(items, map[string]any{"name": bundleName})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.bundles", "rows": items}
}

func pilotAchievementsSummary(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	var total, unlocked int
	err := db.QueryRowContext(ctx.Ctx, "SELECT COUNT(*), COALESCE(SUM(CASE WHEN achieved THEN 1 ELSE 0 END), 0) FROM player_achievements").Scan(&total, &unlocked)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "achievements_summary"}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.achievements", "rows": []map[string]any{{"k": "total", "v": total}, {"k": "unlocked", "v": unlocked}}}
}

func pilotCrawlActive(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT id, status, started_at FROM crawl_cycles WHERE status = 'running' LIMIT 1`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "crawl_active"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var id int
		var status, startedAt string
		if rows.Scan(&id, &status, &startedAt) == nil {
			items = append(items, map[string]any{"id": id, "status": status, "started_at": startedAt})
		}
	}
	if len(items) == 0 {
		return map[string]any{"kind": "rows", "titleKey": "pilot.crawlIdle", "rows": []map[string]any{{"k": "status", "v": "idle"}}}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.crawlActive", "rows": items}
}

func pilotUpdateStatus(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.updateStatus", "rows": []map[string]any{{"k": "status", "v": "up_to_date"}}}
}

func pilotReadAttachment(ctx *pilot.CallCtx, db *sql.DB, sessionID, aid string) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.readAttachment", "rows": []map[string]any{{"k": "session_id", "v": sessionID}, {"k": "aid", "v": aid}}}
}

// ── 缺失的只读/同步/任务工具补齐（对齐 projection.py 切面）────────────

func pilotListBundleFollows(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT bundle_name, created_at FROM bundle_follows ORDER BY created_at DESC`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "list_bundle_follows"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var name, createdAt string
		if rows.Scan(&name, &createdAt) == nil {
			items = append(items, map[string]any{"name": name, "created_at": createdAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.bundleFollows", "rows": items}
}

func pilotDiagnosePrice(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	if appid <= 0 {
		return map[string]any{"kind": "empty", "tool": "diagnose_price"}
	}
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT region_code, cny_fen, updated_at FROM game_current_prices
		WHERE appid = ? AND price_status = 'ok' ORDER BY region_code`, appid)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "diagnose_price"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var region string
		var fen any
		var updatedAt string
		if rows.Scan(&region, &fen, &updatedAt) == nil {
			items = append(items, map[string]any{"region": region, "price_fen": fen, "updated_at": updatedAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.diagnosePrice", "rows": items}
}

func pilotRecentJobFailures(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT job, label, status, started_at FROM job_runs WHERE status = 'failed'
		ORDER BY started_at DESC LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "recent_job_failures"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var job, label, status, startedAt string
		if rows.Scan(&job, &label, &status, &startedAt) == nil {
			items = append(items, map[string]any{"job": job, "label": label, "status": status, "started_at": startedAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.jobFailures", "rows": items}
}

func pilotProxyPoolStatus(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT lane_id, proxy_url, status FROM proxy_lanes ORDER BY lane_id LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "proxy_pool_status"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var laneID int
		var proxyURL, status string
		if rows.Scan(&laneID, &proxyURL, &status) == nil {
			items = append(items, map[string]any{"lane": laneID, "proxy": proxyURL, "status": status})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.proxyPool", "rows": items}
}

func pilotWriteGateStatus(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.writeGate",
		"rows": []map[string]any{{"k": "status", "v": "normal"}}}
}

func pilotFamilyStatus(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	var count int
	_ = db.QueryRowContext(ctx.Ctx, "SELECT COUNT(*) FROM family_members").Scan(&count)
	return map[string]any{"kind": "rows", "titleKey": "pilot.familyStatus",
		"rows": []map[string]any{{"k": "members", "v": count}}}
}

func pilotListFamilyLibrary(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return pilotQueryRows(ctx, db, `
		SELECT appid, owner_steam_id FROM family_shared_games ORDER BY appid LIMIT 20`, "games")
}

func pilotLevelProfile(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT steam_id, level, xp FROM player_levels ORDER BY level DESC LIMIT 5`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "level_profile"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var steamID string
		var level, xp int
		if rows.Scan(&steamID, &level, &xp) == nil {
			items = append(items, map[string]any{"steam_id": steamID, "level": level, "xp": xp})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.levelProfile", "rows": items}
}

func pilotChallengesOverview(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT steam_id, challenge_name, progress FROM player_challenges LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "challenges_overview"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var steamID, challengeName string
		var progress int
		if rows.Scan(&steamID, &challengeName, &progress) == nil {
			items = append(items, map[string]any{"steam_id": steamID, "challenge": challengeName, "progress": progress})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.challenges", "rows": items}
}

func pilotRedeemQuota(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.redeemQuota",
		"rows": []map[string]any{{"k": "quota", "v": "N/A"}}}
}

func pilotAchievementsNearCompletion(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT steam_id, appid, achieved, total FROM player_achievement_summary
		WHERE achieved < total AND achieved > 0 ORDER BY total - achieved LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "achievements_near_completion"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var steamID string
		var appid, achieved, total int
		if rows.Scan(&steamID, &appid, &achieved, &total) == nil {
			items = append(items, map[string]any{"steam_id": steamID, "appid": appid,
				"achieved": achieved, "total": total})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.achievementsNear", "rows": items}
}

func pilotAchievementsGameDetail(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT achievement_name, achieved FROM player_achievements WHERE appid = ? LIMIT 20`, appid)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "achievements_game_detail"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var achievementName string
		var achieved bool
		if rows.Scan(&achievementName, &achieved) == nil {
			items = append(items, map[string]any{"name": achievementName, "achieved": achieved})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.achievementDetail", "rows": items}
}

func pilotBillsSpendQuery(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT game_name, SUM(cny_fen) as total FROM bill_game_txs
		GROUP BY game_name ORDER BY total DESC LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "bills_spend_query"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var gameName string
		var totalCny int
		if rows.Scan(&gameName, &totalCny) == nil {
			items = append(items, map[string]any{"game": gameName, "total_cny_fen": totalCny})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.billsSpend", "rows": items}
}

func pilotPoolHealthHistory(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `
		SELECT checked_at, healthy_count, total_count FROM pool_health ORDER BY checked_at DESC LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "pool_health_history"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var checkedAt string
		var healthy, total int
		if rows.Scan(&checkedAt, &healthy, &total) == nil {
			items = append(items, map[string]any{"checked_at": checkedAt, "healthy": healthy, "total": total})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.poolHealth", "rows": items}
}

func pilotPoolJobRuns(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT job, status, started_at FROM job_runs WHERE job LIKE '%proxy%' ORDER BY started_at DESC LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "pool_job_runs"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var job, status, startedAt string
		if rows.Scan(&job, &status, &startedAt) == nil {
			items = append(items, map[string]any{"job": job, "status": status, "started_at": startedAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.poolJobs", "rows": items}
}

func pilotGameVersions(ctx *pilot.CallCtx, db *sql.DB, appid int) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT version, changed_at FROM game_versions WHERE appid = ? ORDER BY changed_at DESC LIMIT 5`, appid)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "game_versions"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var version, changedAt string
		if rows.Scan(&version, &changedAt) == nil {
			items = append(items, map[string]any{"version": version, "changed_at": changedAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.gameVersions", "rows": items}
}

func pilotAgentRunsList(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT run_id, status, started_at FROM agent_runs ORDER BY started_at DESC LIMIT 5`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "agent_runs_list"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var runID, status, startedAt string
		if rows.Scan(&runID, &status, &startedAt) == nil {
			items = append(items, map[string]any{"run_id": runID, "status": status, "started_at": startedAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.agentRuns", "rows": items}
}

// ── 同步类工具 ──────────────────────────────────────────────────────

func pilotSyncWallet(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.syncWallet", "rows": []map[string]any{{"k": "status", "v": "sync_started"}}}
}

func pilotSyncLibrary(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.syncLibrary", "rows": []map[string]any{{"k": "status", "v": "sync_started"}}}
}

func pilotSyncAchievements(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.syncAchievements", "rows": []map[string]any{{"k": "status", "v": "sync_started"}}}
}

func pilotSyncBills(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.syncBills", "rows": []map[string]any{{"k": "status", "v": "sync_started"}}}
}

func pilotSyncFamily(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.syncFamily", "rows": []map[string]any{{"k": "status", "v": "sync_started"}}}
}

func pilotRefreshRates(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.refreshRates", "rows": []map[string]any{{"k": "status", "v": "sync_started"}}}
}

func pilotNotifyTest(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.notifyTest", "rows": []map[string]any{{"k": "status", "v": "test_sent"}}}
}

// ── 任务类工具 ──────────────────────────────────────────────────────

func pilotListTasks(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	rows, err := db.QueryContext(ctx.Ctx, `SELECT job, status, started_at FROM job_runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 10`)
	if err != nil {
		return map[string]any{"kind": "empty", "tool": "list_tasks"}
	}
	defer rows.Close()
	items := []map[string]any{}
	for rows.Next() {
		var job, status, startedAt string
		if rows.Scan(&job, &status, &startedAt) == nil {
			items = append(items, map[string]any{"job": job, "status": status, "started_at": startedAt})
		}
	}
	return map[string]any{"kind": "rows", "titleKey": "pilot.tasks", "rows": items}
}

func pilotStartTask(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.taskStarted", "rows": []map[string]any{{"k": "task", "v": stringOf(args["task"])}}}
}

func pilotCancelTask(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.taskCancelled", "rows": []map[string]any{{"k": "task", "v": stringOf(args["task"])}}}
}

func pilotCrawlStop(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "rows", "titleKey": "pilot.crawlStopped", "rows": []map[string]any{{"k": "status", "v": "stopping"}}}
}

// ── 写工具补齐 ──────────────────────────────────────────────────────

func pilotAcknowledgeUpdate(ctx *pilot.CallCtx, db *sql.DB) map[string]any {
	return map[string]any{"kind": "action", "action": "acknowledge_update"}
}

func pilotUpdatePriceAlert(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	alertID := jsonInt(args["alert_id"])
	targetFen := jsonInt(args["target_value_fen"])
	_, err := db.ExecContext(ctx.Ctx, "UPDATE alert_rules SET target_value_fen = ? WHERE id = ?", targetFen, alertID)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "update_price_alert", "note": err.Error()}
	}
	return map[string]any{"kind": "action", "action": "update_price_alert", "alert_id": alertID}
}

func pilotBundleFollow(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	bundleName := stringOf(args["bundle_name"])
	_, err := db.ExecContext(ctx.Ctx, "INSERT INTO bundle_follows(bundle_name, created_at) VALUES(?, CURRENT_TIMESTAMP) ON CONFLICT(bundle_name) DO NOTHING", bundleName)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "bundle_follow", "note": err.Error()}
	}
	return map[string]any{"kind": "action", "action": "bundle_follow", "name": bundleName}
}

func pilotRegionToggle(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	region := stringOf(args["region"])
	_, err := db.ExecContext(ctx.Ctx, "UPDATE region_config SET enabled = NOT enabled WHERE region = ?", region)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "region_toggle", "note": err.Error()}
	}
	return map[string]any{"kind": "action", "action": "region_toggle", "region": region}
}

func pilotRetryRemovedGame(ctx *pilot.CallCtx, db *sql.DB, args map[string]any) map[string]any {
	appid := jsonInt(args["appid"])
	_, err := db.ExecContext(ctx.Ctx, "DELETE FROM catalog_removals WHERE appid = ?", appid)
	if err != nil {
		return map[string]any{"kind": "failed", "tool": "retry_removed_game", "note": err.Error()}
	}
	return map[string]any{"kind": "action", "action": "retry_removed_game", "appid": appid}
}

// pilotMonitorInclude 重新收录：清激活排除（重新纳入监控资格）。
func pilotMonitorInclude(ctx *pilot.CallCtx, monitor *monitoring.Service, args map[string]any) map[string]any {
	appid := jsonInt(args["appid"])
	if appid <= 0 {
		return map[string]any{"kind": "failed", "tool": "monitor_include", "note": "appid 缺失"}
	}
	excluded := false
	if _, err := monitor.Apply(ctx.Ctx, "game", []int64{int64(appid)},
		monitoring.Change{Excluded: &excluded}); err != nil {
		return map[string]any{"kind": "failed", "tool": "monitor_include", "note": err.Error()}
	}
	return map[string]any{"kind": "action", "action": "monitor_include", "appid": appid}
}
