package bundles

// 抓取层纯函数与出网管线用例（对照 Python test_bundle_browse_layer.py）：
// 未收录不写行、锁区写 locked、分单位原样（零小数货币不 ×100）、
// best_purchase_option 优先、位置对齐 vs id 对齐、plan 批贪心装批、
// 南亚拆区、单包身份兜底（force_package 不兜底）。

import (
	"context"
	"errors"
	"math"
	"net/http"
	"reflect"
	"testing"

	"holdexar/server-go/internal/storebrowse"
)

func TestNotListedItemWritesNothing(t *testing.T) {
	bogus := map[string]any{
		"id":             float64(999999999),
		"item_type":      float64(2),
		"name":           nil,
		"store_url_slug": "",
		"success":        float64(15),
		"visible":        false,
	}
	if isRealItem(bogus) {
		t.Fatal("name/slug 全空 = 未收录")
	}
	if row := browseRow(bogus, 999999999, "us", "bundleid"); row != nil {
		t.Fatalf("row=%+v（未收录不写行）", row)
	}
	if row := browseRow(nil, 13608, "us", "bundleid"); row != nil {
		t.Fatal("条目缺失不写行")
	}
}

func TestRestrictedRealItemIsLocked(t *testing.T) {
	restricted := stubItem(510898, map[string]any{
		"item_type":      float64(1),
		"success":        float64(15),
		"visible":        false,
		"unvailable_for_country_restriction": true,
		"best_purchase_option":               nil,
	})
	row := browseRow(restricted, 510898, "cn", "packageid")
	if row == nil {
		t.Fatal("真实存在但本区不售：写 locked 行")
	}
	if row.PriceStatus != "locked" || row.Price != nil {
		t.Fatalf("status=%q price=%v", row.PriceStatus, row.Price)
	}
	if row.MPS == nil || *row.MPS != 1 {
		t.Fatalf("mps=%v（item_type=1 → Sub）", row.MPS)
	}
}

func TestVisibleWithoutOptionIsLocked(t *testing.T) {
	row := browseRow(stubItem(13608, map[string]any{
		"best_purchase_option": nil,
		"purchase_options":     []any{},
	}), 13608, "cn", "bundleid")
	if row == nil || row.PriceStatus != "locked" {
		t.Fatalf("row=%+v", row)
	}
}

func TestZeroDecimalCurrencyPriceStaysCents(t *testing.T) {
	item := stubItem(13608, map[string]any{
		"best_purchase_option": map[string]any{
			"final_price_in_cents":         float64(499500),
			"price_before_bundle_discount": float64(549500),
			"discount_pct":                 float64(9),
			"bundle_discount_pct":          float64(10),
			"package_group":                "default",
		},
	})
	row := browseRow(item, 13608, "jp", "bundleid")
	if row == nil {
		t.Fatal("row missing")
	}
	if row.Price == nil || *row.Price != 499500 {
		t.Fatalf("price=%v（JPY final=499500 即「¥4,995」的分，不得再乘 100）", row.Price)
	}
	if row.OriginalPrice == nil || *row.OriginalPrice != 549500 {
		t.Fatalf("original=%v", row.OriginalPrice)
	}
	if row.Currency != "JPY" {
		t.Fatalf("currency=%q", row.Currency)
	}
	want := int64(math.RoundToEven(499500 * 0.04353))
	if got := rowCnyFen(*row, map[string]float64{"JPY": 0.04353}); got == nil || *got != want {
		t.Fatalf("cny=%v want %v（分→分折算）", got, want)
	}
}

func TestOriginalPricePrefersBeforeBundleDiscount(t *testing.T) {
	item := stubItem(13608, map[string]any{
		"best_purchase_option": map[string]any{
			"final_price_in_cents":         float64(1000),
			"original_price_in_cents":      float64(2000),
			"price_before_bundle_discount": float64(1800),
			"package_group":                "default",
		},
	})
	row := browseRow(item, 13608, "us", "bundleid")
	if row.OriginalPrice == nil || *row.OriginalPrice != 1800 {
		t.Fatalf("original=%v", row.OriginalPrice)
	}
}

