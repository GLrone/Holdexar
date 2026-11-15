// 捆绑包抓取层：IStoreBrowseService/GetItems/v1（与 app 链路 storebrowse 同源）。
//
// 抓取区 = 监控启用区（与游戏侧同口径，全禁用区返回 ErrRegionsDisabled 让
// 上层区分 400 与跳过）；南亚（CC_LIST 的 pk 聚合位）按 Steam 实际拆
// 巴基斯坦（pk）/ 孟加拉（bd）两区各发一次。出网直连（browse 按
// country_code 返回各区数据，出口 IP 不参与判定）+ 进程级限流（与 app 爬取
// 链共享 200 发/300 秒窗口）。价格单位一律「分」：final_price_in_cents 对
// 零小数货币同样不 ×100（cny_fen 按「分 → 分」折算，存量归一见迁移 v2）。
package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/storebrowse"
)

// 全禁用区错误（对齐 regions.service.effective_regions 的 ValueError 文案）：
// 手动导入转 400、链尾按跳过语义处理。
var ErrRegionsDisabled = errors.New("未启用任何区服：请先在「我」页勾选要爬取的地区")

// browseEndpoint Steam browse 端点（包级 var：测试覆盖到本地打桩服务器）。
var browseEndpoint = storebrowse.BrowseURL

const (
	browseLang    = "english"        // 名称与 app 链路同口径，区域价与语言无关
	browseTimeout = 20 * time.Second // 单发请求超时（关注集每区一发，超长自动再切）
	ratesTTL      = 5 * time.Minute  // 汇率进程内缓存（对齐 get_rates 300s）
	cdnPrefix     = "https://shared.akamai.steamstatic.com/store_item_assets/"
)

// southAsiaExtra 南亚拆区：pk 聚合位 → Steam 侧 pk/bd 两个独立区。
// bd 不是独立追踪位，币种随南亚位（USD）。
var southAsiaExtra = map[string][]string{"pk": {"bd"}}

// ccCurrency cc（小写）→ 货币：对齐 crawler/config.py CC_LIST 的静态清单。
var ccCurrency = map[string]string{
	"cn": "CNY", "ru": "RUB", "kz": "KZT", "ua": "UAH",
	"pk": "USD", "tr": "USD", "ar": "USD", "az": "USD",
	"vn": "VND", "id": "IDR", "in": "INR", "br": "BRL",
	"cl": "CLP", "jp": "JPY", "hk": "HKD", "ph": "PHP", "tw": "TWD",
	"kw": "KWD", "sa": "SAR", "za": "ZAR", "qa": "QAR",
	"my": "MYR", "th": "THB", "pe": "PEN", "mx": "MXN",
	"sg": "SGD", "ae": "AED", "uy": "UYU", "co": "COP",
	"kr": "KRW", "nz": "NZD", "pl": "PLN", "cr": "CRC",
	"ca": "CAD", "us": "USD", "au": "AUD", "pt": "EUR",
	"gb": "GBP", "no": "NOK", "il": "ILS", "ch": "CHF",
}

func init() {
	for _, extra := range southAsiaExtra["pk"] {
		ccCurrency[extra] = ccCurrency["pk"]
	}
}

// defaultExchangeRates 汇率兜底表（fx_rates 缺失币种沿用；对齐
// games/pricing.py DEFAULT_EXCHANGE_RATES）。
var defaultExchangeRates = map[string]float64{
	"CNY": 1.0, "USD": 7.25, "RUB": 0.079, "KZT": 0.015,
	"UAH": 0.175, "TRY": 0.22, "ARS": 0.0083, "VND": 0.00029,
	"IDR": 0.00046, "INR": 0.087, "BRL": 1.42, "CLP": 0.0076,
	"JPY": 0.048, "HKD": 0.928, "PHP": 0.126, "AZN": 4.26,
	"TWD": 0.21, "KWD": 3.31, "SAR": 1.93, "ZAR": 0.38,
	"QAR": 1.97, "MYR": 1.55, "THB": 0.20, "PEN": 2.12,
	"MXN": 0.42, "SGD": 5.40, "AED": 1.97, "UYU": 0.21,
	"COP": 0.0018, "KRW": 0.0052, "NZD": 4.50, "PLN": 2.15,
	"CRC": 0.13, "CAD": 5.20, "AUD": 4.70, "EUR": 7.90,
	"GBP": 9.20, "NOK": 0.66, "ILS": 1.95, "CHF": 8.10,
}

