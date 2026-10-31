package games

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"
)

type gameDetail struct {
	Appid              int64            `json:"appid"`
	Name               string           `json:"name"`
	NameEn             *string          `json:"nameEn"`
	Type               string           `json:"type"`
	HeaderImage        string           `json:"headerImage"`
	StoreURL           string           `json:"storeUrl"`
	ChineseSupport     *string          `json:"chineseSupport"`
	FamilySharing      bool             `json:"familySharing"`
	TradingCards       bool             `json:"tradingCards"`
	ReleaseDate        *string          `json:"releaseDate"`
	Tags               []tagItem        `json:"tags"`
	Developers         []any            `json:"developers"`
	Publishers         []any            `json:"publishers"`
	PositiveRate       *float64         `json:"positiveRate"`
	PositiveReviews    int64            `json:"positiveReviews"`
	ReviewCount        int64            `json:"reviewCount"`
	IsAdult            bool             `json:"isAdult"`
	XgpTier            *string          `json:"xgpTier"`
	IsVisualNovel      bool             `json:"isVisualNovel"`
	HlFlag             int64            `json:"hlFlag"`
	PpFlag             int64            `json:"ppFlag"`
	PpChangedAt        *string          `json:"ppChangedAt"`
	SeriesID           *string          `json:"seriesId"`
	IsHb               bool             `json:"isHb"`
	IsEpic             bool             `json:"isEpic"`
	EpicDate           *string          `json:"epicDate"`
	HbData             *string          `json:"hbData"`
	BundleCount        *int64           `json:"bundleCount"`
	ViewCount          int64            `json:"viewCount"`
	RemovedAt          *string          `json:"removedAt"`
	StoreDataMissing   bool             `json:"storeDataMissing"`
	FreeKind           *string          `json:"freeKind"`
	PromoEndAt         *int64           `json:"promoEndAt"`
	PriceMatrix        map[string][]any `json:"priceMatrix"`
	UnavailableRegions []string         `json:"unavailableRegions"`
	CnPriceCents       *int64           `json:"cnPriceCents"`
	CnCnyFen           *int64           `json:"cnCnyFen"`
	CnDiscount         int64            `json:"cnDiscount"`
	CnDiscountEndsAt   *int64           `json:"cnDiscountEndsAt"`
	LowestCnyFen       *int64           `json:"lowestCnyFen"`
	LowestRegionCode   string           `json:"lowestRegionCode"`
	SavingsFen         int64            `json:"savingsFen"`
	Versions           []versionItem    `json:"versions"`
	LinkedBundles      []bundleItem     `json:"linkedBundles"`
}

type tagItem struct {
	TagID  int64   `json:"tagid"`
	Name   *string `json:"name"`
	NameEn *string `json:"nameEn"`
}

type versionItem struct {
	Suffix string `json:"suffix"`
	IsGold bool   `json:"isGold"`
	SubID  *int64 `json:"subId"`
}

type bundleItem struct {
	BundleID          int64  `json:"bundleId"`
	Name              string `json:"name"`
	HeaderImage       string `json:"headerImage"`
	URL               string `json:"url"`
	MustPurchaseAsSet int64  `json:"mustPurchaseAsSet"`
	ItemKind          int64  `json:"itemKind"`
	PriceCny          *int64 `json:"priceCny"`
	LowestRegion      string `json:"lowestRegion"`
	LowestPriceFen    *int64 `json:"lowestPriceFen"`
	DiffFen           int64  `json:"diffFen"`
}

