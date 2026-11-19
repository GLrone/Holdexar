package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"math"
	"path/filepath"
	"reflect"
	"testing"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

const (
	bidA = int64(990101)
	bidB = int64(990102)
	bidC = int64(990103)
	app1 = int64(990111)
	app2 = int64(990112)
	app3 = int64(990113)
	appS = int64(990114)
)

func execSeed(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.Exec(query, args...); err != nil {
		t.Fatalf("seed %s: %v", query, err)
	}
}

func openListDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "bundles-list.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	for _, q := range []string{
		`CREATE TABLE bundles(bundle_id INTEGER PRIMARY KEY,name TEXT,must_purchase_as_set INTEGER,item_kind INTEGER,
			header_image TEXT,url TEXT,min_cny_fen INTEGER,diff_fen INTEGER,is_lowest INTEGER,smart_score REAL)`,
		`CREATE TABLE bundle_region_prices(id INTEGER PRIMARY KEY,bundle_id INTEGER,region_code TEXT,currency TEXT,
			price INTEGER,discount_percent INTEGER NOT NULL DEFAULT 0,bundle_base_discount INTEGER NOT NULL DEFAULT 0,
			price_status TEXT,cny_fen INTEGER,discount_end_ts INTEGER,app_ids JSON,crawled_at TEXT)`,
		`CREATE TABLE games(appid INTEGER PRIMARY KEY,name TEXT,header_image TEXT,positive_rate INTEGER,review_count INTEGER,release_date TEXT)`,
		`CREATE TABLE game_current_prices(appid INTEGER,region_code TEXT,currency TEXT,price INTEGER,price_status TEXT,cny_fen INTEGER,PRIMARY KEY(appid,region_code))`,
		`CREATE TABLE monitor_targets(target_type TEXT,target_id INTEGER)`,
		`CREATE TABLE monitor_sources(target_type TEXT,target_id INTEGER,source TEXT,active INTEGER)`,
		`CREATE TABLE monitor_exclusions(target_type TEXT,target_id INTEGER,active INTEGER,reason TEXT)`,
		`CREATE TABLE crawl_regions(code TEXT PRIMARY KEY,enabled INTEGER)`,
		`CREATE TABLE wishlist_items(steamid TEXT,appid INTEGER,active INTEGER,owned INTEGER,PRIMARY KEY(steamid,appid))`,
		`CREATE TABLE tracked_accounts(steamid TEXT PRIMARY KEY,label TEXT,persona_name TEXT)`,
	} {
		execSeed(t, db, q)
	}
	return db
}

func seedBaseListDB(t *testing.T) *sql.DB {
	t.Helper()
	db := openListDB(t)
	execSeed(t, db, `INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind,header_image,url,min_cny_fen,diff_fen,is_lowest,smart_score) VALUES
		(990101,'测试补齐包',0,NULL,NULL,NULL,15000,0,1,0.0),
		(990102,'无价包',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
		(990103,'混写包',0,NULL,'https://shared.fastly.steamstatic.com/store_item_assets/steam/bundles/1/header.jpg',NULL,NULL,0,1,NULL)`)
	execSeed(t, db, `INSERT INTO bundle_region_prices(id,bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids) VALUES
		(1,990101,'CN','CNY',10000,0,10,'ok',10000,'[990111,990112,990113]'),
		(2,990101,'JP','USD',3000,0,10,'ok',15000,'[990111,990112,990113]'),
		(3,990101,'jp','USD',2000,0,10,'ok',NULL,'[990111,990112,990113]'),
		(4,990101,'US','USD',5000,0,10,'ok',36000,'[990111,990112]'),
		(5,990103,'CN','CNY',20000,0,10,'ok',20000,'[990111,990112,990113]'),
		(6,990103,'RU','USD',100,0,10,'ok',800,'[990114]')`)
	execSeed(t, db, `INSERT INTO games(appid,name,positive_rate,review_count) VALUES(990111,'测试游戏一',9000,1000),(990112,'测试游戏二',9000,1000)`)
	execSeed(t, db, `INSERT INTO game_current_prices(appid,region_code,currency,price,price_status,cny_fen) VALUES
		(990111,'CN','CNY',3000,'ok',3000),(990112,'CN','CNY',4000,'ok',4000),(990111,'JP','JPY',1000,'ok',5000)`)
	return db
}

func mustList(t *testing.T, db *sql.DB, opts ListOptions) *ListResult {
	t.Helper()
	res, err := ListBundles(context.Background(), db, opts)
	if err != nil {
		t.Fatal(err)
	}
	return res
}

