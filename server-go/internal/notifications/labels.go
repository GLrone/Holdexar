package notifications

var typeLabels = []struct {
	key      string
	label    string
	category string
}{
	{"PRICE_DROP", "价格下降", "price"},
	{"PRICE_INCREASE", "价格上涨", "price"},
	{"PERMANENT_PRICE_CHANGE", "原价调整", "price"},
	{"NEW_HISTORICAL_LOW", "本地历史新低", "low"},
	{"HISTORICAL_LOW_MATCH", "追平本地历史低价", "low"},
	{"PRICE_RESTORED", "恢复购买", "availability"},
	{"PRICE_UNAVAILABLE", "暂不售", "availability"},
	{"REGION_LOCKED", "进入锁区", "availability"},
	{"REGION_UNLOCKED", "解除锁区", "availability"},
	{"FREE_PROMO", "限时免费", "lifecycle"},
	{"REMOVED", "游戏下架", "lifecycle"},
	{"RULE_PRICE", "指定价格", "rules"},
	{"RULE_PCT", "指定折扣", "rules"},
	{"RULE_LOW", "低价规则", "rules"},
	{"FEATURED", "精选与监控池折扣", "featured"},
	{"BUNDLE_DEAL", "捆绑包精选", "featured"},
	{"EPIC_FREE", "Epic 免费游戏", "content"},
	{"HB_CHOICE", "Humble Choice", "content"},
	{"PROXY_ALERT", "连接通道提醒", "system"},
	{"SYSTEM_ALERT", "系统运行提醒", "system"},
}

var categoryLabels = []struct {
	key        string
	label      string
	eventTypes int
}{
	{"price", "价格变化", 3},
	{"low", "历史低价", 2},
	{"availability", "可购买状态", 4},
	{"lifecycle", "免费与下架", 2},
	{"rules", "我的提醒", 3},
	{"featured", "价格精选", 2},
	{"content", "免费游戏与月包", 2},
	{"system", "运行提醒", 2},
}

var regionToCurrency = map[string]string{
	"CN": "CNY", "RU": "RUB", "KZ": "KZT", "UA": "UAH", "PK": "USD",
	"TR": "USD", "AR": "USD", "AZ": "USD", "VN": "VND", "ID": "IDR",
	"IN": "INR", "BR": "BRL", "CL": "CLP", "JP": "JPY", "HK": "HKD",
	"PH": "PHP", "TW": "TWD", "KW": "KWD", "SA": "SAR", "ZA": "ZAR",
	"QA": "QAR", "MY": "MYR", "TH": "THB", "PE": "PEN", "MX": "MXN",
	"SG": "SGD", "AE": "AED", "UY": "UYU", "CO": "COP", "KR": "KRW",
	"NZ": "NZD", "PL": "PLN", "CR": "CRC", "CA": "CAD", "US": "USD",
	"AU": "AUD", "PT": "EUR", "GB": "GBP", "NO": "NOK", "IL": "ILS",
	"CH": "CHF",
}

var eventLabels = map[string]string{
	"PRICE_DROP":             "价格下降",
	"PRICE_INCREASE":         "价格上涨",
	"NEW_HISTORICAL_LOW":     "历史新低",
	"HISTORICAL_LOW_MATCH":   "追平史低",
	"PERMANENT_PRICE_CHANGE": "原价调整",
	"REGION_LOCKED":          "进入锁区",
	"REGION_UNLOCKED":        "解除锁区",
	"PRICE_UNAVAILABLE":      "暂不售",
	"PRICE_RESTORED":         "恢复可购买",
	"FREE_PROMO":             "限时免费",
	"REMOVED":                "游戏下架",
}

func eventLabel(eventType string) string {
	if label, ok := eventLabels[eventType]; ok {
		return label
	}
	return "价格变化"
}
