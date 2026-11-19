package dbwriter

import (
	"math"
	"regexp"
	"strings"
	"time"
)

type BatchEntry struct {
	Game   GameData
	Prices []PriceRow
}

// 指针 nil = 键缺省/显式 None：带缺省的列落模型缺省值，其余落 NULL。
type GameData struct {
	AppID           int64
	Name            *string
	NameEn          *string
	Type            *string
	HeaderImage     *string
	StoreURL        *string
	ChineseSupport  *string
	FamilySharing   *bool
	TradingCards    *bool
	IsAdult         *bool
	IsVisualNovel   *bool
	ReleaseDate     *string
	Developers      []any // JSON 列；nil 落 JSON null 字面量，空切片落 []
	Publishers      []any
	PositiveRate    *int64
	PositiveReviews *int64
	ReviewCount     *int64
	CreatedAt       *time.Time
	UpdatedAt       *time.Time
}

// nil 取字段缺省值（RegionCode/Currency=""、数值/bool=0/false、PriceStatus="ok"）。
type PriceRow struct {
	AppID             int64
	RegionCode        *string
	Currency          *string
	Price             *int64
	OriginalPrice     *int64
	DiscountPercent   *int64
	SubID             *int64
	IsGold            *bool
	VersionSuffix     *string
	IsBundle          *bool
	PriceStatus       *string
	DiscountEndTS     *int64
	DiscountDesc      *string
	BundleID          *int64
	BundleDiscountPct *int64
	SteamAnswer       *string
	PromoEndTS        *int64 // 免费分类取结束时间用，不落价格表
}

// 显式命名的标准版（"Standard Edition"/"Standard Digital Edition"）是本体
// 自身的官方命名，不算版本款；带附加词的礼包 SKU 不在此列。
var plainStandardSuffixRe = regexp.MustCompile(`(?i)^standard( digital)? edition$`)

// 仅匹配锚定测试入口词形，防裸 beta/测试误伤正常标题。
var testEntryRe = regexp.MustCompile(
	`(?i)测试版|测试服|技术测试|封闭测试|公开测试|压力测试|内测|公测|封测|体验版` +
		`|play[\t\n\f\r \v_-]?test|beta[\t\n\f\r \v_-]?test|closed[\t\n\f\r \v_-]?beta` +
		`|open[\t\n\f\r \v_-]?beta|stress[\t\n\f\r \v_-]?test|tech[\t\n\f\r \v_-]?test`)

func isPlainStandardSuffix(suffix *string) bool {
	if suffix == nil || *suffix == "" {
		return false
	}
	return plainStandardSuffixRe.MatchString(strings.TrimSpace(*suffix))
}

func isTestEntry(name string) bool {
	return name != "" && testEntryRe.MatchString(name)
}

func isOkPriceRow(price *int64) bool { return price != nil }

// computeCnyFen 按银行家舍入折算人民币分；0 为免费合法值，无汇率返回 nil。
func (w *Writer) computeCnyFen(priceCents int64, currency string) *int64 {
	if priceCents == 0 {
		fen := int64(0)
		return &fen
	}
	rate, ok := w.fxRate(currency)
	if !ok {
		return nil
	}
	fen := int64(math.RoundToEven(float64(priceCents) * rate))
	return &fen
}

func (w *Writer) fxRate(currency string) (float64, bool) {
	if r, ok := w.FxRates[currency]; ok {
		return r, true
	}
	if currency == "CNY" {
		return 1.0, true
	}
	return 0, false
}

type gameRowValues struct {
	appid         int64
	name, nameEn  any
	typ           any
	headerImage   any
	storeURL      any
	chineseSupp   any
	familySharing int64
	tradingCards  int64
	isEpic        int64
	isHb          int64
	hlFlag        int64
	ppFlag        int64
	diffFen       int64
	isAdult       int64
	isVisualNovel int64
	removedStrik  int64
	releaseDate   any
	developers    string
	publishers    string
	positiveRate  any
	positiveRev   int64
	reviewCount   int64
	viewCount     int64
	createdAt     any
	updatedAt     string
	// hasFree=false 时不写入免费态两列，防纯 locked/missing 响应洗掉免费标记。
	hasFree  bool
	freeKind any
	promoEnd any
	nowTime  time.Time
}