func listIDs(res *ListResult) []int64 {
	out := make([]int64, 0, len(res.Bundles))
	for _, item := range res.Bundles {
		out = append(out, item.BundleID)
	}
	return out
}

func itemByID(res *ListResult, id int64) *BundleListItem {
	for i := range res.Bundles {
		if res.Bundles[i].BundleID == id {
			return &res.Bundles[i]
		}
	}
	return nil
}

func refreshSnapshots(t *testing.T, db *sql.DB, ids []int64, tracked []string) {
	t.Helper()
	conn, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if _, err := RefreshSortCache(context.Background(), conn, ids, tracked); err != nil {
		t.Fatal(err)
	}
}

func TestListPagesKeepGlobalTotalsAndStableOrder(t *testing.T) {
	db := seedBaseListDB(t)
	first := mustList(t, db, ListOptions{Limit: 1})
	second := mustList(t, db, ListOptions{Limit: 1, Offset: 1})
	if first.Total != 2 || second.Total != 2 {
		t.Fatalf("totals=%d/%d", first.Total, second.Total)
	}
	if first.CompletableCount != 2 {
		t.Fatalf("completable=%d", first.CompletableCount)
	}
	got := append(listIDs(first), listIDs(second)...)
	if !reflect.DeepEqual(got, []int64{bidA, bidC}) {
		t.Fatalf("order=%v", got)
	}
	if first.Offset != 0 || first.Limit != 1 || second.Offset != 1 {
		t.Fatalf("echo offset=%d/%d limit=%d", first.Offset, second.Offset, first.Limit)
	}
	empty := mustList(t, db, ListOptions{Limit: 1, Offset: 2})
	if len(empty.Bundles) != 0 || empty.Total != 2 {
		t.Fatalf("empty page=%d total=%d", len(empty.Bundles), empty.Total)
	}
}

func TestListAggregation(t *testing.T) {
	db := seedBaseListDB(t)
	res := mustList(t, db, ListOptions{Limit: 10})
	a := itemByID(res, bidA)
	if a == nil {
		t.Fatal("bundle A missing")
	}
	if a.Name != "测试补齐包" || a.MustPurchaseAsSet != 0 || a.ItemKind != 0 {
		t.Fatalf("A head=%+v", a.bundleSummary)
	}
	if !reflect.DeepEqual(a.AppIDs, []int64{app1, app2, app3}) {
		t.Fatalf("A appIds=%v", a.AppIDs)
	}
	if len(a.RegionPrices) != 3 {
		t.Fatalf("A regions=%v", a.RegionPrices)
	}
	for _, code := range []string{"CN", "JP", "US"} {
		if _, ok := a.RegionPrices[code]; !ok {
			t.Fatalf("A missing region %s in %v", code, a.RegionPrices)
		}
	}
	if v := a.RegionPrices["JP"].CnyFen; v == nil || *v != 15000 {
		t.Fatalf("A JP cny=%v（大写行有价者胜出）", v)
	}
	if v := a.RegionPrices["CN"].CnyFen; v == nil || *v != 10000 {
		t.Fatalf("A CN cny=%v", v)
	}
	if a.LowestRegion != "cn" || a.LowestCnyFen == nil || *a.LowestCnyFen != 10000 || a.DiffFen != 0 {
		t.Fatalf("A lowest=%s/%v diff=%d", a.LowestRegion, a.LowestCnyFen, a.DiffFen)
	}
	if a.URL != "https://store.steampowered.com/bundle/990101/" {
		t.Fatalf("A url=%s", a.URL)
	}
	raw, err := json.Marshal(a.RegionPrices["US"])
	if err != nil {
		t.Fatal(err)
	}
	var keys map[string]any
	if err := json.Unmarshal(raw, &keys); err != nil {
		t.Fatal(err)
	}
	for _, banned := range []string{"appIds", "lockedCount", "formatted"} {
		if _, ok := keys[banned]; ok {
			t.Fatalf("list entry carries %s: %s", banned, raw)
		}
	}
	if len(keys) != 6 {
		t.Fatalf("list entry keys=%v", keys)
	}
	itemRaw, err := json.Marshal(*a)
	if err != nil {
		t.Fatal(err)
	}
	var itemKeys map[string]json.RawMessage
	if err := json.Unmarshal(itemRaw, &itemKeys); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"bundleId", "name", "headerImage", "url", "mustPurchaseAsSet", "itemKind",
		"appIds", "regionPrices", "cnCnyFen", "lowestRegion", "lowestCnyFen", "diffFen", "smartScore",
		"priceData", "ownership"} {
		if _, ok := itemKeys[key]; !ok {
			t.Fatalf("item missing key %s: %s", key, itemRaw)
		}
	}

	c := itemByID(res, bidC)
	if c == nil {
		t.Fatal("bundle C missing")
	}
	if _, ok := c.RegionPrices["RU"]; ok {
		t.Fatalf("C keeps pollution row: %v", c.RegionPrices)
	}
	if len(c.RegionPrices) != 1 {
		t.Fatalf("C regions=%v", c.RegionPrices)
	}
	if c.LowestRegion != "cn" || c.LowestCnyFen == nil || *c.LowestCnyFen != 20000 {
		t.Fatalf("C lowest=%s/%v", c.LowestRegion, c.LowestCnyFen)
	}
	if !reflect.DeepEqual(c.AppIDs, []int64{app1, app2, app3}) {
		t.Fatalf("C appIds=%v", c.AppIDs)
	}
	if itemByID(res, bidB) != nil {
		t.Fatal("无价包不应出现在列表")
	}
	for _, item := range res.Bundles {
		if item.DiffFen < 0 {
			t.Fatalf("negative diff %d", item.DiffFen)
		}
	}
}

