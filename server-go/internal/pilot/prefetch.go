package pilot

import (
	"cmp"
	"context"
	"database/sql"
	"fmt"
	"regexp"
	"slices"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/agent"
)

const (
	regionCardTop      = 8
	recentCardTurns    = 6
	resumeToolChars    = 400
	resumeTextChars    = 600
	resumeMaxTools     = 12
	prefetchNoteHeader = "（系统侧同步·仅供参照）以下是与本轮提问直接对应的实时预取事实（当轮刚取回的真实数据，非缓存、非估计）："
)

var (
	priceTagRe            = regexp.MustCompile(`《([^》]{1,40})》`)
	prefetchRegionWords   = []string{"全区", "各区", "哪个区", "哪个地区", "各区域", "比价"}
	prefetchFreeWords     = []string{"喜加一", "免费游戏", "免费领", "限时免费", "白送游戏"}
	prefetchFreeWeak      = []string{"白嫖"}
	prefetchFreeCtxWords  = []string{"游戏", "steam", "epic", "领", "入库"}
	prefetchCalendarWords = []string{"活动", "日历"}
	prefetchRatesWords    = []string{"汇率"}

	intentNoteLabels = map[string]string{
		IntentHowTo:         "指引咨询",
		IntentCreateAlert:   "设置价格提醒",
		IntentAddMonitor:    "添加关注",
		IntentPriceAnalysis: "价格查询/分析",
		IntentFindGames:     "游戏商店/推荐",
		IntentNavigate:      "页面导航",
		IntentChat:          "自由问答",
	}
)

type prefetchSpec struct {
	Name string
	Args map[string]any
}

type prefetchItem struct {
	Name       string
	Label      string
	CardKind   string
	Step       map[string]any
	Card       map[string]any
	DurationMS int
	Summary    string
}

type prefetchResult struct {
	Items []prefetchItem
	Note  map[string]any
	Cards []any
	Force bool
}

func extractTitle(question string) string {
	m := priceTagRe.FindStringSubmatch(question)
	if len(m) > 1 {
		return strings.TrimSpace(m[1])
	}
	return ""
}

func exactCatalogAppID(ctx context.Context, db *sql.DB, title string) int {
	q := strings.TrimSpace(title)
	if q == "" || db == nil {
		return 0
	}
	rows, err := db.QueryContext(ctx, `
		SELECT g.appid FROM games g
		WHERE (g.name = ? OR LOWER(g.name_en) = LOWER(?))
		AND NOT EXISTS (SELECT 1 FROM catalog_removals r WHERE r.appid = g.appid)
		LIMIT 2`, q, q)
	if err != nil {
		return 0
	}
	defer rows.Close()
	ids := []int{}
	for rows.Next() {
		var id int
		if rows.Scan(&id) == nil && id > 0 {
			ids = append(ids, id)
		}
	}
	if len(ids) == 1 {
		return ids[0]
	}
	return 0
}

func (s *Service) prefetchPlan(ctx context.Context, question string, appid *int,
	state *SessionState, intent string) ([]prefetchSpec, bool) {
	q := question
	priceAsk := intent == IntentPriceAnalysis || containsAny(q, priceWords)
	regionAsk := containsAny(q, prefetchRegionWords)
	certain := false
	target := 0
	if appid != nil && *appid > 0 {
		target = *appid
		certain = true
	}
	if target == 0 && state != nil && state.LastGame != nil && hasSingleGameTarget(q) {
		target = jsonInt(state.LastGame["appid"])
	}
	if target == 0 && priceAsk {
		title := extractTitle(q)
		if title != "" {
			if exact := exactCatalogAppID(ctx, s.deps.DB, title); exact > 0 {
				target = exact
				certain = true
			}
		}
		if target == 0 {
			query := q
			if title != "" {
				query = title
			}
			matches := s.searchGamesFor(ctx, query)
			if len(matches) == 1 {
				if first, ok := matches[0].(map[string]any); ok {
					target = jsonInt(first["appid"])
				}
			}
		}
	}
	pureAsk := intent != IntentCreateAlert && intent != IntentAddMonitor &&
		!containsAny(q, findWords) && !containsAny(q, nonPriceKeywords)
	force := pureAsk && certain && target > 0
	if target > 0 && (priceAsk || regionAsk) {
		if regionAsk {
			return []prefetchSpec{{Name: "get_region_prices", Args: map[string]any{"appid": target}}}, force
		}
		return []prefetchSpec{{Name: "get_price_briefing", Args: map[string]any{"appid": target}}}, force
	}
	if containsAny(q, prefetchFreeWords) || (containsAny(q, prefetchFreeWeak) && containsAny(strings.ToLower(q), prefetchFreeCtxWords)) {
		return []prefetchSpec{{Name: "epic_free", Args: map[string]any{}}, {Name: "steam_free", Args: map[string]any{}}}, pureAsk
	}
	if containsAny(q, prefetchCalendarWords) {
		return []prefetchSpec{{Name: "calendar_events", Args: map[string]any{}}}, pureAsk
	}
	if containsAny(q, prefetchRatesWords) {
		return []prefetchSpec{{Name: "rates_overview", Args: map[string]any{}}}, pureAsk
	}
	return nil, false
}

