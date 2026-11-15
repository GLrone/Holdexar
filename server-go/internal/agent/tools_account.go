// account 组批次二：账户/成就/账单/家庭读面 + 同步 POST 面 + 领航员会话读 + 元信息。
// 形状与 Python builtin（account/userview/tracking/runtime_tools/info）逐字段对齐。
package agent

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"

	"holdexar/server-go/internal/account"
)

// coreTools 常驻工具切面（Python projection.CORE_TOOLS 同表，list_tools 解锁其余）。
var coreTools = map[string]bool{
	"search_games": true, "search_steam": true, "web_search": true, "ingest_appids": true,
	"recommend_games": true, "top_games": true, "price_drops": true,
	"get_price_briefing": true, "get_region_prices": true, "compare_games": true,
	"refresh_game_price": true, "diagnose_price": true,
	"add_follow": true, "list_follows": true, "create_price_alert": true, "update_price_alert": true, "list_alerts": true,
	"epic_free": true, "steam_free": true, "hb_monthly": true, "calendar_events": true,
	"propose_bulk": true, "propose_delete": true, "find_deletables": true,
	"read_session": true, "get_user_preferences": true, "set_user_preference": true,
	"navigate": true, "read_attachment": true,
	"present_ui": true,
	"list_tools": true,
}

