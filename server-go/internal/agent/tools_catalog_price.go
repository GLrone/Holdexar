// tools_catalog_price.go：目录榜单 / 价格事实 / 捆绑包 / 优惠 / 汇率 / 日历
// 读面 16 工具（MCP 批次一）。纯 GET 薄包装（进程内 fetch 主通道）+ 少量
// db 直查小件，载荷形状对齐 Python builtin 对应工具；零新增注入。
package agent

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/sched"
)

// ─── 公共小件 ────────────────────────────────────────────────────────────────

// pyMedian Python statistics.median（非空升序 int 序列）等位：奇数取中位；
// 偶数取中间两值算术平均（真除法浮点）后 int() 截断。
func pyMedian(sorted []int64) int64 {
	mid := len(sorted) / 2
	if len(sorted)%2 == 1 {
		return sorted[mid]
	}
	sum := sorted[mid-1] + sorted[mid]
	if sum%2 == 0 {
		return sum / 2
	}
	return int64(float64(sum) / 2.0)
}

// pyCut 字符串截断（Python s[:n] 语义：按码点切）。
func pyCut(s string, n int) string {
	if len(s) <= n {
		return s
	}
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n])
}

// isoNaiveAt 库内时间文本 → Python datetime.isoformat() 裸形态（空格换 T，
// 小数位去尾零）；解析不动原样返回。
func isoNaiveAt(s string) any {
	if len(s) < 19 {
		return nil
	}
	base := s[:10] + "T" + s[11:19]
	if len(s) > 19 && s[19] == '.' {
		if frac := strings.TrimRight(s[20:], "0"); frac != "" {
			return base + "." + frac
		}
	}
	return base
}

// trendPoint 历史端点行投影字段（走势只消费 timestamp/cnyFen）。
type trendPoint struct {
	Timestamp string `json:"timestamp"`
	CnyFen    int64  `json:"cnyFen"`
}

// trendMaxPoints 价格走势抽样上限（Python _TREND_MAX_POINTS）。
const trendMaxPoints = 72

// trendSeries 对齐 Python prices._trend_series：同日取末值、只保留价格发生
// 变化的日点 [YYYY-MM-DD, 分]（阶梯图无损），超上限等距抽样并保最低点与末点。
func trendSeries(points []trendPoint) []any {
	byDay := map[string]int64{}
	for _, p := range points {
		if p.CnyFen > 0 && p.Timestamp != "" {
			day := p.Timestamp
			if len(day) > 10 {
				day = day[:10]
			}
			byDay[day] = p.CnyFen // 同日赋值 = 取末值
		}
	}
	days := make([]string, 0, len(byDay))
	for d := range byDay {
		days = append(days, d)
	}
	sort.Strings(days)
	var dates []string
	var fens []int64
	for _, d := range days {
		fen := byDay[d]
		if len(fens) == 0 || fens[len(fens)-1] != fen {
			dates = append(dates, d)
			fens = append(fens, fen)
		}
	}
	idx := make([]int, 0, len(dates))
	for i := range dates {
		idx = append(idx, i)
	}
	if len(dates) > trendMaxPoints {
		stride := float64(len(dates)) / float64(trendMaxPoints)
		keep := map[int]bool{}
		for i := 0; i < trendMaxPoints; i++ {
			keep[int(float64(i)*stride)] = true
		}
		keep[len(dates)-1] = true
		minIdx := 0
		for i := 1; i < len(dates); i++ {
			if fens[i] < fens[minIdx] {
				minIdx = i
			}
		}
		keep[minIdx] = true
		idx = idx[:0]
		for i := range keep {
			idx = append(idx, i)
		}
		sort.Ints(idx)
	}
	out := make([]any, 0, len(idx))
	for _, i := range idx {
		out = append(out, []any{dates[i], fens[i]})
	}
	return out
}

// offerRow 对齐 Python _shared.offer_row：现价/史低带 data，类型键取
// offerLow/offerPrice，折扣以 extra 键值对覆盖。
func offerRow(name string, priceFen, lowFen *int64, extraKey string, extraVal any) *Obj {
	data := newObj()
	hasPrice := priceFen != nil && *priceFen > 0
	hasLow := lowFen != nil && *lowFen > 0
	if hasPrice {
		data.set("priceFen", *priceFen)
	}
	if hasLow {
		data.set("lowFen", *lowFen)
	}
	vKey := "offerPrice"
	if hasPrice && hasLow {
		vKey = "offerLow"
	}
	if extraKey != "" {
		vKey = extraKey // extra 键优先于默认值键（Python extra_key or default_key）
	}
	row := newObj()
	row.set("k", name)
	row.set("vKey", vKey)
	row.set("data", data)
	if extraKey != "" && extraVal != nil {
		row.set("v", pyStrOr(extraVal))
	}
	return row
}

// briefNoteItem 对齐 Python _shared.brief_item 的 note 变体（事件注记）。
func briefNoteItem(appid int64, brief account.Brief, hasBrief bool, note *Obj) *Obj {
	item := briefItem(appid, brief, hasBrief)
	item.set("note", note)
	return item
}

// ─── 目录/榜单 ───────────────────────────────────────────────────────────────

