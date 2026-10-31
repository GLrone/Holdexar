package agent

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"

	"holdexar/server-go/internal/games"
)

// ─── 目录检索 ───────────────────────────────────────────────────────────────

// searchGames 本地目录名检索（含未爬价/锁区游戏；不触发抓取）。
// Python: search_catalog → {"kind": "games", "items": [...]}。
func searchGames(c *CallContext) (*ToolResult, error) {
	q := strings.TrimSpace(pyStrOr(c.Args["q"]))
	items, err := c.searchCatalog(q)
	if err != nil {
		return nil, err
	}
	out := newObj()
	out.set("kind", "games")
	out.set("items", items)
	return &ToolResult{Data: out}, nil
}

// searchCatalog 目录检索：优先 FTS 影子列，退回 LIKE（games 包单一实现）。
// 行形状 = 检索原始行（basePriceFen 键，无中文支持列——与 Python 工具链一致）。
func (c *CallContext) searchCatalog(q string) ([]any, error) {
	rows, err := games.SearchCatalog(c.Ctx, c.db, q, 5)
	if err != nil {
		return nil, err
	}
	items := make([]any, 0, len(rows))
	for _, r := range rows {
		obj := newObj()
		obj.set("appid", r["appid"])
		obj.set("name", r["name"])
		obj.set("nameEn", r["nameEn"])
		obj.set("basePriceFen", r["basePriceFen"])
		obj.set("discount", r["discount"])
		obj.set("positiveRate", r["positiveRate"])
		obj.set("reviewCount", r["reviewCount"])
		items = append(items, obj)
	}
	return items, nil
}

// gameItem 清单行 → 推荐卡条目（appid/name/cnyFen/discount/positiveRate/
// reviewCount/chineseSupport；清单行无中文支持列 → null，与 Python _game_item 一致）。
func (c *CallContext) gameItem(it map[string]any) *Obj {
	obj := newObj()
	obj.set("appid", it["appid"])
	obj.set("name", it["name"])
	obj.set("cnyFen", it["basePriceFen"])
	obj.set("discount", it["discount"])
	obj.set("positiveRate", pyRateValue(it["positiveRate"]))
	obj.set("reviewCount", it["reviewCount"])
	obj.set("chineseSupport", nil)
	return obj
}

// pyRateValue 好评率恒为 positive_rate/100 的 Python 浮点（98.0 形态）；
// 端点 JSON 的整数字面量（encoding/json 渲染 95.0 → 95）忠实重建为浮点。
func pyRateValue(v any) any {
	if n, ok := v.(json.Number); ok && !strings.Contains(n.String(), ".") {
		if f, err := n.Float64(); err == nil {
			return f
		}
	}
	return v
}

// recommendGames 结构化条件挑库内游戏（smart 排序；自然语言整句不作名称）。
func recommendGames(c *CallContext) (*ToolResult, error) {
	onlyDiscounted := pyBool(c.Args["only_discounted"])
	minRating, err := pyIntOr(c.Args["min_rating"])
	if err != nil {
		return nil, err
	}
	maxPriceYuan := c.Args["max_price_yuan"]
	query := urlValues()
	query.set("sort", "smart")
	query.set("limit", "5")
	query.set("onlyDiscounted", strconv.FormatBool(onlyDiscounted))
	query.set("minRating", strconv.FormatInt(minRating, 10))
	if pyBool(maxPriceYuan) {
		f, err := pyFloatOr(maxPriceYuan)
		if err != nil {
			return nil, err
		}
		query.set("maxPrice", strconv.FormatInt(int64(f*100), 10))
	}
	var page struct {
		Items []map[string]any `json:"items"`
	}
	if err := c.getJSON("/api/v1/games?"+query.encode(), &page); err != nil {
		return nil, err
	}
	items := make([]any, 0, len(page.Items))
	for _, it := range page.Items {
		items = append(items, c.gameItem(it))
	}
	out := newObj()
	out.set("kind", "games")
	out.set("items", items)
	return &ToolResult{Data: out}, nil
}

// ─── 数据查看 ───────────────────────────────────────────────────────────────