func TestListFiltersApplyBeforePagination(t *testing.T) {
	db := seedBaseListDB(t)
	page := mustList(t, db, ListOptions{Query: "混写", Limit: 1})
	if page.Total != 1 || len(page.Bundles) != 1 || page.Bundles[0].BundleID != bidC {
		t.Fatalf("query page=%d %v", page.Total, listIDs(page))
	}
	if got := mustList(t, db, ListOptions{PriceMin: floatPtr(150)}).Total; got != 1 {
		t.Fatalf("price_min total=%d", got)
	}
	if got := mustList(t, db, ListOptions{Region: "RU", RegionMode: "cheaper"}).Total; got != 0 {
		t.Fatalf("cheaper total=%d", got)
	}
	if got := mustList(t, db, ListOptions{Region: "RU", RegionMode: "locked"}).Total; got != 2 {
		t.Fatalf("locked total=%d", got)
	}
	if got := mustList(t, db, ListOptions{OnlyDiscounted: true}).Total; got != 0 {
		t.Fatalf("only_discounted total=%d", got)
	}
	gift := mustList(t, db, ListOptions{GiftOnly: true, Limit: 1})
	if gift.Total != 1 || len(gift.Bundles) != 1 || gift.Bundles[0].BundleID != bidA {
		t.Fatalf("gift_only=%d %v", gift.Total, listIDs(gift))
	}
	percent := mustList(t, db, ListOptions{Region: "JP", DiffType: "percent", DiffMin: floatPtr(-50), DiffMax: floatPtr(-50)})
	if percent.Total != 1 || percent.Bundles[0].BundleID != bidA {
		t.Fatalf("percent=%d %v", percent.Total, listIDs(percent))
	}
}

func floatPtr(v float64) *float64 { return &v }

func TestListRegionAnchorAndDiscountSort(t *testing.T) {
	db := seedBaseListDB(t)
	execSeed(t, db, `UPDATE bundle_region_prices SET cny_fen=1000, discount_percent=25 WHERE bundle_id=990101 AND region_code='JP'`)
	execSeed(t, db, `UPDATE bundle_region_prices SET discount_percent=60 WHERE bundle_id=990103 AND region_code='CN'`)
	execSeed(t, db, `INSERT INTO bundle_region_prices(id,bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids)
		VALUES(9,990103,'US','USD',6000,0,10,'ok',6000,'[990111,990112,990113]')`)
	refreshSnapshots(t, db, []int64{bidA, bidC}, nil)

	smart := mustList(t, db, ListOptions{Sort: "smart", Limit: 1})
	if len(smart.Bundles) != 1 || smart.Bundles[0].BundleID != bidC {
		t.Fatalf("smart first=%v", listIDs(smart))
	}
	for _, sort := range []string{"smart", "diff", "discount"} {
		page := mustList(t, db, ListOptions{Sort: sort, Region: "JP", Limit: 1})
		if page.Total != 2 {
			t.Fatalf("%s region total=%d", sort, page.Total)
		}
		if len(page.Bundles) != 1 || page.Bundles[0].BundleID != bidA {
			t.Fatalf("%s region first=%v", sort, listIDs(page))
		}
	}
	discount := mustList(t, db, ListOptions{Sort: "discount", Limit: 1})
	if len(discount.Bundles) != 1 || discount.Bundles[0].BundleID != bidC {
		t.Fatalf("discount first=%v", listIDs(discount))
	}
}