// gameDetail 游戏详情：元数据 + 全区当前价 + 版本列表 + 关联捆绑包。
// 返回 nil = 游戏不存在。
func (h *handler) gameDetail(ctx context.Context, appid int64) (*gameDetail, error) {
	rates, err := h.loadRates(ctx)
	if err != nil {
		return nil, err
	}

	var g gameRow
	var storeURL, chineseSupport *string
	var positiveReviews, viewCount int64
	var developers, publishers sql.NullString
	var freeKind *string
	var promoEndAt *int64
	err = h.db.QueryRowContext(ctx,
		"SELECT appid, name, name_en, type, header_image, store_url, chinese_support,"+
			" family_sharing, trading_cards, release_date, developers, publishers, positive_rate,"+
			" positive_reviews, review_count, is_adult, xgp_tier, is_visual_novel, hl_flag, pp_flag,"+
			" pp_changed_at, series_id, is_hb, is_epic, epic_date, hb_data, bundle_count, view_count,"+
			" removed_at, min_cny_fen, diff_fen, free_kind, promo_end_at FROM games WHERE appid = ?",
		appid,
	).Scan(
		&g.appid, &g.name, &g.nameEn, &g.typ, &g.headerImage, &storeURL, &chineseSupport,
		&g.familySharing, &g.tradingCards, &g.releaseDate, &developers, &publishers, &g.positiveRate,
		&positiveReviews, &g.reviewCount, &g.isAdult, &g.xgpTier, &g.isVisualNovel, &g.hlFlag, &g.ppFlag,
		&g.ppChangedAt, &g.seriesID, &g.isHb, &g.isEpic, &g.epicDate, &g.hbData, &g.bundleCount, &viewCount,
		&g.removedAt, &g.minCnyFen, &g.diffFen, &freeKind, &promoEndAt,
	)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("游戏详情查询失败: %w", err)
	}

	priceRows, err := h.queryDetailPrices(ctx, appid)
	if err != nil {
		return nil, err
	}

	type priceEntry struct {
		code         string
		cents        int64
		cnyFen       *int64
		discount     int64
		discountEnds *int64
	}
	var entries []priceEntry
	unavailable := make([]string, 0)
	for _, p := range priceRows {
		code := strings.ToUpper(p.regionCode)
		if p.priceStatus == "missing" || p.priceStatus == "blocked" {
			unavailable = append(unavailable, code)
			continue
		}
		if p.price == nil {
			continue
		}
		cents := *p.price
		cnyFen := p.cnyFen
		if cnyFen == nil && cents > 0 {
			currency := ""
			if p.currency != nil {
				currency = *p.currency
			}
			if currency == "" {
				currency = regionToCurrency[code]
			}
			if currency != "" {
				cnyFen = convertMinorToCnyFen(cents, currency, rates)
			}
		}
		discount := p.discountPercent
		entries = append(entries, priceEntry{
			code: code, cents: cents, cnyFen: cnyFen,
			discount: discount, discountEnds: p.discountEndTs,
		})
	}

	priceMatrix := map[string][]any{}
	for code, currency := range regionToCurrency {
		for _, e := range entries {
			if e.code != code {
				continue
			}
			priceMatrix[code] = []any{
				formatMinorUnits(e.cents, currency), derefZero(e.cnyFen), e.cents, e.discount,
			}
			break
		}
	}

	var cnData *priceEntry
	for i := range entries {
		if entries[i].code == "CN" {
			cnData = &entries[i]
			break
		}
	}
	var cnPriceCents, cnCnyFen, cnDiscountEnds *int64
	cnDiscount := int64(0)
	if cnData != nil {
		v := cnData.cents
		cnPriceCents = &v
		cnCnyFen = cnData.cnyFen
		cnDiscount = cnData.discount
		cnDiscountEnds = cnData.discountEnds
	}

	lowestRegionCode := ""
	for _, e := range entries {
		if e.code != "CN" && g.minCnyFen != nil && e.cnyFen != nil && *e.cnyFen == *g.minCnyFen {
			lowestRegionCode = strings.ToLower(e.code)
			break
		}
	}

	versions := make([]versionItem, 0)
	vrows, err := h.db.QueryContext(ctx,
		"SELECT DISTINCT version_suffix, is_gold, sub_id FROM game_price_history WHERE appid = ?", appid)
	if err != nil {
		return nil, fmt.Errorf("版本查询失败: %w", err)
	}
	for vrows.Next() {
		var suffix sql.NullString
		var isGold sql.NullInt64
		var subID sql.NullInt64
		if err := vrows.Scan(&suffix, &isGold, &subID); err != nil {
			vrows.Close()
			return nil, fmt.Errorf("版本行读取失败: %w", err)
		}
		if suffix.Valid && strings.TrimSpace(suffix.String) != "" {
			versions = append(versions, versionItem{
				Suffix: suffix.String, IsGold: isGold.Int64 != 0, SubID: nullableInt64(subID),
			})
		}
	}
	if err := vrows.Err(); err != nil {
		vrows.Close()
		return nil, fmt.Errorf("版本行遍历失败: %w", err)
	}
	vrows.Close()

	tags, err := h.namedTags(ctx, appid)
	if err != nil {
		return nil, err
	}
	bundles, err := h.linkedBundles(ctx, appid)
	if err != nil {
		return nil, err
	}

	headerImage := buildSteamHeaderURL(appid)
	if g.headerImage != nil && *g.headerImage != "" {
		headerImage = *g.headerImage
	}
	storeURLValue := buildSteamStoreURL(appid)
	if storeURL != nil && *storeURL != "" {
		storeURLValue = *storeURL
	}
	typ := "game"
	if g.typ != nil && *g.typ != "" {
		typ = *g.typ
	}

	return &gameDetail{
		Appid: g.appid, Name: g.name, NameEn: g.nameEn, Type: typ,
		HeaderImage: headerImage, StoreURL: storeURLValue,
		ChineseSupport: chineseSupport, FamilySharing: g.familySharing, TradingCards: g.tradingCards,
		ReleaseDate: g.releaseDate, Tags: tags,
		Developers: parseJSONList(developers), Publishers: parseJSONList(publishers),
		PositiveRate: rateRatio(g.positiveRate), PositiveReviews: positiveReviews, ReviewCount: g.reviewCount,
		IsAdult: g.isAdult, XgpTier: g.xgpTier, IsVisualNovel: g.isVisualNovel,
		HlFlag: g.hlFlag, PpFlag: g.ppFlag, PpChangedAt: g.ppChangedAt,
		SeriesID: g.seriesID, IsHb: g.isHb, IsEpic: g.isEpic, EpicDate: g.epicDate, HbData: g.hbData,
		BundleCount: g.bundleCount, ViewCount: viewCount, RemovedAt: g.removedAt,
		StoreDataMissing: len(entries) == 0, FreeKind: freeKind, PromoEndAt: promoEndAt,
		PriceMatrix: priceMatrix, UnavailableRegions: sortedStrings(unavailable),
		CnPriceCents: cnPriceCents, CnCnyFen: cnCnyFen, CnDiscount: cnDiscount,
		CnDiscountEndsAt: cnDiscountEnds, LowestCnyFen: g.minCnyFen, LowestRegionCode: lowestRegionCode,
		SavingsFen: g.diffFen, Versions: versions, LinkedBundles: bundles,
	}, nil
}

