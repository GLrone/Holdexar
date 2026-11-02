package games

import (
	"math"
	"sort"
	"strconv"
	"strings"
	"time"
)

type gameRow struct {
	appid         int64
	name          string
	nameEn        *string
	typ           *string
	positiveRate  *int64
	reviewCount   int64
	isAdult       bool
	xgpTier       *string
	isVisualNovel bool
	releaseDate   *string
	minCnyFen     *int64
	diffFen       int64
	smartScore    *float64
	headerImage   *string
	hlFlag        int64
	ppFlag        int64
	ppChangedAt   *string
	updatedAt     *string
	familySharing bool
	tradingCards  bool
	seriesID      *string
	isHb          bool
	isEpic        bool
	epicDate      *string
	hbData        *string
	bundleCount   *int64
	removedAt     *string
}

type cnRow struct {
	price           *int64
	discountPercent int64
	discountEndTs   *int64
	cnyFen          *int64
}

type priceRow struct {
	appid           int64
	regionCode      string
	currency        *string
	price           *int64
	discountPercent int64
	priceStatus     string
	cnyFen          *int64
	discountEndTs   *int64
	attemptOutcome  *string
	steamAnswer     *string
	lastSuccessAt   *string
	updatedAt       *string
}

type listItem struct {
	Appid              int64            `json:"appid"`
	Name               string           `json:"name"`
	NameEn             *string          `json:"nameEn"`
	Discount           int64            `json:"discount"`
	DiscountLabel      string           `json:"discountLabel"`
	DiscountEndsAt     *int64           `json:"discountEndsAt"`
	PositiveRate       *pyFloat         `json:"positiveRate"`
	ReviewCount        int64            `json:"reviewCount"`
	IsAdult            bool             `json:"isAdult"`
	XgpTier            *string          `json:"xgpTier"`
	IsVisualNovel      bool             `json:"isVisualNovel"`
	ReleaseDate        string           `json:"releaseDate"`
	BasePriceFen       *int64           `json:"basePriceFen"`
	LowestPriceFen     *int64           `json:"lowestPriceFen"`
	SavingsFen         int64            `json:"savingsFen"`
	HeaderImage        string           `json:"headerImage"`
	PriceMatrix        map[string][]any `json:"priceMatrix"`
	UnavailableRegions []string         `json:"unavailableRegions"`
	HlFlag             int64            `json:"hlFlag"`
	PpFlag             int64            `json:"ppFlag"`
	PpChangedAt        *string          `json:"ppChangedAt"`
	UpdatedAt          *string          `json:"updatedAt"`
	PriceData          map[string]any   `json:"priceData"`
	FamilySharing      bool             `json:"familySharing"`
	TradingCards       bool             `json:"tradingCards"`
	SeriesID           *string          `json:"seriesId"`
	IsHb               bool             `json:"isHb"`
	IsEpic             bool             `json:"isEpic"`
	EpicDate           *string          `json:"epicDate"`
	HbData             *string          `json:"hbData"`
	BundleCount        *int64           `json:"bundleCount"`
	RemovedAt          *string          `json:"removedAt"`
}

func derefInt(v *int64) any {
	if v == nil {
		return nil
	}
	return *v
}

// derefZero NULL → 0（矩阵第二位口径）。
func derefZero(v *int64) int64 {
	if v == nil {
		return 0
	}
	return *v
}

// pyFloat Python float 的 JSON 形态：整值浮点必须带一位小数（96.0 而非 96），
// 与 json.dumps 的浮点 repr 对齐。
type pyFloat float64

func (f pyFloat) MarshalJSON() ([]byte, error) {
	v := float64(f)
	if v == math.Trunc(v) && math.Abs(v) < 1e16 {
		return []byte(strconv.FormatFloat(v, 'f', 1, 64)), nil
	}
	return []byte(strconv.FormatFloat(v, 'g', -1, 64)), nil
}

// rateRatio positive_rate / 100（Python 同款口径）。
func rateRatio(v *int64) *pyFloat {
	if v == nil {
		return nil
	}
	f := pyFloat(float64(*v) / 100)
	return &f
}

func buildSteamHeaderURL(appid int64) string {
	return "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/" +
		strconv.FormatInt(appid, 10) + "/header.jpg"
}

func buildSteamStoreURL(appid int64) string {
	return "https://store.steampowered.com/app/" + strconv.FormatInt(appid, 10) + "/"
}