func TestDiscountFallsBackToComputed(t *testing.T) {
	item := stubItem(13608, map[string]any{
		"best_purchase_option": map[string]any{
			"final_price_in_cents":         float64(7500),
			"price_before_bundle_discount": float64(10000),
			"package_group":                "default",
		},
	})
	row := browseRow(item, 13608, "us", "bundleid")
	if row.DiscountPercent != 25 {
		t.Fatalf("discount=%d（discount_pct 缺失时按原价/现价反算）", row.DiscountPercent)
	}
}

func TestDiscountEndTSFromOptionActiveDiscounts(t *testing.T) {
	// 促销截止取自选项级 active_discounts[0]（限时促销），非顶层结构档
	item := stubItem(13608, map[string]any{
		"active_discounts": []any{map[string]any{"discount_end_date": float64(999999)}}, // 顶层干扰项
		"best_purchase_option": map[string]any{
			"final_price_in_cents": float64(4856),
			"discount_pct":         float64(10),
			"active_discounts":     []any{map[string]any{"discount_end_date": float64(1767199999)}},
			"package_group":        "default",
		},
	})
	row := browseRow(item, 13608, "us", "bundleid")
	if row.DiscountEndTS == nil || *row.DiscountEndTS != 1767199999 {
		t.Fatalf("discount_end_ts=%v（选项级 active_discounts[0]）", row.DiscountEndTS)
	}
	// 无促销 → nil
	plain := browseRow(stubItem(13608, nil), 13608, "us", "bundleid")
	if plain.DiscountEndTS != nil {
		t.Fatalf("discount_end_ts=%v（无促销应为 nil）", plain.DiscountEndTS)
	}
}

func TestCnyFenNoneWithoutRate(t *testing.T) {
	row := browseRow(stubItem(13608, nil), 13608, "us", "bundleid")
	if got := rowCnyFen(*row, map[string]float64{}); got != nil {
		t.Fatalf("cny=%v（无汇率回 nil）", got)
	}
	locked := browseRow(stubItem(13608, map[string]any{
		"success": float64(15), "visible": false, "best_purchase_option": nil,
	}), 13608, "us", "bundleid")
	if got := rowCnyFen(*locked, map[string]float64{"USD": 7.25}); got != nil {
		t.Fatalf("cny=%v（无价行回 nil）", got)
	}
}

func TestHeaderImageFromAssetFormat(t *testing.T) {
	row := browseRow(stubItem(13608, nil), 13608, "us", "bundleid")
	if len(row.HeaderImage) < 10 || row.HeaderImage[len(row.HeaderImage)-len("/steam/bundles/13608/header.jpg"):] != "/steam/bundles/13608/header.jpg" {
		t.Fatalf("header=%q", row.HeaderImage)
	}
}

func TestOptionSelection(t *testing.T) {
	// best_purchase_option 优先
	best := stubItem(13608, map[string]any{
		"best_purchase_option": map[string]any{"final_price_in_cents": float64(4856), "package_group": "default"},
		"purchase_options":     []any{map[string]any{"final_price_in_cents": float64(9999), "package_group": "default"}},
	})
	status, opt := browseOption(best)
	if status != "ok" || toInt(opt["final_price_in_cents"]) == nil || *toInt(opt["final_price_in_cents"]) != 4856 {
		t.Fatalf("best: %s %v", status, opt["final_price_in_cents"])
	}
	// 无 best → default 组第一个
	filtered := stubItem(13608, map[string]any{
		"best_purchase_option": nil,
		"purchase_options": []any{
			map[string]any{"final_price_in_cents": float64(111), "package_group": "other"},
			map[string]any{"final_price_in_cents": float64(222), "package_group": "default"},
		},
	})
	status, opt = browseOption(filtered)
	if status != "ok" || *toInt(opt["final_price_in_cents"]) != 222 {
		t.Fatalf("default group: %s %v", status, opt["final_price_in_cents"])
	}
	// 选项无价 → no_price（选项仍在场）
	noPrice := stubItem(13608, map[string]any{
		"best_purchase_option": nil,
		"purchase_options":     []any{map[string]any{"package_group": "default"}},
	})
	status, opt = browseOption(noPrice)
	if status != "no_price" || opt == nil {
		t.Fatalf("no_price: %s %v", status, opt)
	}
}