// topGames 热销榜 topsellers 库内前 8（榜单缓存零外网，顺序即榜序）。
// Python: top_games → games_boards.get_board("topsellers") + briefs_for。
func topGames(c *CallContext) (*ToolResult, error) {
	status, body, err := c.fetch(c.Ctx, "/api/v1/games/boards/topsellers")
	if err != nil {
		return nil, err
	}
	if status == http.StatusBadGateway {
		// 榜单源完全失败（无缓存无 stale）→ Python get_board 回 [] 的空态
		return emptyResult("no_data"), nil
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
	var page struct {
		AppIDs []string `json:"appIds"`
	}
	if err := jsonDecoder(body).Decode(&page); err != nil {
		return nil, err
	}
	total := len(page.AppIDs)
	if total == 0 {
		return emptyResult("no_data"), nil
	}
	limit := total
	if limit > readRowsLimit {
		limit = readRowsLimit
	}
	ids := make([]int64, 0, limit)
	for _, raw := range page.AppIDs[:limit] {
		id, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
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
		brief, ok := briefs[id]
		items = append(items, briefItem(id, brief, ok))
	}
	return &ToolResult{Data: gamesCard("top", items, total)}, nil
}

// ─── 价格事实 ────────────────────────────────────────────────────────────────

// priceCtxValue 价格上下文值（price-context 端点 at/lowest 元素）。
type priceCtxValue struct {
	CnyFen     int64  `json:"cnyFen"`
	Discount   int64  `json:"discount"`
	SnapshotAt string `json:"snapshotAt"`
}

// factsData 单游戏价格事实内部形态（briefing 与 compare 共用一份组装）。
type factsData struct {
	appid        int64
	name         string
	positiveRate *float64
	reviewCount  int64
	cnFen        any // nil = 国区不可买（矩阵无 CN 列且无上下文兜底）
	cnDiscount   int64
	alt          *Obj // {region, cnyFen, discount} | nil
	lowest       *Obj // {cnyFen, snapshotAt} | nil
	year         *Obj // {minFen, maxFen, medianFen, count} | nil
	trend        []any
}

// matrixColumn 详情价格矩阵列解码（[display, cnyFen, cents, discount]）。
func matrixColumn(raw json.RawMessage) []any {
	var col []any
	if json.Unmarshal(raw, &col) != nil {
		return nil
	}
	return col
}

// columnDiscount 矩阵列折扣位（col[3]；越界/非数回 0）。
func columnDiscount(col []any) int64 {
	if len(col) > 3 {
		if n, err := pyIntOr(col[3]); err == nil {
			return n
		}
	}
	return 0
}

// priceFacts 单游戏价格事实（Python prices.price_facts 等位）。
// 返回 nil = 游戏不存在（detail 404）。
func (c *CallContext) priceFacts(appid int64) (*factsData, error) {
	d, err := c.fetchDetail(appid)
	if err != nil || d == nil {
		return nil, err
	}
	today := time.Now().In(sched.DefaultLoc).Format("2006-01-02")
	path := "/api/v1/games/" + strconv.FormatInt(appid, 10)
	var ctx struct {
		At     *priceCtxValue `json:"at"`
		Lowest *priceCtxValue `json:"lowest"`
	}
	if err := c.getJSON(path+"/price-context?date="+today+"&region=cn", &ctx); err != nil {
		return nil, err
	}
	var hist struct {
		Points []trendPoint `json:"points"`
	}
	if err := c.getJSON(path+"/history?region=cn&days=365", &hist); err != nil {
		return nil, err
	}

	f := &factsData{appid: appid, name: d.Name, positiveRate: d.PositiveRate, reviewCount: d.ReviewCount}
	if col := matrixColumn(d.PriceMatrix["CN"]); col != nil && len(col) >= 2 {
		f.cnFen = col[1]
		f.cnDiscount = columnDiscount(col)
	}
	if f.cnFen == nil && ctx.At != nil && ctx.At.CnyFen > 0 {
		f.cnFen = ctx.At.CnyFen
		f.cnDiscount = ctx.At.Discount
	}
	// 国区不可买（锁区/未爬）→ 最低可购区兜底（详情 lowest 事实 + 矩阵折扣）
	if f.cnFen == nil && d.LowestCnyFen != nil && *d.LowestCnyFen > 0 && d.LowestRegionCode != "" {
		alt := newObj()
		alt.set("region", strings.ToUpper(d.LowestRegionCode))
		alt.set("cnyFen", *d.LowestCnyFen)
		alt.set("discount", columnDiscount(matrixColumn(d.PriceMatrix[strings.ToUpper(d.LowestRegionCode)])))
		f.alt = alt
	}
	if ctx.Lowest != nil {
		lowest := newObj()
		lowest.set("cnyFen", ctx.Lowest.CnyFen)
		lowest.set("snapshotAt", ctx.Lowest.SnapshotAt)
		f.lowest = lowest
	}
	yearFens := make([]int64, 0, len(hist.Points))
	for _, p := range hist.Points {
		if p.CnyFen > 0 {
			yearFens = append(yearFens, p.CnyFen)
		}
	}
	if len(yearFens) > 0 {
		sort.Slice(yearFens, func(i, j int) bool { return yearFens[i] < yearFens[j] })
		year := newObj()
		year.set("minFen", yearFens[0])
		year.set("maxFen", yearFens[len(yearFens)-1])
		year.set("medianFen", pyMedian(yearFens))
		year.set("count", len(yearFens))
		f.year = year
	}
	f.trend = trendSeries(hist.Points)
	return f, nil
}

// priceFactsObj 事实 → briefing 载荷（键序对齐 Python）。
func priceFactsObj(f *factsData) *Obj {
	out := newObj()
	out.set("kind", "price")
	out.set("appid", f.appid)
	out.set("name", f.name)
	out.set("positiveRate", f.positiveRate)
	out.set("reviewCount", f.reviewCount)
	cn := newObj()
	cn.set("cnyFen", f.cnFen)
	cn.set("discount", f.cnDiscount)
	out.set("cn", cn)
	out.set("alt", f.alt)
	out.set("lowest", f.lowest)
	out.set("year", f.year)
	out.set("trend", f.trend)
	return out
}

// getPriceBriefing 单游戏价格事实：国区现价+史低+近一年区间+走势。
func getPriceBriefing(c *CallContext) (*ToolResult, error) {
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	f, err := c.priceFacts(appid)
	if err != nil {
		return nil, err
	}
	if f == nil {
		return emptyResult("no_data"), nil
	}
	return &ToolResult{Data: priceFactsObj(f)}, nil
}

// getRegionPrices 全区现价清单（priceMatrix 唯一事实源），主账号结算区置顶。
func getRegionPrices(c *CallContext) (*ToolResult, error) {
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	d, err := c.fetchDetail(appid)
	if err != nil {
		return nil, err
	}
	if d == nil {
		return emptyResult("no_game"), nil
	}
	wallet, err := account.PrimaryWalletRegion(c.Ctx, c.db)
	if err != nil {
		return nil, err
	}
	accountRegion := strings.ToLower(wallet)
	codes := make([]string, 0, len(d.PriceMatrix))
	for code := range d.PriceMatrix {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	type regionRow struct {
		obj    *Obj
		region string
		cny    int64
	}
	items := make([]regionRow, 0, len(codes))
	for _, code := range codes {
		col := matrixColumn(d.PriceMatrix[code])
		if len(col) < 2 {
			continue
		}
		cny, err := pyIntOr(col[1])
		if err != nil || cny <= 0 {
			continue
		}
		row := newObj()
		row.set("region", strings.ToUpper(code))
		row.set("display", col[0])
		row.set("cnyFen", cny)
		row.set("discount", columnDiscount(col))
		items = append(items, regionRow{obj: row, region: strings.ToUpper(code), cny: cny})
	}
	sort.SliceStable(items, func(i, j int) bool {
		a, b := items[i], items[j]
		am, bm := strings.ToLower(a.region) != accountRegion, strings.ToLower(b.region) != accountRegion
		if am != bm {
			return !am // 主账号结算区置顶
		}
		if a.region != b.region {
			return a.region < b.region
		}
		return a.cny < b.cny
	})
	rows := make([]any, 0, len(items))
	for _, it := range items {
		rows = append(rows, it.obj)
	}
	out := newObj()
	out.set("kind", "region_prices")
	out.set("appid", appid)
	out.set("name", d.Name)
	out.set("accountRegion", accountRegion)
	out.set("items", rows)
	out.set("count", len(items))
	return &ToolResult{Data: out}, nil
}

// compareMax 并排对比上限（Python _COMPARE_MAX）。
const compareMax = 3

// compareItem 事实 → 对比行（CN 不可买取最低可购区）。
func compareItem(f *factsData) *Obj {
	useAlt := f.cnFen == nil && f.alt != nil
	var cnyFen any = f.cnFen
	discount := f.cnDiscount
	var region any
	if useAlt {
		cnyFen = f.alt.get("cnyFen")
		if n, err := pyIntOr(f.alt.get("discount")); err == nil {
			discount = n
		}
		region = f.alt.get("region")
	}
	var lowestFen any
	if f.lowest != nil {
		lowestFen = f.lowest.get("cnyFen")
	}
	var medianFen any
	if f.year != nil {
		medianFen = f.year.get("medianFen")
	}
	obj := newObj()
	obj.set("appid", f.appid)
	obj.set("name", f.name)
	obj.set("positiveRate", f.positiveRate)
	obj.set("reviewCount", f.reviewCount)
	obj.set("cnyFen", cnyFen)
	obj.set("discount", discount)
	obj.set("region", region)
	obj.set("lowestFen", lowestFen)
	obj.set("medianFen", medianFen)
	return obj
}

// compareGames 2~3 款并排对比（每款复用 price_facts 同一份事实）。
func compareGames(c *CallContext) (*ToolResult, error) {
	rawList, _ := c.Args["appids"].([]any)
	ids := make([]int64, 0, len(rawList))
	for _, raw := range rawList {
		aid, err := pyIntOr(raw)
		if err != nil || aid <= 0 {
			continue
		}
		dup := false
		for _, id := range ids {
			if id == aid {
				dup = true
				break
			}
		}
		if !dup {
			ids = append(ids, aid)
		}
	}
	if len(ids) < 2 {
		return emptyResult("need_two"), nil
	}
	if len(ids) > compareMax {
		ids = ids[:compareMax]
	}
	items := make([]any, 0, len(ids))
	for _, id := range ids {
		f, err := c.priceFacts(id)
		if err != nil {
			return nil, err
		}
		if f == nil {
			continue
		}
		items = append(items, compareItem(f))
	}
	if len(items) < 2 {
		return emptyResult("no_data"), nil
	}
	out := newObj()
	out.set("kind", "compare")
	out.set("items", items)
	return &ToolResult{Data: out}, nil
}

// ─── 价格诊断 ────────────────────────────────────────────────────────────────

// diagnosePrice 价格异常归因：appid≤0 投影最近失败任务，正常走覆盖诊断。
func diagnosePrice(c *CallContext) (*ToolResult, error) {
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	if appid <= 0 {
		return diagnoseRecentFailures(c)
	}
	return diagnoseGame(c, appid)
}

// diagnoseRecentFailures appid≤0：最近失败任务投影（前 5）。
func diagnoseRecentFailures(c *CallContext) (*ToolResult, error) {
	var jobs []map[string]any
	if err := c.getJSON("/api/v1/crawl/jobs?limit=10", &jobs); err != nil {
		return nil, err
	}
	var failed []map[string]any
	for _, j := range jobs {
		if pyStrOr(j["status"]) == "failed" {
			failed = append(failed, j)
		}
	}
	if len(failed) == 0 {
		return emptyResult("need_appid"), nil
	}
	rows := make([]any, 0, 5)
	for _, j := range failed {
		if len(rows) >= 5 {
			break
		}
		row := newObj()
		row.set("k", strings.TrimSpace("#"+pyStrOr(j["id"])+" "+pyStrOr(j["kind"])))
		row.set("v", pyCut(pyStrOr(j["error"]), 80))
		row.set("at", j["startedAt"])
		row.set("tone", "bad")
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsResult("jobs", rows, len(failed))}, nil
}

// diagPriceRow 活表价行（诊断消费字段）。
type diagPriceRow struct {
	region      string
	price       sql.NullInt64
	cnyFen      sql.NullInt64
	discount    int64
	outcome     sql.NullString
	answer      sql.NullString
	lastSuccess sql.NullString
}

// diagnoseGame 覆盖口径诊断：现价 + 启用区覆盖（outcome/answer/lastSuccessAt）。
// 覆盖计算与游戏卡同一口径（分母 = 启用区服，成功观察 = attempt_outcome='success'）。
func diagnoseGame(c *CallContext, appid int64) (*ToolResult, error) {
	var name string
	var minCny sql.NullInt64
	err := c.db.QueryRowContext(c.Ctx,
		"SELECT name, min_cny_fen FROM games WHERE appid = ?", appid).Scan(&name, &minCny)
	if err == sql.ErrNoRows {
		return emptyResult("no_data"), nil
	}
	if err != nil {
		return nil, err
	}
	rows, err := c.db.QueryContext(c.Ctx,
		"SELECT region_code, price, cny_fen, discount_percent, attempt_outcome, steam_answer,"+
			" CAST(last_success_at AS TEXT) FROM game_current_prices WHERE appid = ?", appid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	priceRows := make([]diagPriceRow, 0, 8)
	for rows.Next() {
		var r diagPriceRow
		if err := rows.Scan(&r.region, &r.price, &r.cnyFen, &r.discount,
			&r.outcome, &r.answer, &r.lastSuccess); err != nil {
			return nil, err
		}
		r.region = strings.ToUpper(r.region)
		priceRows = append(priceRows, r)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	// 启用区服 = 覆盖分母（与 effective_regions 同源）
	var regionsExpected []string
	regionRows, err := c.db.QueryContext(c.Ctx,
		"SELECT code FROM crawl_regions WHERE enabled = 1 ORDER BY sort")
	if err != nil {
		return nil, err
	}
	defer regionRows.Close()
	for regionRows.Next() {
		var code string
		if err := regionRows.Scan(&code); err != nil {
			return nil, err
		}
		regionsExpected = append(regionsExpected, strings.ToUpper(code))
	}
	if err := regionRows.Err(); err != nil {
		return nil, err
	}

	rowsByRegion := map[string]diagPriceRow{}
	var cnRow *diagPriceRow
	for i := range priceRows {
		r := priceRows[i]
		rowsByRegion[r.region] = r
		if r.region == "CN" {
			cnRow = &priceRows[i]
		}
	}
	type problem struct {
		code    string
		outcome string
		answer  string
		at      any
	}
	var problems []problem
	success := 0
	for _, code := range regionsExpected {
		p, ok := rowsByRegion[code]
		if !ok {
			problems = append(problems, problem{code: code, outcome: "notAttempted"})
			continue
		}
		if p.outcome.Valid && p.outcome.String == "success" {
			success++
			continue
		}
		outcome := "failed"
		if p.outcome.Valid && p.outcome.String != "" {
			outcome = p.outcome.String
		}
		answer := ""
		if p.answer.Valid {
			answer = p.answer.String
		}
		var at any
		if p.lastSuccess.Valid && p.lastSuccess.String != "" {
			at = isoNaiveAt(p.lastSuccess.String)
		}
		problems = append(problems, problem{code: code, outcome: outcome, answer: answer, at: at})
	}

	outRows := make([]any, 0, len(problems)+1)
	if len(regionsExpected) > 0 {
		row := newObj()
		row.set("k", name)
		row.set("vKey", "coverage")
		data := newObj()
		data.set("ok", success)
		data.set("total", len(regionsExpected))
		row.set("data", data)
		if len(problems) == 0 {
			row.set("tone", "ok")
		} else {
			row.set("tone", "warn")
		}
		outRows = append(outRows, row)
	}
	for i, p := range problems {
		if i >= readRowsLimit {
			break
		}
		row := newObj()
		row.set("k", p.code)
		row.set("vKey", p.outcome)
		row.set("v", p.answer)
		row.set("at", p.at)
		if p.outcome == "failed" {
			row.set("tone", "bad")
		} else {
			row.set("tone", "warn")
		}
		outRows = append(outRows, row)
	}
	if len(outRows) == 0 {
		row := newObj()
		row.set("k", name)
		row.set("vKey", "noCoverage")
		row.set("tone", "warn")
		outRows = append(outRows, row)
	}

	var cn any
	if cnRow != nil && cnRow.price.Valid && cnRow.price.Int64 > 0 {
		cnObj := newObj()
		if cnRow.cnyFen.Valid {
			cnObj.set("cnyFen", cnRow.cnyFen.Int64)
		} else {
			cnObj.set("cnyFen", 0)
		}
		cnObj.set("discount", cnRow.discount)
		cn = cnObj
	}
	var lowestPriceFen any
	if cnRow != nil && cnRow.price.Valid && (!minCny.Valid || cnRow.price.Int64 < minCny.Int64) {
		lowestPriceFen = cnRow.price.Int64
	} else if minCny.Valid {
		lowestPriceFen = minCny.Int64
	}
	if name == "" {
		name = fmt.Sprintf("AppID %d", appid)
	}
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", "diagnosis")
	out.set("rows", outRows)
	out.set("appid", appid)
	out.set("name", name)
	out.set("cn", cn)
	out.set("lowestPriceFen", lowestPriceFen)
	return &ToolResult{Data: out}, nil
}

// ─── 历史与版本 ──────────────────────────────────────────────────────────────

// histLowestView 历史端点史低值。
type histLowestView struct {
	CnyFen         int64    `json:"cnyFen"`
	CnyYuan        *float64 `json:"cnyYuan"`
	Timestamp      string   `json:"timestamp"`
	CnyYuanPoint   *float64 `json:"cnyYuanPoint"`
	FormattedPrice string   `json:"formattedPrice"`
}

// histHighestView 历史端点史高值。
type histHighestView struct {
	CnyFen  int64    `json:"cnyFen"`
	CnyYuan *float64 `json:"cnyYuan"`
}

// histVersionView 历史端点版本条目。
type histVersionView struct {
	SubID   *int64  `json:"subId"`
	Suffix  *string `json:"suffix"`
	IsGold  bool    `json:"isGold"`
	Product string  `json:"product"`
}

// histLowestObj 史低值 → Obj（nil 保持 null）。
func histLowestObj(v *histLowestView) *Obj {
	if v == nil {
		return nil
	}
	obj := newObj()
	obj.set("cnyFen", v.CnyFen)
	obj.set("cnyYuan", v.CnyYuan)
	obj.set("timestamp", v.Timestamp)
	obj.set("cnyYuanPoint", v.CnyYuanPoint)
	obj.set("formattedPrice", v.FormattedPrice)
	return obj
}

// histHighestObj 史高值 → Obj。
func histHighestObj(v *histHighestView) *Obj {
	if v == nil {
		return nil
	}
	obj := newObj()
	obj.set("cnyFen", v.CnyFen)
	obj.set("cnyYuan", v.CnyYuan)
	return obj
}

// histVersionObjs 版本条目 → Obj 列表。
func histVersionObjs(list []histVersionView) []any {
	out := make([]any, 0, len(list))
	for _, v := range list {
		obj := newObj()
		obj.set("subId", v.SubID)
		obj.set("suffix", v.Suffix)
		obj.set("isGold", v.IsGold)
		obj.set("product", v.Product)
		out = append(out, obj)
	}
	return out
}

// gamePriceHistory 历史价格走势：全量点位 → trend 抽样，极值与版本透传。
func gamePriceHistory(c *CallContext) (*ToolResult, error) {
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	region := "cn"
	if pyBool(c.Args["region"]) {
		region = strings.ToLower(strings.TrimSpace(pyStrOr(c.Args["region"])))
	}
	days := int64(365)
	if pyBool(c.Args["days"]) {
		if days, err = pyIntOr(c.Args["days"]); err != nil {
			return nil, err
		}
	}
	var subID *int64
	if pyBool(c.Args["sub_id"]) {
		v, err := pyIntOr(c.Args["sub_id"])
		if err != nil {
			return nil, err
		}
		subID = &v
	}
	queryDays := days
	if queryDays < 0 {
		// 负窗 = 全部时间（Python if days > 0 才开窗）
		queryDays = 0
	}
	q := urlValues()
	q.set("region", region)
	q.set("days", strconv.FormatInt(queryDays, 10))
	if subID != nil {
		q.set("subId", strconv.FormatInt(*subID, 10))
	}
	var hist struct {
		Region   string            `json:"region"`
		Points   []trendPoint      `json:"points"`
		Lowest   *histLowestView   `json:"lowest"`
		Highest  *histHighestView  `json:"highest"`
		Count    int64             `json:"count"`
		Versions []histVersionView `json:"versions"`
	}
	if err := c.getJSON("/api/v1/games/"+strconv.FormatInt(appid, 10)+"/history?"+q.encode(), &hist); err != nil {
		return nil, err
	}
	if len(hist.Points) == 0 {
		out := newObj()
		out.set("kind", "empty")
		out.set("note", "no_data")
		out.set("region", region)
		return &ToolResult{Data: out}, nil
	}
	count := hist.Count
	if count == 0 {
		count = int64(len(hist.Points))
	}
	outRegion := hist.Region
	if outRegion == "" {
		outRegion = region
	}
	out := newObj()
	out.set("kind", "price_history")
	out.set("appid", appid)
	out.set("region", outRegion)
	out.set("subId", subID)
	out.set("days", days)
	out.set("points", trendSeries(hist.Points))
	out.set("lowest", histLowestObj(hist.Lowest))
	out.set("highest", histHighestObj(hist.Highest))
	out.set("count", count)
	out.set("versions", histVersionObjs(hist.Versions))
	return &ToolResult{Data: out}, nil
}

// gameVersions 全版本 × 全区最新价（逐区压成 原币价/折扣/折合）。
func gameVersions(c *CallContext) (*ToolResult, error) {
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	var resp struct {
		Versions []struct {
			SubID   *int64                     `json:"subId"`
			Suffix  *string                    `json:"suffix"`
			IsGold  bool                       `json:"isGold"`
			Regions map[string]json.RawMessage `json:"regions"`
		} `json:"versions"`
	}
	if err := c.getJSON("/api/v1/games/"+strconv.FormatInt(appid, 10)+"/versions", &resp); err != nil {
		return nil, err
	}
	if len(resp.Versions) == 0 {
		return emptyResult("no_data"), nil
	}
	versions := make([]any, 0, len(resp.Versions))
	for _, v := range resp.Versions {
		obj := newObj()
		obj.set("subId", v.SubID)
		suffix := ""
		if v.Suffix != nil {
			suffix = *v.Suffix
		}
		obj.set("suffix", suffix)
		obj.set("isGold", v.IsGold)
		codes := make([]string, 0, len(v.Regions))
		for code := range v.Regions {
			codes = append(codes, code)
		}
		sort.Strings(codes)
		regions := newObj()
		for _, code := range codes {
			var r struct {
				Formatted string `json:"formatted"`
				Discount  int64  `json:"discount"`
				CnyFen    *int64 `json:"cnyFen"`
			}
			if err := json.Unmarshal(v.Regions[code], &r); err != nil {
				return nil, err
			}
			ro := newObj()
			ro.set("price", r.Formatted)
			ro.set("discount", r.Discount)
			ro.set("cnyFen", r.CnyFen)
			regions.set(code, ro)
		}
		obj.set("regions", regions)
		versions = append(versions, obj)
	}
	out := newObj()
	out.set("kind", "game_versions")
	out.set("appid", appid)
	out.set("versions", versions)
	return &ToolResult{Data: out}, nil
}

// ─── 捆绑包 ──────────────────────────────────────────────────────────────────

// bundleMembersLimit 成员截断线（超出只给 memberTotal）。
const bundleMembersLimit = 30

// bundleDetailView 捆绑包详情端点投影字段。
type bundleDetailView struct {
	BundleID          int64                      `json:"bundleId"`
	Name              string                     `json:"name"`
	URL               string                     `json:"url"`
	ItemKind          int64                      `json:"itemKind"`
	MustPurchaseAsSet int64                      `json:"mustPurchaseAsSet"`
	RegionPrices      map[string]json.RawMessage `json:"regionPrices"`
	Games             []struct {
		AppID  int64                      `json:"appid"`
		Name   *string                    `json:"name"`
		Prices map[string]json.RawMessage `json:"prices"`
	} `json:"games"`
	CnCnyFen     *int64 `json:"cnCnyFen"`
	LowestRegion string `json:"lowestRegion"`
	LowestCnyFen *int64 `json:"lowestCnyFen"`
	DiffFen      int64  `json:"diffFen"`
}

// fetchBundle 进程内取捆绑包详情；404 回 nil（包不存在，非错误）。
func (c *CallContext) fetchBundle(id int64) (*bundleDetailView, error) {
	status, body, err := c.fetch(c.Ctx, "/api/v1/bundles/"+strconv.FormatInt(id, 10))
	if err != nil {
		return nil, err
	}
	if status == http.StatusNotFound {
		return nil, nil
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
	var d bundleDetailView
	if err := jsonDecoder(body).Decode(&d); err != nil {
		return nil, err
	}
	return &d, nil
}

// regionPriceObj 区价行（剥离 appIds；键序对齐端点声明序）。
func regionPriceObj(raw json.RawMessage) (*Obj, error) {
	var r struct {
		PriceMinor      *int64  `json:"priceMinor"`
		Currency        *string `json:"currency"`
		DiscountPercent int64   `json:"discountPercent"`
		BaseDiscount    int64   `json:"baseDiscount"`
		CnyFen          *int64  `json:"cnyFen"`
		DiscountEndsAt  *int64  `json:"discountEndsAt"`
		LockedCount     int64   `json:"lockedCount"`
		Formatted       string  `json:"formatted"`
	}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &r); err != nil {
			return nil, err
		}
	}
	obj := newObj()
	obj.set("priceMinor", r.PriceMinor)
	obj.set("currency", r.Currency)
	obj.set("discountPercent", r.DiscountPercent)
	obj.set("baseDiscount", r.BaseDiscount)
	obj.set("cnyFen", r.CnyFen)
	obj.set("discountEndsAt", r.DiscountEndsAt)
	obj.set("lockedCount", r.LockedCount)
	obj.set("formatted", r.Formatted)
	return obj, nil
}

// memberPriceObj 成员单区价（priceMinor/currency/cnyFen）。
func memberPriceObj(raw json.RawMessage) (*Obj, error) {
	var r struct {
		PriceMinor *int64  `json:"priceMinor"`
		Currency   *string `json:"currency"`
		CnyFen     *int64  `json:"cnyFen"`
	}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &r); err != nil {
			return nil, err
		}
	}
	obj := newObj()
	obj.set("priceMinor", r.PriceMinor)
	obj.set("currency", r.Currency)
	obj.set("cnyFen", r.CnyFen)
	return obj, nil
}

// bundleDetail 捆绑包详情：各区包价 + 包内成员（各区现价）。
func bundleDetail(c *CallContext) (*ToolResult, error) {
	if !pyBool(c.Args["bundle_id"]) {
		return emptyResult("no_bundle"), nil
	}
	bid, err := pyIntOr(c.Args["bundle_id"])
	if err != nil {
		return nil, err
	}
	d, err := c.fetchBundle(bid)
	if err != nil {
		return nil, err
	}
	if d == nil {
		return emptyResult("no_bundle"), nil
	}
	codes := make([]string, 0, len(d.RegionPrices))
	for code := range d.RegionPrices {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	regionPrices := newObj()
	for _, code := range codes {
		obj, err := regionPriceObj(d.RegionPrices[code])
		if err != nil {
			return nil, err
		}
		regionPrices.set(code, obj)
	}
	memberTotal := len(d.Games)
	members := make([]any, 0, memberTotal)
	for _, m := range d.Games {
		if len(members) >= bundleMembersLimit {
			break
		}
		obj := newObj()
		obj.set("appid", m.AppID)
		obj.set("name", m.Name)
		pcodes := make([]string, 0, len(m.Prices))
		for code := range m.Prices {
			pcodes = append(pcodes, code)
		}
		sort.Strings(pcodes)
		prices := newObj()
		for _, code := range pcodes {
			po, err := memberPriceObj(m.Prices[code])
			if err != nil {
				return nil, err
			}
			prices.set(code, po)
		}
		obj.set("prices", prices)
		members = append(members, obj)
	}
	lowest := newObj()
	lowest.set("region", strPtrOrNull(d.LowestRegion))
	lowest.set("cnyFen", d.LowestCnyFen)
	out := newObj()
	out.set("kind", "bundle_detail")
	out.set("bundleId", d.BundleID)
	out.set("name", d.Name)
	out.set("url", strPtrOrNull(d.URL))
	out.set("itemKind", d.ItemKind)
	out.set("mustPurchaseAsSet", d.MustPurchaseAsSet)
	out.set("regionPrices", regionPrices)
	out.set("cnCnyFen", d.CnCnyFen)
	out.set("lowest", lowest)
	out.set("diffFen", d.DiffFen)
	out.set("members", members)
	out.set("memberTotal", memberTotal)
	return &ToolResult{Data: out}, nil
}

// listBundles 捆绑包列表前列（diff 差价降序，缓存聚合零外网）。
func listBundles(c *CallContext) (*ToolResult, error) {
	var resp struct {
		Bundles []struct {
			BundleID     int64  `json:"bundleId"`
			Name         string `json:"name"`
			CnCnyFen     *int64 `json:"cnCnyFen"`
			LowestRegion string `json:"lowestRegion"`
			LowestCnyFen *int64 `json:"lowestCnyFen"`
		} `json:"bundles"`
		Total int64 `json:"total"`
	}
	if err := c.getJSON("/api/v1/bundles?sort=diff", &resp); err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(resp.Bundles))
	for _, b := range resp.Bundles {
		k := b.Name
		if k == "" {
			k = fmt.Sprintf("Bundle %d", b.BundleID)
		}
		row := newObj()
		row.set("k", k)
		row.set("vKey", "bundleLow")
		data := newObj()
		data.set("priceFen", b.CnCnyFen)
		data.set("lowFen", b.LowestCnyFen)
		data.set("bundleId", b.BundleID)
		row.set("data", data)
		row.set("v", strings.ToUpper(b.LowestRegion))
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsResult("bundlesAll", rows, int(resp.Total))}, nil
}

// bundleNames 包名映射（Python bundles_service.names_for 等位：缺名回 Bundle N）。
func (c *CallContext) bundleNames(ids []int64) (map[int64]string, error) {
	out := map[int64]string{}
	for start := 0; start < len(ids); start += 500 {
		end := start + 500
		if end > len(ids) {
			end = len(ids)
		}
		part := ids[start:end]
		ph := make([]string, len(part))
		args := make([]any, len(part))
		for i, id := range part {
			ph[i] = "?"
			args[i] = id
		}
		rows, err := c.db.QueryContext(c.Ctx,
			"SELECT bundle_id, name FROM bundles WHERE bundle_id IN ("+strings.Join(ph, ",")+")", args...)
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var id int64
			var name string
			if err := rows.Scan(&id, &name); err != nil {
				rows.Close()
				return nil, err
			}
			if name == "" {
				name = fmt.Sprintf("Bundle %d", id)
			}
			out[id] = name
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return nil, err
		}
		rows.Close()
	}
	return out, nil
}

