package dbwriter

// 价格决策纯函数（无 IO）：games 行归一化 + 免费态分类 + 差量门禁 /
// 版本名回填 / 标准版选择。语义与 Python 侧同链路逐行对齐。

import (
	"math"
	"regexp"
	"strings"
	"time"
)

// BatchEntry 一个抓取结果：一款游戏的元数据 + 其价格行（一个任务 =
// 一个区 × 一批 appid，每 appid 一条 entry）。
type BatchEntry struct {
	Game   GameData
	Prices []PriceRow
}

// GameData games 元数据（对齐调用方 build_game_data 的 dict 形状）。
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
	PositiveReviews *int64 // nil 落缺省 0
	ReviewCount     *int64 // nil 落缺省 0
	CreatedAt       *time.Time
	UpdatedAt       *time.Time // nil 取 Writer.Now()
}

// PriceRow 单区价格行（对齐调用方 prices_arr 的 dict 形状）。
// 指针 nil = Python .get 缺省：RegionCode/Currency 落空串、DiscountPercent/
// SubID 落 0、IsGold/IsBundle 落 false、PriceStatus 落 "ok"。
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

// 测试入口命名（测试版/测试服/公测/Playtest 等）→ 不进监控池的判定词形。
// 只认锚定的「测试入口」组合词——裸 beta/测试会误伤标题自带裸词的正经游戏。
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

// isOkPriceRow ok 行价格合法性：nil 才是坏数据（0 = 免费游戏，合法）。
func isOkPriceRow(price *int64) bool { return price != nil }

// computeCnyFen cny_fen = ROUND(price_cents * rate_to_cny)（银行家舍入，
// 对齐 Python round）；0 = 免费游戏，合法值；无汇率 → NULL。
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

// ── games 行归一化 ──────────────────────────────────────────────

type gameRowValues struct {
	appid         int64
	name, nameEn  any // string 或 nil
	typ           any
	headerImage   any
	storeURL      any
	chineseSupp   any
	familySharing int64
	tradingCards  int64
	isEpic        int64 // 模型缺省列：调用方不携带，落缺省 0
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
	createdAt     any // string 或 nil
	updatedAt     string
	// 免费态字段：hasFree=true 时随行携带（批量 upsert 以 excluded 引用
	// 逐行落）；false 时两键不进语句——纯 locked/missing 响应绝不洗掉库内免费态
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

// normalizeGame games 行归一化 + 免费态/测试入口分类。
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
	// 测试入口优先于价格分类：Beta/测试页常挂正价（页面透传本体价），
	// 价格分类判不出免费态，只有命名能定性；'beta' 与 f2p/promo 同走
	// free_kind 非空脱池机制
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

// ── 免费态分类 ──────────────────────────────────────────────────

type freeState struct {
	kind     string
	promoEnd *int64
}

// classifyFreeKind 单区价格行 → (free_kind, promo_end_at)。
//   - probe = CN ok 行（锚区）；CN 无 ok 行时回退任意区 ok 行。
//   - probe price=0：original>0 → promo（限时赠送，结束时间取各行
//     promo_end_ts 最大值）；original=0 → f2p（永久免费）。
//   - probe price>0（付费）→ 显式清标记——仅当 CN 行在场：各区任务独立
//     分类，区服促销不同步时非 CN 任务不得抹掉 CN 刚打的 promo 标；
//     CN 行缺席且 probe 付费 → nil（无信号，调用方不动标记）。
//   - 一条 ok 行都没有（纯 locked/missing 响应）→ nil：下架/断网不该
//     抹掉免费态。
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

// ── 基线（差量门禁 + 版本名回填） ────────────────────────────────

type obsKey struct {
	region string
	sub    int64
}

// snapTuple 差量比较键：价三件套 + 版本后缀 + gold 标。
type snapTuple struct {
	price    int64
	original *int64
	discount int64
	suffix   *string
	isGold   bool
}

type baseline struct {
	known  map[int64]string     // sub_id → 同 sub 最新非空版本名
	latest map[obsKey]snapTuple // (区, sub) → 最新快照
}

// ── 价格决策 ────────────────────────────────────────────────────

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

// historyRow 历史切片行。
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

// planPriceRows 价格决策：差量门禁 / 版本名回填 / 标准版选择。
// 活动快照标签由调用方按批求值传入（同一观测时刻本就该是同一个 key）。
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
		// 丢名行沿用同 sub_id 的历史名（known_suffix）——标准版判定 /
		// 差量比较全部一致
		if suffix == nil || *suffix == "" {
			if s, ok := base.known[derefI64(p.SubID, 0)]; ok {
				suffix = &s
			}
		}
		isBundle := derefBool(p.IsBundle, false)
		status := derefStr(p.PriceStatus, "ok")
		// 质量门禁：ok 但无价格 = 成功响应里的坏数据。行级降级记史不记现；
		// 区级欠账结转只在「选中行自身被降级」时发生
		rowDegraded := false
		if status == "ok" && !isOkPriceRow(priceCents) {
			status = "missing"
			rowDegraded = true
		}
		var cnyFen *int64
		if priceCents != nil {
			cnyFen = w.computeCnyFen(*priceCents, currency)
		}

		// 所有有价格的版本写入 history；永久免费 price=0 不进 history
		//（无价格事件），限时赠送 price=0+original>0 是真实价格事件照写；
		// 相对最新快照无变化的行不写（差量门禁）
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

		// 标准版候选: 非 gold 且非捆绑包，且名字无版本后缀——显式命名的
		// Standard Edition 归一化为标准版候选
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

	// 每个区域选标准版写入 current：优先有价（ok+price 有值）行——该区
	// 任何版本有价就算有数；全部无价才落 missing 状态行
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

	// 无标准版候选的区域也写入 current：该区有价（非捆绑包）行里取
	// sub_id 最小的确定性兜底——最老 SKU 即本体。全部无价才落状态行，
	// 同区多行降级时以最后一条为准
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
		// 门禁降级行的实时状态在主循环里已算过，重算保持独立
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

	// 门禁降级按行不扩散：只对「选中行自身被门禁降级」的区欠账结转
	degradedRegions = map[string]bool{}
	for i := range currentBatch {
		if currentBatch[i].degraded {
			degradedRegions[currentBatch[i].region] = true
		}
	}

	// 门禁降级是失败观察；明确答复含 locked / 无购买选项。导入和旧调用方
	// 没有显式 answer 时按状态推导
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

// ── 取值辅助（对齐 Python .get 缺省语义） ────────────────────────

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