// detailView 游戏详情端点响应中工具投影所需的字段切片。
type detailView struct {
	Name             string                     `json:"name"`
	NameEn           *string                    `json:"nameEn"`
	ReleaseDate      *string                    `json:"releaseDate"`
	ReviewCount      int64                      `json:"reviewCount"`
	PositiveRate     *float64                   `json:"positiveRate"`
	ChineseSupport   *string                    `json:"chineseSupport"`
	Tags             []tagRef                   `json:"tags"`
	HlFlag           int64                      `json:"hlFlag"`
	FreeKind         *string                    `json:"freeKind"`
	PriceMatrix      map[string]json.RawMessage `json:"priceMatrix"`
	LowestCnyFen     *int64                     `json:"lowestCnyFen"`
	LowestRegionCode string                     `json:"lowestRegionCode"`
	SavingsFen       int64                      `json:"savingsFen"`
	CnCnyFen         *int64                     `json:"cnCnyFen"`
	CnDiscount       int64                      `json:"cnDiscount"`
	CnDiscountEndsAt *int64                     `json:"cnDiscountEndsAt"`
	RemovedAt        *string                    `json:"removedAt"`
	Versions         []any                      `json:"versions"`
	LinkedBundles    []bundleView               `json:"linkedBundles"`
}

type tagRef struct {
	Name   *string `json:"name"`
	NameEn *string `json:"nameEn"`
}

// bundleView 关联捆绑包条目（详情端点 linkedBundles 数组元素）。
type bundleView struct {
	BundleID       int64  `json:"bundleId"`
	Name           string `json:"name"`
	URL            string `json:"url"`
	ItemKind       int64  `json:"itemKind"`
	PriceCny       *int64 `json:"priceCny"`
	LowestRegion   string `json:"lowestRegion"`
	LowestPriceFen *int64 `json:"lowestPriceFen"`
	DiffFen        int64  `json:"diffFen"`
}