// listBundleFollows 关注的捆绑包清单。
func listBundleFollows(c *CallContext) (*ToolResult, error) {
	var resp struct {
		BundleIDs []int64 `json:"bundleIds"`
	}
	if err := c.getJSON("/api/v1/bundles/follows", &resp); err != nil {
		return nil, err
	}
	names, err := c.bundleNames(resp.BundleIDs)
	if err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(resp.BundleIDs))
	for _, id := range resp.BundleIDs {
		row := newObj()
		k, ok := names[id]
		if !ok {
			k = fmt.Sprintf("Bundle %d", id)
		}
		row.set("k", k)
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsResult("bundles", rows, len(resp.BundleIDs))}, nil
}

// ─── 优惠 / 汇率 / 日历 ──────────────────────────────────────────────────────

// hbMonthly 当月 HB 慈善包 contents（库内已爬快照）。
func hbMonthly(c *CallContext) (*ToolResult, error) {
	var resp struct {
		Games []struct {
			AppID        int64  `json:"appid"`
			Name         string `json:"name"`
			PriceFen     *int64 `json:"priceFen"`
			Discount     int64  `json:"discount"`
			LowestCnyFen *int64 `json:"lowestCnyFen"`
		} `json:"games"`
	}
	if err := c.getJSON("/api/v1/metadata/hb/offers", &resp); err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(resp.Games))
	for _, g := range resp.Games {
		name := g.Name
		if name == "" {
			name = fmt.Sprintf("AppID %d", g.AppID)
		}
		extraKey, extraVal := "", any(nil)
		if g.Discount != 0 {
			extraKey = "offerDiscount"
			extraVal = fmt.Sprintf("-%d%%", g.Discount)
		}
		rows = append(rows, offerRow(name, g.PriceFen, g.LowestCnyFen, extraKey, extraVal))
	}
	return &ToolResult{Data: rowsResult("hb", rows, len(rows))}, nil
}