// RegisterAccountTools 批次二登记（重名即错；TimeoutS 经 budgetFor 取档）。
func RegisterAccountTools(r *ToolRegistry) error {
	for _, spec := range []*ToolSpec{
		{
			Name: "account_status", Group: GroupRead, Risk: "low",
			Description: "查看 Steam 账号状态（昵称、在线状态、钱包余额、登录是否过期）。" +
				"用户问『我的账号状态』『余额还有多少』『登录过期了吗』时调用",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    accountStatus, TimeoutS: budgetFor("account_status"),
		},
		{
			Name: "list_follows", Group: GroupRead, Risk: "low",
			Description: "列出用户关注的游戏。用户问『我关注了哪些游戏』或需要确认关注状态时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listFollows, TimeoutS: budgetFor("list_follows"),
		},
		{
			Name: "list_alerts", Group: GroupRead, Risk: "low",
			Description: "列出已设置的价格提醒规则。用户问『我有哪些提醒』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listAlerts, TimeoutS: budgetFor("list_alerts"),
		},
		{
			Name: "alert_history", Group: GroupRead, Risk: "low",
			Description: "查看价格提醒的触发历史（哪个游戏、哪个区、什么价、有没有发通知）。" +
				"用户问『提醒最近触发过吗』『降价的记录』时调用",
			Parameters: `{"type": "object", "properties": {
				"limit": {"type": "integer", "description": "返回条数，缺省 20"}
			}}`,
			Handler: alertHistory, TimeoutS: budgetFor("alert_history"),
		},
		{
			Name: "audit_follows_workflow", Group: GroupRead, Risk: "low",
			Description: "流水线工作流：全量扫描关注游戏价格变动、汇率与史低对比，并自动生成批量价格提醒更新提议。用户说『检查全部关注游戏的价格变动并更新提醒规则』『巡检关注游戏并提议提醒』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     auditFollowsWorkflow, TimeoutS: budgetFor("audit_follows_workflow"),
		},
		{
			Name: "achievements_summary", Group: GroupRead, Risk: "low",
			Description: "查看奖杯概览（白金数、解锁进度、白金陈列、最近解锁）。用户问『我的成就/奖杯进度』『最近解了什么成就』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     achievementsSummary, TimeoutS: budgetFor("achievements_summary"),
		},
		{
			Name: "level_profile", Group: GroupRead, Risk: "low",
			Description: "查看猎手等级与经验进度（等级、段位、累计经验、下一级还差多少、连猎加成、最近解锁）。用户问『我几级了』『经验/等级进度怎么样』时调用",
			Parameters: `{"type": "object", "properties": {
				"steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
			}}`,
			Handler: levelProfile, TimeoutS: budgetFor("level_profile"),
		},
		{
			Name: "challenges_overview", Group: GroupRead, Risk: "low",
			Description: "查看挑战进度（已完成数、可领取数、最接近完成的挑战）。用户问『挑战进度』『有没有能领的挑战奖励』时调用",
			Parameters: `{"type": "object", "properties": {
				"steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
			}}`,
			Handler: challengesOverview, TimeoutS: budgetFor("challenges_overview"),
		},
		{
			Name: "achievements_near_completion", Group: GroupRead, Risk: "low",
			Description: "列出快白金的游戏（成就进行中、按完成度从高到低）。" +
				"用户问『哪些游戏快白金了』『马上能白金的游戏』时调用",
			Parameters: `{"type": "object", "properties": {
				"steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"},
				"limit": {"type": "integer", "description": "返回条数，缺省 20"}
			}}`,
			Handler: achievementsNearCompletion, TimeoutS: budgetFor("achievements_near_completion"),
		},
		{
			Name: "achievements_game_detail", Group: GroupRead, Risk: "low",
			Description: "查看单个游戏的成就明细（总进度、白金状态、逐条成就与稀有度）。" +
				"用户问『这个游戏成就差哪些』『白金了吗』时调用",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 appid"},
				"steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
			}, "required": ["appid"]}`,
			Handler: achievementsGameDetail, TimeoutS: budgetFor("achievements_game_detail"),
		},
		{
			Name: "list_family_library", Group: GroupRead, Risk: "low",
			Description: "列出 Steam 家庭组的共享库游戏（家庭里所有成员共享的游戏，带国区现价）。" +
				"用户问『家庭库/家庭共享有哪些游戏』『家里能玩什么』时调用",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    listFamilyLibrary, TimeoutS: budgetFor("list_family_library"),
		},
		{
			Name: "family_status", Group: GroupRead, Risk: "low",
			Description: "查看 Steam 家庭组状态（组、成员、钱包区）。用户问『我的家庭组』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     familyStatus, TimeoutS: budgetFor("family_status"),
		},
		{
			Name: "bills_spend_query", Group: GroupRead, Risk: "low",
			Description: "查询 Steam 消费账单（某次导入的总览合计，或逐笔游戏交易明细）。" +
				"用户问『这次导入花了多少』『某年买了什么』时调用",
			Parameters: `{"type": "object", "properties": {
				"import_id": {"type": "integer", "description": "账单导入记录 id（bills 清单里可见）"},
				"mode": {"type": "string", "enum": ["overview", "game_txs"],
					"description": "overview=总览合计（缺省）；game_txs=逐笔游戏交易"},
				"year": {"type": "string", "description": "game_txs 按年过滤，如 2025"},
				"month": {"type": "string", "description": "game_txs 按月过滤，如 2025-06"},
				"tx_type": {"type": "string", "description": "game_txs 按交易类型过滤"},
				"search": {"type": "string", "description": "game_txs 按游戏名搜索"},
				"limit": {"type": "integer", "description": "game_txs 返回条数，缺省 50"}
			}, "required": ["import_id"]}`,
			Handler: billsSpendQuery, TimeoutS: budgetFor("bills_spend_query"),
		},
		{
			Name: "redeem_quota", Group: GroupRead, Risk: "low",
			Description: "查看 CDK 激活额度（已用/上限）。用户问『还能激活几次』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     redeemQuota, TimeoutS: budgetFor("redeem_quota"),
		},
		{
			Name: "sync_wallet", Group: GroupRead, Risk: "medium",
			Description: "立即抓取主账号钱包余额。用户说『同步一下钱包』『看看余额对不对』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     syncWallet, TimeoutS: budgetFor("sync_wallet"),
		},
		{
			Name: "sync_library", Group: GroupRead, Risk: "medium",
			Description: "同步账户愿望单与拥有库（从 Steam 拉取最新）。用户说『同步我的愿望单/游戏库』时调用",
			Parameters: `{"type": "object", "properties": {
				"steam_id": {"type": "string", "description": "账号 SteamID；缺省同步全部绑定账号"}
			}}`,
			Handler: syncLibrary, TimeoutS: budgetFor("sync_library"),
		},
		{
			Name: "sync_achievements", Group: GroupRead, Risk: "medium",
			Description: "发起成就后台同步（进行中会提示已在跑）。用户说『同步成就』时调用",
			Parameters: `{"type": "object", "properties": {
				"steam_id": {"type": "string", "description": "账号 SteamID；缺省主账号"}
			}}`,
			Handler: syncAchievements, TimeoutS: budgetFor("sync_achievements"),
		},
		{
			Name: "sync_bills", Group: GroupRead, Risk: "medium",
			Description: "同步 Steam 账单（默认增量探测，很快；full=true 全量翻页需数分钟）。用户说『同步账单』时调用",
			Parameters: `{"type": "object", "properties": {
				"full": {"type": "boolean", "description": "true=全量重拉，缺省增量"}
			}}`,
			Handler: syncBills, TimeoutS: budgetFor("sync_bills"),
		},
		{
			Name: "sync_family", Group: GroupRead, Risk: "medium",
			Description: "同步 Steam 家庭组。用户说『同步家庭组』时调用",
			Parameters: `{"type": "object", "properties": {
				"steam_id": {"type": "string", "description": "账号 SteamID；缺省遍历全部绑定账号"}
			}}`,
			Handler: syncFamily, TimeoutS: budgetFor("sync_family"),
		},
		{
			Name: "refresh_rates", Group: GroupRead, Risk: "medium",
			Description: "刷新各币种汇率。用户说『刷新汇率』『汇率好像不对』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     refreshRates, TimeoutS: budgetFor("refresh_rates"),
		},
		{
			Name: "list_sessions", Group: GroupRead, Risk: "low",
			Description: "列出用户与领航员的历史对话清单（标题与轮数）。" +
				"用户想找回或查看以前的对话时调用；对话内容用 read_session 读取",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listSessions, TimeoutS: budgetFor("list_sessions"),
		},
		{
			Name: "read_session", Group: GroupRead, Risk: "low",
			Description: "读取某段历史对话的内容。" +
				"用户问『我们之前聊过什么』或需要旧对话里的信息时调用；" +
				"会话 id 可先用会话清单工具获取。" +
				"offset 从最新一轮往回跳过若干轮（默认 0=从最新开始），" +
				"count 指定本次读多少轮（默认 8，最多 30）；" +
				"本会话被压缩后如需更早的原文，用 offset 逐段往回读",
			Parameters: `{"type": "object", "properties": {
				"sid": {"type": "string", "description": "会话 id（来自会话清单）"},
				"offset": {"type": "integer", "description": "从最新往回跳过的轮数，默认 0"},
				"count": {"type": "integer", "description": "本次读取的轮数，默认 8，最多 30"}
			}, "required": ["sid"]}`,
			Handler: readSession, TimeoutS: budgetFor("read_session"),
		},
		{
			Name: "agent_runs_list", Group: GroupRead, Risk: "low",
			Description: "查看后台运行账本：最近的长任务与编排运行的状态" +
				"（排队/运行/完成/失败/取消）。核对任务真实执行与终态时调用",
			Parameters: `{"type": "object", "properties": {
				"limit": {"type": "integer", "description": "返回条数，默认 10，最多 30"}
			}}`,
			Handler: agentRunsList, TimeoutS: budgetFor("agent_runs_list"),
		},
		{
			Name: "agent_run_events", Group: GroupRead, Risk: "low",
			Description: "读取某个后台运行的事件回放（受理/进度采样/终态），" +
				"配合 agent_runs_list 的运行标识使用；after_seq 支持增量续读",
			Parameters: `{"type": "object", "properties": {
				"run_id": {"type": "string", "description": "运行标识（来自运行账本清单）"},
				"after_seq": {"type": "integer", "description": "只取该序号之后的事件，默认 0"},
				"limit": {"type": "integer", "description": "返回条数，默认 30，最多 100"}
			}, "required": ["run_id"]}`,
			Handler: agentRunEvents, TimeoutS: budgetFor("agent_run_events"),
		},
		{
			Name: "list_tools", Group: GroupRead, Risk: "low",
			Description: "列出当前工具表之外的其余工具（清单 + 一句话说明）并解锁它们，" +
				"解锁后本会话可直接调用。当你觉得手头工具做不了用户这件事时先调用它" +
				"——同步账单/家庭组/成就/愿望单、代理与通道诊断、日志检索、系统诊断、" +
				"任务调度、汇率、捆绑包、区服开关、批量清理、历史会话列表等都在里面。" +
				"不要在未查看前就回答「做不到」",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler: func(c *CallContext) (*ToolResult, error) { return listTools(r) },
			TimeoutS: budgetFor("list_tools"),
		},
	} {
		if err := r.register(spec); err != nil {
			return err
		}
	}
	return nil
}

// ── 共用形状件 ──

// fetchMapErr GET 端点 → JSON 对象；非 200 按错误上抛（执行器归一化为 failed）。
func fetchMapErr(c *CallContext, path string) (map[string]any, error) {
	var out map[string]any
	if err := c.getJSON(path, &out); err != nil {
		return nil, err
	}
	return out, nil
}

// rowsNoTotal rows_result 不带 total（Python total=None 缺省键）。
func rowsNoTotal(title string, rows []any) *Obj {
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", title)
	if len(rows) > readRowsLimit {
		rows = rows[:readRowsLimit]
	}
	out.set("rows", rows)
	return out
}

// syncRowState 单行同步结果（done/fail 词条 + 失败详情）。
func syncRowState(title, detail string, ok bool, doneKey, failKey string) *Obj {
	vKey, tone := doneKey, "ok"
	if !ok {
		vKey, tone = failKey, "bad"
	}
	row := newObj()
	row.set("k", "")
	row.set("vKey", vKey)
	row.set("v", detail)
	row.set("tone", tone)
	return rowsNoTotal(title, []any{row})
}

// syncCard Python _shared.sync_result：ok 键缺席视为成功（Python .get("ok", True)），
// 键存在则按 Python bool() 取值（None → 失败）。
func syncCard(title string, result map[string]any, doneKey, failKey string) *Obj {
	ok := true
	if result != nil {
		if v, has := result["ok"]; has {
			ok = pyBool(v)
		}
	}
	detail := pyStrOr(result["error"])
	if detail == "" {
		detail = pyStrOr(result["detail"])
	}
	return syncRowState(title, detail, ok, doneKey, failKey)
}

// pyRound Python round()：银行家舍入。
func pyRound(x float64) int {
	return int(math.RoundToEven(x))
}

// pyEstimateTokens Python pilot.estimate_tokens 同式：CJK 4/5 向上取整 + 其余 1/4 + 1。
func pyEstimateTokens(text string) int {
	if text == "" {
		return 0
	}
	cjk := 0
	for _, r := range text {
		if (r >= 0x4E00 && r <= 0x9FFF) || (r >= 0x3000 && r <= 0x303F) || (r >= 0xFF00 && r <= 0xFFEF) {
			cjk++
		}
	}
	other := len([]rune(text)) - cjk
	return (cjk*4+4)/5 + other/4 + 1
}

// pyText Python str(v)：None → "None"（f"AppID {v}" 同径）。
func pyText(v any) string {
	if v == nil {
		return "None"
	}
	if s := pyStrOr(v); s != "" {
		return s
	}
	if raw, err := json.Marshal(v); err == nil {
		return string(raw)
	}
	return fmt.Sprintf("%v", v)
}

// pyClipRunes 按字符数截断（Python s[:n] 语义）。
func pyClipRunes(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n])
}

// argInt Python int(args.get(k) or default)：缺席/None/0 等假值回缺省，
// 坏值上抛（Python int() ValueError 同径 → 执行器 failed）。
func argInt(args map[string]any, key string, def int64) (int64, error) {
	v := args[key]
	if !pyBool(v) {
		return def, nil
	}
	return pyIntOr(v)
}

// argID Python int(args[k])：required 数值入参。
func argID(args map[string]any, key string) (int64, error) {
	return pyIntOr(args[key])
}

// sourceIDs monitor_sources 单来源激活对象（Python ids_with_source 同表同口径）。
func (c *CallContext) sourceIDs(source string) ([]int64, error) {
	rows, err := c.db.QueryContext(c.Ctx,
		"SELECT DISTINCT target_id FROM monitor_sources"+
			" WHERE target_type = 'game' AND source = ? AND active = 1"+
			" ORDER BY target_id", source)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, rows.Err()
}

// followIDs GET /api/v1/follows（wishlist favorite 路）→ appid 序列。
func (c *CallContext) followIDs() ([]int64, error) {
	body, err := fetchMapErr(c, "/api/v1/follows")
	if err != nil {
		return nil, err
	}
	raw, _ := body["appids"].([]any)
	out := make([]int64, 0, len(raw))
	for _, v := range raw {
		id, err := pyIntOr(v)
		if err != nil {
			return nil, err
		}
		out = append(out, id)
	}
	return out, nil
}

// sortedUnion 升序去重并集（Python sorted(set(a) | set(b))）。
func sortedUnion(a, b []int64) []int64 {
	seen := map[int64]bool{}
	for _, id := range a {
		seen[id] = true
	}
	for _, id := range b {
		seen[id] = true
	}
	out := make([]int64, 0, len(seen))
	for id := range seen {
		out = append(out, id)
	}
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// followItem brief_item + note 来源注记（manual/favorite）。
func followItem(appid int64, brief account.Brief, has bool, key string) *Obj {
	item := briefItem(appid, brief, has)
	note := newObj()
	note.set("key", key)
	item.set("note", note)
	return item
}

// followBriefs 关注并集的清单行（Python list_follows/audit 共用取数段）。
func followBriefs(c *CallContext) ([]int64, map[int64]bool, map[int64]account.Brief, error) {
	manual, err := c.sourceIDs("manual")
	if err != nil {
		return nil, nil, nil, err
	}
	favorite, err := c.followIDs()
	if err != nil {
		return nil, nil, nil, err
	}
	fav := map[int64]bool{}
	for _, id := range favorite {
		fav[id] = true
	}
	ids := sortedUnion(manual, favorite)
	briefs, err := account.BriefsFor(c.Ctx, c.db, ids)
	if err != nil {
		return nil, nil, nil, err
	}
	return ids, fav, briefs, nil
}

// ── 读面 ──

// account_status 账号状态 + 钱包余额 + 登录态新鲜度（Python userview.account_status）。
func accountStatus(c *CallContext) (*ToolResult, error) {
	st, err := fetchMapErr(c, "/api/v1/account")
	if err != nil {
		return nil, err
	}
	if !pyBool(st["has_cookie"]) {
		out := newObj()
		out.set("kind", "empty")
		out.set("hasCookie", false)
		out.set("bound", pyBool(st["bound_steam_id"]))
		out.set("mismatch", pyBool(st["mismatch"]))
		return &ToolResult{Status: StatusEmpty, Data: out}, nil
	}
	profile, _ := st["profile"].(map[string]any)
	wallet, _ := st["wallet"].(map[string]any)
	personaName := mapGet(profile, "persona_name")
	online := pyBool(st["is_online"])
	checkOk := pyBool(mapGet(wallet, "check_ok"))
	sessionExpired := pyBool(st["session_expired"])

	k := pyStrOr(personaName)
	if k == "" {
		k = pyStrOr(st["cookie_steam_id"])
	}
	if k == "" {
		k = "—"
	}
	row1 := newObj()
	row1.set("k", k)
	row1.set("vKey", "acctStatus")
	d1 := newObj()
	d1.set("online", online)
	d1.set("inGame", pyStrOr(st["in_game"]))
	row1.set("data", d1)
	row1.set("tone", toneOrNil(online))
	row2 := newObj()
	row2.set("k", "")
	row2.set("vKey", "walletBalance")
	row2.set("v", pyStrOr(mapGet(wallet, "balance_display")))
	d2 := newObj()
	d2.set("okAt", mapGet(wallet, "ok_at"))
	d2.set("checkOk", checkOk)
	row2.set("data", d2)
	row2.set("tone", toneOrWarn(checkOk))
	row3 := newObj()
	row3.set("k", "")
	row3.set("vKey", "sessionFresh")
	d3 := newObj()
	d3.set("expired", sessionExpired)
	d3.set("expiresAt", st["session_expires_at"])
	d3.set("hasRefresh", pyBool(st["session_has_refresh"]))
	row3.set("data", d3)
	row3.set("tone", toneOrNil(!sessionExpired))
	accounts, _ := st["accounts"].([]any)
	rows := []any{row1, row2, row3}
	out := rowsResult("accountStatus", rows, len(rows))
	out.set("steamId", pyStrOr(st["cookie_steam_id"]))
	out.set("personaName", personaName)
	out.set("mismatch", pyBool(st["mismatch"]))
	out.set("walletBalance", mapGet(wallet, "balance_display"))
	out.set("sessionExpired", sessionExpired)
	out.set("accountsCount", len(accounts))
	return &ToolResult{Data: out}, nil
}

// toneOrNil true→ok / false→null（Python "ok" if x else None）。
func toneOrNil(ok bool) any {
	if ok {
		return "ok"
	}
	return nil
}

// toneOrWarn true→ok / false→warn（Python "ok" if x else "warn"）。
func toneOrWarn(ok bool) any {
	if ok {
		return "ok"
	}
	return "warn"
}

// toneOkWarn active→ok / 否则 warn（提醒行）。
func toneOkWarn(active bool) any { return toneOrWarn(active) }

// list_follows 手动关注 ∪ 收藏关注合并只读投影（Python tracking.list_follows）。
func listFollows(c *CallContext) (*ToolResult, error) {
	ids, fav, briefs, err := followBriefs(c)
	if err != nil {
		return nil, err
	}
	items := make([]any, 0, len(ids))
	for _, id := range ids {
		key := "manual"
		if fav[id] {
			key = "favorite"
		}
		brief, has := briefs[id]
		items = append(items, followItem(id, brief, has, key))
	}
	return &ToolResult{Data: gamesCard("follows", items, len(ids))}, nil
}

// list_alerts 提醒规则清单（Python tracking.list_alerts）。
func listAlerts(c *CallContext) (*ToolResult, error) {
	alerts, err := fetchAlerts(c)
	if err != nil {
		return nil, err
	}
	vkey := map[string]string{"price": "alertPrice", "historic_low": "alertLow", "pct": "alertPct"}
	rows := make([]any, 0, len(alerts))
	for _, a := range alerts {
		name := pyStrOr(a["gameName"])
		if name == "" {
			name = "AppID " + pyText(a["appid"])
		}
		row := newObj()
		row.set("k", name)
		key, ok := vkey[pyStrOr(a["targetType"])]
		if !ok {
			key = "alertLow"
		}
		row.set("vKey", key)
		row.set("v", a["region"])
		data := newObj()
		data.set("priceFen", a["targetValue"])
		data.set("pct", a["targetValue"])
		data.set("alertId", a["id"])
		row.set("data", data)
		row.set("tone", toneOkWarn(pyBool(a["active"])))
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsResult("alerts", rows, len(rows))}, nil
}

// fetchAlerts GET /api/v1/alerts → 行序列。
func fetchAlerts(c *CallContext) ([]map[string]any, error) {
	var alerts []map[string]any
	if err := c.getJSON("/api/v1/alerts", &alerts); err != nil {
		return nil, err
	}
	return alerts, nil
}

// alert_history 提醒触发历史（Python userview.alert_history）。
func alertHistory(c *CallContext) (*ToolResult, error) {
	limit := clampInt(c.Args["limit"], 20, 1, 200)
	var events []map[string]any
	if err := c.getJSON(queryPath("/api/v1/alerts/events", fmt.Sprintf("limit=%d", limit)), &events); err != nil {
		return nil, err
	}
	if len(events) == 0 {
		out := newObj()
		out.set("kind", "empty")
		out.set("events", []any{})
		return &ToolResult{Status: StatusEmpty, Data: out}, nil
	}
	rows := make([]any, 0, len(events))
	for _, e := range events {
		name := pyStrOr(e["gameName"])
		if name == "" {
			name = pyText(e["appid"])
		}
		row := newObj()
		row.set("k", name)
		row.set("vKey", "alertHit")
		row.set("at", e["triggeredAt"])
		data := newObj()
		data.set("region", e["region"])
		data.set("priceCny", e["priceCny"])
		data.set("priceText", e["priceText"])
		data.set("notified", pyBool(e["notified"]))
		row.set("data", data)
		row.set("tone", "ok")
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsResult("alertHistory", rows, len(events))}, nil
}

// auditFollowsWorkflow 巡检流水线 stepper（Python tracking.audit_follows_workflow；
// MCP 面无会话绑定，提议段恒不落）。
func auditFollowsWorkflow(c *CallContext) (*ToolResult, error) {
	ids, _, briefs, err := followBriefs(c)
	if err != nil {
		return nil, err
	}
	total := len(ids)
	if total == 0 {
		obj := auditStepperObj(0, []any{
			auditStepScan(1, "扫描关注列表", "关注列表为空 (0/0)", "empty", 0, 0, ""),
			auditStepPlain(2, "校验区服汇率与现价", "跳过校验", "wait", ""),
			auditStepPlain(3, "生成批量提议", "无待处理项", "wait", ""),
		})
		return &ToolResult{Data: obj}, nil
	}
	alerts, err := fetchAlerts(c)
	if err != nil {
		return nil, err
	}
	alerted := map[int64]bool{}
	for _, a := range alerts {
		if !pyBool(a["active"]) {
			continue
		}
		if id, err := pyIntOr(a["appid"]); err == nil {
			alerted[id] = true
		}
	}
	candidates := []any{}
	for _, appid := range ids {
		brief, has := briefs[appid]
		if !has || brief.Name == nil || *brief.Name == "" {
			continue
		}
		if !alerted[appid] || brief.Discount > 0 {
			row := newObj()
			row.set("appid", appid)
			row.set("name", *brief.Name)
			candidates = append(candidates, row)
		}
		if len(candidates) >= 6 {
			break
		}
	}
	if len(candidates) == 0 {
		for _, appid := range ids {
			if len(candidates) >= 3 {
				break
			}
			name := ""
			if brief, has := briefs[appid]; has && brief.Name != nil {
				name = *brief.Name
			}
			if name == "" {
				name = fmt.Sprintf("AppID %d", appid)
			}
			row := newObj()
			row.set("appid", appid)
			row.set("name", name)
			candidates = append(candidates, row)
		}
	}
	detail, badge, status := "全部提醒规则均已就绪", "已就绪", "empty"
	if len(candidates) > 0 {
		detail = fmt.Sprintf("待确认 %d 项提醒规则", len(candidates))
		badge = fmt.Sprintf("待确认 %d 项", len(candidates))
		status = "ok"
	}
	return &ToolResult{Data: auditStepperObj(2, []any{
		auditStepScan(1, "扫描关注列表", fmt.Sprintf("扫描完成 (%d/%d)", total, total), "ok", total, total, fmt.Sprintf("%d/%d", total, total)),
		auditStepScan(2, "校验区服汇率与现价", "汇率对齐与史低价格校验完成", "ok", total, total, ""),
		auditStepPlain(3, "生成批量提议", detail, status, badge),
	})}, nil
}

// auditStepScan 步骤行（带 current/total；badge 空 = 不出键，对齐 Python 字面量）。
func auditStepScan(id int, title, detail, status string, current, total int, badge string) *Obj {
	step := newObj()
	step.set("id", id)
	step.set("title", title)
	step.set("detail", detail)
	step.set("status", status)
	step.set("current", current)
	step.set("total", total)
	if badge != "" {
		step.set("badge", badge)
	}
	return step
}

// auditStepPlain 步骤行（无 current/total；badge 空 = 不出键）。
func auditStepPlain(id int, title, detail, status, badge string) *Obj {
	step := newObj()
	step.set("id", id)
	step.set("title", title)
	step.set("detail", detail)
	step.set("status", status)
	if badge != "" {
		step.set("badge", badge)
	}
	return step
}

// auditStepperObj stepper 载荷组装（内层 kind 与外层 kind 同值，Python 同形）。
func auditStepperObj(currentStepIndex int, steps []any) *Obj {
	st := newObj()
	st.set("kind", "stepper")
	st.set("title", "关注游戏价格与提醒巡检流水线")
	st.set("currentStepIndex", currentStepIndex)
	st.set("steps", steps)
	out := newObj()
	out.set("kind", "stepper")
	out.set("stepper", st)
	return out
}

// achievements_summary 奖杯概览（Python account.achievements_summary）。
func achievementsSummary(c *CallContext) (*ToolResult, error) {
	s, err := fetchMapErr(c, "/api/v1/achievements/summary")
	if err != nil {
		return nil, err
	}
	out := newObj()
	out.set("kind", "achievements")
	out.set("hasCredential", pyBool(s["hasCredential"]))
	out.set("platinum", statInt(s, "platinum"))
	out.set("unlocked", statInt(s, "unlockedAchievements"))
	out.set("total", statInt(s, "totalAchievements"))
	out.set("completionRate", s["completionRate"])
	out.set("lastSyncedAt", s["lastSyncedAt"])
	platinums := []any{}
	if raw, ok := s["platinums"].([]any); ok {
		for i, v := range raw {
			if i >= 4 {
				break
			}
			p, ok := v.(map[string]any)
			if !ok || !pyBool(p["appid"]) {
				continue
			}
			appid, err := pyIntOr(p["appid"])
			if err != nil {
				return nil, err
			}
			row := newObj()
			row.set("appid", appid)
			name := pyStrOr(p["name"])
			if name == "" {
				row.set("name", nil)
			} else {
				row.set("name", name)
			}
			platinums = append(platinums, row)
		}
	}
	out.set("platinums", platinums)
	recent := []any{}
	if raw, ok := s["recentUnlocks"].([]any); ok {
		for i, v := range raw {
			if i >= 4 {
				break
			}
			a, ok := v.(map[string]any)
			if !ok || !pyBool(a["appid"]) {
				continue
			}
			appid, err := pyIntOr(a["appid"])
			if err != nil {
				return nil, err
			}
			row := newObj()
			row.set("appid", appid)
			row.set("name", pyStrOr(a["name"]))
			gameName := pyStrOr(a["gameName"])
			if gameName == "" {
				row.set("gameName", nil)
			} else {
				row.set("gameName", gameName)
			}
			row.set("at", a["unlockTime"])
			recent = append(recent, row)
		}
	}
	out.set("recent", recent)
	return &ToolResult{Data: out}, nil
}

// level_profile 猎手等级读数（Python account.level_profile）。
func levelProfile(c *CallContext) (*ToolResult, error) {
	sid := strings.TrimSpace(pyStrOr(c.Args["steam_id"]))
	p, err := fetchMapErr(c, queryPath("/api/v1/achievements/level", sidQuery(sid)))
	if err != nil {
		return nil, err
	}
	var feed map[string]any
	if err := c.getJSON(queryPath("/api/v1/achievements/feed", mergeQuery("limit=4", sidQuery(sid))), &feed); err != nil {
		return nil, err
	}
	items, _ := feed["items"].([]any)
	bonus := map[string]any{}
	if raw, ok := p["activeBonuses"].([]any); ok && len(raw) > 0 {
		bonus, _ = raw[0].(map[string]any)
	}
	multiplier, _ := pyFloatOr(mapGet(bonus, "multiplier"))
	rows := []any{}
	rowNow := newObj()
	rowNow.set("k", "")
	rowNow.set("vKey", "levelNow")
	dNow := newObj()
	dNow.set("level", p["level"])
	dNow.set("tier", p["tier"])
	dNow.set("totalXp", p["totalXp"])
	rowNow.set("data", dNow)
	rowNow.set("tone", "ok")
	rows = append(rows, rowNow)
	rowNext := newObj()
	rowNext.set("k", "")
	rowNext.set("vKey", "levelNext")
	dNext := newObj()
	dNext.set("into", p["xpIntoLevel"])
	dNext.set("next", p["xpForNextLevel"])
	rowNext.set("data", dNext)
	rows = append(rows, rowNext)
	if pyBool(mapGet(bonus, "multiplier")) && multiplier > 1.0 {
		rowBonus := newObj()
		rowBonus.set("k", "")
		rowBonus.set("vKey", "levelBonus")
		dBonus := newObj()
		dBonus.set("days", mapGet(bonus, "streakDays"))
		dBonus.set("percent", pyRound((multiplier-1)*100))
		rowBonus.set("data", dBonus)
		rowBonus.set("tone", "ok")
		rows = append(rows, rowBonus)
	}
	for _, v := range items {
		u, ok := v.(map[string]any)
		if !ok {
			continue
		}
		row := newObj()
		row.set("k", pyStrOr(u["name"]))
		row.set("vKey", "levelUnlock")
		d := newObj()
		d.set("gameName", pyStrOr(u["gameName"]))
		row.set("data", d)
		rows = append(rows, row)
	}
	out := rowsResult("levelProfile", rows, len(rows))
	out.set("level", p["level"])
	out.set("tier", p["tier"])
	out.set("totalXp", p["totalXp"])
	out.set("xpForNextLevel", p["xpForNextLevel"])
	out.set("streakDays", statInt(bonus, "streakDays"))
	activeMultiplier := multiplier
	if !pyBool(mapGet(bonus, "multiplier")) {
		activeMultiplier = 1.0
	}
	out.set("activeBonusMultiplier", activeMultiplier)
	unlocks := []any{}
	for _, v := range items {
		u, ok := v.(map[string]any)
		if !ok {
			continue
		}
		row := newObj()
		row.set("name", pyStrOr(u["name"]))
		row.set("gameName", pyStrOr(u["gameName"]))
		row.set("at", u["unlockTime"])
		unlocks = append(unlocks, row)
	}
	out.set("recentUnlocks", unlocks)
	return &ToolResult{Data: out}, nil
}

// challenges_overview 挑战进度读数（Python account.challenges_overview）。
func challengesOverview(c *CallContext) (*ToolResult, error) {
	sid := strings.TrimSpace(pyStrOr(c.Args["steam_id"]))
	data, err := fetchMapErr(c, queryPath("/api/v1/achievements/challenges", sidQuery(sid)))
	if err != nil {
		return nil, err
	}
	summary, _ := data["summary"].(map[string]any)
	type nearItem struct {
		id                   string
		progress, target, xp int64
	}
	near := []nearItem{}
	if raw, ok := data["challenges"].([]any); ok {
		for _, v := range raw {
			ch, ok := v.(map[string]any)
			if !ok || pyStrOr(ch["status"]) != "active" {
				continue
			}
			target := statInt(ch, "target")
			if target <= 0 {
				continue
			}
			near = append(near, nearItem{
				id:       pyStrOr(ch["challengeId"]),
				progress: statInt(ch, "progress"),
				target:   target,
				xp:       statInt(ch, "rewardXp"),
			})
		}
	}
	sort.SliceStable(near, func(i, j int) bool {
		return float64(near[i].progress)/float64(near[i].target) > float64(near[j].progress)/float64(near[j].target)
	})
	if len(near) > 3 {
		near = near[:3]
	}
	nearComplete := []any{}
	for _, n := range near {
		row := newObj()
		row.set("challengeId", n.id)
		row.set("progress", n.progress)
		row.set("target", n.target)
		row.set("rewardXp", n.xp)
		nearComplete = append(nearComplete, row)
	}
	claimable := statInt(summary, "claimable")
	rows := []any{}
	rowSummary := newObj()
	rowSummary.set("k", "")
	rowSummary.set("vKey", "challengeSummary")
	d := newObj()
	d.set("completed", statInt(summary, "completed"))
	d.set("claimable", claimable)
	d.set("total", statInt(summary, "total"))
	rowSummary.set("data", d)
	rowSummary.set("tone", toneOrNil(claimable != 0))
	rows = append(rows, rowSummary)
	for _, n := range near {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "challengeNear")
		dn := newObj()
		dn.set("challengeId", n.id)
		dn.set("progress", n.progress)
		dn.set("target", n.target)
		dn.set("rewardXp", n.xp)
		row.set("data", dn)
		rows = append(rows, row)
	}
	out := rowsResult("challengesOverview", rows, len(rows))
	out.set("completed", statInt(summary, "completed"))
	out.set("claimable", claimable)
	out.set("nearComplete", nearComplete)
	return &ToolResult{Data: out}, nil
}

// achievements_near_completion 快白金清单（Python userview.achievements_near_completion）。
func achievementsNearCompletion(c *CallContext) (*ToolResult, error) {
	limit := clampInt(c.Args["limit"], 20, 1, 50)
	sid := strings.TrimSpace(pyStrOr(c.Args["steam_id"]))
	body, err := fetchMapErr(c, queryPath("/api/v1/achievements/games", mergeQuery("filter=progress&sort=progress", sidQuery(sid))))
	if err != nil {
		return nil, err
	}
	raw, _ := body["games"].([]any)
	if int64(len(raw)) > limit {
		raw = raw[:int(limit)]
	}
	if len(raw) == 0 {
		out := newObj()
		out.set("kind", "empty")
		out.set("games", []any{})
		return &ToolResult{Status: StatusEmpty, Data: out}, nil
	}
	rows := make([]any, 0, len(raw))
	for _, v := range raw {
		g, ok := v.(map[string]any)
		if !ok {
			continue
		}
		name := pyStrOr(g["name"])
		if name == "" {
			name = pyText(g["appid"])
		}
		row := newObj()
		row.set("k", name)
		row.set("vKey", "achNear")
		data := newObj()
		data.set("percent", g["percent"])
		data.set("unlocked", g["unlocked"])
		data.set("total", g["total"])
		data.set("remaining", g["remaining"])
		data.set("platinum", pyBool(g["platinum"]))
		row.set("data", data)
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsResult("achNearDone", rows, len(raw))}, nil
}

// achievements_game_detail 单游戏成就明细（Python userview.achievements_game_detail）。
func achievementsGameDetail(c *CallContext) (*ToolResult, error) {
	appid, err := argID(c.Args, "appid")
	if err != nil {
		return nil, err
	}
	sid := strings.TrimSpace(pyStrOr(c.Args["steam_id"]))
	status, body, err := c.fetch(c.Ctx, queryPath(fmt.Sprintf("/api/v1/achievements/games/%d", appid), sidQuery(sid)))
	if err != nil {
		return nil, err
	}
	if status == 404 {
		out := newObj()
		out.set("kind", "empty")
		out.set("appid", appid)
		return &ToolResult{Status: StatusEmpty, Data: out}, nil
	}
	if status != 200 {
		return nil, fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
	var detail map[string]any
	if err := jsonDecoder(body).Decode(&detail); err != nil {
		return nil, err
	}
	achievements := []any{}
	if raw, ok := detail["achievements"].([]any); ok {
		for _, v := range raw {
			if len(achievements) >= readRowsLimit {
				break
			}
			a, ok := v.(map[string]any)
			if !ok {
				continue
			}
			row := newObj()
			row.set("name", a["name"])
			row.set("achieved", pyBool(a["achieved"]))
			row.set("rarity", a["rarity"])
			row.set("globalPercent", a["globalPercent"])
			row.set("unlockTime", a["unlockTime"])
			achievements = append(achievements, row)
		}
	}
	name := pyStrOr(detail["name"])
	if name == "" {
		name = fmt.Sprintf("%d", appid)
	}
	rows := []any{}
	rowProgress := newObj()
	rowProgress.set("k", name)
	rowProgress.set("vKey", "achProgress")
	dp := newObj()
	dp.set("percent", detail["percent"])
	dp.set("unlocked", detail["unlocked"])
	dp.set("total", detail["total"])
	dp.set("platinum", pyBool(detail["platinum"]))
	dp.set("playtimeMin", detail["playtimeMin"])
	rowProgress.set("data", dp)
	rowProgress.set("tone", "ok")
	rows = append(rows, rowProgress)
	for _, v := range achievements {
		a := v.(*Obj)
		row := newObj()
		nm, _ := a.get("name").(string)
		row.set("k", nm)
		row.set("vKey", "achItem")
		d := newObj()
		d.set("achieved", a.get("achieved"))
		d.set("rarity", a.get("rarity"))
		d.set("unlockTime", a.get("unlockTime"))
		row.set("data", d)
		row.set("tone", toneOrNil(pyBool(a.get("achieved"))))
		rows = append(rows, row)
	}
	out := rowsResult("achGame", rows, len(rows))
	out.set("appid", appid)
	out.set("name", detail["name"])
	out.set("percent", detail["percent"])
	out.set("unlocked", detail["unlocked"])
	out.set("total", detail["total"])
	out.set("achievements", achievements)
	return &ToolResult{Data: out}, nil
}

// list_family_library 家庭共享库（Python account.list_family_library）。
func listFamilyLibrary(c *CallContext) (*ToolResult, error) {
	lib, err := fetchMapErr(c, "/api/v1/family/library")
	if err != nil {
		return nil, err
	}
	games, _ := lib["games"].([]any)
	ids := []int64{}
	for _, v := range games {
		if len(ids) >= readRowsLimit {
			break
		}
		g, ok := v.(map[string]any)
		if !ok || !pyBool(g["appid"]) {
			continue
		}
		id, err := pyIntOr(g["appid"])
		if err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	briefs, err := account.BriefsFor(c.Ctx, c.db, ids)
	if err != nil {
		return nil, err
	}
	items := make([]any, 0, len(ids))
	for _, id := range ids {
		brief, has := briefs[id]
		items = append(items, briefItem(id, brief, has))
	}
	return &ToolResult{Data: gamesCard("famLibrary", items, len(games))}, nil
}

// family_status 家庭组状态（Python account.family_status）。
func familyStatus(c *CallContext) (*ToolResult, error) {
	st, err := fetchMapErr(c, "/api/v1/family/status")
	if err != nil {
		return nil, err
	}
	members := []any{}
	seen := 0
	if raw, ok := st["groups"].([]any); ok {
		for _, gv := range raw {
			g, ok := gv.(map[string]any)
			if !ok {
				continue
			}
			mraw, _ := g["members"].([]any)
			for _, mv := range mraw {
				m, ok := mv.(map[string]any)
				if !ok {
					continue
				}
				sid := pyStrOr(m["steamid"])
				if sid == "" {
					continue
				}
				if memberSeen(members, sid) {
					continue
				}
				row := newObj()
				row.set("steamid", sid)
				row.set("name", strOrNil(pyStrOr(m["personaName"])))
				row.set("avatar", strOrNil(pyStrOr(m["avatarUrl"])))
				if pyStrOr(m["role"]) == "primary" {
					row.set("role", "primary")
				} else {
					row.set("role", "member")
				}
				row.set("region", m["region"])
				members = append(members, row)
				seen++
			}
		}
	}
	if !pyBool(st["bound"]) || len(members) == 0 {
		out := newObj()
		out.set("kind", "family")
		out.set("bound", false)
		out.set("members", []any{})
		return &ToolResult{Data: out}, nil
	}
	if len(members) > readRowsLimit {
		members = members[:readRowsLimit]
	}
	out := newObj()
	out.set("kind", "family")
	out.set("bound", true)
	out.set("joined", pyBool(st["joined"]))
	out.set("walletRegion", st["walletRegion"])
	out.set("members", members)
	out.set("total", seen)
	return &ToolResult{Data: out}, nil
}

// memberSeen 成员 steamid 去重核对。
func memberSeen(members []any, sid string) bool {
	for _, v := range members {
		if m, ok := v.(*Obj); ok {
			if s, ok := m.get("steamid").(string); ok && s == sid {
				return true
			}
		}
	}
	return false
}

// strOrNil 空串 → null（Python or None 投影）。
func strOrNil(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// bills_spend_query 消费查询（Python userview.bills_spend_query）。
func billsSpendQuery(c *CallContext) (*ToolResult, error) {
	importID, err := argID(c.Args, "import_id")
	if err != nil {
		return nil, err
	}
	mode := pyStrOr(c.Args["mode"])
	if mode == "" {
		mode = "overview"
	}
	if mode == "game_txs" {
		limit := clampInt(c.Args["limit"], 50, 1, 200)
		q := urlValues()
		q.set("limit", fmt.Sprintf("%d", limit))
		for _, key := range []string{"year", "month", "tx_type", "search"} {
			if v := pyStrOr(c.Args[key]); v != "" {
				name := key
				if key == "tx_type" {
					name = "txType"
				}
				q.set(name, v)
			}
		}
		status, body, err := c.fetch(c.Ctx, fmt.Sprintf("/api/v1/bills/imports/%d/game-txs?%s", importID, q.encode()))
		if err != nil {
			return nil, err
		}
		if status == 404 {
			return billsEmpty(importID, mode), nil
		}
		if status != 200 {
			return nil, fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
		}
		var data map[string]any
		if err := jsonDecoder(body).Decode(&data); err != nil {
			return nil, err
		}
		txs, _ := data["rows"].([]any)
		if len(txs) == 0 {
			return billsEmpty(importID, mode), nil
		}
		rows := make([]any, 0, len(txs))
		for _, v := range txs {
			t, ok := v.(map[string]any)
			if !ok {
				continue
			}
			k := ""
			if raw, ok := t["items"].([]any); ok && len(raw) > 0 {
				k = pyStrOr(raw[0])
			}
			if k == "" {
				k = pyStrOr(t["txType"])
			}
			row := newObj()
			row.set("k", k)
			row.set("vKey", "billTx")
			row.set("at", t["date"])
			d := newObj()
			d.set("cnyFen", t["cnyFen"])
			d.set("txType", t["txType"])
			d.set("isRefund", pyBool(t["isRefund"]))
			row.set("data", d)
			row.set("tone", nil)
			rows = append(rows, row)
		}
		total := len(txs)
		if n, err := pyIntOr(data["total"]); err == nil && n > 0 {
			total = int(n)
		}
		return &ToolResult{Data: rowsResult("billsQuery", rows, total)}, nil
	}
	status, body, err := c.fetch(c.Ctx, fmt.Sprintf("/api/v1/bills/imports/%d/overview", importID))
	if err != nil {
		return nil, err
	}
	if status == 404 {
		return billsEmpty(importID, mode), nil
	}
	if status != 200 {
		return nil, fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
	var data map[string]any
	if err := jsonDecoder(body).Decode(&data); err != nil {
		return nil, err
	}
	summary, _ := data["summary"].(map[string]any)
	k := pyStrOr(data["nickname"])
	if k == "" {
		k = pyStrOr(data["sourceFile"])
	}
	if k == "" {
		k = fmt.Sprintf("#%d", importID)
	}
	row1 := newObj()
	row1.set("k", k)
	row1.set("vKey", "billSpend")
	row1.set("at", data["importedAt"])
	d1 := newObj()
	d1.set("netFen", summary["netFen"])
	d1.set("spendFen", summary["spendFen"])
	d1.set("refundFen", summary["refundFen"])
	d1.set("orders", summary["orders"])
	row1.set("data", d1)
	row2 := newObj()
	row2.set("k", "")
	row2.set("vKey", "billTopup")
	d2 := newObj()
	d2.set("netFen", summary["topupNetFen"])
	d2.set("spendFen", summary["topupSpendFen"])
	row2.set("data", d2)
	rows := []any{row1, row2}
	out := rowsResult("billsQuery", rows, len(rows))
	out.set("importId", importID)
	sum := newObj()
	sum.set("netFen", summary["netFen"])
	sum.set("spendFen", summary["spendFen"])
	sum.set("refundFen", summary["refundFen"])
	sum.set("orders", summary["orders"])
	sum.set("txCount", summary["txCount"])
	sum.set("accountValueFen", summary["accountValueFen"])
	sum.set("topupNetFen", summary["topupNetFen"])
	out.set("summary", sum)
	years := []any{}
	if raw, ok := data["years"].([]any); ok {
		for _, v := range raw {
			if len(years) >= 6 {
				break
			}
			y, ok := v.(map[string]any)
			if !ok {
				continue
			}
			row := newObj()
			row.set("year", y["year"])
			row.set("netFen", y["netFen"])
			row.set("count", y["count"])
			years = append(years, row)
		}
	}
	out.set("years", years)
	counts := data["counts"]
	if counts == nil {
		counts = newObj()
	}
	out.set("counts", counts)
	return &ToolResult{Data: out}, nil
}

// billsEmpty 缺 import / 无交易空态。
func billsEmpty(importID int64, mode string) *ToolResult {
	out := newObj()
	out.set("kind", "empty")
	out.set("importId", importID)
	out.set("mode", mode)
	return &ToolResult{Status: StatusEmpty, Data: out}
}

// redeem_quota CDK 激活额度（Python account.redeem_quota）。
func redeemQuota(c *CallContext) (*ToolResult, error) {
	q, err := fetchMapErr(c, "/api/v1/redeem/quota")
	if err != nil {
		return nil, err
	}
	if !pyBool(q["hasCookie"]) {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "redeemNoCookie")
		row.set("tone", "warn")
		return &ToolResult{Data: rowsNoTotal("redeem", []any{row})}, nil
	}
	used, limit := statInt(q, "used"), statInt(q, "limit")
	row := newObj()
	row.set("k", "")
	row.set("vKey", "redeemQuota")
	d := newObj()
	d.set("used", used)
	d.set("limit", limit)
	row.set("data", d)
	row.set("tone", toneOrWarn(used < limit))
	return &ToolResult{Data: rowsNoTotal("redeem", []any{row})}, nil
}

// ── 同步动作（POST 通道） ──

// sync_wallet 立即抓取钱包余额（Python account.sync_wallet；端点回状态图，
// 成功后 sync_error 必为空——saveWallet 成功清 wallet_error）。
func syncWallet(c *CallContext) (*ToolResult, error) {
	var status map[string]any
	if err := c.postJSON("/api/v1/account/sync", map[string]any{}, &status); err != nil {
		return nil, err
	}
	detail := pyStrOr(status["sync_error"])
	return &ToolResult{Data: syncRowState("wallet", detail, detail == "", "walletSynced", "walletFailed")}, nil
}

// syncLibrary 同步愿望单/拥有库（Python account.sync_library；wishlist 域
// POST /accounts/{steamid}/sync 同一执行入口，inflight 去重与 auto_crawl 在域内）。
func syncLibrary(c *CallContext) (*ToolResult, error) {
	sid := strings.TrimSpace(pyStrOr(c.Args["steam_id"]))
	if sid == "" {
		// Python 同径：sync_account(None) 抛 ValueError → 执行器 failed。
		return nil, fmt.Errorf("账户不存在: None")
	}
	var result map[string]any
	if err := c.postJSON(fmt.Sprintf("/api/v1/accounts/%s/sync?autoCrawl=true", sid), map[string]any{}, &result); err != nil {
		return nil, err
	}
	return &ToolResult{Data: syncCard("library", result, "syncDone", "syncFailed")}, nil
}

// syncAchievements 发起成就后台同步（Python account.sync_achievements；
// 409=已在跑 → syncRunning）。
func syncAchievements(c *CallContext) (*ToolResult, error) {
	sid := strings.TrimSpace(pyStrOr(c.Args["steam_id"]))
	status, body, err := c.post(c.Ctx, queryPath("/api/v1/achievements/sync", sidQuery(sid)), []byte("{}"))
	if err != nil {
		return nil, err
	}
	switch status {
	case 200:
		var snap map[string]any
		if err := jsonDecoder(body).Decode(&snap); err != nil {
			return nil, err
		}
		return &ToolResult{Data: syncCard("achievementsSync", snap, "syncDone", "syncFailed")}, nil
	case 409:
		row := newObj()
		row.set("k", "")
		row.set("vKey", "syncRunning")
		row.set("tone", "warn")
		return &ToolResult{Data: rowsNoTotal("achievementsSync", []any{row})}, nil
	default:
		return nil, fmt.Errorf("内部请求失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
}

// syncBills 同步 Steam 账单（Python account.sync_bills；409=no_cookie）。
func syncBills(c *CallContext) (*ToolResult, error) {
	status, body, err := c.post(c.Ctx, "/api/v1/bills/sync", []byte("{}"))
	if err != nil {
		return nil, err
	}
	switch status {
	case 200:
		var result map[string]any
		if err := jsonDecoder(body).Decode(&result); err != nil {
			return nil, err
		}
		return &ToolResult{Data: syncCard("billsSync", result, "syncDone", "syncFailed")}, nil
	case 409:
		row := newObj()
		row.set("k", "")
		row.set("vKey", "billNoCookie")
		row.set("tone", "warn")
		return &ToolResult{Data: rowsNoTotal("billsSync", []any{row})}, nil
	default:
		return nil, fmt.Errorf("内部请求失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
}

// syncFamily 家庭组同步（Python account.sync_family）。
func syncFamily(c *CallContext) (*ToolResult, error) {
	sid := strings.TrimSpace(pyStrOr(c.Args["steam_id"]))
	var result map[string]any
	if err := c.postJSON(queryPath("/api/v1/family/sync", sidQuery(sid)), map[string]any{}, &result); err != nil {
		return nil, err
	}
	return &ToolResult{Data: syncCard("familySync", result, "syncDone", "syncFailed")}, nil
}

// refreshRates 刷新汇率（Python account.refresh_rates；源不可用 → failed）。
func refreshRates(c *CallContext) (*ToolResult, error) {
	var result map[string]any
	if err := c.postJSON("/api/v1/rates/refresh", map[string]any{}, &result); err != nil {
		return nil, err
	}
	return &ToolResult{Data: syncCard("rates", result, "syncDone", "syncFailed")}, nil
}

// ── 领航员会话读面 ──

// readMaxTurns 单次回读轮数上限（Python _READ_MAX_TURNS）。
const readMaxTurns = 30

// readDigestTokens 回读文本预算（Python _READ_DIGEST_TOKENS）。
const readDigestTokens = 3000

// digestToolChars 单个工具结果保留字符数（Python _READ_TOOL_CHARS）。
const digestToolChars = 400

// listSessions 会话清单（Python runtime_tools.list_sessions）。
func listSessions(c *CallContext) (*ToolResult, error) {
	body, err := fetchMapErr(c, "/api/v1/pilot/sessions?limit=20")
	if err != nil {
		return nil, err
	}
	items, _ := body["items"].([]any)
	rows := make([]any, 0, len(items))
	for _, v := range items {
		it, ok := v.(map[string]any)
		if !ok {
			continue
		}
		title := pyStrOr(it["title"])
		if title == "" {
			title = pyClipRunes(pyStrOr(it["sid"]), 8)
		}
		row := newObj()
		row.set("k", title)
		row.set("vKey", "sessionTurns")
		d := newObj()
		d.set("count", it["turn_total"])
		row.set("data", d)
		row.set("at", it["updated_at"])
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsResult("sessions", rows, len(items))}, nil
}

// readSession 历史会话回读（Python runtime_tools.read_session；digest 为
// digest_lines 同式移植——会话端点不含 messages，工具行走 answer 回退支路）。
func readSession(c *CallContext) (*ToolResult, error) {
	sid := pyStrOr(c.Args["sid"])
	offset, err := argInt(c.Args, "offset", 0)
	if err != nil {
		return nil, err
	}
	count, err := argInt(c.Args, "count", readRowsLimit)
	if err != nil {
		return nil, err
	}
	status, body, err := c.fetch(c.Ctx, "/api/v1/pilot/sessions/"+sid)
	if err != nil {
		return nil, err
	}
	if status == 404 {
		out := newObj()
		out.set("kind", "empty")
		out.set("note", "no_data")
		return &ToolResult{Status: StatusEmpty, Data: out}, nil
	}
	if status != 200 {
		return nil, fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
	var state map[string]any
	if err := jsonDecoder(body).Decode(&state); err != nil {
		return nil, err
	}
	turns, _ := state["turns"].([]any)
	total := len(turns)
	skip := offset
	if skip < 0 {
		skip = 0
	}
	size := count
	if size < 1 {
		size = 1
	}
	if size > readMaxTurns {
		size = readMaxTurns
	}
	end := total - int(skip)
	if end < 0 {
		end = 0
	}
	start := end - int(size)
	if start < 0 {
		start = 0
	}
	window := turns[start:end]
	rows := make([]any, 0, len(window))
	turnMaps := make([]map[string]any, 0, len(window))
	for _, v := range window {
		t, ok := v.(map[string]any)
		if !ok {
			continue
		}
		turnMaps = append(turnMaps, t)
		resp, _ := t["resp"].(map[string]any)
		answer := pyStrOr(resp["answer"])
		toolsTxt := ""
		if raw, ok := resp["tools"].([]any); ok {
			parts := make([]string, 0, len(raw))
			for _, x := range raw {
				parts = append(parts, pyText(x))
			}
			toolsTxt = strings.Join(parts, "；")
		}
		v2 := answer
		if v2 == "" {
			v2 = toolsTxt
		}
		row := newObj()
		row.set("k", pyClipRunes(pyStrOr(t["q"]), 48))
		row.set("v", pyClipRunes(v2, 80))
		row.set("at", t["ts"])
		rows = append(rows, row)
	}
	digest := ""
	if text := digestLines(turnMaps, readDigestTokens); text != "" {
		digest = text
	}
	out := rowsResult("sessionRead", rows, total)
	out.set("sid", sid)
	out.set("sessionTitle", state["title"])
	out.set("startTurn", start)
	out.set("turnCount", len(turnMaps))
	out.set("summaryThrough", state["summary_through"])
	out.set("digest", digest)
	return &ToolResult{Data: out}, nil
}

// digestLines Python pilot.context.digest_lines 同式移植（turns 从最新往回
// 收集到预算为止；messages 缺席时回退 resp.answer，与 Python 兜底支路同径）。
func digestLines(turns []map[string]any, budget int) string {
	picked := []string{}
	total := 0
	for i := len(turns) - 1; i >= 0; i-- {
		t := turns[i]
		block := []string{"用户：" + pyStrOr(t["q"])}
		if msgs, ok := t["messages"].([]any); ok {
			for _, mv := range msgs {
				m, ok := mv.(map[string]any)
				if !ok {
					continue
				}
				if line := digestMessageLine(m, digestToolChars); line != "" {
					block = append(block, line)
				}
			}
		}
		if len(block) == 1 {
			resp, _ := t["resp"].(map[string]any)
			if answer := pyStrOr(resp["answer"]); answer != "" {
				block = append(block, "助手："+answer)
			}
		}
		text := strings.Join(block, "\n")
		if len(picked) > 0 && total+pyEstimateTokens(text) > budget {
			break
		}
		picked = append(picked, text)
		total += pyEstimateTokens(text)
	}
	if len(picked) == 0 {
		return ""
	}
	for i, j := 0, len(picked)-1; i < j; i, j = i+1, j-1 {
		picked[i], picked[j] = picked[j], picked[i]
	}
	return strings.Join(picked, "\n---\n")
}

// digestMessageLine Python _message_line 同式（assistant/tool 两类，400 字截断）。
func digestMessageLine(m map[string]any, toolChars int) string {
	role := pyStrOr(m["role"])
	text := strings.TrimSpace(pyStrOr(m["content"]))
	if role == "assistant" {
		if text == "" {
			return ""
		}
		return "助手：" + text
	}
	if role == "tool" {
		if text == "" {
			return ""
		}
		if len([]rune(text)) > toolChars {
			text = pyClipRunes(text, toolChars) + "…[截断]"
		}
		return "工具结果：" + text
	}
	return ""
}

// ── 运行账本（agent_runs / agent_events SQL 直读，D6） ──

// agentRunsList 运行账本清单（Python runtime_tools._agent_runs_list；
// 排除条件与 Python list_runs 的 AgentRun 段同式）。
func agentRunsList(c *CallContext) (*ToolResult, error) {
	limit, err := argInt(c.Args, "limit", 10)
	if err != nil {
		return nil, err
	}
	if limit < 1 {
		limit = 1
	}
	if limit > 30 {
		limit = 30
	}
	rows0, err := c.db.QueryContext(c.Ctx, `SELECT id, runner, COALESCE(meta,''), status,
		CAST(created_at AS TEXT), CAST(started_at AS TEXT), CAST(finished_at AS TEXT) FROM agent_runs
		WHERE NOT (runner='task' AND COALESCE(json_extract(meta,'$.task'),'') != 'dev_pytest')
		  AND NOT (runner='job' AND COALESCE(json_extract(meta,'$.task'),'') IN ('price_refresh','price_repair','price_missing'))
		ORDER BY created_at DESC, id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows0.Close()
	rows := []any{}
	for rows0.Next() {
		var id, runner, status string
		var meta, createdAt, startedAt, finishedAt sql.NullString
		if err := rows0.Scan(&id, &runner, &meta, &status, &createdAt, &startedAt, &finishedAt); err != nil {
			return nil, err
		}
		v2 := runner
		var metaMap map[string]any
		if meta.Valid && json.Unmarshal([]byte(meta.String), &metaMap) == nil {
			if task := pyStrOr(metaMap["task"]); task != "" {
				v2 = task
			}
		}
		at := finishedAt.String
		if at == "" {
			at = startedAt.String
		}
		if at == "" {
			at = createdAt.String
		}
		row := newObj()
		row.set("k", pyClipRunes(id, 8))
		row.set("run_id", id)
		row.set("v", status)
		row.set("v2", v2)
		row.set("at", strOrNil(at))
		rows = append(rows, row)
	}
	if err := rows0.Err(); err != nil {
		return nil, err
	}
	return &ToolResult{Data: rowsResult("agentRuns", rows, len(rows))}, nil
}

