//go:build windows

// Package pilot 领航员：确定性意图路由、多协议 LLM 编排与会话账本。
// 语义对齐 app/domains/pilot/*（intent/session/llm/config/attachments/service）。
package pilot

import "strings"

// 意图常量（内部运行信息，不进用户文案）。
const (
	IntentHowTo         = "how_to"
	IntentCreateAlert   = "create_alert"
	IntentAddMonitor    = "add_monitor"
	IntentPriceAnalysis = "price_analysis"
	IntentFindGames     = "find_games"
	IntentNavigate      = "navigate"
	IntentChat          = "chat"
)

var (
	howToWords = []string{"怎么设置", "怎么添加", "怎么加", "怎么弄", "如何设置", "如何添加", "在哪",
		"哪里设置", "怎么用", "教程", "怎么打开", "如何打开"}
	alertChannelWords = []string{"提醒", "告诉我"}
	alertCondWords    = []string{"低于", "以下", "史低", "跌到", "跌至", "降至", "打"}
	monitorWords      = []string{"加进关注", "加入关注", "关注一下", "帮我关注", "关注"}
	monitorBlockWords = []string{"值得关注"}
	priceWords        = []string{"价格", "史低", "最低价", "值不值", "值得买", "值得入手", "多少钱", "贵不",
		"几折", "折扣", "降价", "打折吗", "入手", "等等党", "还差多少"}
	findWords = []string{"推荐", "找一个", "找几", "找款", "类似", "有没有好玩", "特卖", "便宜的游戏",
		"好玩的游戏", "小众", "捡漏"}

	navVerbs = []string{"打开", "看看", "看一下", "看下", "跳到", "跳转", "切到", "切换到", "前往",
		"带我去", "进入", "回到", "返回", "查看", "逛逛", "我要看", "去"}
	navAliases = map[string][]string{
		"dashboard":    {"仪表盘", "首页", "主页", "总览"},
		"library":      {"游戏商店", "找游戏", "发现页"},
		"gamelib":      {"游戏库"},
		"follows":      {"我的关注", "关注列表", "监控池", "监控"},
		"bundles":      {"捆绑包", "礼包"},
		"alerts":       {"价格提醒", "提醒列表", "提醒"},
		"events":       {"活动日历", "活动"},
		"achievements": {"成就"},
		"family":       {"家庭共享", "家庭", "家人"},
		"bills":        {"账单", "消费记录"},
		"rates":        {"汇率"},
		"toolbox":      {"工具箱"},
		"crawl":        {"抓取任务", "任务"},
		"proxies":      {"代理池", "代理", "节点", "网络"},
		"fetch":        {"自动抓取"},
		"logs":         {"日志"},
		"settings":     {"设置", "配置"},
	}
	navBlockWords = []string{
		"怎么", "如何", "哪", "吗", "什么", "为什么", "为啥",
		"别去", "别打开", "不要去", "不要打开", "别跳", "先别",
		"有没有", "有哪些", "多少", "谁", "帮我", "顺便", "并且", "而且",
		"查查", "分析", "建议", "报错", "异常", "失败",
		"打折", "降价", "史低", "便宜", "好玩", "推荐", "配置", "教程",
		"能不能", "可不可以", "是不是",
		"改", "修改", "换", "添加", "同步", "清理", "重试", "刷新", "找", "查",
	}
	// GuardWords 守卫词表（唯一来源 agent.tools.policy；写工具 + 命中即拒）。
	GuardWords = []string{"清空", "删除", "移除", "移出", "停用", "停止", "批量", "全部", "所有"}
)

func containsAny(text string, words []string) bool {
	for _, w := range words {
		if strings.Contains(text, w) {
			return true
		}
	}
	return false
}

// IsGuarded 问句是否命中写工具守卫词。
func IsGuarded(question string) bool { return containsAny(question, GuardWords) }

func looksLikeAlert(q string) bool {
	return containsAny(q, alertChannelWords) && containsAny(q, alertCondWords)
}

// NavTarget 导航直通目标：导航动词与页面别名同时命中且无疑问/守卫形态。
func NavTarget(question string) string {
	q := strings.TrimSpace(question)
	if q == "" || len([]rune(q)) > 12 {
		return ""
	}
	if containsAny(q, GuardWords) || containsAny(q, navBlockWords) {
		return ""
	}
	if !containsAny(q, navVerbs) {
		return ""
	}
	for target, aliases := range navAliases {
		if containsAny(q, aliases) {
			return target
		}
	}
	return ""
}

// Route 确定性关键词分派；未命中交 LLM 自由问答。
func Route(question string) string {
	q := strings.TrimSpace(question)
	if q == "" {
		return IntentChat
	}
	guarded := IsGuarded(q)
	if !guarded && containsAny(q, howToWords) {
		return IntentHowTo
	}
	if NavTarget(q) != "" {
		return IntentNavigate
	}
	if !guarded && looksLikeAlert(q) {
		return IntentCreateAlert
	}
	if !guarded && containsAny(q, monitorWords) && !containsAny(q, monitorBlockWords) {
		return IntentAddMonitor
	}
	if containsAny(q, priceWords) {
		return IntentPriceAnalysis
	}
	if containsAny(q, findWords) {
		return IntentFindGames
	}
	return IntentChat
}
