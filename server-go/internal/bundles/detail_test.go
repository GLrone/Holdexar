package bundles

import (
	"context"
	"reflect"
	"strings"
	"testing"
)

func TestBundleDetailShapeAndGames(t *testing.T) {
	db := seedBaseListDB(t)
	detail, err := GetBundleDetail(context.Background(), db, bidA)
	if err != nil {
		t.Fatal(err)
	}
	if detail == nil {
		t.Fatal("detail nil")
	}
	if !reflect.DeepEqual(detail.AppIDs, []int64{app1, app2, app3}) {
		t.Fatalf("appIds=%v", detail.AppIDs)
	}
	if detail.URL != "https://store.steampowered.com/bundle/990101/" {
		t.Fatalf("url=%s", detail.URL)
	}
	if detail.HeaderImage != "https://shared.akamai.steamstatic.com/store_item_assets/steam/bundles/990101/header.jpg" {
		t.Fatalf("header=%s", detail.HeaderImage)
	}
	if got := detail.RegionPrices["US"].LockedCount; got != 1 {
		t.Fatalf("US lockedCount=%d", got)
	}
	if got := detail.RegionPrices["CN"].LockedCount; got != 0 {
		t.Fatalf("CN lockedCount=%d", got)
	}
	if got := detail.RegionPrices["US"].AppIDs; !reflect.DeepEqual(got, []int64{app1, app2}) {
		t.Fatalf("US appIds=%v", got)
	}
	if !strings.HasPrefix(detail.RegionPrices["CN"].Formatted, "¥") {
		t.Fatalf("CN formatted=%s", detail.RegionPrices["CN"].Formatted)
	}

	if len(detail.Games) != 3 {
		t.Fatalf("games=%d", len(detail.Games))
	}
	byAppid := map[int64]DetailGame{}
	for _, g := range detail.Games {
		byAppid[g.AppID] = g
	}
	g1 := byAppid[app1]
	if g1.Name == nil || *g1.Name != "测试游戏一" {
		t.Fatalf("g1=%+v", g1)
	}
	if v := g1.Prices["CN"].CnyFen; v == nil || *v != 3000 {
		t.Fatalf("g1 CN cny=%v", v)
	}
	if v := g1.Prices["JP"].CnyFen; v == nil || *v != 5000 {
		t.Fatalf("g1 JP cny=%v", v)
	}
	g3 := byAppid[app3]
	if g3.Name != nil {
		t.Fatalf("g3 name=%v", g3.Name)
	}
	if len(g3.Prices) != 0 {
		t.Fatalf("g3 prices=%v", g3.Prices)
	}
	if g3.HeaderImage != "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/990113/header.jpg" {
		t.Fatalf("g3 header=%s（未入库走 AppID 兜底）", g3.HeaderImage)
	}
	g2 := byAppid[app2]
	if g2.Name == nil || *g2.Name != "测试游戏二" {
		t.Fatalf("g2=%+v", g2)
	}
	if len(g2.Prices) != 1 {
		t.Fatalf("g2 prices=%v", g2.Prices)
	}
}

func TestBundleDetailDualProductIsolationAndImageNormalize(t *testing.T) {
	db := seedBaseListDB(t)
	detail, err := GetBundleDetail(context.Background(), db, bidC)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := detail.RegionPrices["RU"]; ok {
		t.Fatalf("C keeps pollution row: %v", detail.RegionPrices)
	}
	if detail.LowestRegion != "cn" || detail.LowestCnyFen == nil || *detail.LowestCnyFen != 20000 {
		t.Fatalf("C lowest=%s/%v", detail.LowestRegion, detail.LowestCnyFen)
	}
	if !strings.HasPrefix(detail.HeaderImage, "https://shared.akamai.steamstatic.com/store_item_assets/steam/bundles/") {
		t.Fatalf("C header=%s", detail.HeaderImage)
	}
}

func TestBundleDetailNotFound(t *testing.T) {
	db := seedBaseListDB(t)
	detail, err := GetBundleDetail(context.Background(), db, 990999)
	if err != nil {
		t.Fatalf("not-found must not error: %v", err)
	}
	if detail != nil {
		t.Fatalf("detail=%+v", detail)
	}
}

func TestBundleDetailSubKindFallback(t *testing.T) {
	db := seedBaseListDB(t)
	execSeed(t, db, `INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind,header_image,url) VALUES(990105,'订阅形态',1,NULL,NULL,NULL)`)
	detail, err := GetBundleDetail(context.Background(), db, 990105)
	if err != nil {
		t.Fatal(err)
	}
	if detail == nil {
		t.Fatal("detail nil")
	}
	if detail.ItemKind != 1 || detail.MustPurchaseAsSet != 1 {
		t.Fatalf("kind=%d mps=%d", detail.ItemKind, detail.MustPurchaseAsSet)
	}
	if detail.URL != "https://store.steampowered.com/sub/990105/" {
		t.Fatalf("url=%s", detail.URL)
	}
	if !strings.Contains(detail.HeaderImage, "/steam/subs/990105/header.jpg") {
		t.Fatalf("header=%s", detail.HeaderImage)
	}
	if len(detail.Games) != 0 {
		t.Fatalf("games=%v", detail.Games)
	}
	if len(detail.RegionPrices) != 0 {
		t.Fatalf("regionPrices=%v", detail.RegionPrices)
	}
}
