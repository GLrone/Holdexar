//go:build windows

// decision.go 领航员内部快速决策代理：卡片准入裁决 + 模型视图增强 + 追问建议。
// 语义对齐 pilot/decision.py。
package pilot

import (
	"fmt"
	"strings"
)

// CardDecision 卡片发放裁决。
type CardDecision struct {
	ShouldEmit bool
	Card       map[string]any
	Kind       string
	Status     string // "rendered" | "suppressed"
	Reason     string // "ok" | "suppressed_by_model" | "no_price_intent" | "empty" | ...
	Summary    string
}

// 跨轮去重的卡种（静态清单类可抑制；价格/对比类不抑制）。
var repeatableSuppressKinds = map[string]bool{
	"games": true, "family": true, "achievements": true, "rows": true, "epic": true,
}

var followupKeys = map[string]bool{
	"saleNext": true, "saleLive": true, "saleWatch": true, "priceDropsRecent": true,
	"openCalendar": true, "compareRegions": true, "priceHistory": true,
	"similarGames": true, "addFollow": true, "setAlert": true,
	"myFollows": true, "ownedSkip": true, "freeNow": true, "auditFollows": true,
	"retryWeb": true, "hotDeals": true, "highRated": true, "budgetFriendly": true,
	"familyOverlap": true, "listAlerts": true,
}

var followupByIntent = map[string][]string{
	"price_analysis": {"priceHistory", "compareRegions", "similarGames", "addFollow", "setAlert"},
	"find_games":     {"highRated", "budgetFriendly", "ownedSkip", "priceDropsRecent"},
	"add_monitor":    {"myFollows", "setAlert", "priceDropsRecent"},
	"create_alert":   {"listAlerts", "myFollows"},
}

var followupByTopic = []struct {
	words []string
	keys  []string
}{
	{[]string{"大促", "特卖", "促销", "打折活动", "夏促", "秋促", "冬促", "史低", "什么时候开始"},
		[]string{"saleNext", "saleLive", "saleWatch", "openCalendar"}},
	{[]string{"活动", "日历", "游戏节", "新品节"}, []string{"openCalendar", "saleNext", "saleLive"}},
	{[]string{"免费", "喜加一", "白嫖", "白送", "限免"}, []string{"freeNow", "priceDropsRecent"}},
	{[]string{"降价", "跌了", "新低", "便宜了"}, []string{"priceDropsRecent", "myFollows", "hotDeals"}},
	{[]string{"汇率", "折合", "换算"}, []string{"compareRegions"}},
	{[]string{"家庭", "共享"}, []string{"familyOverlap"}},
	{[]string{"哪个区", "哪个地区", "全区", "各区", "比价"}, []string{"compareRegions"}},
}

var followupByCard = []struct {
	kinds []string
	keys  []string
}{
	{[]string{"price", "regions", "compare"},
		[]string{"priceHistory", "compareRegions", "similarGames", "addFollow", "setAlert"}},
	{[]string{"games"}, []string{"highRated", "budgetFriendly", "ownedSkip", "priceDropsRecent"}},
	{[]string{"family"}, []string{"familyOverlap"}},
	{[]string{"rows", "epic"}, []string{"priceDropsRecent", "myFollows"}},
}

var domainIntents = map[string]bool{
	"price_analysis": true, "find_games": true, "add_monitor": true, "create_alert": true,
}

var domainHints = []string{
	"steam", "愿望单", "喜加一", "夏促", "秋促", "冬促", "特卖", "区服",
	"俄区", "土区", "港区", "港服", "国区", "阿根廷", "土耳其", "激活码",
	"家庭共享", "成就", "奖杯", "关注列表", "降价提醒", "比价", "汇率折算",
	"节点", "延迟", "刷新", "入库", "促销日历",
}

var outOfDomain = []string{
	"手柄", "鼠标", "键盘", "耳机", "电源", "显卡", "主板", "内存", "硬盘",
	"平板", "手机", "笔记本", "主机", "外设", "显示器", "路由器",
	"选手", "战队", "比分", "夺冠", "冠军", "决赛", "半决赛", "小组赛",
	"解说", "直播", "主播", "签约", "转会", "禁赛",
	"联动", "周边", "手办", "快递", "门店", "客服申诉", "解封", "封号",
	"战绩查询", "段位", "排位", "匹配机制",
}

var nonPriceKeywords = []string{
	"配置", "显卡", "cpu", "内存", "优化", "帧率", "画质", "卡顿", "闪退",
	"打不开", "黑屏", "启动不了", "好玩吗", "好不好玩", "评价", "通关", "攻略",
	"流程", "剧情", "结局", "主角", "背景", "故事", "玩法", "联机", "mod",
	"汉化", "补丁", "按键", "怎么过", "怎么打",
}

var singleItemCheckTools = map[string]bool{
	"list_follows": true, "list_alerts": true, "list_owned": true,
	"list_wishlist": true, "list_family_library": true,
}

var diagnosticTools = map[string]bool{
	"proxy_pool_status": true, "proxy_exit_detail": true, "proxy_latency_test": true,
	"write_gate_status": true, "recent_job_failures": true, "diagnose_price": true, "list_tasks": true,
}