func TestRemovedViewExclusive(t *testing.T) {
	db := seedBaseListDB(t)
	execSeed(t, db, `INSERT INTO monitor_targets(target_type,target_id) VALUES('bundle',990101),('bundle',990103)`)
	execSeed(t, db, `INSERT INTO monitor_exclusions(target_type,target_id,active,reason) VALUES('bundle',990101,1,'bundle_removed')`)
	if got := mustList(t, db, ListOptions{Limit: 10}).Total; got != 0 {
		t.Fatalf("default total=%d", got)
	}
	removed := mustList(t, db, ListOptions{Limit: 10, Removed: true})
	ids := listIDs(removed)
	if removed.Total != 2 || !reflect.DeepEqual(ids, []int64{bidA, bidC}) {
		t.Fatalf("removed view=%d %v", removed.Total, ids)
	}
}

func TestTrackedRegionFilter(t *testing.T) {
	db := seedBaseListDB(t)
	execSeed(t, db, `INSERT INTO crawl_regions(code,enabled) VALUES('cn',1),('us',1)`)
	refreshSnapshots(t, db, []int64{bidA, bidB, bidC}, []string{"CN", "US"})
	a := itemByID(mustList(t, db, ListOptions{Limit: 10}), bidA)
	if len(a.RegionPrices) != 2 {
		t.Fatalf("A tracked regions=%v", a.RegionPrices)
	}
	if _, ok := a.RegionPrices["JP"]; ok {
		t.Fatal("JP 行应被追踪区过滤剔除")
	}
	if a.LowestRegion != "cn" || a.DiffFen != 0 {
		t.Fatalf("A lowest=%s diff=%d", a.LowestRegion, a.DiffFen)
	}

	execSeed(t, db, `INSERT INTO bundle_region_prices(id,bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids)
		VALUES(11,990103,'pk','USD',100,0,10,'ok',100,'[990111,990112,990113]'),
		      (12,990103,'BD','USD',90,0,10,'ok',90,'[990111,990112,990113]')`)
	execSeed(t, db, `DELETE FROM crawl_regions`)
	execSeed(t, db, `INSERT INTO crawl_regions(code,enabled) VALUES('cn',1),('pk',1)`)
	detailWithPK, err := GetBundleDetail(context.Background(), db, bidC)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := detailWithPK.RegionPrices["PK"]; !ok {
		t.Fatal("pk 追踪位应放行 PK 行")
	}
	if _, ok := detailWithPK.RegionPrices["BD"]; !ok {
		t.Fatal("pk 追踪位应放行 BD 行")
	}
	execSeed(t, db, `DELETE FROM crawl_regions`)
	execSeed(t, db, `INSERT INTO crawl_regions(code,enabled) VALUES('cn',1),('us',1)`)
	detailNoPK, err := GetBundleDetail(context.Background(), db, bidC)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := detailNoPK.RegionPrices["PK"]; ok {
		t.Fatal("不含 pk 时 PK 行应剔除")
	}
	if _, ok := detailNoPK.RegionPrices["BD"]; ok {
		t.Fatal("不含 pk 时 BD 行应剔除")
	}

	execSeed(t, db, `INSERT INTO bundle_region_prices(id,bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids)
		VALUES(13,990103,'TR','USD',100,0,10,'ok',5,'[990111,990112,990113]')`)
	refreshSnapshots(t, db, []int64{bidC}, []string{"CN", "US"})
	c := itemByID(mustList(t, db, ListOptions{Limit: 10}), bidC)
	if _, ok := c.RegionPrices["TR"]; ok {
		t.Fatal("TR 未追踪应剔除")
	}
	if c.LowestRegion != "cn" {
		t.Fatalf("C lowest=%s（未被 TR 带偏）", c.LowestRegion)
	}
}