// epicOfferURLDefault 清单条目缺 url 的兑底（Epic 免费游戏页）。
const epicOfferURLDefault = "https://store.epicgames.com/en-US/free-games"

// epicMobileURLDefault 移动商店页入口（App 内领取）。
const epicMobileURLDefault = "https://store.epicgames.com/mobile"

// epicFree Epic 当期 + 预告白送（库快照/缓存，不触发外网）。
func epicFree(c *CallContext) (*ToolResult, error) {
	var resp struct {
		Offers []struct {
			Title         string `json:"title"`
			TitleCn       string `json:"titleCn"`
			AppID         *int64 `json:"appid"`
			Start         string `json:"start"`
			End           string `json:"end"`
			Upcoming      bool   `json:"upcoming"`
			Image         string `json:"image"`
			URL           string `json:"url"`
			PriceOriginal string `json:"priceOriginal"`
		} `json:"offers"`
		Mobile *struct {
			Title  *string `json:"title"`
			Image  string  `json:"image"`
			URL    string  `json:"url"`
			End    *string `json:"end"`
			Worth  *string `json:"worth"`
			Source string  `json:"source"`
			Direct bool    `json:"direct"`
		} `json:"mobile"`
		FetchedAt *string `json:"fetchedAt"`
	}
	if err := c.getJSON("/api/v1/metadata/epic/offers", &resp); err != nil {
		return nil, err
	}
	offers := make([]any, 0, len(resp.Offers))
	rows := make([]any, 0, len(resp.Offers))
	for _, o := range resp.Offers {
		obj := newObj()
		obj.set("title", o.Title)
		obj.set("titleCn", o.TitleCn)
		obj.set("appid", o.AppID)
		obj.set("start", o.Start)
		obj.set("end", o.End)
		obj.set("upcoming", o.Upcoming)
		obj.set("image", o.Image)
		url := o.URL
		if url == "" {
			url = epicOfferURLDefault
		}
		obj.set("url", url)
		obj.set("priceOriginal", o.PriceOriginal)
		offers = append(offers, obj)

		k := o.TitleCn
		if k == "" {
			k = o.Title
		}
		if k == "" {
			if o.AppID != nil {
				k = fmt.Sprintf("AppID %d", *o.AppID)
			} else {
				k = "—"
			}
		}
		v := o.End
		if v == "" {
			v = o.Start
		}
		row := newObj()
		row.set("k", k)
		if o.Upcoming {
			row.set("vKey", "epicUpcoming")
		} else {
			row.set("vKey", "epicFree")
		}
		row.set("v", v)
		if o.Upcoming {
			row.set("tone", "warn")
		} else {
			row.set("tone", "ok")
		}
		rows = append(rows, row)
	}
	var mobile *Obj
	if resp.Mobile != nil {
		m := newObj()
		m.set("title", resp.Mobile.Title)
		m.set("image", resp.Mobile.Image)
		url := resp.Mobile.URL
		if url == "" {
			url = epicMobileURLDefault
		}
		m.set("url", url)
		m.set("end", resp.Mobile.End)
		m.set("worth", resp.Mobile.Worth)
		m.set("source", resp.Mobile.Source)
		m.set("direct", resp.Mobile.Direct)
		mobile = m
	}
	out := newObj()
	out.set("kind", "epic")
	out.set("titleKey", "epic")
	out.set("offers", offers)
	out.set("rows", rows)
	out.set("mobile", mobile)
	out.set("fetchedAt", resp.FetchedAt)
	out.set("total", len(offers))
	return &ToolResult{Data: out}, nil
}

