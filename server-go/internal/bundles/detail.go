package bundles

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
)

type DetailRegionPrice struct {
	PriceMinor      *int64  `json:"priceMinor"`
	Currency        *string `json:"currency"`
	DiscountPercent int64   `json:"discountPercent"`
	BaseDiscount    int64   `json:"baseDiscount"`
	CnyFen          *int64  `json:"cnyFen"`
	DiscountEndsAt  *int64  `json:"discountEndsAt"`
	AppIDs          []int64 `json:"appIds"`
	LockedCount     int     `json:"lockedCount"`
	Formatted       string  `json:"formatted"`
}

type DetailGamePrice struct {
	PriceMinor *int64  `json:"priceMinor"`
	Currency   *string `json:"currency"`
	CnyFen     *int64  `json:"cnyFen"`
}

type DetailGame struct {
	AppID       int64                      `json:"appid"`
	Name        *string                    `json:"name"`
	HeaderImage string                     `json:"headerImage"`
	Prices      map[string]DetailGamePrice `json:"prices"`
}

type BundleDetail struct {
	bundleSummary
	RegionPrices map[string]DetailRegionPrice `json:"regionPrices"`
	Games        []DetailGame                 `json:"games"`
}

func GetBundleDetail(ctx context.Context, db *sql.DB, bundleID int64) (*BundleDetail, error) {
	bundles, pricesByBundle, err := loadBundles(ctx, db, []int64{bundleID})
	if err != nil {
		return nil, err
	}
	if len(bundles) == 0 {
		return nil, nil
	}
	tracked := trackedRegionCodes(ctx, db)
	agg := aggregateBundle(bundles[0], pricesByBundle[bundleID], tracked)

	detail := &BundleDetail{
		bundleSummary: agg.summary,
		RegionPrices:  make(map[string]DetailRegionPrice, len(agg.regions)),
		Games:         []DetailGame{},
	}
	for _, e := range agg.regions {
		detail.RegionPrices[e.code] = DetailRegionPrice{
			PriceMinor:      e.priceMinor,
			Currency:        e.currency,
			DiscountPercent: e.discountPercent,
			BaseDiscount:    e.baseDiscount,
			CnyFen:          e.cnyFen,
			DiscountEndsAt:  e.discountEndsAt,
			AppIDs:          e.appIDs,
			LockedCount:     e.lockedCount,
			Formatted:       e.formatted,
		}
	}
	if len(agg.summary.AppIDs) == 0 {
		return detail, nil
	}

	ph := strings.TrimSuffix(strings.Repeat("?,", len(agg.summary.AppIDs)), ",")
	args := make([]any, 0, len(agg.summary.AppIDs))
	for _, appid := range agg.summary.AppIDs {
		args = append(args, appid)
	}
	type gameMeta struct {
		name        sql.NullString
		headerImage sql.NullString
	}
	metaRows, err := db.QueryContext(ctx,
		"SELECT appid,name,header_image FROM games WHERE appid IN ("+ph+")", args...)
	if err != nil {
		return nil, fmt.Errorf("包内游戏元数据读取失败: %w", err)
	}
	defer metaRows.Close()
	metaByAppid := map[int64]gameMeta{}
	for metaRows.Next() {
		var appid int64
		var meta gameMeta
		if err := metaRows.Scan(&appid, &meta.name, &meta.headerImage); err != nil {
			return nil, fmt.Errorf("包内游戏元数据行读取失败: %w", err)
		}
		metaByAppid[appid] = meta
	}
	if err := metaRows.Err(); err != nil {
		return nil, fmt.Errorf("包内游戏元数据行遍历失败: %w", err)
	}

	priceRows, err := db.QueryContext(ctx,
		"SELECT appid,region_code,currency,price,cny_fen FROM game_current_prices "+
			"WHERE appid IN ("+ph+") AND price_status = 'ok'", args...)
	if err != nil {
		return nil, fmt.Errorf("包内游戏现价读取失败: %w", err)
	}
	defer priceRows.Close()
	pricesByAppid := map[int64]map[string]DetailGamePrice{}
	for priceRows.Next() {
		var appid int64
		var regionCode, currency sql.NullString
		var price, cnyFen sql.NullInt64
		if err := priceRows.Scan(&appid, &regionCode, &currency, &price, &cnyFen); err != nil {
			return nil, fmt.Errorf("包内游戏现价行读取失败: %w", err)
		}
		code := strings.ToUpper(regionCode.String)
		byAppid, ok := pricesByAppid[appid]
		if !ok {
			byAppid = map[string]DetailGamePrice{}
			pricesByAppid[appid] = byAppid
		}
		byAppid[code] = DetailGamePrice{
			PriceMinor: nullInt64Ptr(price),
			Currency:   upperOrNull(currency),
			CnyFen:     nullInt64Ptr(cnyFen),
		}
	}
	if err := priceRows.Err(); err != nil {
		return nil, fmt.Errorf("包内游戏现价行遍历失败: %w", err)
	}

	for _, appid := range agg.summary.AppIDs {
		game := DetailGame{
			AppID:       appid,
			HeaderImage: fmt.Sprintf("https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/%d/header.jpg", appid),
			Prices:      map[string]DetailGamePrice{},
		}
		if meta, ok := metaByAppid[appid]; ok {
			if meta.name.Valid {
				name := meta.name.String
				game.Name = &name
			}
			if meta.headerImage.Valid && meta.headerImage.String != "" {
				game.HeaderImage = meta.headerImage.String
			}
		}
		if prices, ok := pricesByAppid[appid]; ok {
			game.Prices = prices
		}
		detail.Games = append(detail.Games, game)
	}
	return detail, nil
}