func TestOwnershipFiltersCountAllMembersBeforeLimit(t *testing.T) {
	db := seedBaseListDB(t)
	execSeed(t, db, `INSERT INTO tracked_accounts(steamid,label) VALUES('bundle-self','Self'),('bundle-family','Family')`)
	execSeed(t, db, `INSERT INTO wishlist_items(steamid,appid,active,owned) VALUES('bundle-self',990111,1,1),('bundle-family',990112,1,1)`)
	execSeed(t, db, `UPDATE bundle_region_prices SET app_ids='[990112]' WHERE bundle_id=990103 AND region_code='CN'`)

	first := mustList(t, db, ListOptions{Limit: 1, PrimarySteamID: "bundle-self"})
	if first.Bundles[0].BundleID != bidA {
		t.Fatalf("first=%v", listIDs(first))
	}
	if got := first.Bundles[0].Ownership.Type; got == nil || *got != "owned" || first.Bundles[0].Ownership.Account != nil {
		t.Fatalf("A ownership=%+v", first.Bundles[0].Ownership)
	}
	second := mustList(t, db, ListOptions{Limit: 1, Offset: 1, PrimarySteamID: "bundle-self"})
	if second.Bundles[0].BundleID != bidC {
		t.Fatalf("second=%v", listIDs(second))
	}
	if got := second.Bundles[0].Ownership.Type; got == nil || *got != "family" {
		t.Fatalf("C ownership=%+v", second.Bundles[0].Ownership)
	}
	if got := second.Bundles[0].Ownership.Account; got == nil || *got != "Family" {
		t.Fatalf("C account=%v", second.Bundles[0].Ownership.Account)
	}

	ownedHidden := mustList(t, db, ListOptions{HideOwned: true, Limit: 1, PrimarySteamID: "bundle-self"})
	if ownedHidden.Total != 1 || ownedHidden.Bundles[0].BundleID != bidC {
		t.Fatalf("hide_owned=%d %v", ownedHidden.Total, listIDs(ownedHidden))
	}
	if got := ownedHidden.Bundles[0].Ownership.Type; got == nil || *got != "family" {
		t.Fatalf("hide_owned C ownership=%+v", ownedHidden.Bundles[0].Ownership)
	}
	familyHidden := mustList(t, db, ListOptions{HideFamily: true, Limit: 1, PrimarySteamID: "bundle-self"})
	if familyHidden.Total != 1 || familyHidden.Bundles[0].BundleID != bidA {
		t.Fatalf("hide_family=%d %v", familyHidden.Total, listIDs(familyHidden))
	}
	if got := familyHidden.Bundles[0].Ownership.Type; got == nil || *got != "owned" {
		t.Fatalf("hide_family A ownership=%+v", familyHidden.Bundles[0].Ownership)
	}
	if got := mustList(t, db, ListOptions{HideOwned: true, HideFamily: true, PrimarySteamID: "bundle-self"}).Total; got != 0 {
		t.Fatalf("both hidden total=%d", got)
	}
	plain := mustList(t, db, ListOptions{Limit: 10, PrimarySteamID: "bundle-self"})
	a := itemByID(plain, bidA)
	if got := a.Ownership.Type; got == nil || *got != "owned" {
		t.Fatalf("plain A ownership=%+v", a.Ownership)
	}
	c := itemByID(plain, bidC)
	if got := c.Ownership.Type; got == nil || *got != "family" {
		t.Fatalf("plain C ownership=%+v", c.Ownership)
	}
}

func TestHideOwnedWithoutOwnedRowsUsesFastPath(t *testing.T) {
	db := seedBaseListDB(t)
	res := mustList(t, db, ListOptions{Limit: 10, HideOwned: true})
	if res.Total != 2 {
		t.Fatalf("total=%d", res.Total)
	}
	for _, item := range res.Bundles {
		if item.Ownership.Type != nil {
			t.Fatalf("no owned rows → ownership null, got %+v", item.Ownership)
		}
	}
}

func TestListPriceDataFreshness(t *testing.T) {
	db := seedBaseListDB(t)
	crawled := time.Now().Add(-2 * time.Hour).Format("2006-01-02 15:04:05.000000")
	execSeed(t, db, `UPDATE bundle_region_prices SET crawled_at=? WHERE bundle_id=990101`, crawled)
	res := mustList(t, db, ListOptions{Limit: 10})
	a := itemByID(res, bidA)
	if a.PriceData.ObservedAt == nil || a.PriceData.Freshness == nil || *a.PriceData.Freshness != "fresh" {
		t.Fatalf("A priceData=%+v", a.PriceData)
	}
	if a.PriceData.AgeHours == nil || *a.PriceData.AgeHours <= 1.9 || *a.PriceData.AgeHours >= 2.1 {
		t.Fatalf("A ageHours=%v", a.PriceData.AgeHours)
	}
	c := itemByID(res, bidC)
	if c.PriceData.ObservedAt != nil || c.PriceData.AgeHours != nil || c.PriceData.Freshness != nil {
		t.Fatalf("C priceData=%+v（无观察不冒充旧数据）", c.PriceData)
	}
}