// steamFree Steam 限时免费清单（库内 promo 快照）。
func steamFree(c *CallContext) (*ToolResult, error) {
	var resp struct {
		Offers []struct {
			AppID int64  `json:"appid"`
			Name  string `json:"name"`
		} `json:"offers"`
	}
	if err := c.getJSON("/api/v1/metadata/steam/offers", &resp); err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(resp.Offers))
	for _, g := range resp.Offers {
		name := g.Name
		if name == "" {
			name = fmt.Sprintf("AppID %d", g.AppID)
		}
		rows = append(rows, offerRow(name, nil, nil, "", nil))
	}
	return &ToolResult{Data: rowsResult("steamFree", rows, len(rows))}, nil
}

// priceDrops 最近价格事件（price_events 账本只读投影 + 简报）。
func priceDrops(c *CallContext) (*ToolResult, error) {
	var events []map[string]any
	if err := c.getJSON("/api/v1/crawl/price-events?limit=20", &events); err != nil {
		return nil, err
	}
	ids := make([]int64, 0, len(events))
	for _, e := range events {
		aid, err := pyIntOr(e["appid"])
		if err != nil || aid <= 0 {
			continue
		}
		ids = append(ids, aid)
	}
	briefs := map[int64]account.Brief{}
	if len(ids) > 0 {
		fetched, err := account.BriefsFor(c.Ctx, c.db, ids)
		if err != nil {
			return nil, err
		}
		briefs = fetched
	}
	items := make([]any, 0, len(ids))
	for _, e := range events {
		aid, err := pyIntOr(e["appid"])
		if err != nil || aid <= 0 {
			continue
		}
		note := newObj()
		note.set("key", "ev_"+strings.ToLower(pyStrOr(e["eventType"])))
		note.set("v", pyStrOr(e["region"]))
		note.set("at", e["occurredAt"])
		brief, ok := briefs[aid]
		items = append(items, briefNoteItem(aid, brief, ok, note))
	}
	return &ToolResult{Data: gamesCard("drops", items, len(items))}, nil
}