// queryDetailPrices 详情全区价格行（保持库内行序：最低区命中取首行）。
func (h *handler) queryDetailPrices(ctx context.Context, appid int64) ([]priceRow, error) {
	rows, err := h.db.QueryContext(ctx,
		"SELECT appid, region_code, currency, price, discount_percent,"+
			" price_status, cny_fen, discount_end_ts FROM game_current_prices WHERE appid = ?", appid)
	if err != nil {
		return nil, fmt.Errorf("详情价格查询失败: %w", err)
	}
	defer rows.Close()
	var out []priceRow
	for rows.Next() {
		var p priceRow
		if err := rows.Scan(&p.appid, &p.regionCode, &p.currency, &p.price,
			&p.discountPercent, &p.priceStatus, &p.cnyFen, &p.discountEndTs); err != nil {
			return nil, fmt.Errorf("详情价格行读取失败: %w", err)
		}
		out = append(out, p)
	}
	return out, rows.Err()
}

// namedTags 展示面标签：只留至少一个语言对照到名字的标签，票重降序。
func (h *handler) namedTags(ctx context.Context, appid int64) ([]tagItem, error) {
	rows, err := h.db.QueryContext(ctx,
		"SELECT gt.tagid, t.name_zh, t.name_en FROM game_tags gt"+
			" LEFT JOIN tags t ON t.tagid = gt.tagid WHERE gt.appid = ?"+
			" ORDER BY gt.weight DESC, gt.tagid", appid)
	if err != nil {
		return nil, fmt.Errorf("标签查询失败: %w", err)
	}
	defer rows.Close()
	out := make([]tagItem, 0)
	for rows.Next() {
		var tagid sql.NullInt64
		var nameZh, nameEn sql.NullString
		if err := rows.Scan(&tagid, &nameZh, &nameEn); err != nil {
			return nil, fmt.Errorf("标签行读取失败: %w", err)
		}
		if !nameZh.Valid && !nameEn.Valid {
			continue
		}
		item := tagItem{TagID: tagid.Int64}
		if nameZh.Valid {
			v := nameZh.String
			item.Name = &v
		}
		if nameEn.Valid {
			v := nameEn.String
			item.NameEn = &v
		}
		out = append(out, item)
	}
	return out, rows.Err()
}