// buildListItem 列表项：cnyFen 只读快照列（GET 不回算汇率）；覆盖分母 = 启用区服。
func buildListItem(g gameRow, cn *cnRow, prices []priceRow, regionsExpected []string, pendingUnits map[string]bool) listItem {
	var baseCnPrice *int64
	if cn != nil {
		baseCnPrice = cn.price
	}

	type priceEntry struct {
		cents    int64
		cnyFen   *int64
		stale    bool
		discount int64
	}
	priceMap := map[string]priceEntry{}
	unavailable := make([]string, 0)
	for _, p := range prices {
		code := strings.ToUpper(p.regionCode)
		if code == "" {
			continue
		}
		stale := p.attemptOutcome == nil || *p.attemptOutcome != "success"
		if p.priceStatus == "missing" || p.priceStatus == "blocked" {
			unavailable = append(unavailable, code)
		}
		if p.price == nil || *p.price <= 0 {
			continue
		}
		discount := p.discountPercent
		priceMap[code] = priceEntry{cents: *p.price, cnyFen: p.cnyFen, stale: stale, discount: discount}
	}

	allPrices := map[string][]any{}
	lowestCnyFen := minNonNil(baseCnPrice, g.minCnyFen)
	for code, currency := range regionToCurrency {
		e, ok := priceMap[code]
		if !ok {
			continue
		}
		allPrices[code] = []any{
			formatMinorUnits(e.cents, currency), derefZero(e.cnyFen), e.cents, e.discount, e.stale,
		}
	}
	discount := int64(0)
	if cn != nil {
		discount = cn.discountPercent
	}

	coverage := buildCoverage(prices, regionsExpected)
	priceData := regionalFreshness(prices, regionsExpected, pendingUnits)
	priceData["coverage"] = coverage

	var releaseDate string
	if g.releaseDate != nil {
		releaseDate = *g.releaseDate
	}
	headerImage := buildSteamHeaderURL(g.appid)
	if g.headerImage != nil && *g.headerImage != "" {
		headerImage = *g.headerImage
	}

	return listItem{
		Appid:              g.appid,
		Name:               g.name,
		NameEn:             g.nameEn,
		Discount:           discount,
		DiscountLabel:      discountLabel(discount),
		DiscountEndsAt:     cnDiscountEnds(cn),
		PositiveRate:       rateRatio(g.positiveRate),
		ReviewCount:        g.reviewCount,
		IsAdult:            g.isAdult,
		XgpTier:            g.xgpTier,
		IsVisualNovel:      g.isVisualNovel,
		ReleaseDate:        releaseDate,
		BasePriceFen:       baseCnPrice,
		LowestPriceFen:     lowestCnyFen,
		SavingsFen:         g.diffFen,
		HeaderImage:        headerImage,
		PriceMatrix:        allPrices,
		UnavailableRegions: sortedStrings(unavailable),
		HlFlag:             g.hlFlag,
		PpFlag:             g.ppFlag,
		PpChangedAt:        goIsoformatPtr(g.ppChangedAt),
		UpdatedAt:          goIsoformatPtr(g.updatedAt),
		PriceData:          priceData,
		FamilySharing:      g.familySharing,
		TradingCards:       g.tradingCards,
		SeriesID:           g.seriesID,
		IsHb:               g.isHb,
		IsEpic:             g.isEpic,
		EpicDate:           g.epicDate,
		HbData:             g.hbData,
		BundleCount:        g.bundleCount,
		RemovedAt:          goIsoformatPtr(g.removedAt),
	}
}

func discountLabel(discount int64) string {
	if discount > 0 {
		return "-" + strconv.FormatInt(discount, 10) + "%"
	}
	return ""
}

func cnDiscountEnds(cn *cnRow) *int64 {
	if cn == nil {
		return nil
	}
	return cn.discountEndTs
}

// buildCoverage 覆盖（唯一事实源 = 活表尝试状态）：分母 = 启用区服；
// regions 只点名非成功区。启用区为空时不给覆盖。
func buildCoverage(rows []priceRow, regionsExpected []string) map[string]any {
	if len(regionsExpected) == 0 {
		return nil
	}
	rowsByRegion := map[string]*priceRow{}
	for i := range rows {
		c := strings.ToUpper(rows[i].regionCode)
		if c != "" {
			rowsByRegion[c] = &rows[i]
		}
	}
	problems := map[string]any{}
	success := 0
	for _, code := range regionsExpected {
		p := rowsByRegion[code]
		if p == nil {
			problems[code] = map[string]any{
				"outcome": "notAttempted", "answer": nil, "lastSuccessAt": nil,
			}
			continue
		}
		if p.attemptOutcome != nil && *p.attemptOutcome == "success" {
			success++
			continue
		}
		outcome := "failed"
		if p.attemptOutcome != nil && *p.attemptOutcome != "" {
			outcome = *p.attemptOutcome
		}
		problems[code] = map[string]any{
			"outcome": outcome, "answer": p.steamAnswer, "lastSuccessAt": goIsoformatPtr(p.lastSuccessAt),
		}
	}
	failed, notAttempted := 0, 0
	for _, v := range problems {
		m := v.(map[string]any)
		switch m["outcome"] {
		case "failed":
			failed++
		case "notAttempted":
			notAttempted++
		}
	}
	expected := len(regionsExpected)
	var cov any
	if expected > 0 {
		cov = roundN(float64(success)/float64(expected), 4)
	}
	return map[string]any{
		"expectedUnits": expected,
		"success":       success,
		"failed":        failed,
		"notAttempted":  notAttempted,
		"coverage":      cov,
		"regions":       problems,
	}
}