// ratesOverview 汇率快照（fx_rates 本地库，零外网）。
func ratesOverview(c *CallContext) (*ToolResult, error) {
	var resp struct {
		Rates []struct {
			Currency  string      `json:"currency"`
			RateToCny json.Number `json:"rateToCny"`
		} `json:"rates"`
	}
	if err := c.getJSON("/api/v1/rates", &resp); err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(resp.Rates))
	for _, r := range resp.Rates {
		row := newObj()
		row.set("k", r.Currency)
		data := newObj()
		data.set("rate", r.RateToCny)
		row.set("vKey", "rateLine")
		row.set("data", data)
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsResult("rates", rows, len(rows))}, nil
}

// steamEventView 活动日历条目投影字段。
type steamEventView struct {
	Key      string  `json:"key"`
	Category string  `json:"category"`
	NameEn   string  `json:"nameEn"`
	NameZh   *string `json:"nameZh"`
	Start    string  `json:"start"`
	End      string  `json:"end"`
}

// calendarUpcomingRowsLimit 日历行上限（live+upcoming 合计）。
const calendarUpcomingRowsLimit = 12

// seasonalSaleCategory 季节大促类目（置顶优先）。
const seasonalSaleCategory = "seasonal_sale"

// calendarEventName 行名：中文名 → 英文名 → key → 占位。
func calendarEventName(e steamEventView) string {
	if e.NameZh != nil && *e.NameZh != "" {
		return *e.NameZh
	}
	if e.NameEn != "" {
		return e.NameEn
	}
	if e.Key != "" {
		return e.Key
	}
	return "—"
}

