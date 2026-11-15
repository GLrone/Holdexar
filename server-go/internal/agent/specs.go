package agent

import ()

// 预算分档（等价 Python projection.py budget_for）：同步刷新类 240（等待多为
// 共享写闸排队，短预算会把正常排队误判成失败）、运行面调度工具 60、单工具
// 覆盖表、默认 20。写组与 propose_* 在 Go MCP 面不存在（fail-closed），不设档。
const (
	budgetDefaultS = 20.0
	budgetSlowS    = 240.0
	budgetTaskS    = 60.0
)

// syncBudgetTools 同步刷新工具（Python _SYNC_TOOLS 同表，幂等拉取不设守卫）。
var syncBudgetTools = map[string]bool{
	"sync_wallet": true, "sync_library": true, "sync_achievements": true,
	"sync_bills": true, "sync_family": true, "sync_subscriptions": true,
	"subscription_sync": true, "refresh_rates": true, "ingest_appids": true,
	"refresh_game_price": true, "notify_test": true,
}

// taskBudgetTools 运行面调度工具（Python _TASK_TOOLS 同表）。
var taskBudgetTools = map[string]bool{
	"list_tasks": true, "start_task": true, "cancel_task": true, "crawl_stop": true,
}

// toolBudgetS 单工具预算覆盖（Python _TOOL_BUDGET_S 同表：40 网络往返 /
// 60 重扫聚合 / 90 流水线巡检 / 180 全量出口检测）。
var toolBudgetS = map[string]float64{
	"search_steam": 40.0, "web_search": 40.0, "hb_monthly": 40.0, "epic_free": 40.0,
	"steam_free": 40.0, "calendar_events": 40.0, "rates_overview": 40.0,
	"diagnose_price": 60.0, "bills_summary": 60.0, "achievements_summary": 60.0,
	"level_profile": 60.0, "challenges_overview": 60.0,
	"list_family_library": 60.0, "list_owned": 60.0, "top_games": 60.0,
	"price_drops": 60.0, "list_bundles": 60.0, "read_session": 60.0,
	"find_deletables": 90.0, "audit_follows_workflow": 90.0,
	"achievements_near_completion": 60.0, "bills_spend_query": 60.0,
	"proxy_latency_test": 180.0,
}

// budgetFor 工具执行预算（秒）。
func budgetFor(name string) float64 {
	if syncBudgetTools[name] {
		return budgetSlowS
	}
	if taskBudgetTools[name] {
		return budgetTaskS
	}
	if v, ok := toolBudgetS[name]; ok {
		return v
	}
	return budgetDefaultS
}

// applyDefaultBudget 未显式指定 TimeoutS 的工具落默认预算；预置值原样保留
//（工具批经 budgetFor 取档后写入 spec.TimeoutS）。
func applyDefaultBudget(spec *ToolSpec) {
	if spec.TimeoutS == 0 {
		spec.TimeoutS = budgetDefaultS
	}
}