const (
	freshHours   = 6.0
	laggingHours = 12.0
)

// regionalFreshness 同一份价格行生成地区明细与对象新鲜度（纯函数，不取价）。
func regionalFreshness(rows []priceRow, regions []string, pendingUnits map[string]bool) map[string]any {
	indexed := map[string]*priceRow{}
	for i := range rows {
		c := strings.ToUpper(rows[i].regionCode)
		if c != "" {
			indexed[c] = &rows[i]
		}
	}
	codes := make([]string, 0, len(regions))
	for _, cc := range regions {
		codes = append(codes, strings.ToUpper(cc))
	}
	if len(codes) == 0 {
		for c := range indexed {
			codes = append(codes, c)
		}
	}
	now := time.Now()
	units := make(map[string]any, len(codes))
	var minText *string
	var minTime time.Time
	for _, code := range codes {
		row := indexed[code]
		if row == nil {
			units[code] = map[string]any{
				"status": "notAttempted", "outcome": "notAttempted", "answer": nil,
				"lastSuccessAt": nil, "lastAttemptAt": nil, "ageHours": nil, "freshness": nil,
				// pending 集来自爬取域（deferred），恒走降级表达式
				"pending": true, "needsUpdate": true,
			}
			continue
		}
		obs, age, fresh := freshnessOf(row.lastSuccessAt, now)
		outcome := "failed"
		if row.attemptOutcome != nil && *row.attemptOutcome != "" {
			outcome = *row.attemptOutcome
		}
		pending := pendingUnits[strconv.FormatInt(row.appid, 10)+":"+code] ||
			(outcome != "success" && row.priceStatus != "blocked")
		ageAny := any(nil)
		if age != nil {
			ageAny = *age
		}
		needsUpdate := pending || outcome != "success" || age == nil || *age >= freshHours
		units[code] = map[string]any{
			"status":        row.priceStatus,
			"outcome":       outcome,
			"answer":        row.steamAnswer,
			"lastSuccessAt": obs,
			"lastAttemptAt": goIsoformatPtr(row.updatedAt),
			"ageHours":      ageAny,
			"freshness":     fresh,
			"pending":       pending,
			"needsUpdate":   needsUpdate,
		}
		if row.lastSuccessAt != nil {
			if t, ok := parseStoredTime(*row.lastSuccessAt); ok {
				if minText == nil || t.Before(minTime) {
					t2 := t
					minTime = t2
					text := *row.lastSuccessAt
					minText = &text
				}
			}
		}
	}
	obs, age, fresh := freshnessOf(minText, now)
	ageAny := any(nil)
	if age != nil {
		ageAny = *age
	}
	return map[string]any{
		"observedAt": obs, "ageHours": ageAny, "freshness": fresh, "regions": units,
	}
}

// goIsoformat 库内 naive 文本（空格分隔）→ Python datetime.isoformat 形态
// （T 分隔；微秒为 0 时省略小数段）。无法解析时原样返回。
func goIsoformat(text string) string {
	t, ok := parseStoredTime(text)
	if !ok {
		return text
	}
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05")
	}
	return t.Format("2006-01-02T15:04:05.000000")
}

// pyHours 复刻 Python (reference - observed).total_seconds() / 3600.0 的浮点
// 路径：微秒整数（datetime 精度）→ /1e6 → /3600 两次除法，避免单次除法在
// 舍入边界产生末位分歧。
func pyHours(d time.Duration) float64 {
	return (float64(int64(d)/1000) / 1e6) / 3600.0
}

func goIsoformatPtr(v *string) *string {
	if v == nil {
		return nil
	}
	s := goIsoformat(*v)
	return &s
}

// freshnessOf 观察时刻 → (observedAt, ageHours, freshness)；无数据三者皆空。
func freshnessOf(observedAt *string, now time.Time) (any, *float64, any) {
	if observedAt == nil {
		return nil, nil, nil
	}
	t, ok := parseStoredTime(*observedAt)
	if !ok {
		return nil, nil, nil
	}
	age := roundN(pyHours(now.Sub(t)), 3)
	var bucket string
	switch {
	case age < freshHours:
		bucket = "fresh"
	case age < laggingHours:
		bucket = "lagging"
	default:
		bucket = "stale"
	}
	obs := goIsoformat(*observedAt)
	return obs, &age, bucket
}

// parseStoredTime 库内 datetime 文本（naive）解析；按服务器本地时区解释。
func parseStoredTime(text string) (time.Time, bool) {
	t, err := time.ParseInLocation("2006-01-02 15:04:05.999999999", text, time.Local)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

func minNonNil(a, b *int64) *int64 {
	switch {
	case a == nil && b == nil:
		return nil
	case a == nil:
		return b
	case b == nil:
		return a
	case *a < *b:
		return a
	default:
		return b
	}
}

func sortedStrings(s []string) []string {
	out := make([]string, len(s))
	copy(out, s)
	sort.Strings(out)
	return out
}