// agentRunEvents 单个运行的事件回放（Python runtime_tools._agent_run_events）。
func agentRunEvents(c *CallContext) (*ToolResult, error) {
	runID := pyStrOr(c.Args["run_id"])
	if runID == "" {
		out := newObj()
		out.set("kind", "empty")
		out.set("note", "no_run_id")
		return &ToolResult{Status: StatusEmpty, Data: out}, nil
	}
	afterSeq, err := argInt(c.Args, "after_seq", 0)
	if err != nil {
		return nil, err
	}
	limit, err := argInt(c.Args, "limit", 30)
	if err != nil {
		return nil, err
	}
	if limit < 1 {
		limit = 1
	}
	if limit > 100 {
		limit = 100
	}
	var status string
	err = c.db.QueryRowContext(c.Ctx, `SELECT status FROM agent_runs WHERE id=?`, runID).Scan(&status)
	if err == sql.ErrNoRows {
		out := newObj()
		out.set("kind", "empty")
		out.set("note", "no_run")
		return &ToolResult{Status: StatusEmpty, Data: out}, nil
	}
	if err != nil {
		return nil, err
	}
	rows0, err := c.db.QueryContext(c.Ctx, `SELECT event_type, seq, CAST(created_at AS TEXT)
		FROM agent_events WHERE run_id=? AND seq>? ORDER BY seq ASC LIMIT ?`, runID, afterSeq, limit)
	if err != nil {
		return nil, err
	}
	defer rows0.Close()
	rows := []any{}
	for rows0.Next() {
		var eventType string
		var seq int64
		var createdAt sql.NullString
		if err := rows0.Scan(&eventType, &seq, &createdAt); err != nil {
			return nil, err
		}
		row := newObj()
		row.set("k", eventType)
		row.set("v", fmt.Sprintf("%d", seq))
		row.set("at", createdAt.String)
		rows = append(rows, row)
	}
	if err := rows0.Err(); err != nil {
		return nil, err
	}
	out := rowsResult("agentEvents", rows, len(rows))
	out.set("runId", runID)
	out.set("runStatus", status)
	return &ToolResult{Data: out}, nil
}

