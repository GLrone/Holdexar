package agent

import ()

// budgetDefaultS 只读工具默认执行预算（秒），与 Python BUDGET_DEFAULT_S 一致。
const budgetDefaultS = 20.0

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
	} {
		spec.TimeoutS = budgetDefaultS
		if err := r.register(spec); err != nil {
			return nil, err
		}
	}
	return r, nil
}