var dataSynthesisTools = map[string]bool{
	"steam_free": true, "hb_monthly": true, "calendar_events": true, "rates_overview": true,
}

// EvaluateIntent 评估问句的意图与价格/导购倾向。
func EvaluateIntent(question string) map[string]any {
	q := strings.TrimSpace(question)
	intent := Route(q)
	hasPrice := containsAny(q, priceWords)
	hasFind := containsAny(q, findWords)
	isPrice := intent == IntentPriceAnalysis || hasPrice
	isFind := intent == IntentFindGames || hasFind
	isNonPrice := containsAny(q, nonPriceKeywords) && !hasPrice
	nav := NavTarget(q)
	confidence := "low"
	if isPrice || isFind || nav != "" {
		confidence = "high"
	}
	return map[string]any{
		"intent": intent, "confidence": confidence, "is_price": isPrice,
		"is_find": isFind, "is_non_price": isNonPrice, "nav_target": nav,
	}
}

// hasSingleGameTarget 问句是否明确针对单款游戏。
func hasSingleGameTarget(q string) bool {
	if strings.Contains(q, "《") && strings.Contains(q, "》") {
		return true
	}
	for _, p := range []string{"它", "这游戏", "该游戏", "这款"} {
		if strings.Contains(q, p) {
			return true
		}
	}
	for _, p := range []string{"在不在", "有没有", "买了吗", "买了没", "在关注里吗",
		"在库里吗", "在愿望单吗", "关注了吗", "设置了提醒吗"} {
		if strings.Contains(q, p) {
			return true
		}
	}
	return false
}

// isFullListRequest 问句是否旨在查看完整清单。
func isFullListRequest(q string) bool {
	for _, ind := range []string{"在不在", "有没有", "买了吗", "买了没", "在吗",
		"关注了吗", "在关注里吗", "在库里吗", "在愿望单吗"} {
		if strings.Contains(q, ind) {
			return false
		}
	}
	return containsAny(q, []string{"列表", "清单", "有哪些", "全部", "所有"})
}

// CardIdentity 卡片身份：同 kind 下区分内容的最小锚。
func CardIdentity(kind string, card map[string]any) string {
	if kind == "epic" {
		offers := anySlice(card["offers"])
		first := ""
		if len(offers) > 0 {
			if offer, ok := offers[0].(map[string]any); ok {
				first = firstNonEmptyStr(stringOf(offer["title"]), stringOf(offer["titleCn"]))
			}
		}
		return fmt.Sprintf("epic:%s:%d", first, len(offers))
	}
	for _, key := range []string{"name", "appid", "title", "titleKey"} {
		if v := stringOf(card[key]); v != "" {
			return v
		}
	}
	items := anySlice(card["items"])
	if len(items) == 0 {
		items = anySlice(card["rows"])
	}
	for _, item := range items {
		if entry, ok := item.(map[string]any); ok {
			if name := stringOf(entry["name"]); name != "" {
				return name
			}
			if k := stringOf(entry["k"]); k != "" {
				return k
			}
		}
	}
	return ""
}