// wantEntry 一次整表抓取的候选：(bundle_id, 库内形态)。Kind 为 nil 表示
// 形态未知（按 bundleid 问）。
type wantEntry struct {
	BundleID int64
	Kind     *int64
}

// regionRow browse 条目 → 区域行（价格统一「分」）。
type regionRow struct {
	BundleID        int64
	RegionCode      string
	Price           *int64
	OriginalPrice   *int64
	Currency        string
	DiscountPercent int64
	BaseDiscount    int64
	DiscountEndTS   *int64
	AppIDs          []int64
	PriceStatus     string
	Name            string
	HeaderImage     string
	MPS             *int64 // 购买语义票：nil = 锁区/无选项不投票
	ItemKind        *int64 // 形态票：nil = 条目未报 item_type
	Kind            string // 请求形态（bundleid/packageid）
}

// fetchCCs 本次捆绑包抓取的 cc 列表：监控启用区（sort,code 序）+ 南亚拆区展开。
func (s *Service) fetchCCs(ctx context.Context) ([]string, error) {
	rows, err := s.DB.QueryContext(ctx, "SELECT code FROM crawl_regions WHERE enabled=1 ORDER BY sort, code")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var enabled []string
	for rows.Next() {
		var code string
		if err := rows.Scan(&code); err != nil {
			return nil, err
		}
		enabled = append(enabled, strings.ToLower(code))
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if len(enabled) == 0 {
		return nil, ErrRegionsDisabled
	}
	ccs := make([]string, 0, len(enabled)+1)
	for _, code := range enabled {
		ccs = append(ccs, code)
		ccs = append(ccs, southAsiaExtra[code]...)
	}
	return ccs, nil
}

// loadRates 汇率表：fx_rates 覆盖兜底表 + CNY=1.0，TTL 缓存对齐 get_rates。
func (s *Service) loadRates(ctx context.Context) (map[string]float64, error) {
	s.ratesMu.Lock()
	defer s.ratesMu.Unlock()
	if s.ratesCache != nil && time.Since(s.ratesAt) < ratesTTL {
		return s.ratesCache, nil
	}
	rates := make(map[string]float64, len(defaultExchangeRates)+8)
	for code, rate := range defaultExchangeRates {
		rates[code] = rate
	}
	rows, err := s.DB.QueryContext(ctx, "SELECT currency_code, rate_to_cny FROM fx_rates")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var code string
		var rate float64
		if err := rows.Scan(&code, &rate); err != nil {
			continue // 转换失败跳过该行（对齐 Python try/except continue）
		}
		rates[strings.ToUpper(code)] = rate
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	rates["CNY"] = 1.0
	s.ratesCache = rates
	s.ratesAt = time.Now()
	return rates, nil
}

// fetchRegionsBatched 整表逐区一发：want → {bundle_id: 区域行}。
//
// 抓取区每区按形态（bundleid/packageid）分组各发；单发超 300 条或 URL 超长由
// PlanIDBatches 自动再切。全部（区 × 形态 × 批）请求一次 fan-out 并发，结果按
// 计划序合并（对齐 asyncio.gather 保序）。请求失败的区不写行（下轮重试），
// 锁区照写 locked，未收录（伪造 id）不写行。
func (s *Service) fetchRegionsBatched(ctx context.Context, want []wantEntry, ccs []string) (map[int64][]regionRow, error) {
	if ccs == nil {
		resolved, err := s.fetchCCs(ctx)
		if err != nil {
			return nil, err
		}
		ccs = resolved
	}
	// 形态分组（保持 want 首现顺序，对齐 Python dict 插入序）
	groups := map[string][]storebrowse.ID{}
	var kindOrder []string
	for _, w := range want {
		kind := browseKind(w.Kind)
		if _, seen := groups[kind]; !seen {
			kindOrder = append(kindOrder, kind)
		}
		groups[kind] = append(groups[kind], idSpec(w.BundleID, kind))
	}
	type fetchTask struct {
		cc, kind string
		ids      []storebrowse.ID
		batchIDs []int64
	}
	var tasks []fetchTask
	for _, cc := range ccs {
		for _, kind := range kindOrder {
			for _, batch := range storebrowse.PlanIDBatches(groups[kind], cc, browseLang, true, storebrowse.DefaultBatchSize) {
				task := fetchTask{cc: cc, kind: kind, ids: batch}
				for _, id := range batch {
					task.batchIDs = append(task.batchIDs, idOfKind(id, kind))
				}
				tasks = append(tasks, task)
			}
		}
	}
	results := make([][]storebrowse.Item, len(tasks))
	var wg sync.WaitGroup
	for i, task := range tasks {
		wg.Add(1)
		go func(i int, task fetchTask) {
			defer wg.Done()
			items, err := s.fetchBrowseRegion(ctx, task.ids, task.cc)
			if err != nil {
				return // 请求失败：该区该批不写行（下轮重试）
			}
			results[i] = items
		}(i, task)
	}
	wg.Wait()
	out := map[int64][]regionRow{}
	for i, task := range tasks {
		for _, pair := range alignItems(results[i], task.batchIDs) {
			row := browseRow(pair.item, pair.id, task.cc, task.kind)
			if row != nil {
				out[pair.id] = append(out[pair.id], *row)
			}
		}
	}
	return out, nil
}

// idSpec bundle_id 按形态填进对应字段（另一形态留 0 → omitempty 不编码）。
func idSpec(bundleID int64, kind string) storebrowse.ID {
	if kind == "packageid" {
		return storebrowse.ID{PackageID: bundleID}
	}
	return storebrowse.ID{BundleID: bundleID}
}

// idOfKind 取 ID 结构体在请求形态下的业务编号。
func idOfKind(id storebrowse.ID, kind string) int64 {
	if kind == "packageid" {
		return id.PackageID
	}
	return id.BundleID
}

// fetchBrowseRegion 单区一发。返回 store_items（与请求 ids 位置对齐）；
// 请求失败返回 nil。失败（nil）与「条目锁区」严格区分：前者不写行（下轮
// 刷新重试），后者照写 locked（该区不售，不进补抓账本）。
func (s *Service) fetchBrowseRegion(ctx context.Context, ids []storebrowse.ID, cc string) ([]storebrowse.Item, error) {
	url := browseEndpoint + strings.TrimPrefix(storebrowse.BuildIDsURL(ids, cc, browseLang, true), storebrowse.BrowseURL)
	_, body, err := s.client.GetJSON(ctx, url, "")
	if err != nil {
		return nil, err
	}
	var payload struct {
		Response *struct {
			StoreItems []storebrowse.Item `json:"store_items"`
		} `json:"response"`
	}
	if err := json.Unmarshal(body, &payload); err != nil || payload.Response == nil {
		return nil, nil // 形状异常按空表处理：该区不写行
	}
	return payload.Response.StoreItems, nil
}

// fetchBundleRegions 整包抓取（单包出口：手动导入共用，抓取区=监控启用区）。
//
// 形态：forcePackage（/sub/ 链接）→ 直接按 packageid 问；其余先按 bundleid
// 问，一条真实条目都没有（未收录）再按 packageid 兜一次——数字 ID 空间
// bundle/sub 并存，同号可能两侧都有产品。真实存在但全区锁区仍算成功
// （写 locked 行），与「抓取失败（空表）」严格区分。
func (s *Service) fetchBundleRegions(ctx context.Context, bundleID int64, forcePackage bool) ([]regionRow, error) {
	primary := int64(0)
	if forcePackage {
		primary = 1
	}
	kind := primary
	rows, err := s.fetchRegionsBatched(ctx, []wantEntry{{BundleID: bundleID, Kind: &kind}}, nil)
	if err != nil {
		return nil, err
	}
	out := rows[bundleID]
	if len(out) > 0 || forcePackage {
		return out, nil
	}
	alt := 1 - primary
	altRows, err := s.fetchRegionsBatched(ctx, []wantEntry{{BundleID: bundleID, Kind: &alt}}, nil)
	if err != nil {
		return nil, err
	}
	if len(altRows[bundleID]) > 0 {
		return altRows[bundleID], nil
	}
	return out, nil
}

// browseKind 形态 → ids 键：item_kind=1（Sub 形态）用 packageid，其余
// bundleid。判据是形态不是购买语义——mps（必须整包）在形态为 bundle 的包上
// 也会是 1，拿 mps 选键会按 packageid 去问一个 bundle 产品。
func browseKind(kind *int64) string {
	if kind != nil && *kind == 1 {
		return "packageid"
	}
	return "bundleid"
}

// isRealItem 条目是否对应真实商品。未收录（伪造 id / 已下架）也回
// success=15 + visible=false，与「真实存在但本区不售」同形——区分点在
// name / store_url_slug：真实商品两者皆有，未收录两者全空。
func isRealItem(item storebrowse.Item) bool {
	return item != nil && (pyStr(item["name"]) != "" || pyStr(item["store_url_slug"]) != "")
}

// browseOption 条目 → (price_status, 购买选项)。锁区/无选项语义对齐
// browse_store.evaluate。
func browseOption(item storebrowse.Item) (string, storebrowse.Item) {
	if success := toInt(item["success"]); success == nil || *success != 1 || !pyTruthy(item["visible"]) {
		return "locked", nil
	}
	if pyTruthy(item["unvailable_for_country_restriction"]) {
		return "locked", nil
	}
	opt := itemMap(item["best_purchase_option"])
	if len(opt) == 0 {
		opts := itemList(item["purchase_options"])
		if hasDefaultGroup(opts) {
			opts = defaultGroup(opts)
		}
		if len(opts) > 0 {
			opt = opts[0]
		} else {
			opt = nil
		}
	}
	if opt == nil {
		return "locked", nil // 可见但无购买选项 = 该区不售
	}
	if toInt(opt["final_price_in_cents"]) == nil {
		return "no_price", opt
	}
	return "ok", opt
}

func hasDefaultGroup(opts []storebrowse.Item) bool {
	for _, o := range opts {
		if pyStr(o["package_group"]) == "default" {
			return true
		}
	}
	return false
}

func defaultGroup(opts []storebrowse.Item) []storebrowse.Item {
	out := make([]storebrowse.Item, 0, len(opts))
	for _, o := range opts {
		if pyStr(o["package_group"]) == "default" {
			out = append(out, o)
		}
	}
	return out
}

// browseRow browse 条目 → 区域行。未收录/条目缺失 → nil（不写行）。
func browseRow(item storebrowse.Item, bundleID int64, cc, kind string) *regionRow {
	if item == nil || !isRealItem(item) {
		return nil
	}
	status, option := browseOption(item)
	hasOpt := option != nil
	if option == nil {
		option = storebrowse.Item{}
	}
	price := toInt(option["final_price_in_cents"])
	// 捆绑包原价 = 叠折前价（price_before_bundle_discount）；促销时另有 original
	original := orInt(toInt(option["price_before_bundle_discount"]), toInt(option["original_price_in_cents"]))
	discount := int64(0)
	if d := toInt(option["discount_pct"]); d != nil {
		discount = *d
	}
	if discount == 0 && original != nil && price != nil && *original > *price {
		discount = int64(math.RoundToEven(float64(*original-*price) * 100 / float64(*original)))
	}
	// 促销截止：active_discounts[0] 是当前叠加在基础档上的限时促销
	// （bundle_discount_pct 是无时限的结构档），Unix 秒；无促销 = nil
	var discountEndTS *int64
	if active := itemList(option["active_discounts"]); len(active) > 0 {
		discountEndTS = toInt(active[0]["discount_end_date"])
	}
	// 形态（链接/CDN 用）：item_type 1=Sub / 2=Bundle，Steam 权威
	var itemKind *int64
	switch itemType := toInt(item["item_type"]); {
	case itemType != nil && *itemType == 1:
		itemKind = itemType
	case itemType != nil && *itemType == 2:
		zero := int64(0)
		itemKind = &zero
	}
	// 购买语义（是否必须整包）：Sub 形态恒不可拆；bundle 形态以选项级
	// must_purchase_as_set 为准（形态与语义解耦，不能只看 item_type）。
	// 锁区行（无真实选项）不投语义票（mps=nil）：逐区选项不一致时由落库层
	// 多数决归位，不能让锁区行伪装成「可补齐」的反对票
	var mps *int64
	one, zero := int64(1), int64(0)
	switch {
	case itemKind != nil && *itemKind == 1:
		mps = &one // Sub 形态恒不可拆
	case hasOpt && pyTruthy(option["must_purchase_as_set"]):
		mps = &one
	case itemKind != nil && *itemKind == 0 && hasOpt:
		mps = &zero // 有真实选项且未标必须整包 → 可补齐
	}
	var appIDs []int64
	if raw, ok := item["included_appids"].([]any); ok {
		for _, v := range raw {
			if id := toInt(v); id != nil {
				appIDs = append(appIDs, *id)
			}
		}
	}
	row := &regionRow{
		BundleID:        bundleID,
		RegionCode:      cc,
		Price:           price,
		OriginalPrice:   original,
		Currency:        ccCurrency[cc],
		DiscountPercent: discount,
		BaseDiscount:    int64(0),
		DiscountEndTS:   discountEndTS,
		AppIDs:          appIDs,
		PriceStatus:     status,
		Name:            pyStr(item["name"]),
		HeaderImage:     headerImageURL(item),
		MPS:             mps,
		ItemKind:        itemKind,
		Kind:            kind,
	}
	if bd := toInt(option["bundle_discount_pct"]); bd != nil {
		row.BaseDiscount = *bd
	}
	return row
}

// headerImageURL 条目资产 → 头图 URL（捆绑包链路复用同一资产约定）。
func headerImageURL(item storebrowse.Item) string {
	if item == nil {
		return ""
	}
	assets := itemMap(item["assets"])
	format := pyStr(assets["asset_url_format"])
	filename := pyStr(assets["header"])
	if format == "" || filename == "" {
		return ""
	}
	return cdnPrefix + strings.ReplaceAll(format, "${FILENAME}", filename)
}

// alignedPair 条目 ↔ 请求 id 的对齐对；条目缺失为 nil。
type alignedPair struct {
	id   int64
	item storebrowse.Item
}

// alignItems 条目 ↔ 请求 id 对齐：条数一致按位置（未收录条目回 id=0，位置
// 是唯一可靠映射）；条数不符降级按 id 映射。
func alignItems(items []storebrowse.Item, ids []int64) []alignedPair {
	if len(items) == len(ids) {
		out := make([]alignedPair, len(ids))
		for i, id := range ids {
			var item storebrowse.Item
			if i < len(items) {
				item = items[i]
			}
			out[i] = alignedPair{id: id, item: item}
		}
		return out
	}
	byID := map[int64]storebrowse.Item{}
	for _, item := range items {
		if item == nil {
			continue
		}
		if id := toInt(item["id"]); id != nil && *id != 0 {
			byID[*id] = item
		}
	}
	out := make([]alignedPair, 0, len(ids))
	for _, id := range ids {
		out = append(out, alignedPair{id: id, item: byID[id]})
	}
	return out
}

// rowCnyFen 分 → CNY 分：所有币种统一 cents，直接 × 汇率（与 games/pricing
// 同约定）。无小数货币（JPY/KRW/VND…）不可按「除数 1 再 ×100」折算——
// *_in_cents 对所有币种都是「分」，那样会把分当元再乘 100，cny_fen 虚高
// 100 倍（存量已由迁移 v2 归位）。无价/无汇率 → nil。
func rowCnyFen(row regionRow, rateMap map[string]float64) *int64 {
	if row.Price == nil || *row.Price == 0 {
		return nil
	}
	rate, ok := rateMap[strings.ToUpper(row.Currency)]
	if !ok {
		return nil
	}
	fen := int64(math.RoundToEven(float64(*row.Price) * rate))
	return &fen
}

// majority 0/1 票多数决；平票/无票 → nil（不改写库内既有值）。
func majority(votes []int64) *int64 {
	if len(votes) == 0 {
		return nil
	}
	ones, zeros := 0, 0
	for _, v := range votes {
		if v == 1 {
			ones++
		} else {
			zeros++
		}
	}
	if ones == zeros {
		return nil
	}
	result := int64(0)
	if ones > zeros {
		result = 1
	}
	return &result
}

// dedupInt64 去重计数用（单品甄别判 len(set(appids))）。
func dedupInt64(ids []int64) []int64 {
	seen := map[int64]bool{}
	out := ids[:0]
	for _, id := range ids {
		if !seen[id] {
			seen[id] = true
			out = append(out, id)
		}
	}
	return out
}

// ── Python 动态值取数助手（JSON 反序列化值形态：nil/bool/string/float64/
// []any/map[string]any）───────────────────────────────────────────────

// toInt 对齐 _to_int：int()/int(float) 截断、整数字符串可转，其余 None。
func toInt(v any) *int64 {
	switch t := v.(type) {
	case int64:
		return &t
	case int:
		i := int64(t)
		return &i
	case float64:
		i := int64(t)
		return &i
	case string:
		if i, err := strconv.ParseInt(strings.TrimSpace(t), 10, 64); err == nil {
			return &i
		}
	}
	return nil
}

// orInt Python `a or b`：a 非 nil 且非 0 取 a，否则取 b。
func orInt(a, b *int64) *int64 {
	if a != nil && *a != 0 {
		return a
	}
	return b
}

// pyTruthy Python truthy 判定（bool/数字/字符串/容器）。
func pyTruthy(v any) bool {
	switch t := v.(type) {
	case nil:
		return false
	case bool:
		return t
	case string:
		return t != ""
	case int64:
		return t != 0
	case int:
		return t != 0
	case float64:
		return t != 0
	case []any:
		return len(t) > 0
	case map[string]any:
		return len(t) > 0
	}
	return true
}

// pyStr Python `v or ""` 的字符串取值：非字符串一律空串。
func pyStr(v any) string {
	s, _ := v.(string)
	return s
}

// itemMap 字段 → 选项对象（非对象回 nil）。
func itemMap(v any) storebrowse.Item {
	m, _ := v.(map[string]any)
	return m
}

// itemList 字段 → 条目数组（非数组元素跳过）。
func itemList(v any) []storebrowse.Item {
	raw, _ := v.([]any)
	out := make([]storebrowse.Item, 0, len(raw))
	for _, e := range raw {
		if m, ok := e.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

// pyJSONInts 整数数组 → Python json.dumps 缺省分隔符（", "）文本。
// 包内自持同源副本（与 dbwriter/rates 同源，包间禁 import）。
func pyJSONInts(ids []int64) string {
	var b strings.Builder
	b.WriteByte('[')
	for i, id := range ids {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString(strconv.FormatInt(id, 10))
	}
	b.WriteByte(']')
	return b.String()
}

// sortedKeys map 键排序（快照 tracked 参数序列化前归一）。
func sortedKeys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// nullInt64 SQL NULL → 指针；invalid → nil。
func bundleNullKind(v sql.NullInt64) *int64 {
	if !v.Valid {
		return nil
	}
	value := v.Int64
	return &value
}