func (s *Service) prefetchFacts(ctx context.Context, question string, appid *int,
	state *SessionState, intent string, recentCards [][2]string) *prefetchResult {
	plan, force := s.prefetchPlan(ctx, question, appid, state, intent)
	if len(plan) == 0 {
		return nil
	}
	sid := sidOf(state)
	type outcome struct {
		result   map[string]any
		duration int
	}
	outcomes := make([]outcome, len(plan))
	var wg sync.WaitGroup
	for i, spec := range plan {
		wg.Go(func() {
			started := time.Now()
			res := s.ToolExecute(ctx, spec.Name, spec.Args, false, sid)
			outcomes[i] = outcome{result: res, duration: int(time.Since(started).Milliseconds())}
		})
	}
	wg.Wait()

	items := []prefetchItem{}
	cards := []any{}
	noteLines := []string{}
	for i, spec := range plan {
		res := outcomes[i].result
		if res == nil || isTerminalFailKind(stringOf(res["kind"])) {
			continue
		}
		dec := s.DecideCard(spec.Name, res, spec.Args, question, toMapSlice(cards), cardOf(spec.Name, res), recentCards)
		var card map[string]any
		var cardKind string
		if dec.ShouldEmit && dec.Card != nil {
			card = dec.Card
			cardKind = stringOf(card["kind"])
			cards = append(cards, card)
		}
		items = append(items, prefetchItem{
			Name:       spec.Name,
			Label:      s.ToolNameLabel(spec.Name),
			CardKind:   cardKind,
			Step:       toolStep(spec.Name, res),
			Card:       card,
			DurationMS: outcomes[i].duration,
			Summary:    toolSummary(spec.Name, res),
		})
		argsStr := ""
		if len(spec.Args) > 0 {
			argsStr = " " + string(mustJSON(spec.Args))
		}
		view := mustJSON(EnhanceModelView(res, dec))
		noteLines = append(noteLines, fmt.Sprintf("- %s%s: %s", spec.Name, argsStr, string(view)))
	}
	if len(items) == 0 {
		return nil
	}
	note := map[string]any{
		"role": "user",
		"content": prefetchNoteHeader + "\n" + strings.Join(noteLines, "\n") +
			"\n以上就是所问对象/主题的当轮实时结果，已足够直接回答——请基于它们给结论，" +
			"不要再检索验证或重复调用相同工具；仅当确需其他维度数据时才调用其他工具。",
	}
	return &prefetchResult{Items: items, Note: note, Cards: cards, Force: force}
}

func isTerminalFailKind(kind string) bool {
	switch kind {
	case "empty", "disabled", "failed", "timeout", "denied", "unknown_tool":
		return true
	}
	return false
}

func isEmptyToolResult(result map[string]any) bool {
	if result == nil {
		return true
	}
	kind := stringOf(result["kind"])
	switch kind {
	case "empty", "disabled", "failed", "timeout", "denied", "unknown_tool", "not_found":
		return true
	case "rows":
		return len(anySlice(result["rows"])) == 0
	case "games", "compare":
		return len(anySlice(result["items"])) == 0
	case "epic":
		return len(anySlice(result["offers"])) == 0 && result["mobile"] == nil
	}
	return false
}

func cardKindHint(toolName string) string {
	hints := agent.CardKindHints()
	if kind, ok := hints[toolName]; ok {
		return kind
	}
	if agent.IsCardHintRowsTool(toolName) {
		return "rows"
	}
	if agent.IsReadTool(toolName) || agent.IsWriteTool(toolName) || agent.IsSyncTool(toolName) || agent.IsTaskTool(toolName) {
		return "rows"
	}
	return ""
}

