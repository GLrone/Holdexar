package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/storebrowse"
)

var ErrRegionsDisabled = errors.New("未启用任何区服：请先在「我」页勾选要爬取的地区")

var browseEndpoint = storebrowse.BrowseURL

const (
	browseLang    = "english"
	browseTimeout = 20 * time.Second
	ratesTTL      = 5 * time.Minute
	cdnPrefix     = "https://shared.akamai.steamstatic.com/store_item_assets/"
)

// southAsiaExtra 将南亚追踪位 pk 展开为 pk/bd 两个请求区，均按 USD 计价。
var southAsiaExtra = map[string][]string{"pk": {"bd"}}

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

type wantEntry struct {
	BundleID int64
	Kind     *int64
}

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
	ItemKind        *int64
	Kind            string
}

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
			continue
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

func (s *Service) fetchRegionsBatched(ctx context.Context, want []wantEntry, ccs []string) (map[int64][]regionRow, error) {
	if ccs == nil {
		resolved, err := s.fetchCCs(ctx)
		if err != nil {
			return nil, err
		}
		ccs = resolved
	}
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
		wg.Go(func() {
			items, err := s.fetchBrowseRegion(ctx, task.ids, task.cc)
			if err != nil {
				return
			}
			results[i] = items
		})
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

func idSpec(bundleID int64, kind string) storebrowse.ID {
	if kind == "packageid" {
		return storebrowse.ID{PackageID: bundleID}
	}
	return storebrowse.ID{BundleID: bundleID}
}

func idOfKind(id storebrowse.ID, kind string) int64 {
	if kind == "packageid" {
		return id.PackageID
	}
	return id.BundleID
}

// fetchBrowseRegion 失败返错/nil（不写行，留待重试）；条目锁区仍写 locked 行。
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
		return nil, nil
	}
	return payload.Response.StoreItems, nil
}

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

// browseKind 按形态 item_kind（1=Sub 用 packageid，其余 bundleid）而非购买语义 mps 选键。
func browseKind(kind *int64) string {
	if kind != nil && *kind == 1 {
		return "packageid"
	}
	return "bundleid"
}

// isRealItem 以 name 或 store_url_slug 非空区分真实商品与未收录/伪造 id（两者均可能回 success=15 + visible=false）。
func isRealItem(item storebrowse.Item) bool {
	return item != nil && (pyStr(item["name"]) != "" || pyStr(item["store_url_slug"]) != "")
}

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
		return "locked", nil
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
	// active_discounts[0] 为当前限时促销截止（Unix 秒），区别于无时限结构折扣 bundle_discount_pct。
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
	// Sub 形态恒不可拆；bundle 形态取选项级 must_purchase_as_set。
	// 锁区行不投语义票（mps=nil），防止无选项区伪装成可补齐反对票。
	var mps *int64
	one, zero := int64(1), int64(0)
	switch {
	case itemKind != nil && *itemKind == 1:
		mps = &one
	case hasOpt && pyTruthy(option["must_purchase_as_set"]):
		mps = &one
	case itemKind != nil && *itemKind == 0 && hasOpt:
		mps = &zero
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

type alignedPair struct {
	id   int64
	item storebrowse.Item
}

// alignItems 条目 ↔ 请求 id 对齐：条数一致按位置（未收录条目 id=0 须靠位置映射）；否则降级按 id 映射。
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

// rowCnyFen 所有币种均以分为单位直接乘汇率（零小数币种同口径，不可再 ×100）。
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

func orInt(a, b *int64) *int64 {
	if a != nil && *a != 0 {
		return a
	}
	return b
}

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

func pyStr(v any) string {
	s, _ := v.(string)
	return s
}

func itemMap(v any) storebrowse.Item {
	m, _ := v.(map[string]any)
	return m
}

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

// pyJSONInts 将整数切片格式化为 ", " 分隔的 JSON 数组文本。
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

func sortedKeys(m map[string]bool) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

func bundleNullKind(v sql.NullInt64) *int64 {
	if !v.Valid {
		return nil
	}
	value := v.Int64
	return &value
}