// calendarLiveRow 进行中行。
func calendarLiveRow(e steamEventView) *Obj {
	row := newObj()
	row.set("k", calendarEventName(e))
	row.set("vKey", "eventLive")
	row.set("v", e.Start+"~"+e.End)
	row.set("tone", "ok")
	return row
}

// calendarUpcomingRow 预告行。
func calendarUpcomingRow(e steamEventView) *Obj {
	row := newObj()
	row.set("k", calendarEventName(e))
	row.set("vKey", "eventUpcoming")
	row.set("v", e.Start)
	return row
}

// calendarEvents Steam 官方活动日历（库内已爬，进行中在前）。
func calendarEvents(c *CallContext) (*ToolResult, error) {
	var resp struct {
		Events   []steamEventView `json:"events"`
		Live     []steamEventView `json:"live"`
		Upcoming []steamEventView `json:"upcoming"`
	}
	if err := c.getJSON("/api/v1/steam-events", &resp); err != nil {
		return nil, err
	}
	upcoming := resp.Upcoming
	if len(upcoming) == 0 {
		// 端点未给 upcoming 时由全量 events 按开始日派生（北京时间日历日）
		today := time.Now().In(sched.DefaultLoc).Format("2006-01-02")
		for _, e := range resp.Events {
			if e.Start > today {
				upcoming = append(upcoming, e)
			}
		}
	}
	ordered := make([]steamEventView, 0, len(upcoming))
	for _, e := range upcoming {
		if e.Category == seasonalSaleCategory {
			ordered = append(ordered, e)
		}
	}
	for _, e := range upcoming {
		if e.Category != seasonalSaleCategory {
			ordered = append(ordered, e)
		}
	}
	if len(ordered) > calendarUpcomingRowsLimit {
		ordered = ordered[:calendarUpcomingRowsLimit]
	}
	rows := make([]any, 0, len(resp.Live)+len(ordered))
	for _, e := range resp.Live {
		rows = append(rows, calendarLiveRow(e))
	}
	for _, e := range ordered {
		rows = append(rows, calendarUpcomingRow(e))
	}
	if len(rows) > calendarUpcomingRowsLimit {
		rows = rows[:calendarUpcomingRowsLimit]
	}
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", "calendar")
	out.set("rows", rows)
	out.set("total", len(rows))
	return &ToolResult{Data: out}, nil
}