func regionsCard(result map[string]any) map[string]any {
	items := anySlice(result["items"])
	if len(items) == 0 {
		return nil
	}
	account := strings.ToLower(stringOf(result["accountRegion"]))
	parsed := make([]map[string]any, 0, len(items))
	for _, raw := range items {
		if m, ok := raw.(map[string]any); ok {
			parsed = append(parsed, m)
		}
	}
	if len(parsed) == 0 {
		return nil
	}
	slices.SortFunc(parsed, func(a, b map[string]any) int {
		return cmp.Compare(jsonInt(a["cnyFen"]), jsonInt(b["cnyFen"]))
	})
	keep := []map[string]any{}
	limit := minInt(regionCardTop, len(parsed))
	accountSeen := false
	for i := 0; i < limit; i++ {
		keep = append(keep, parsed[i])
		if strings.ToLower(stringOf(parsed[i]["region"])) == account {
			accountSeen = true
		}
	}
	if account != "" && !accountSeen {
		for _, m := range parsed[limit:] {
			if strings.ToLower(stringOf(m["region"])) == account {
				keep = append(keep, m)
				break
			}
		}
	}
	slices.SortFunc(keep, func(a, b map[string]any) int {
		return cmp.Compare(jsonInt(a["cnyFen"]), jsonInt(b["cnyFen"]))
	})
	count := jsonInt(result["count"])
	if count == 0 {
		count = len(items)
	}
	return map[string]any{
		"kind":          "regions",
		"appid":         result["appid"],
		"name":          result["name"],
		"accountRegion": account,
		"items":         keep,
		"count":         count,
	}
}

func cardOf(name string, result map[string]any) map[string]any {
	if result == nil {
		return nil
	}
	kind := stringOf(result["kind"])
	if kind == "stepper" || result["stepper"] != nil {
		st, _ := result["stepper"].(map[string]any)
		if st == nil {
			st = result
		}
		return map[string]any{
			"kind":             "stepper",
			"title":            stringOr(st["title"], "流水线进度"),
			"description":      st["description"],
			"currentStepIndex": jsonInt(st["currentStepIndex"]),
			"steps":            anySlice(st["steps"]),
		}
	}
	switch kind {
	case "rows":
		rows := anySlice(result["rows"])
		if len(rows) > 6 {
			rows = rows[:6]
		}
		card := map[string]any{
			"kind":     "rows",
			"titleKey": stringOf(result["titleKey"]),
			"rows":     rows,
		}
		if result["name"] != nil {
			card["name"] = result["name"]
		}
		if result["appid"] != nil {
			card["appid"] = result["appid"]
		}
		return card
	case "games":
		items := anySlice(result["items"])
		if len(items) == 0 {
			return nil
		}
		if len(items) > 5 {
			items = items[:5]
		}
		card := map[string]any{"kind": "games", "items": items}
		if tk := stringOf(result["titleKey"]); tk != "" {
			card["titleKey"] = tk
		}
		if result["total"] != nil {
			card["total"] = result["total"]
		}
		return card
	case "family", "achievements":
		return result
	case "price":
		card := map[string]any{"kind": "price"}
		for k, v := range result {
			if k != "kind" {
				card[k] = v
			}
		}
		return card
	case "region_prices":
		return regionsCard(result)
	case "compare":
		items := anySlice(result["items"])
		if len(items) == 0 {
			return nil
		}
		return map[string]any{"kind": "compare", "items": items}
	case "action":
		card := map[string]any{"kind": "action"}
		for k, v := range result {
			card[k] = v
		}
		return card
	case "proposal":
		args, _ := result["args"].(map[string]any)
		if args == nil {
			args = map[string]any{}
		}
		items := anySlice(result["items"])
		if items == nil {
			items = []any{}
		}
		return map[string]any{
			"kind":   "proposal",
			"pid":    result["pid"],
			"action": result["action"],
			"items":  items,
			"args":   args,
			"state":  stringOr(result["state"], "pending"),
		}
	case "epic":
		offers := anySlice(result["offers"])
		if len(offers) == 0 && result["mobile"] == nil {
			return nil
		}
		total := jsonInt(result["total"])
		if result["total"] == nil {
			total = len(offers)
		}
		return map[string]any{
			"kind":      "epic",
			"titleKey":  stringOr(result["titleKey"], "epic"),
			"offers":    offers,
			"mobile":    result["mobile"],
			"fetchedAt": result["fetchedAt"],
			"total":     total,
		}
	case "navigate":
		if path := stringOf(result["path"]); path != "" {
			return map[string]any{"kind": "navigate", "target": result["target"], "path": path}
		}
		return nil
	}
	if name == "present_ui" || stringOf(result["componentId"]) != "" || stringOf(result["component_id"]) != "" {
		return result
	}
	return nil
}