// linkedBundles 关联捆绑包：app_ids JSON 逐行命中，差价快照降序，GET 不现算。
func (h *handler) linkedBundles(ctx context.Context, appid int64) ([]bundleItem, error) {
	pairs, err := h.db.QueryContext(ctx, "SELECT bundle_id, app_ids FROM bundles")
	if err != nil {
		return nil, fmt.Errorf("捆绑包查询失败: %w", err)
	}
	var hitIDs []int64
	for pairs.Next() {
		var bid int64
		var appIDs sql.NullString
		if err := pairs.Scan(&bid, &appIDs); err != nil {
			pairs.Close()
			return nil, fmt.Errorf("捆绑包行读取失败: %w", err)
		}
		if appIDs.Valid && jsonContainsAppID(appIDs.String, appid) {
			hitIDs = append(hitIDs, bid)
		}
	}
	if err := pairs.Err(); err != nil {
		pairs.Close()
		return nil, fmt.Errorf("捆绑包行遍历失败: %w", err)
	}
	pairs.Close()
	if len(hitIDs) == 0 {
		return []bundleItem{}, nil
	}

	ph := strings.TrimSuffix(strings.Repeat("?,", len(hitIDs)), ",")
	args := make([]any, 0, len(hitIDs))
	for _, id := range hitIDs {
		args = append(args, id)
	}

	rows, err := h.db.QueryContext(ctx,
		"SELECT bundle_id, name, header_image, url, must_purchase_as_set, item_kind,"+
			" min_cny_fen, diff_fen FROM bundles WHERE bundle_id IN ("+ph+")"+
			" ORDER BY diff_fen DESC, bundle_id ASC", args...)
	if err != nil {
		return nil, fmt.Errorf("捆绑包主档查询失败: %w", err)
	}
	defer rows.Close()

	type bundleRowT struct {
		bundleID          int64
		name              string
		headerImage       *string
		url               *string
		mustPurchaseAsSet *int64
		itemKind          *int64
		minCnyFen         *int64
		diffFen           int64
	}
	var bundles []bundleRowT
	for rows.Next() {
		var b bundleRowT
		var mps, kind sql.NullInt64
		if err := rows.Scan(&b.bundleID, &b.name, &b.headerImage, &b.url, &mps, &kind,
			&b.minCnyFen, &b.diffFen); err != nil {
			return nil, fmt.Errorf("捆绑包主档行读取失败: %w", err)
		}
		if mps.Valid {
			v := mps.Int64
			b.mustPurchaseAsSet = &v
		}
		if kind.Valid {
			v := kind.Int64
			b.itemKind = &v
		}
		bundles = append(bundles, b)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("捆绑包主档行遍历失败: %w", err)
	}
	rows.Close()

	cnRows, err := h.db.QueryContext(ctx,
		"SELECT bundle_id, region_code, cny_fen FROM bundle_region_prices WHERE bundle_id IN ("+ph+")",
		args...)
	if err != nil {
		return nil, fmt.Errorf("捆绑包区价查询失败: %w", err)
	}
	defer cnRows.Close()
	type regionPrice struct {
		code string
		fen  *int64
	}
	byBundle := map[int64][]regionPrice{}
	for cnRows.Next() {
		var bid int64
		var code string
		var fen sql.NullInt64
		if err := cnRows.Scan(&bid, &code, &fen); err != nil {
			return nil, fmt.Errorf("捆绑包区价行读取失败: %w", err)
		}
		var p *int64
		if fen.Valid {
			v := fen.Int64
			p = &v
		}
		byBundle[bid] = append(byBundle[bid], regionPrice{code: strings.ToUpper(code), fen: p})
	}
	if err := cnRows.Err(); err != nil {
		return nil, fmt.Errorf("捆绑包区价行遍历失败: %w", err)
	}

	out := make([]bundleItem, 0, len(bundles))
	for _, b := range bundles {
		regions := byBundle[b.bundleID]
		var cnFen *int64
		for _, r := range regions {
			if r.code == "CN" {
				cnFen = r.fen
				break
			}
		}
		lowestCnyFen := minNonNil(cnFen, b.minCnyFen)
		lowestRegion := ""
		if lowestCnyFen != nil {
			if cnFen != nil && *cnFen <= *lowestCnyFen {
				lowestRegion = "cn"
			} else {
				for _, r := range regions {
					if r.code != "CN" && r.fen != nil && *r.fen == *lowestCnyFen {
						lowestRegion = strings.ToLower(r.code)
						break
					}
				}
			}
		}
		kind := int64(-1)
		if b.itemKind != nil {
			kind = *b.itemKind
		}
		if kind != 0 && kind != 1 {
			if b.mustPurchaseAsSet != nil {
				kind = *b.mustPurchaseAsSet
			} else {
				kind = 0
			}
		}
		url := ""
		if b.url != nil {
			url = *b.url
		}
		if url == "" {
			kindWord := "bundle"
			if kind == 1 {
				kindWord = "sub"
			}
			url = fmt.Sprintf("https://store.steampowered.com/%s/%d/", kindWord, b.bundleID)
		}
		image := bundleFallbackImage(b.bundleID, kind)
		if n := normalizeBundleImage(b.headerImage); n != nil {
			image = *n
		}
		mps := int64(-1)
		if b.mustPurchaseAsSet != nil {
			mps = *b.mustPurchaseAsSet
		}
		out = append(out, bundleItem{
			BundleID: b.bundleID, Name: b.name, HeaderImage: image, URL: url,
			MustPurchaseAsSet: mps, ItemKind: kind, PriceCny: cnFen,
			LowestRegion: lowestRegion, LowestPriceFen: lowestCnyFen, DiffFen: b.diffFen,
		})
	}
	return out, nil
}