func TestSemanticShapeSplit(t *testing.T) {
	// bundle 形态 + 选项级 must_purchase_as_set：形态 0 / 语义 1（实例 63575）
	bundleMPS := browseRow(stubItem(63575, map[string]any{
		"best_purchase_option": map[string]any{
			"final_price_in_cents": float64(10000), "must_purchase_as_set": true, "package_group": "default",
		},
	}), 63575, "us", "bundleid")
	if bundleMPS.ItemKind == nil || *bundleMPS.ItemKind != 0 {
		t.Fatalf("item_kind=%v（形态：bundle）", bundleMPS.ItemKind)
	}
	if bundleMPS.MPS == nil || *bundleMPS.MPS != 1 {
		t.Fatalf("mps=%v（语义：必须整包）", bundleMPS.MPS)
	}
	// sub 形态：语义恒不可拆，选项 mps 只锦上添花
	sub := browseRow(stubItem(1066582, map[string]any{
		"item_type": float64(1),
		"best_purchase_option": map[string]any{
			"final_price_in_cents": float64(499500), "must_purchase_as_set": false, "package_group": "default",
		},
	}), 1066582, "jp", "packageid")
	if sub.ItemKind == nil || *sub.ItemKind != 1 || sub.MPS == nil || *sub.MPS != 1 {
		t.Fatalf("sub kind=%v mps=%v", sub.ItemKind, sub.MPS)
	}
	// 普通 bundle 形态：可补齐
	plain := browseRow(stubItem(13608, nil), 13608, "us", "bundleid")
	if plain.ItemKind == nil || *plain.ItemKind != 0 || plain.MPS == nil || *plain.MPS != 0 {
		t.Fatalf("plain kind=%v mps=%v", plain.ItemKind, plain.MPS)
	}
	// 锁区行（无真实选项）不投语义票：形态仍有（顶层 item_type 不受锁区影响）
	locked := browseRow(stubItem(63575, map[string]any{
		"success":        float64(15),
		"visible":        false,
		"unvailable_for_country_restriction": true,
		"best_purchase_option":               nil,
		"purchase_options":                   []any{},
	}), 63575, "ru", "bundleid")
	if locked.PriceStatus != "locked" || locked.ItemKind == nil || *locked.ItemKind != 0 || locked.MPS != nil {
		t.Fatalf("locked status=%q kind=%v mps=%v", locked.PriceStatus, locked.ItemKind, locked.MPS)
	}
}

func TestMajorityVote(t *testing.T) {
	votes := []int64{0}
	for i := 0; i < 40; i++ {
		votes = append(votes, 1)
	}
	if got := majority(votes); got == nil || *got != 1 {
		t.Fatalf("40:1 → %v", got)
	}
	if got := majority([]int64{1, 1, 0}); got == nil || *got != 1 {
		t.Fatalf("[1,1,0] → %v", got)
	}
	if got := majority(nil); got != nil {
		t.Fatalf("[] → %v", got)
	}
	if got := majority([]int64{0, 1}); got != nil {
		t.Fatalf("平票不改写 → %v", got)
	}
}

func TestBrowseKindReadsShapeNotSemantics(t *testing.T) {
	if browseKind(iptr(0)) != "bundleid" || browseKind(iptr(1)) != "packageid" || browseKind(nil) != "bundleid" {
		t.Fatal("选键判据是形态：mps=1 的 bundle 形态包必须仍按 bundleid 问")
	}
}

func TestAlignment(t *testing.T) {
	// 条数一致按位置（未收录条目回 id=0，位置是唯一可靠映射）
	items := []storebrowse.Item{{"id": float64(1)}, {"id": float64(0)}, {"id": float64(3)}}
	pairs := alignItems(items, []int64{1, 2, 3})
	if len(pairs) != 3 || pairs[0].id != 1 || pairs[1].id != 2 || pairs[2].id != 3 {
		t.Fatalf("ids=%v", pairs)
	}
	if toInt(pairs[1].item["id"]) == nil || *toInt(pairs[1].item["id"]) != 0 {
		t.Fatalf("positional item2=%v", pairs[1].item)
	}
	// 条数不符降级按 id 映射
	short := []storebrowse.Item{{"id": float64(3)}, {"id": float64(1)}}
	pairs = alignItems(short, []int64{1, 2, 3})
	if toInt(pairs[0].item["id"]) == nil || *toInt(pairs[0].item["id"]) != 1 {
		t.Fatalf("by id item1=%v", pairs[0].item)
	}
	if pairs[1].item != nil {
		t.Fatalf("id=2 missing → %v", pairs[1].item)
	}
	if toInt(pairs[2].item["id"]) == nil || *toInt(pairs[2].item["id"]) != 3 {
		t.Fatalf("by id item3=%v", pairs[2].item)
	}
}