func toolStep(name string, result map[string]any) map[string]any {
	kind := stringOf(result["kind"])
	if kind == "denied" {
		return map[string]any{"label": "denied", "status": "denied", "data": map[string]any{}}
	}
	if kind == "disabled" {
		return map[string]any{"label": agent.StepLabel(name), "status": "empty", "data": map[string]any{}}
	}
	if kind == "timeout" || kind == "failed" || kind == "stopped" {
		return map[string]any{"label": agent.StepLabel(name), "status": kind, "data": map[string]any{}}
	}
	switch name {
	case "audit_follows_workflow":
		st, _ := result["stepper"].(map[string]any)
		ok := false
		for _, raw := range anySlice(st["steps"]) {
			if s, _ := raw.(map[string]any); stringOf(s["status"]) == "ok" {
				ok = true
				break
			}
		}
		status := "empty"
		if ok {
			status = "ok"
		}
		return map[string]any{"label": agent.StepLabel(name), "status": status, "data": map[string]any{}}
	case "present_ui":
		ok := stringOf(result["componentId"]) != "" || (kind != "" && kind != "denied")
		status := "empty"
		if ok {
			status = "ok"
		}
		return map[string]any{"label": "presentUI", "status": status, "data": map[string]any{}}
	case "get_region_prices", "compare_games", "search_games", "recommend_games", "search_steam",
		"top_games", "price_drops", "list_follows", "list_wishlist", "list_owned", "list_family_library":
		items := anySlice(result["items"])
		status := "empty"
		if len(items) > 0 {
			status = "ok"
		}
		return map[string]any{"label": agent.StepLabel(name), "status": status, "data": map[string]any{"count": len(items)}}
	case "achievements_summary", "family_status":
		bound := true
		if v, has := result["hasCredential"]; has {
			bound, _ = v.(bool)
		} else if v, has := result["bound"]; has {
			bound, _ = v.(bool)
		}
		status := "empty"
		if bound {
			status = "ok"
		}
		return map[string]any{"label": agent.StepLabel(name), "status": status, "data": map[string]any{}}
	case "read_attachment":
		got := stringOf(result["content"]) != "" || stringOf(result["lines"]) != ""
		count := 0
		if got {
			count = jsonInt(result["end_line"]) - jsonInt(result["start_line"]) + 1
			if count < 0 {
				count = 0
			}
		}
		status := "empty"
		if got {
			status = "ok"
		}
		return map[string]any{"label": "attachmentRead", "status": status, "data": map[string]any{"count": count}}
	case "epic_free":
		offers := anySlice(result["offers"])
		if len(offers) == 0 {
			offers = anySlice(result["rows"])
		}
		status := "empty"
		if len(offers) > 0 {
			status = "ok"
		}
		return map[string]any{"label": "epicFree", "status": status, "data": map[string]any{"count": len(offers)}}
	case "get_price_briefing":
		status := "empty"
		if kind == "price" {
			status = "ok"
		}
		return map[string]any{"label": "price", "status": status, "data": map[string]any{"name": result["name"]}}
	case "add_follow", "create_price_alert":
		label := "alert"
		if name == "add_follow" {
			label = "follow"
		}
		status := "empty"
		if jsonInt(result["appid"]) > 0 {
			status = "ok"
		}
		return map[string]any{"label": label, "status": status, "data": map[string]any{"name": result["name"]}}
	case "navigate":
		status := "empty"
		if stringOf(result["path"]) != "" {
			status = "ok"
		}
		return map[string]any{
			"label":  "navigate",
			"status": status,
			"data": map[string]any{
				"target":  stringOf(result["target"]),
				"path":    stringOf(result["path"]),
				"section": stringOf(result["section"]),
			},
		}
	case "acknowledge_update":
		status := "empty"
		if v, ok := result["acknowledged"].(bool); ok && v {
			status = "ok"
		}
		return map[string]any{"label": agent.StepLabel(name), "status": status, "data": map[string]any{}}
	}
	if agent.IsReadTool(name) {
		rows := anySlice(result["rows"])
		status := "empty"
		if len(rows) > 0 {
			status = "ok"
		}
		data := map[string]any{"count": len(rows)}
		if name == "diagnose_price" && stringOf(result["name"]) != "" {
			data["name"] = result["name"]
		}
		return map[string]any{"label": agent.StepLabel(name), "status": status, "data": data}
	}
	if agent.IsTaskTool(name) {
		rows := anySlice(result["rows"])
		tone := "ok"
		if len(rows) > 0 {
			if row, ok := rows[0].(map[string]any); ok {
				if t := stringOf(row["tone"]); t != "" {
					tone = t
				}
			}
		}
		status := "ok"
		if tone == "bad" {
			status = "denied"
		} else if tone == "warn" {
			status = "empty"
		}
		return map[string]any{"label": agent.StepLabel(name), "status": status, "data": map[string]any{}}
	}
	if agent.IsWriteTool(name) || agent.IsSyncTool(name) {
		rows := anySlice(result["rows"])
		status := "empty"
		if len(rows) > 0 {
			status = "ok"
		}
		return map[string]any{"label": agent.StepLabel(name), "status": status, "data": map[string]any{}}
	}
	return map[string]any{"label": agent.StepLabel(name), "status": "ok", "data": map[string]any{}}
}