// DecideCard 裁决工具结果是否在前端生成可视化卡片。
func (s *Service) DecideCard(toolName string, result map[string]any, args map[string]any,
	question string, currentCards []map[string]any, baseCard map[string]any,
	recentCards [][2]string) CardDecision {
	if args == nil {
		args = map[string]any{}
	}
	if baseCard == nil {
		return CardDecision{ShouldEmit: false, Status: "suppressed",
			Reason: "empty_or_failed", Summary: "工具未返回有效可视化数据"}
	}
	// 模型显式控制
	if v, ok := args["emit_card"]; ok {
		if b, ok := v.(bool); ok && !b {
			kind := stringOf(baseCard["kind"])
			return CardDecision{ShouldEmit: false, Kind: kind,
				Status: "suppressed", Reason: "suppressed_by_model",
				Summary: "模型显式指定 emit_card=False"}
		}
	}
	kind := stringOf(baseCard["kind"])
	intentInfo := EvaluateIntent(question)
	isNonPrice, _ := intentInfo["is_non_price"].(bool)
	isFind, _ := intentInfo["is_find"].(bool)
	explicitAllow := false
	if v, ok := args["emit_card"].(bool); ok && v {
		explicitAllow = true
	}

	// 价格/地区/对比卡：非比价导向时抑制
	if kind == "price" || kind == "regions" || kind == "compare" {
		if isNonPrice && !explicitAllow {
			return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
				Reason: "no_price_intent", Summary: "问句非比价导向"}
		}
	}
	// games 卡：空候选/搜索但非找游戏意图/单款核查
	if kind == "games" {
		items := anySlice(baseCard["items"])
		if len(items) == 0 {
			return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
				Reason: "empty_candidates", Summary: "未检索到匹配游戏候选"}
		}
		if toolName == "search_games" && !isFind && !explicitAllow {
			return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
				Reason: "intermediate_retrieval", Summary: "中间推导数据未向用户展示"}
		}
		if hasSingleGameTarget(question) && !isFullListRequest(question) && !explicitAllow {
			return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
				Reason: "single_game_query", Summary: "针对单款游戏核查"}
		}
	}
	// 单对象核验工具
	if singleItemCheckTools[toolName] {
		if hasSingleGameTarget(question) && !isFullListRequest(question) && !explicitAllow {
			return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
				Reason: "single_game_query", Summary: "问句仅核验单款游戏"}
		}
	}
	// Epic 卡：空 offer 抑制
	if kind == "epic" {
		offers := anySlice(baseCard["offers"])
		if len(offers) == 0 && baseCard["mobile"] == nil {
			return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
				Reason: "empty_candidates", Summary: "Epic 免费游戏数据为空"}
		}
	}
	// 数据综合类工具默认不外泄为卡片
	if dataSynthesisTools[toolName] && !explicitAllow {
		return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
			Reason: "internal_data_for_synthesis", Summary: "数据供大模型总结"}
	}
	// 诊断工具非诊断问句抑制
	if diagnosticTools[toolName] && !containsAny(question, []string{"代理", "节点", "网络", "延迟", "排查", "诊断"}) && !explicitAllow {
		return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
			Reason: "internal_diagnostic", Summary: "技术诊断数据供内部推演"}
	}
	// 卡片上限
	if len(currentCards) >= maxCardsPerTurn {
		return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
			Reason: "quota_exceeded", Summary: "本轮卡片数已达上限"}
	}
	// 跨轮去重：静态清单类近轮已呈现过即抑制
	if repeatableSuppressKinds[kind] {
		identity := CardIdentity(kind, baseCard)
		if identity != "" {
			for _, recent := range recentCards {
				if recent[0] == kind && recent[1] == identity {
					return CardDecision{ShouldEmit: false, Kind: kind, Status: "suppressed",
						Reason: "already_shown", Summary: "同内容近轮已呈现"}
				}
			}
		}
	}
	return CardDecision{ShouldEmit: true, Card: baseCard, Kind: kind,
		Status: "rendered", Reason: "ok"}
}

// EnhanceModelView 在回灌大模型的视图中注入卡片呈现状态。
func EnhanceModelView(result map[string]any, decision CardDecision) map[string]any {
	out := map[string]any{}
	for k, v := range result {
		if k == "trend" {
			continue
		}
		out[k] = v
	}
	out["_ui_card"] = map[string]any{
		"status": decision.Status, "kind": decision.Kind,
		"summary": decision.Summary, "reason": decision.Reason,
	}
	return out
}

// SuggestFollowups 本轮追问候选（确定性产出）。
func SuggestFollowups(question, intent string, cards []map[string]any,
	lastGame map[string]any, toolsRan int) []map[string]any {
	q := strings.TrimSpace(question)
	var ordered []string
	add := func(keys []string) {
		for _, k := range keys {
			if followupKeys[k] {
				found := false
				for _, existing := range ordered {
					if existing == k {
						found = true
						break
					}
				}
				if !found {
					ordered = append(ordered, k)
				}
			}
		}
	}
	add(followupByIntent[intent])
	for _, topic := range followupByTopic {
		if containsAny(q, topic.words) {
			add(topic.keys)
		}
	}
	if len(ordered) == 0 {
		kinds := map[string]bool{}
		for _, card := range cards {
			kinds[stringOf(card["kind"])] = true
		}
		for _, byCard := range followupByCard {
			for _, kind := range byCard.kinds {
				if kinds[kind] {
					add(byCard.keys)
					break
				}
			}
			if len(ordered) > 0 {
				break
			}
		}
	}
	name := ""
	if lastGame != nil {
		name = strings.TrimSpace(stringOf(lastGame["name"]))
	}
	if name == "" {
		for _, card := range cards {
			kind := stringOf(card["kind"])
			if kind == "price" || kind == "regions" || kind == "compare" {
				name = strings.TrimSpace(stringOf(card["name"]))
				if name != "" {
					break
				}
			}
		}
	}
	if toolsRan == 0 {
		hasRetry := false
		for _, k := range ordered {
			if k == "retryWeb" {
				hasRetry = true
				break
			}
		}
		if !hasRetry {
			ordered = append(ordered, "retryWeb")
		}
	}
	if len(ordered) == 0 {
		return nil
	}
	inDomain := (domainIntents[intent] || containsAny(q, domainHints)) && !containsAny(q, outOfDomain)
	hasCards := len(cards) > 0
	if !inDomain && !hasCards {
		return nil
	}
	namedKeys := map[string]bool{
		"compareRegions": true, "priceHistory": true, "similarGames": true,
		"addFollow": true, "setAlert": true,
	}
	out := []map[string]any{}
	for _, k := range ordered {
		if namedKeys[k] {
			if name == "" {
				continue
			}
			out = append(out, map[string]any{"key": k, "args": map[string]any{"name": name}})
		} else {
			out = append(out, map[string]any{"key": k, "args": map[string]any{}})
		}
	}
	if len(out) > 4 {
		out = out[:4]
	}
	return out
}