func b2i(b bool) int64 {
	if b {
		return 1
	}
	return 0
}

func (w *Writer) normalizeGame(g GameData, prices []PriceRow) gameRowValues {
	now := w.Now()
	if g.UpdatedAt != nil {
		now = *g.UpdatedAt
	}
	row := gameRowValues{
		appid:         g.AppID,
		name:          g.Name,
		nameEn:        g.NameEn,
		typ:           g.Type,
		headerImage:   g.HeaderImage,
		storeURL:      g.StoreURL,
		chineseSupp:   g.ChineseSupport,
		releaseDate:   g.ReleaseDate,
		familySharing: b2i(derefBool(g.FamilySharing, false)),
		tradingCards:  b2i(derefBool(g.TradingCards, false)),
		isAdult:       b2i(derefBool(g.IsAdult, false)),
		isVisualNovel: b2i(derefBool(g.IsVisualNovel, false)),
		developers:    PyDumps(g.Developers),
		publishers:    PyDumps(g.Publishers),
		positiveRate:  g.PositiveRate,
		positiveRev:   derefI64(g.PositiveReviews, 0),
		reviewCount:   derefI64(g.ReviewCount, 0),
		createdAt:     timeOrNilStr(g.CreatedAt),
		updatedAt:     FormatNaive(now),
		nowTime:       now,
	}
	// 测试入口命名判定优先于价格分类，确保挂正价的 Beta 页仍标 beta 脱池。
	state := classifyFreeKind(prices)
	if isTestEntry(derefStr(g.Name, "")) {
		state = &freeState{kind: "beta"}
	}
	if state != nil {
		row.hasFree = true
		if state.kind != "" {
			row.freeKind = state.kind
		}
		if state.promoEnd != nil {
			row.promoEnd = *state.promoEnd
		}
	}
	return row
}

func timeOrNilStr(t *time.Time) any {
	if t == nil {
		return nil
	}
	return FormatNaive(*t)
}

type freeState struct {
	kind     string
	promoEnd *int64
}

// classifyFreeKind 优先以 CN ok 行作锚区（无 CN 回退首个 ok 行）判 free_kind：
// price=0 按 original>0 区分 promo/f2p；仅 CN 在场且付费时才显式清标记（防非 CN 任务误清），
// 无 ok 行返回 nil（保留原免费态）。
func classifyFreeKind(prices []PriceRow) *freeState {
	var okRows []*PriceRow
	for i := range prices {
		p := &prices[i]
		if derefStr(p.PriceStatus, "ok") == "ok" && p.Price != nil {
			okRows = append(okRows, p)
		}
	}
	if len(okRows) == 0 {
		return nil
	}
	kindOf := func(p *PriceRow) string {
		if *p.Price != 0 {
			return ""
		}
		if derefI64(p.OriginalPrice, 0) > 0 {
			return "promo"
		}
		return "f2p"
	}
	var cn *PriceRow
	for _, p := range okRows {
		if strings.ToUpper(derefStr(p.RegionCode, "")) == "CN" {
			cn = p
			break
		}
	}
	probe := cn
	if probe == nil {
		probe = okRows[0]
	}
	kind := kindOf(probe)
	if kind == "" {
		if cn != nil {
			return &freeState{}
		}
		return nil
	}
	if kind != "promo" {
		return &freeState{kind: "f2p"}
	}
	var maxEnd *int64
	for _, p := range okRows {
		if *p.Price == 0 && derefI64(p.OriginalPrice, 0) > 0 && p.PromoEndTS != nil {
			if maxEnd == nil || *p.PromoEndTS > *maxEnd {
				maxEnd = p.PromoEndTS
			}
		}
	}
	return &freeState{kind: "promo", promoEnd: maxEnd}
}

type obsKey struct {
	region string
	sub    int64
}

type snapTuple struct {
	price    int64
	original *int64
	discount int64
	suffix   *string
	isGold   bool
}

type baseline struct {
	known  map[int64]string
	latest map[obsKey]snapTuple
}

// currentRow 现价行（degraded 仅作决策标记，不落库）。
type currentRow struct {
	appid          int64
	region         string
	currency       string
	price          *int64
	original       *int64
	discount       int64
	subID          int64
	status         string
	cnyFen         *int64
	discountEnd    *int64
	updatedAt      string
	steamAnswer    *string
	attemptOutcome string
	lastSuccessAt  *string
	degraded       bool
}