func TestFetchCCsFollowsEnabledAndExpandsSouthAsia(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	seedCrawlRegion(t, db, "cn", true, 1)
	seedCrawlRegion(t, db, "pk", true, 5)
	seedCrawlRegion(t, db, "us", true, 30)
	seedCrawlRegion(t, db, "ru", false, 2)
	ccs, err := svc.fetchCCs(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(ccs, []string{"cn", "pk", "bd", "us"}) {
		t.Fatalf("ccs=%v", ccs)
	}
}

func TestBDCurrencyFollowsSouthAsiaSlot(t *testing.T) {
	if ccCurrency["bd"] != ccCurrency["pk"] || ccCurrency["bd"] != "USD" {
		t.Fatalf("bd=%q pk=%q（bd 币种随南亚位，落行不出现空币种）", ccCurrency["bd"], ccCurrency["pk"])
	}
}

func TestFetchRegionsBatchedFansOutMonitoredCCs(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	seedCrawlRegion(t, db, "cn", true, 1)
	seedCrawlRegion(t, db, "pk", true, 5)
	seen := map[string]bool{}
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		seen[spec.Context.Country] = true
		var items []map[string]any
		for _, id := range specIDs(t, spec, "bundleid") {
			items = append(items, stubItem(id, nil))
		}
		return http.StatusOK, itemsPayload(items...)
	})
	out, err := svc.fetchRegionsBatched(context.Background(), []wantEntry{{BundleID: 13608, Kind: iptr(0)}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, cc := range []string{"CN", "PK", "BD"} {
		if !seen[cc] {
			t.Fatalf("seen=%v（整表抓取只对监控区含南亚拆区各发一发）", seen)
		}
	}
	rows := out[13608]
	codes := map[string]bool{}
	for _, r := range rows {
		codes[r.RegionCode] = true
	}
	if !codes["cn"] || !codes["pk"] || !codes["bd"] || len(rows) != 3 {
		t.Fatalf("rows=%v", rows)
	}
}

func TestFetchRegionsBatchedSkipsFailedRegion(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	seedCrawlRegion(t, db, "cn", true, 1)
	seedCrawlRegion(t, db, "pk", true, 5)
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		if spec.Context.Country == "BD" {
			return http.StatusInternalServerError, map[string]any{} // 该区失败
		}
		return http.StatusOK, itemsPayload(stubItem(13608, nil))
	})
	out, err := svc.fetchRegionsBatched(context.Background(), []wantEntry{{BundleID: 13608, Kind: iptr(0)}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	rows := out[13608]
	if len(rows) != 2 {
		t.Fatalf("rows=%v（失败区不写行，下轮重试）", rows)
	}
	for _, r := range rows {
		if r.RegionCode == "bd" {
			t.Fatalf("rows=%v", rows)
		}
	}
}

func TestFetchRegionsBatchedMissingItemWritesNothing(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	seedCrawlRegion(t, db, "cn", true, 1)
	// 未收录条目（name/slug 全空）混在位置对齐结果里
	stubBrowse(t, func(q string) (int, any) {
		return http.StatusOK, itemsPayload(map[string]any{
			"id": float64(999999999), "item_type": float64(2), "name": nil,
			"store_url_slug": "", "success": float64(15), "visible": false,
		})
	})
	out, err := svc.fetchRegionsBatched(context.Background(), []wantEntry{{BundleID: 999999999, Kind: iptr(0)}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(out[999999999]) != 0 {
		t.Fatalf("rows=%v（伪造 id 不写行）", out[999999999])
	}
}

func TestPlanBatchesGreedySplit(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	seedCrawlRegion(t, db, "cn", true, 1)
	const total = 301
	var want []wantEntry
	for i := 0; i < total; i++ {
		want = append(want, wantEntry{BundleID: int64(990500 + i), Kind: iptr(0)})
	}
	var batches [][]int64
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		batches = append(batches, specIDs(t, spec, "bundleid"))
		var items []map[string]any
		for _, id := range specIDs(t, spec, "bundleid") {
			items = append(items, stubItem(id, nil))
		}
		return http.StatusOK, itemsPayload(items...)
	})
	out, err := svc.fetchRegionsBatched(context.Background(), want, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(batches) < 2 {
		t.Fatalf("batches=%d（超 300 条或 URL 超长应再切）", len(batches))
	}
	covered := 0
	for _, batch := range batches {
		if len(batch) > 300 {
			t.Fatalf("batch=%d 超单发上限", len(batch))
		}
		covered += len(batch)
	}
	if covered != total {
		t.Fatalf("covered=%d want %d", covered, total)
	}
	if len(out[990500]) != 1 || len(out[990800]) != 1 {
		t.Fatalf("rows missing: %d/%d", len(out[990500]), len(out[990800]))
	}
}

func TestFetchBundleRegionsFallsBackToPackageID(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	seedCrawlRegion(t, db, "cn", true, 1)
	var kinds []string
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		if len(specIDs(t, spec, "packageid")) > 0 {
			kinds = append(kinds, "packageid")
			return http.StatusOK, itemsPayload(stubItem(13608, map[string]any{
				"item_type": float64(1),
				"best_purchase_option": map[string]any{
					"final_price_in_cents": float64(1499), "package_group": "default",
				},
			}))
		}
		kinds = append(kinds, "bundleid")
		return http.StatusOK, itemsPayload() // bundleid 无真实条目
	})
	rows, err := svc.fetchBundleRegions(context.Background(), 13608, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].MPS == nil || *rows[0].MPS != 1 {
		t.Fatalf("rows=%v", rows)
	}
	if !reflect.DeepEqual(kinds, []string{"bundleid", "packageid"}) {
		t.Fatalf("kinds=%v（库内身份问不到 → 换另一身份再问）", kinds)
	}
}