// fetchDetail 进程内取游戏详情；404 回 nil（游戏不存在，非错误）。
func (c *CallContext) fetchDetail(appid int64) (*detailView, error) {
	var d detailView
	status, body, err := c.fetch(c.Ctx, "/api/v1/games/"+strconv.FormatInt(appid, 10))
	if err != nil {
		return nil, err
	}
	if status == http.StatusNotFound {
		return nil, nil
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
	d.PriceMatrix = map[string]json.RawMessage{}
	dec := jsonDecoder(body)
	if err := dec.Decode(&d); err != nil {
		return nil, err
	}
	return &d, nil
}

// gameDetail 游戏完整档案瘦身投影（大数组压成计数与摘要）。
func gameDetail(c *CallContext) (*ToolResult, error) {
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	if appid <= 0 {
		return emptyResult("no_game"), nil
	}
	d, err := c.fetchDetail(appid)
	if err != nil {
		return nil, err
	}
	if d == nil {
		return emptyResult("no_game"), nil
	}
	tracked, err := c.trackedGame(appid)
	if err != nil {
		return nil, err
	}
	tags := make([]any, 0, len(d.Tags))
	for _, t := range d.Tags {
		name := t.Name
		if name == nil || *name == "" {
			name = t.NameEn
		}
		if name == nil || *name == "" {
			continue
		}
		tags = append(tags, *name)
		if len(tags) >= 10 {
			break
		}
	}
	cn := newObj()
	cn.set("cnyFen", d.CnCnyFen)
	cn.set("discount", d.CnDiscount)
	cn.set("discountEndsAt", d.CnDiscountEndsAt)
	lowest := newObj()
	lowest.set("region", strPtrOrNull(d.LowestRegionCode))
	lowest.set("cnyFen", d.LowestCnyFen)
	lowest.set("savingsFen", d.SavingsFen)
	out := newObj()
	out.set("kind", "game_detail")
	out.set("appid", appid)
	out.set("name", d.Name)
	out.set("nameEn", d.NameEn)
	out.set("releaseDate", d.ReleaseDate)
	out.set("reviewCount", d.ReviewCount)
	out.set("positiveRate", d.PositiveRate)
	out.set("chineseSupport", d.ChineseSupport)
	out.set("tags", tags)
	out.set("cn", cn)
	out.set("priceRegionCount", len(d.PriceMatrix))
	out.set("lowest", lowest)
	out.set("hlFlag", d.HlFlag)
	out.set("freeKind", d.FreeKind)
	out.set("tracked", tracked)
	out.set("versionCount", len(d.Versions))
	out.set("linkedBundleCount", len(d.LinkedBundles))
	out.set("removedAt", d.RemovedAt)
	return &ToolResult{Data: out}, nil
}

// linkedBundles 游戏关联捆绑包（详情端点同一数据源，剥离封面图）。
func linkedBundles(c *CallContext) (*ToolResult, error) {
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	if appid <= 0 {
		return emptyResult("no_data"), nil
	}
	d, err := c.fetchDetail(appid)
	if err != nil {
		return nil, err
	}
	if d == nil {
		return emptyResult("no_data"), nil
	}
	items := make([]any, 0, len(d.LinkedBundles))
	for _, b := range d.LinkedBundles {
		obj := newObj()
		obj.set("bundleId", b.BundleID)
		obj.set("name", b.Name)
		obj.set("url", b.URL)
		obj.set("itemKind", b.ItemKind)
		obj.set("priceCny", b.PriceCny)
		obj.set("lowestRegion", b.LowestRegion)
		obj.set("lowestPriceFen", b.LowestPriceFen)
		obj.set("diffFen", b.DiffFen)
		items = append(items, obj)
	}
	if len(items) == 0 {
		return emptyResult("no_data"), nil
	}
	if len(items) > readRowsLimit {
		items = items[:readRowsLimit]
	}
	out := newObj()
	out.set("kind", "bundles")
	out.set("appid", appid)
	out.set("items", items)
	out.set("total", len(d.LinkedBundles))
	return &ToolResult{Data: out}, nil
}

// ratesHistory 汇率走势：日线尾部窗口 + 等距抽样（保末点）。
func ratesHistory(c *CallContext) (*ToolResult, error) {
	cur := strings.ToUpper(strings.TrimSpace(pyStrOr(c.Args["currency"])))
	if cur == "" {
		cur = "USD"
	}
	daysRaw := c.Args["days"]
	if !pyBool(daysRaw) {
		daysRaw = int64(90)
	}
	days, err := pyIntOr(daysRaw)
	if err != nil {
		return nil, err
	}
	window := days
	if window < 1 {
		window = 1
	}
	var rows []struct {
		Date      string   `json:"date"`
		RateToCny *float64 `json:"rateToCny"`
	}
	if err := c.getJSON("/api/v1/rates/history?currency="+cur+"&limit="+strconv.FormatInt(window, 10), &rows); err != nil {
		return nil, err
	}
	seq := make([]any, 0, len(rows))
	values := make([]float64, 0, len(rows))
	for _, r := range rows {
		if r.RateToCny == nil {
			continue
		}
		point := []any{r.Date, *r.RateToCny}
		seq = append(seq, point)
		values = append(values, *r.RateToCny)
	}
	if len(seq) == 0 {
		out := newObj()
		out.set("kind", "empty")
		out.set("note", "no_data")
		out.set("currency", cur)
		return &ToolResult{Data: out}, nil
	}
	if len(seq) > ratesMaxPoints {
		seq = sampleKeep(seq, ratesMaxPoints)
	}
	out := newObj()
	out.set("kind", "rates_history")
	out.set("currency", cur)
	out.set("days", window)
	out.set("points", seq)
	out.set("min", pyMin(values))
	out.set("max", pyMax(values))
	out.set("last", values[len(values)-1])
	out.set("count", len(rows))
	return &ToolResult{Data: out}, nil
}

// ratesMaxPoints 汇率走势抽样上限（与价格走势同量级）。
const ratesMaxPoints = 72

// sampleKeep 等距抽样：int(i*stride) 全保留 + 末点（Python 同构）。
func sampleKeep(seq []any, limit int) []any {
	stride := float64(len(seq)) / float64(limit)
	keep := map[int]bool{}
	for i := 0; i < limit; i++ {
		keep[int(float64(i)*stride)] = true
	}
	keep[len(seq)-1] = true
	idx := make([]int, 0, len(keep))
	for i := range keep {
		idx = append(idx, i)
	}
	sort.Ints(idx)
	out := make([]any, 0, len(idx))
	for _, i := range idx {
		out = append(out, seq[i])
	}
	return out
}

func pyMin(v []float64) float64 {
	m := v[0]
	for _, x := range v[1:] {
		if x < m {
			m = x
		}
	}
	return m
}

func pyMax(v []float64) float64 {
	m := v[0]
	for _, x := range v[1:] {
		if x > m {
			m = x
		}
	}
	return m
}

// regionsConfig 区服配置：全量区服（含启用标记）+ 已购抓取区。
func regionsConfig(c *CallContext) (*ToolResult, error) {
	var resp struct {
		Regions []struct {
			Code     string `json:"code"`
			Name     string `json:"name"`
			Currency string `json:"currency"`
			Enabled  bool   `json:"enabled"`
		} `json:"regions"`
		OwnedRegions []string `json:"ownedRegions"`
	}
	if err := c.getJSON("/api/v1/regions", &resp); err != nil {
		return nil, err
	}
	regions := make([]any, 0, len(resp.Regions))
	enabledCount := 0
	for _, r := range resp.Regions {
		if r.Enabled {
			enabledCount++
		}
		obj := newObj()
		obj.set("code", r.Code)
		obj.set("name", r.Name)
		obj.set("currency", r.Currency)
		obj.set("enabled", r.Enabled)
		regions = append(regions, obj)
	}
	out := newObj()
	out.set("kind", "regions_config")
	out.set("regions", regions)
	out.set("enabledCount", enabledCount)
	if resp.OwnedRegions == nil {
		out.set("ownedRegions", nil)
	} else {
		out.set("ownedRegions", any(resp.OwnedRegions))
	}
	return &ToolResult{Data: out}, nil
}

// ─── 用户面 ─────────────────────────────────────────────────────────────────

// notificationsStatus 通知偏好 + 投递指标合并视图（设置页与统计同源）。
func notificationsStatus(c *CallContext) (*ToolResult, error) {
	var prefs map[string]any
	if err := c.getJSON("/api/v1/notifications/prefs", &prefs); err != nil {
		return nil, err
	}
	var stat map[string]any
	if err := c.getJSON("/api/v1/notifications/stats", &stat); err != nil {
		return nil, err
	}
	smtp, _ := prefs["smtp"].(map[string]any)
	enabled := pyBool(prefs["enabled"])
	smtpConfigured := pyBool(mapGet(smtp, "configured"))
	rows := make([]any, 0, 4)
	row1 := newObj()
	row1.set("k", "")
	row1.set("vKey", "notifyEnabled")
	if enabled {
		row1.set("tone", "ok")
	} else {
		row1.set("tone", "warn")
	}
	d1 := newObj()
	d1.set("quiet", pyBool(prefs["quietEnabled"]))
	d1.set("quietStart", mapGet(prefs, "quietStart"))
	d1.set("quietEnd", mapGet(prefs, "quietEnd"))
	row1.set("data", d1)
	rows = append(rows, row1)
	row2 := newObj()
	row2.set("k", "")
	row2.set("vKey", "notifyStats")
	d2 := newObj()
	d2.set("delivered", statInt(stat, "delivered"))
	d2.set("failed", statInt(stat, "failed"))
	d2.set("pending", statInt(stat, "pending"))
	d2.set("retryable", statInt(stat, "retryable"))
	row2.set("data", d2)
	if statInt(stat, "failed") != 0 {
		row2.set("tone", "bad")
	} else {
		row2.set("tone", "ok")
	}
	rows = append(rows, row2)
	row3 := newObj()
	row3.set("k", "")
	row3.set("vKey", "notifyChannel")
	d3 := newObj()
	d3.set("configured", smtpConfigured)
	d3.set("account", pyStrOr(mapGet(smtp, "userMasked")))
	row3.set("data", d3)
	if smtpConfigured {
		row3.set("tone", "ok")
	} else {
		row3.set("tone", "warn")
	}
	rows = append(rows, row3)
	if last, ok := prefs["lastDelivery"].(map[string]any); ok && last != nil {
		lastStatus := pyStrOr(mapGet(last, "status"))
		row4 := newObj()
		row4.set("k", "")
		row4.set("vKey", "notifyLast")
		row4.set("v", lastStatus)
		row4.set("at", mapGet(last, "at"))
		d4 := newObj()
		d4.set("attempts", mapGet(last, "attempts"))
		d4.set("reason", pyStrOr(mapGet(last, "reason")))
		row4.set("data", d4)
		if lastStatus == "delivered" {
			row4.set("tone", "ok")
		} else {
			row4.set("tone", "bad")
		}
		rows = append(rows, row4)
	}
	disabled := []any{}
	if cats, ok := prefs["categories"].([]any); ok {
		for _, raw := range cats {
			cat, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			if !pyBool(cat["enabled"]) {
				disabled = append(disabled, cat["key"])
			}
		}
	}
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", "notifyStatus")
	out.set("rows", rows)
	out.set("total", len(rows))
	out.set("enabled", enabled)
	out.set("disabledCategories", disabled)
	out.set("smtpConfigured", smtpConfigured)
	out.set("delivered", statInt(stat, "delivered"))
	out.set("failed", statInt(stat, "failed"))
	return &ToolResult{Data: out}, nil
}

// notificationHistory 通知投递账本最近记录（收件邮箱脱敏）。
func notificationHistory(c *CallContext) (*ToolResult, error) {
	limit := clampInt(c.Args["limit"], 20, 1, 500)
	var resp struct {
		Items []map[string]any `json:"items"`
	}
	if err := c.getJSON("/api/v1/notifications/candidates?limit="+strconv.FormatInt(limit, 10), &resp); err != nil {
		return nil, err
	}
	if len(resp.Items) == 0 {
		out := newObj()
		out.set("kind", "empty")
		out.set("items", []any{})
		return &ToolResult{Data: out}, nil
	}
	rows := make([]any, 0, len(resp.Items))
	for _, it := range resp.Items {
		head := pyStrOr(it["eventType"]) + "·" + pyStrOr(it["region"])
		row := newObj()
		row.set("k", strings.Trim(head, "·"))
		row.set("vKey", "notifyCandidate")
		row.set("v", maskAddr(pyStrOr(it["recipient"])))
		row.set("at", it["deliveredAt"])
		data := newObj()
		data.set("appid", it["appid"])
		data.set("status", it["status"])
		data.set("attempts", it["attempts"])
		row.set("data", data)
		switch it["status"] {
		case "delivered":
			row.set("tone", "ok")
		case "failed":
			row.set("tone", "bad")
		default:
			row.set("tone", nil)
		}
		rows = append(rows, row)
	}
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", "notifyHistory")
	out.set("rows", rows)
	out.set("total", len(resp.Items))
	return &ToolResult{Data: out}, nil
}

// ─── 公共小件 ───────────────────────────────────────────────────────────────

// toolAppid 工具入参 appid：int(args.get("appid") or 0)。
func toolAppid(args map[string]any) (int64, error) {
	if !pyBool(args["appid"]) {
		return 0, nil
	}
	return pyIntOr(args["appid"])
}

// emptyResult kind=empty 快捷构造。
func emptyResult(note string) *ToolResult {
	obj := newObj()
	obj.set("kind", "empty")
	obj.set("note", note)
	return &ToolResult{Status: StatusEmpty, Data: obj}
}

// trackedGame 关注态：monitor_sources 有效来源且未被排除（monitoring
// active_ids 的成员判定等价式；Python 工具链同源语义）。
func (c *CallContext) trackedGame(appid int64) (bool, error) {
	row := c.db.QueryRowContext(c.Ctx,
		"SELECT EXISTS(SELECT 1 FROM monitor_sources ms"+
			" JOIN monitor_targets mt ON mt.target_type = ms.target_type AND mt.target_id = ms.target_id"+
			" WHERE ms.target_type = 'game' AND ms.target_id = ? AND ms.active = 1"+
			" AND NOT EXISTS (SELECT 1 FROM monitor_exclusions me"+
			" WHERE me.target_type = 'game' AND me.target_id = ms.target_id AND me.active = 1))", appid)
	var tracked bool
	if err := row.Scan(&tracked); err != nil {
		return false, err
	}
	return tracked, nil
}

func mapGet(m map[string]any, key string) any {
	if m == nil {
		return nil
	}
	return m[key]
}

// statInt 统计字段 → int64（JSON 数值缺省 0）。
func statInt(stat map[string]any, key string) int64 {
	n, err := pyIntOr(mapGet(stat, key))
	if err != nil {
		return 0
	}
	return n
}

// pyStrOr str(value or "")：None/False/0 → 空串；其余按字符串化。
func pyStrOr(v any) string {
	if !pyBool(v) {
		return ""
	}
	switch x := v.(type) {
	case string:
		return x
	case json.Number:
		return x.String()
	case bool:
		if x {
			return "True"
		}
		return "False"
	default:
		return fmt.Sprintf("%v", x)
	}
}

func strPtrOrNull(s string) any {
	if s == "" {
		return nil
	}
	return s
}