func TestListSnapshotMissingLowestRegionEmpty(t *testing.T) {
	db := seedBaseListDB(t)
	execSeed(t, db, `INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind,min_cny_fen,diff_fen) VALUES(990104,'无快照包',0,NULL,NULL,0)`)
	execSeed(t, db, `INSERT INTO bundle_region_prices(id,bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids)
		VALUES(20,990104,'CN','CNY',5000,0,10,'ok',NULL,'[990111,990112]')`)
	res := mustList(t, db, ListOptions{Limit: 10})
	d := itemByID(res, 990104)
	if d == nil {
		t.Fatal("bundle D missing")
	}
	if d.LowestRegion != "" || d.LowestCnyFen != nil {
		t.Fatalf("D lowest=%s/%v", d.LowestRegion, d.LowestCnyFen)
	}
	if v := d.RegionPrices["CN"].CnyFen; v != nil {
		t.Fatalf("D CN cny=%v", v)
	}
}

func TestListSameRegionCaseDedupLowerWinsWhenPriced(t *testing.T) {
	db := seedBaseListDB(t)
	execSeed(t, db, `INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind,min_cny_fen,diff_fen) VALUES(990107,'大小写包',0,NULL,NULL,0)`)
	execSeed(t, db, `INSERT INTO bundle_region_prices(id,bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids)
		VALUES(21,990107,'JP','USD',3000,0,10,'ok',NULL,'[990111]'),
		      (22,990107,'jp','USD',2000,0,10,'ok',7000,'[990111]')`)
	res := mustList(t, db, ListOptions{Limit: 10})
	g := itemByID(res, 990107)
	if g == nil {
		t.Fatal("bundle G missing")
	}
	entry, ok := g.RegionPrices["JP"]
	if !ok {
		t.Fatalf("G regions=%v", g.RegionPrices)
	}
	if entry.CnyFen == nil || *entry.CnyFen != 7000 {
		t.Fatalf("G JP cny=%v（有价行胜出）", entry.CnyFen)
	}
}

func TestListQueryUsesUnicodeLowerUDF(t *testing.T) {
	db := seedBaseListDB(t)
	execSeed(t, db, `INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind,min_cny_fen,diff_fen) VALUES(990106,'Мир Шоп',0,NULL,NULL,0)`)
	execSeed(t, db, `INSERT INTO bundle_region_prices(id,bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,price_status,cny_fen,app_ids)
		VALUES(30,990106,'CN','CNY',10000,0,10,'ok',10000,'[990111]')`)
	res := mustList(t, db, ListOptions{Query: "мир", Limit: 10})
	if res.Total != 1 || len(res.Bundles) != 1 || res.Bundles[0].BundleID != 990106 {
		t.Fatalf("cyrillic query=%d %v", res.Total, listIDs(res))
	}

	var lowered string
	if err := db.QueryRow("SELECT bundle_lower('Мир Шоп')").Scan(&lowered); err != nil {
		t.Fatal(err)
	}
	if lowered != "мир шоп" {
		t.Fatalf("bundle_lower=%q", lowered)
	}
	var nullLower sql.NullString
	if err := db.QueryRow("SELECT bundle_lower(NULL)").Scan(&nullLower); err != nil || nullLower.String != "" {
		t.Fatalf("bundle_lower(NULL)=%v,%v", nullLower, err)
	}
	diff := int64(9000)
	var score float64
	if err := db.QueryRow("SELECT bundle_save_score(9000)").Scan(&score); err != nil {
		t.Fatal(err)
	}
	if math.Abs(score-derived.SaveScore(&diff)) > 1e-12 {
		t.Fatalf("bundle_save_score=%v want %v", score, derived.SaveScore(&diff))
	}
}

func TestTrackedRegionCodesReader(t *testing.T) {
	db := openListDB(t)
	if got := trackedRegionCodes(context.Background(), db); got != nil {
		t.Fatalf("empty table → nil, got %v", got)
	}
	execSeed(t, db, `INSERT INTO crawl_regions(code,enabled) VALUES('cn',1),( 'us',0),('jp',1)`)
	got := trackedRegionCodes(context.Background(), db)
	if !got["CN"] || !got["JP"] || got["US"] || len(got) != 2 {
		t.Fatalf("tracked=%v", got)
	}
}