// buildRegistry 登记全部 MCP 可用工具（声明序 = Python 注册表子集序）。
// 重名即错（程序错误，启动即失败）。
func buildRegistry() (*ToolRegistry, error) {
	r := newRegistry()
	for _, spec := range []*ToolSpec{
		{
			Name: "search_games", Group: GroupRead, Risk: "low",
			Description: "在本地游戏目录按名称检索游戏（中英文均可），返回候选（含 appid、现价、折扣、好评率）。" +
				"注意：目录未收录、或收录了但还没爬过价格的游戏检索不到——那时改用 search_steam 去 Steam 商店搜",
			Parameters: `{"type": "object", "properties": {"q": {"type": "string", "description": "游戏名称关键词"}}, "required": ["q"]}`,
			Handler:    searchGames,
		},
		{
			Name: "recommend_games", Group: GroupRead, Risk: "low",
			Description: "按条件从用户库内挑游戏，可组合：打折中 / 好评率下限 / 价格上限（元）",
			Parameters: `{"type": "object", "properties": {
				"only_discounted": {"type": "boolean", "description": "只看打折中的游戏"},
				"min_rating": {"type": "integer", "description": "好评率下限（0-100）"},
				"max_price_yuan": {"type": "number", "description": "现价上限（人民币元）"}
			}}`,
			Handler: recommendGames,
		},
		{
			Name: "game_detail", Group: GroupRead, Risk: "low",
			Description: "取某游戏的完整档案：名称、评测与好评率、标签、国区现价与折扣、" +
				"最低价区、史低旗标、是否在关注中。用户问『这游戏什么情况/什么类型/" +
				"值得买吗』时调用；逐区现价明细用 get_region_prices",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 AppID"}
			}, "required": ["appid"]}`,
			Handler: gameDetail,
		},
		{
			Name: "linked_bundles", Group: GroupRead, Risk: "low",
			Description: "取某游戏出现在哪些捆绑包里，及各包国区价、最低区与差价。" +
				"用户问『有没有捆绑包更划算/这游戏和谁打包卖』时调用",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 AppID"}
			}, "required": ["appid"]}`,
			Handler: linkedBundles,
		},
		{
			Name: "rates_history", Group: GroupRead, Risk: "low",
			Description: "取某币种对人民币的汇率走势（默认美元近 90 天），点位已抽样。" +
				"用户问『为什么外区折算价变了/汇率现在多少』时调用",
			Parameters: `{"type": "object", "properties": {
				"currency": {"type": "string", "description": "币种代码（如 USD/JPY/RUB），默认 USD"},
				"days": {"type": "integer", "description": "回看天数，默认 90"}
			}}`,
			Handler: ratesHistory,
		},
		{
			Name: "regions_config", Group: GroupRead, Risk: "low",
			Description: "读取区服配置：全部区服、哪些已启用抓取、已购游戏抓取区设置。" +
				"用户问『现在抓哪几个区/能看哪里的价』时调用",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    regionsConfig,
		},
		{
			Name: "notifications_status", Group: GroupRead, Risk: "low",
			Description: "查看通知开关与投递情况（总开关、静默时段、送达/失败计数、最近一封结果）。" +
				"用户问『通知开了吗』『邮件发成功没有』时调用",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    notificationsStatus,
		},
		{
			Name: "notification_history", Group: GroupRead, Risk: "low",
			Description: "查看最近的通知投递记录（发了什么、状态如何、重试几次）。" +
				"用户问『最近发过哪些通知』『哪封没发出去』时调用",
			Parameters: `{"type": "object", "properties": {
				"limit": {"type": "integer", "description": "返回条数，缺省 20"}
			}}`,
			Handler: notificationHistory,
		},
		{
			Name: "list_accounts", Group: GroupRead, Risk: "low",
			Description: "绑定 Steam 账号清单（名称/好友码/主账号/拥有统计）——只读账号面，无任何凭据",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listAccounts,
		},
		{
			Name: "list_wishlist", Group: GroupRead, Risk: "low",
			Description: "账户愿望单游戏（监控池内 wishlisted 来源，库内快照，带国区现价简报）",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listWishlist,
		},
		{
			Name: "list_owned", Group: GroupRead, Risk: "low",
			Description: "账户已拥有游戏（监控池内 owned 来源，带国区现价简报）",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listOwned,
		},
		{
			Name: "bills_summary", Group: GroupRead, Risk: "low",
			Description: "本地账单导入记录与消费合计（新→旧，含退款口径）。" +
				"用户问『我的账单/消费记录/花了多少钱』时调用",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    billsSummary,
		},
		{
			Name: "get_user_preferences", Group: GroupRead, Risk: "low",
			Description: "查询用户已保存的长期偏好清单（如常用区服、类型偏好、预算习惯）。用户询问『我的偏好是什么/你记住了我什么』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     getUserPreferences,
		},
		{
			Name: "set_user_preference", Group: GroupRead, Risk: "medium",
			Description: "保存或更新用户的长期偏好。用户明确要求『记住我的偏好/记住我喜欢XX/记住我只买XX区』时调用",
			Parameters: `{"type": "object", "properties": {
				"category": {"type": "string", "enum": ["region_preference", "genre_preference", "budget_habit", "preference"], "description": "偏好类别"},
				"key": {"type": "string", "description": "偏好键名（如 preferred_regions, liked_genres, max_budget_cny）"},
				"value": {"description": "偏好内容（可以是列表、字典或字符串/数值）"}
			}, "required": ["key", "value"]}`,
			Handler: setUserPreference,
		},
	} {
		applyDefaultBudget(spec)
		if err := r.register(spec); err != nil {
			return nil, err
		}
	}
	// 阶段 6 三批补齐（66 个准入工具，重名即错）：
	// 批一目录/价格/捆绑/优惠/汇率/日历 16、批二账户/同步/会话/元信息 25、
	// 批三运维/代理/诊断/网络/运行时 25——分批清单见 docs/migration/MCP_TOOLS_SPEC.md §四。
	for _, register := range []func(*ToolRegistry) error{
		RegisterCatalogPriceTools,
		RegisterAccountTools,
		RegisterOpsRuntimeTools,
	} {
		if err := register(r); err != nil {
			return nil, err
		}
	}
	// 预算统一按分档表归一（覆盖既有 14 工具的缺省档，如 list_owned/bills_summary=60）。
	for _, spec := range r.all() {
		spec.TimeoutS = budgetFor(spec.Name)
	}
	return r, nil
}
