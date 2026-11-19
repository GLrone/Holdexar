package main

import (
	"encoding/json"

	"holdexar/server-go/internal/agent"
)

type pilotToolDef struct {
	name        string
	description string
	parameters  string
}

var pilotExtraToolDefs = []pilotToolDef{
	{
		name: "navigate",
		description: "跳转到用户想查看的模块页面。target 取值（用户说法 → target）：" +
			"仪表盘=dashboard、游戏商店=library、游戏库=gamelib、我的关注=follows、" +
			"捆绑包=bundles、价格提醒=alerts、活动日历=events、成就=achievements、" +
			"家庭=family、账单=bills、汇率=rates、工具箱=toolbox、任务=crawl、" +
			"网络=proxies、自动抓取=fetch、日志=logs、设置=settings、" +
			"某款游戏的详情页=target 传 game 且给 appid。" +
			"gamelib/bills/family 三个页面支持 section 定位到具体板块" +
			"（游戏库：owned 我的游戏/insights 游玩洞察/family 家庭库/play 游玩动态；" +
			"账单：ledger 消费总览/topup 充值/cdk 激活码；家庭：contrib 成员贡献/" +
			"heat 热度/buy 合购分析/wish 愿望单）。" +
			"仅当用户表达想查看/打开某模块时调用",
		parameters: `{"type":"object","properties":{"target":{"type":"string","description":"模块标识，取上方取值列表之一；游戏详情页传 game"},"section":{"type":"string","description":"可选板块键，仅 gamelib/bills/family 支持上方列出的取值"},"appid":{"type":"integer","description":"仅 target=game 时必填：游戏 AppID"}},"required":["target"]}`,
	},
	{
		name:        "present_ui",
		description: "自主选择标准 UI 组件呈现结构化分析结果（白名单：metric-grid, data-table, time-series, coverage-matrix, status-board）。当用户明确需要图表、矩阵或多维排查对比时使用。",
		parameters:  `{"type":"object","properties":{"component_id":{"type":"string","description":"注册组件唯一标识（如 'metric-grid', 'data-table', 'time-series', 'coverage-matrix', 'status-board'）"},"data":{"type":"object","description":"组件所需的结构化数据载荷"},"title":{"type":"string","description":"卡片标题（可选）"},"density":{"type":"string","enum":["compact","standard","expanded"],"description":"展示密度，默认为 standard"}},"required":["component_id","data"]}`,
	},
	{
		name: "read_attachment",
		description: "读取用户本轮附带的文本附件（对话里以【附件 N】引用形式给出）。" +
			"附件是长文本，按行号区间读取：start_line 从 1 开始，不传 end_line 读到" +
			"单次上限为止。结果里 end_line 小于你要的区间时表示被截断，" +
			"接着用 start_line=end_line+1 继续读。与附件相关的问题应先读再答",
		parameters: `{"type":"object","properties":{"id":{"type":"string","description":"附件 id（引用里的 id=…）"},"start_line":{"type":"integer","description":"起始行号（从 1 开始，默认 1）"},"end_line":{"type":"integer","description":"结束行号（含该行；缺省读到长度上限）"}},"required":["id"]}`,
	},
	{
		name: "find_deletables",
		description: "清理检查（只读）：扫描失效提醒（游戏已移除/从未抓到价格）与重复账单导入，" +
			"落一份带逐项解释的删除提议，用户确认后才真正删除。用户问『有什么可以清理』时调用",
		parameters: `{"type":"object","properties":{}}`,
	},
	{
		name: "propose_delete",
		description: "提交一份待用户确认的删除清单（本工具不执行删除）。用户明确要求删除某提醒/账单批次/取消关注时调用；" +
			"items 每项 {key, name, reason}，key 形如 alert:3 / bill_import:7 / follow:530（appids 来自只读工具结果）",
		parameters: `{"type":"object","properties":{"items":{"type":"array","items":{"type":"object","properties":{"key":{"type":"string"},"name":{"type":"string"},"reason":{"type":"string","enum":["removed","neverCrawled","noGame","dupImport","userAsk"]}},"required":["key","name","reason"]}}},"required":["items"]}`,
	},
	{
		name: "propose_bulk",
		description: "提交一份待用户确认的批量清单（本工具不执行任何写操作）。当用户要求对多款游戏" +
			"批量加关注或批量设提醒时调用：先用只读工具取到目标清单，再把 appid 数组交给" +
			"本工具，由用户在界面上确认后才会真正执行。一次最多 50 款",
		parameters: `{"type":"object","properties":{"action":{"type":"string","enum":["add_follow","create_price_alert"],"description":"批量动作：加关注 / 设提醒"},"appids":{"type":"array","items":{"type":"integer"},"description":"目标游戏 AppID 数组"},"target_type":{"type":"string","enum":["price","historic_low"],"description":"action=create_price_alert 时有效"},"target_value_yuan":{"type":"number","description":"target_type=price 时必填，人民币元"}},"required":["action","appids"]}`,
	},
	{
		name: "start_task",
		description: "发起一个耗时后台任务，受理后立即返回、不阻塞回答。" +
			"kind 可取 price_refresh（全部价格刷新）、price_repair（补抓缺价/失败的游戏）、" +
			"price_missing（按欠账账本批量补抓，冷却 24h）。" +
			"price_repair 支持可选 params：regions（地区代码，只补这些区）、" +
			"appids（对象列表）、cooldown_minutes（冷却覆盖分钟数，默认 0 立即补）。" +
			"仅当用户明确要求刷新 / 补价格时调用；发起后告知可问进度或让它停下",
		parameters: `{"type":"object","properties":{"kind":{"type":"string","enum":["price_refresh","price_repair","price_missing"],"description":"任务类型"},"params":{"type":"object","description":"任务参数（白名单字段，未支持的任务忽略）","properties":{"regions":{"type":"array","items":{"type":"string"},"description":"地区代码列表（如 cn/us/ru）"},"appids":{"type":"array","items":{"type":"integer"},"description":"对象 appid 列表（≤300）"},"cooldown_minutes":{"type":"integer","description":"补抓冷却覆盖（分钟）"}}}},"required":["kind"]}`,
	},
	{
		name:        "add_follow",
		description: "把某游戏加入用户关注（持续追踪价格）。仅当用户明确要求关注该一款游戏时调用；多款一起关注改用 propose_bulk",
		parameters:  `{"type":"object","properties":{"appid":{"type":"integer","description":"游戏 AppID"}},"required":["appid"]}`,
	},
	{
		name:        "create_price_alert",
		description: "为中国区创建价格提醒。仅当用户明确要求提醒这一款游戏时调用；用户给出具体价格用 price 类型，用户说史低提醒用 historic_low 类型；多款一起设提醒改用 propose_bulk",
		parameters:  `{"type":"object","properties":{"appid":{"type":"integer","description":"游戏 AppID"},"target_type":{"type":"string","enum":["price","historic_low"]},"target_value_yuan":{"type":"number","description":"price 类必填，人民币元"}},"required":["appid","target_type"]}`,
	},
	{
		name:        "update_price_alert",
		description: "修改已存在的价格提醒：改阈值或启停。用户说『把 XX 的提醒改到 N 块』『先别提醒 XX 了』时调用；alert_id 从提醒清单工具的结果里取",
		parameters:  `{"type":"object","properties":{"alert_id":{"type":"integer","description":"提醒规则 ID"},"target_value_yuan":{"type":"number","description":"新阈值（人民币元；只启停时省略）"},"active":{"type":"boolean","description":"true=启用 false=停用；只改阈值时省略"}},"required":["alert_id"]}`,
	},
	{
		name:        "bundle_follow",
		description: "关注或取关一个捆绑包。用户说『关注这个包』时调用；bundle_id 从捆绑包列表结果里取",
		parameters:  `{"type":"object","properties":{"bundle_id":{"type":"integer","description":"捆绑包 ID"},"follow":{"type":"boolean","description":"true=关注 false=取关，缺省关注"}},"required":["bundle_id"]}`,
	},
	{
		name:        "region_toggle",
		description: "启用或停用一个价格区服（下一轮价格周期生效）。用户说『把 XX 区关了』『加上 XX 区价格』时调用；只支持已有区服代码（如 us/ru/ua），新增区服需要开发者在设置里配置",
		parameters:  `{"type":"object","properties":{"region_code":{"type":"string","description":"区服代码（如 cn/us/ru）"},"enable":{"type":"boolean","description":"true=启用 false=停用"}},"required":["region_code","enable"]}`,
	},
	{
		name:        "retry_removed_game",
		description: "把已移除的游戏重新拉回目录并排队补抓。用户说『把 XX 加回来』『恢复 XX』时调用",
		parameters:  `{"type":"object","properties":{"appid":{"type":"integer","description":"游戏 AppID"}},"required":["appid"]}`,
	},
	{
		name:        "monitor_include",
		description: "解除某游戏的排除状态、恢复监控。用户说『XX 别排除了/恢复监控 XX』时调用",
		parameters:  `{"type":"object","properties":{"appid":{"type":"integer","description":"游戏 AppID"}},"required":["appid"]}`,
	},
	{
		name:        "acknowledge_update",
		description: "确认已查看本次更新成功报告，之后不再重复提示。仅在用户明确要求确认更新完成时调用；不会下载、安装或重启应用，先用 update_status 查看更新结果。",
		parameters:  `{"type":"object","properties":{},"additionalProperties":false}`,
	},
}

func agentToolSpecs() []map[string]any {
	shared, err := agent.SharedSpecs()
	if err != nil {
		return nil
	}
	defs := make([]pilotToolDef, 0, len(shared)+len(pilotExtraToolDefs))
	for _, s := range shared {
		defs = append(defs, pilotToolDef{
			name:        s.Name,
			description: s.Description,
			parameters:  s.ParametersJSON,
		})
	}
	defs = append(defs, pilotExtraToolDefs...)

	cardTools := agent.CardTools()
	out := make([]map[string]any, 0, len(defs))
	for _, def := range defs {
		var params map[string]any
		if json.Unmarshal([]byte(def.parameters), &params) != nil || params == nil {
			params = map[string]any{"type": "object", "properties": map[string]any{}}
		}
		if cardTools[def.name] {
			props, _ := params["properties"].(map[string]any)
			if props == nil {
				props = map[string]any{}
			}
			if _, exists := props["emit_card"]; !exists {
				props["emit_card"] = map[string]any{
					"type":        "boolean",
					"description": "是否渲染卡片（默认 true；中间核验传 false）",
				}
				params["properties"] = props
			}
		}
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