func TestFetchBundleRegionsForcePackageSkipsBundleProbe(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	seedCrawlRegion(t, db, "cn", true, 1)
	var kinds []string
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		if len(specIDs(t, spec, "packageid")) > 0 {
			kinds = append(kinds, "packageid")
		} else {
			kinds = append(kinds, "bundleid")
		}
		return http.StatusOK, itemsPayload()
	})
	rows, err := svc.fetchBundleRegions(context.Background(), 1066582, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 0 {
		t.Fatalf("rows=%v", rows)
	}
	if !reflect.DeepEqual(kinds, []string{"packageid"}) {
		t.Fatalf("kinds=%v（force_package 只问一次，不兜底）", kinds)
	}
}

func TestFetchBundleRegionsLockedRowsCountAsSuccess(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	seedCrawlRegion(t, db, "cn", true, 1)
	rounds := 0
	stubBrowse(t, func(q string) (int, any) {
		rounds++
		return http.StatusOK, itemsPayload(stubItem(13608, map[string]any{
			"success":        float64(15),
			"visible":        false,
			"unvailable_for_country_restriction": true,
			"best_purchase_option":               nil,
		}))
	})
	rows, err := svc.fetchBundleRegions(context.Background(), 13608, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(rows) != 1 || rows[0].PriceStatus != "locked" {
		t.Fatalf("rows=%v（真实存在但全区锁区仍算成功）", rows)
	}
	if rounds != 1 {
		t.Fatalf("rounds=%d（锁区行不算空表，不兜底）", rounds)
	}
}

func TestFetchBundleRegionsUsesEnabledRegionsOnly(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	_, err := svc.fetchBundleRegions(context.Background(), 13608, false)
	if !errors.Is(err, ErrRegionsDisabled) {
		t.Fatalf("err=%v（全禁用区错误上抛，手动导入转 400）", err)
	}
}