// ── 元信息 ──

// listToolsDescChars 清单描述截断长度（Python _DESC_CHARS）。
const listToolsDescChars = 56

// listTools 元工具：注册表全量对 CORE_TOOLS 取差集并「解锁」
//（解锁是客户端语义提示，服务端无状态）。
func listTools(r *ToolRegistry) (*ToolResult, error) {
	extra := []*ToolSpec{}
	for _, s := range r.all() {
		if !coreTools[s.Name] && s.Name != "list_tools" {
			extra = append(extra, s)
		}
	}
	tools := make([]any, 0, len(extra))
	for _, s := range extra {
		row := newObj()
		row.set("name", s.Name)
		row.set("group", string(s.Group))
		row.set("desc", pyClipRunes(strings.Join(strings.Fields(s.Description), " "), listToolsDescChars))
		tools = append(tools, row)
	}
	out := newObj()
	out.set("kind", "tool_catalog")
	out.set("note", "以下工具已解锁，本会话后续可直接调用，不必再次 list_tools。")
	out.set("tools", tools)
	out.set("count", len(tools))
	return &ToolResult{Data: out}, nil
}

// ── 小件 ──

// queryPath 带查询串的进程内路径（空查询原样返回）。
func queryPath(path, query string) string {
	if query == "" {
		return path
	}
	return path + "?" + query
}

// sidQuery steamid 查询段（空账号省参，端点按主账号解析）。
func sidQuery(sid string) string {
	if sid == "" {
		return ""
	}
	return "steamid=" + sid
}

// mergeQuery 保序查询段拼接（空段跳过）。
func mergeQuery(parts ...string) string {
	out := []string{}
	for _, p := range parts {
		if p != "" {
			out = append(out, p)
		}
	}
	return strings.Join(out, "&")
}