// ─── 注册 ────────────────────────────────────────────────────────────────────

// RegisterCatalogPriceTools 登记目录/价格读面 16 工具（批次一）；重名即错。
// 预算按 budgetFor 逐工具取档（60 重扫聚合 / 40 网络往返 / 默认 20）。
func RegisterCatalogPriceTools(r *ToolRegistry) error {
	for _, spec := range []*ToolSpec{
		{
			Name: "top_games", Group: GroupRead, Risk: "low",
			Description: "列出 Steam 热销榜前列。用户问『现在什么游戏火/热销榜』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     topGames,
		},
		{
			Name: "get_price_briefing", Group: GroupRead, Risk: "low",
			Description: "取某游戏的价格事实：国区现价、历史最低、近一年区间",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 AppID"}
			}, "required": ["appid"]}`,
			Handler: getPriceBriefing,
		},
		{
			Name: "get_region_prices", Group: GroupRead, Risk: "low",
			Description: "取某游戏在全部已爬区域的现价：原币价、折合人民币、折扣，主账号结算区排最前。" +
				"用户问『XX 在我账号的地区/某区多少钱』『哪个区最便宜』或质疑回答里的地区不对时调用；" +
				"国区锁区的游戏也能查到其他区的价格",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 AppID"}
			}, "required": ["appid"]}`,
			Handler: getRegionPrices,
		},
		{
			Name: "compare_games", Group: GroupRead, Risk: "low",
			Description: "并排对比 2~3 款游戏的现价、折扣、史低、近一年中位价与好评率。" +
				"用户问『A 和 B 买哪个』『这几款哪个更划算』时调用；appids 先用检索工具确认",
			Parameters: `{"type": "object", "properties": {
				"appids": {"type": "array", "items": {"type": "integer"},
					"minItems": 2, "maxItems": 3, "description": "待对比游戏的 AppID（2~3 个）"}
			}, "required": ["appids"]}`,
			Handler: compareGames,
		},
		{
			Name: "diagnose_price", Group: GroupRead, Risk: "low",
			Description: "诊断某游戏的价格数据状态：各区最近抓取结果（成功/失败/未抓取）、上次成功时间、覆盖率。" +
				"用户问『为什么价格没更新』『数据是不是旧的』时调用；appid 可先用检索工具确认",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 AppID"}
			}, "required": ["appid"]}`,
			Handler: diagnosePrice,
		},
		{
			Name: "game_price_history", Group: GroupRead, Risk: "low",
			Description: "取某游戏的历史价格走势（默认国区近一年，可换区、换时间窗、指定版本），" +
				"点位已抽样并附区间最高最低。用户问『以前卖多少钱/什么时候史低过/现在算便宜吗』时调用",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 AppID"},
				"region": {"type": "string", "description": "区域代码（如 cn/us/ru），默认国区 cn"},
				"days": {"type": "integer", "description": "回看天数，默认 365"},
				"sub_id": {"type": "integer", "description": "版本 sub 包 ID，缺省为标准版序列"}
			}, "required": ["appid"]}`,
			Handler: gamePriceHistory,
		},
		{
			Name: "game_versions", Group: GroupRead, Risk: "low",
			Description: "取某游戏全部版本（标准版/豪华版/Gold 等）在各区的最新价格。" +
				"用户问『豪华版多少钱/黄金版和标准版差多少』时调用",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 AppID"}
			}, "required": ["appid"]}`,
			Handler: gameVersions,
		},
		{
			Name: "bundle_detail", Group: GroupRead, Risk: "low",
			Description: "取捆绑包详情：包内游戏清单与各区现价、各区包价折合人民币、最低区与差价。" +
				"用户问『这个捆绑包值不值/里面有什么』时调用；成员超过 30 个只列前 30 个",
			Parameters: `{"type": "object", "properties": {
				"bundle_id": {"type": "integer", "description": "捆绑包 ID"}
			}, "required": ["bundle_id"]}`,
			Handler: bundleDetail,
		},
		{
			Name: "list_bundles", Group: GroupRead, Risk: "low",
			Description: "列出捆绑包前列（差价降序，含国区现价与最低区价）。用户问『现在有什么捆绑包划算』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listBundles,
		},
		{
			Name: "list_bundle_follows", Group: GroupRead, Risk: "low",
			Description: "列出用户关注的捆绑包",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listBundleFollows,
		},
		{
			Name: "rates_overview", Group: GroupRead, Risk: "low",
			Description: "查看各币种对人民币的当前汇率。用户问『现在汇率多少』『某区折合人民币怎么算』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     ratesOverview,
		},
		{
			Name: "hb_monthly", Group: GroupRead, Risk: "low",
			Description: "列出当月 HB 慈善包（Humble Choice）的游戏清单，含国区现价与史低。" +
				"用户问『HB 月包/本月慈善包有哪些游戏』时调用",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    hbMonthly,
		},
		{
			Name: "epic_free", Group: GroupRead, Risk: "low",
			Description: "列出 Epic 当期正在送和预告即将送的游戏。用户问『Epic 喜加一/免费游戏』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     epicFree,
		},
		{
			Name: "steam_free", Group: GroupRead, Risk: "low",
			Description: "列出 Steam 正在限时免费的游戏。用户问『Steam 有什么免费领』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     steamFree,
		},
		{
			Name: "price_drops", Group: GroupRead, Risk: "low",
			Description: "列出最近的价格事件（新史低、降价、锁区、重新上架等）。" +
				"用户问『最近有什么降价』『哪些游戏创新低』时调用",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    priceDrops,
		},
		{
			Name: "calendar_events", Group: GroupRead, Risk: "low",
			Description: "查看 Steam 官方活动日历（进行中的促销与即将开始的活动）。用户问『最近有什么促销/打折活动』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     calendarEvents,
		},
	} {
		spec.TimeoutS = budgetFor(spec.Name)
		applyDefaultBudget(spec)
		if err := r.register(spec); err != nil {
			return err
		}
	}
	return nil
}