func sessionExpandedTools(state *SessionState) map[string]bool {
	out := map[string]bool{}
	if state == nil {
		return out
	}
	for _, turn := range state.Turns {
		resp, _ := turn["resp"].(map[string]any)
		for _, item := range stringOrAnyList(resp["tool_catalog"]) {
			if item != "" {
				out[item] = true
			}
		}
	}
	return out
}

func stringOrAnyList(raw any) []string {
	switch list := raw.(type) {
	case []string:
		return list
	case []any:
		out := make([]string, 0, len(list))
		for _, v := range list {
			if s, ok := v.(string); ok {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}

func deterministicNote(question, intent string) map[string]any {
	label := intentNoteLabels[intent]
	if label == "" {
		return nil
	}
	text := fmt.Sprintf("（系统侧同步·仅供参照）确定性意图路由将本问句识别为「%s」；工具选择仍由你决定。", label)
	if IsGuarded(question) {
		text += "本问句命中批量/删除类守卫词：写类工具会被拒绝，批量需求请改用 propose_bulk 提交提议交用户确认。"
	}
	if IsVisualRequest(question) {
		text += "\n" + strings.TrimSpace(SVGFrameworkPrompt)
	}
	return map[string]any{"role": "system", "content": text}
}

func cardLedgerNote(recent [][2]string) map[string]any {
	if len(recent) == 0 {
		return nil
	}
	limit := minInt(len(recent), 8)
	parts := make([]string, 0, limit)
	for i := 0; i < limit; i++ {
		parts = append(parts, fmt.Sprintf("%s（%s）", recent[i][0], recent[i][1]))
	}
	return map[string]any{
		"role": "system",
		"content": "（系统侧同步·仅供参照）近几轮已向用户呈现过这些界面卡片：" +
			strings.Join(parts, "；") + "。正文中不要重复罗列这些卡片已展示的数据；" +
			"确属静态清单且内容未变时不要再发同类卡片，需要再次展示时在工具参数传 emit_card: true。",
	}
}

func resumeNote(resume map[string]any) map[string]any {
	if len(resume) == 0 {
		return nil
	}
	tools := anySlice(resume["tools"])
	tail := strings.TrimSpace(stringOf(resume["text"]))
	if len(tools) == 0 && tail == "" {
		return nil
	}
	lines := []string{"（系统侧同步·仅供参照）上一轮回答被用户中断，请接着完成剩余部分，不要重复已完成的工作。"}
	if len(tools) > 0 {
		lines = append(lines, "已完成的工具调用（结果可直接采信，不必重复调用）：")
		for i, raw := range tools {
			if i >= resumeMaxTools {
				break
			}
			item, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			label := stringOr(item["label"], stringOr(item["name"], "工具"))
			summary := strings.Join(strings.Fields(stringOf(item["summary"])), " ")
			if r := []rune(summary); len(r) > resumeToolChars {
				summary = string(r[:resumeToolChars])
			}
			if summary != "" {
				lines = append(lines, fmt.Sprintf("- %s：%s", label, summary))
			} else {
				lines = append(lines, "- "+label)
			}
		}
	}
	if tail != "" {
		lines = append(lines, "上一轮已输出的正文结尾（从这里接着写，不要重头再讲）：")
		if r := []rune(tail); len(r) > resumeTextChars {
			tail = string(r[len(r)-resumeTextChars:])
		}
		lines = append(lines, tail)
	}
	return map[string]any{"role": "system", "content": strings.Join(lines, "\n")}
}

func settleRunningSteps(phases []map[string]any) {
	for _, phase := range phases {
		steps, _ := phase["steps"].([]map[string]any)
		for _, s := range steps {
			if stringOf(s["status"]) == "running" {
				s["status"] = "stopped"
				s["data"] = map[string]any{}
			}
		}
	}
}

func cloneStringBoolMap(src map[string]bool) map[string]bool {
	out := make(map[string]bool, len(src))
	for k, v := range src {
		if v {
			out[k] = true
		}
	}
	return out
}