type historyRow struct {
	appid         int64
	region        string
	currency      string
	price         int64
	original      *int64
	discount      int64
	subID         int64
	isGold        bool
	suffix        *string
	isBundle      bool
	status        string
	cnyFen        *int64
	discountEnd   *int64
	discountDesc  *string
	bundleID      *int64
	bundleDiscPct *int64
	eventKey      *string
	source        string
	snapshotAt    string
}

type stdCandidate struct {
	row      currentRow
	degraded bool
}

func (w *Writer) planPriceRows(prices []PriceRow, base baseline, now time.Time, eventKey *string) (
	current []currentRow, history []historyRow, okRegions, degradedRegions map[string]bool,
) {
	nowStr := FormatNaive(now)
	var historyBatch []historyRow
	standardCandidates := map[string][]stdCandidate{}
	var candidateOrder []string

	for i := range prices {
		p := &prices[i]
		region := strings.ToUpper(derefStr(p.RegionCode, ""))
		priceCents := p.Price
		currency := derefStr(p.Currency, "")
		isGold := derefBool(p.IsGold, false)
		suffix := p.VersionSuffix
		// 缺省版本后缀沿用同 sub_id 历史名，保持标准版判定与差量比较一致。
		if suffix == nil || *suffix == "" {
			if s, ok := base.known[derefI64(p.SubID, 0)]; ok {
				suffix = &s
			}
		}
		isBundle := derefBool(p.IsBundle, false)
		status := derefStr(p.PriceStatus, "ok")
		// 质量门禁：ok 但无价格视为坏数据并降级为 missing，不写历史；
		// 仅当选中行自身被降级时才记现价失败章并触发区级欠账结转。
		rowDegraded := false
		if status == "ok" && !isOkPriceRow(priceCents) {
			status = "missing"
			rowDegraded = true
		}
		var cnyFen *int64
		if priceCents != nil {
			cnyFen = w.computeCnyFen(*priceCents, currency)
		}

		// 仅 price>0 或限时赠送（price=0 且 original>0）写历史，相对最新快照无变化则跳过。
		if status == "ok" && priceCents != nil && (*priceCents > 0 || derefI64(p.OriginalPrice, 0) > 0) {
			prev, hasPrev := base.latest[obsKey{region, derefI64(p.SubID, 0)}]
			same := hasPrev && prev == snapTuple{
				price:    *priceCents,
				original: p.OriginalPrice,
				discount: derefI64(p.DiscountPercent, 0),
				suffix:   suffix,
				isGold:   isGold,
			}
			if !same {
				historyBatch = append(historyBatch, historyRow{
					appid:         p.AppID,
					region:        region,
					currency:      currency,
					price:         *priceCents,
					original:      p.OriginalPrice,
					discount:      derefI64(p.DiscountPercent, 0),
					subID:         derefI64(p.SubID, 0),
					isGold:        isGold,
					suffix:        suffix,
					isBundle:      isBundle,
					status:        status,
					cnyFen:        cnyFen,
					discountEnd:   p.DiscountEndTS,
					discountDesc:  p.DiscountDesc,
					bundleID:      p.BundleID,
					bundleDiscPct: p.BundleDiscountPct,
					eventKey:      eventKey,
					source:        "steam",
					snapshotAt:    nowStr,
				})
			}
		}

		isStandard := !isGold && !isBundle &&
			(suffix == nil || *suffix == "" || isPlainStandardSuffix(suffix))
		if isStandard {
			if _, ok := standardCandidates[region]; !ok {
				candidateOrder = append(candidateOrder, region)
			}
			standardCandidates[region] = append(standardCandidates[region], stdCandidate{
				row: currentRow{
					appid:       p.AppID,
					region:      region,
					currency:    currency,
					price:       priceCents,
					original:    p.OriginalPrice,
					discount:    derefI64(p.DiscountPercent, 0),
					subID:       derefI64(p.SubID, 0),
					status:      status,
					cnyFen:      cnyFen,
					discountEnd: p.DiscountEndTS,
					updatedAt:   nowStr,
					steamAnswer: p.SteamAnswer,
				},
				degraded: rowDegraded,
			})
		}
	}

	var currentBatch []currentRow
	seenRegions := map[string]bool{}
	for _, region := range candidateOrder {
		cands := standardCandidates[region]
		pool := cands
		var priced []stdCandidate
		for _, c := range cands {
			if c.row.price != nil {
				priced = append(priced, c)
			}
		}
		if len(priced) > 0 {
			pool = priced
		}
		sel := pool[0]
		for _, c := range pool[1:] {
			if c.row.subID < sel.row.subID {
				sel = c
			}
		}
		sel.row.degraded = sel.degraded
		currentBatch = append(currentBatch, sel.row)
		seenRegions[region] = true
	}

	fallbackRows := map[string][]*PriceRow{}
	var fallbackOrder []string
	for i := range prices {
		p := &prices[i]
		region := strings.ToUpper(derefStr(p.RegionCode, ""))
		if seenRegions[region] {
			continue
		}
		if _, ok := fallbackRows[region]; !ok {
			fallbackOrder = append(fallbackOrder, region)
		}
		fallbackRows[region] = append(fallbackRows[region], p)
	}
	for _, region := range fallbackOrder {
		rows := fallbackRows[region]
		var priced []*PriceRow
		for _, p := range rows {
			if derefStr(p.PriceStatus, "ok") == "ok" && isOkPriceRow(p.Price) && !derefBool(p.IsBundle, false) {
				priced = append(priced, p)
			}
		}
		var sel *PriceRow
		if len(priced) > 0 {
			sel = priced[0]
			for _, p := range priced[1:] {
				if derefI64(p.SubID, 0) < derefI64(sel.SubID, 0) {
					sel = p
				}
			}
		} else {
			sel = rows[len(rows)-1]
		}
		rawStatus := derefStr(sel.PriceStatus, "ok")
		priceCents := sel.Price
		rowDegraded := rawStatus == "ok" && !isOkPriceRow(priceCents)
		status := rawStatus
		if rowDegraded {
			status = "missing"
		}
		currency := derefStr(sel.Currency, "")
		var cnyFen *int64
		if priceCents != nil {
			cnyFen = w.computeCnyFen(*priceCents, currency)
		}
		currentBatch = append(currentBatch, currentRow{
			appid:       sel.AppID,
			region:      region,
			currency:    currency,
			price:       priceCents,
			original:    sel.OriginalPrice,
			discount:    derefI64(sel.DiscountPercent, 0),
			subID:       derefI64(sel.SubID, 0),
			status:      status,
			cnyFen:      cnyFen,
			discountEnd: sel.DiscountEndTS,
			updatedAt:   nowStr,
			steamAnswer: sel.SteamAnswer,
			degraded:    rowDegraded,
		})
	}

	degradedRegions = map[string]bool{}
	for i := range currentBatch {
		if currentBatch[i].degraded {
			degradedRegions[currentBatch[i].region] = true
		}
	}

	// 降级行记 failed 并清 lastSuccessAt/steamAnswer；未传 steamAnswer 时按状态推导。
	for i := range currentBatch {
		row := &currentBatch[i]
		if degradedRegions[row.region] {
			row.attemptOutcome = "failed"
			row.lastSuccessAt = nil
			row.steamAnswer = nil
			continue
		}
		row.attemptOutcome = "success"
		ls := row.updatedAt
		row.lastSuccessAt = &ls
		if row.steamAnswer == nil || *row.steamAnswer == "" {
			var s string
			switch {
			case row.status == "locked":
				s = "locked"
			case row.status == "missing" || row.status == "blocked":
				s = "no_options"
			case row.price != nil && *row.price == 0:
				s = "free"
			default:
				s = "ok"
			}
			row.steamAnswer = &s
		}
	}

	okRegions = map[string]bool{}
	for i := range currentBatch {
		if currentBatch[i].status == "ok" {
			okRegions[currentBatch[i].region] = true
		}
	}
	return currentBatch, historyBatch, okRegions, degradedRegions
}

func derefStr(p *string, def string) string {
	if p == nil {
		return def
	}
	return *p
}

func derefI64(p *int64, def int64) int64 {
	if p == nil {
		return def
	}
	return *p
}

func derefBool(p *bool, def bool) bool {
	if p == nil {
		return def
	}
	return *p
}