var bundleImageHosts = regexp.MustCompile(
	`(?i)^https?://shared\.(?:fastly\.steamstatic|cdn\.queniuqe)\.com`)

// normalizeBundleImage 快置域名归一到 akamai；非命中原样返回 nil。
func normalizeBundleImage(url *string) *string {
	if url == nil || *url == "" {
		return nil
	}
	if bundleImageHosts.MatchString(*url) {
		v := bundleImageHosts.ReplaceAllString(*url, "https://shared.akamai.steamstatic.com")
		return &v
	}
	v := *url
	return &v
}

func bundleFallbackImage(bundleID, itemKind int64) string {
	kind := "bundles"
	if itemKind == 1 {
		kind = "subs"
	}
	return fmt.Sprintf(
		"https://shared.akamai.steamstatic.com/store_item_assets/steam/%s/%d/header.jpg",
		kind, bundleID)
}

// jsonContainsAppID JSON 数组文本是否包含给定 appid。
func jsonContainsAppID(text string, appid int64) bool {
	var ids []any
	if json.Unmarshal([]byte(text), &ids) != nil {
		return false
	}
	for _, v := range ids {
		if f, ok := v.(float64); ok && int64(f) == appid {
			return true
		}
	}
	return false
}

// parseJSONList JSON 数组列 → 列表；空 / 解析失败回 []。
func parseJSONList(v sql.NullString) []any {
	if !v.Valid || v.String == "" {
		return []any{}
	}
	var out []any
	if json.Unmarshal([]byte(v.String), &out) != nil {
		return []any{}
	}
	return out
}

func nullableInt64(v sql.NullInt64) *int64 {
	if !v.Valid {
		return nil
	}
	val := v.Int64
	return &val
}

// ── 汇率进程内缓存（300s TTL）──

const ratesTTL = 300 * time.Second

var ratesCache struct {
	mu    sync.Mutex
	at    time.Time
	rates map[string]float64
}

// loadRates 全表汇率（缺行用兜底表）；查询失败按错误上抛。
func (h *handler) loadRates(ctx context.Context) (map[string]float64, error) {
	ratesCache.mu.Lock()
	defer ratesCache.mu.Unlock()
	if ratesCache.rates != nil && time.Since(ratesCache.at) < ratesTTL {
		return ratesCache.rates, nil
	}
	rates := make(map[string]float64, len(defaultExchangeRates)+1)
	for k, v := range defaultExchangeRates {
		rates[k] = v
	}
	rows, err := h.db.QueryContext(ctx, "SELECT currency_code, rate_to_cny FROM fx_rates")
	if err != nil {
		return nil, fmt.Errorf("汇率查询失败: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var code string
		var rate float64
		if err := rows.Scan(&code, &rate); err != nil {
			return nil, fmt.Errorf("汇率行读取失败: %w", err)
		}
		rates[strings.ToUpper(code)] = rate
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("汇率行遍历失败: %w", err)
	}
	rates["CNY"] = 1.0
	ratesCache.rates = rates
	ratesCache.at = time.Now()
	return rates, nil
}
