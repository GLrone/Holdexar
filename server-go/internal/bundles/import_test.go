package bundles

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"testing"
)

func TestParseBundleRef(t *testing.T) {
	cases := []struct {
		text string
		id   int64
		kind string
		ok   bool
	}{
		{"https://store.steampowered.com/bundle/21478/", 21478, "bundle", true},
		{"https://store.steampowered.com/sub/61597/?query=1#frag", 61597, "sub", true},
		{"https://steamdb.info/bundle/123/?countrymix=x", 123, "bundle", true},
		{"https://steamdb.in/sub/99/", 99, "sub", true},
		{"https://steamdb.info/BUNDLE/55/", 55, "bundle", true},
		{"  https://store.steampowered.com/sub/7/  ", 7, "sub", true},
		{"21478", 21478, "bundle", true},
		{"", 0, "", false},
		{"abc", 0, "", false},
		{"https://store.steampowered.com/app/123", 0, "", false},
		{"https://example.com/bundle/123", 0, "", false},
	}
	for _, c := range cases {
		id, kind, ok := parseBundleRef(c.text)
		if ok != c.ok || (ok && (id != c.id || kind != c.kind)) {
			t.Fatalf("parse(%q)=%d,%q,%v want %d,%q,%v", c.text, id, kind, ok, c.id, c.kind, c.ok)
		}
	}
}

func TestImportSubFlowDirectPackage(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedCrawlRegion(t, db, "us", true, 30)
	var kinds []string
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		if len(specIDs(t, spec, "packageid")) > 0 {
			kinds = append(kinds, "packageid")
			return http.StatusOK, itemsPayload(stubItem(1066582, map[string]any{
				"item_type":       float64(1),
				"name":            "Overcooked! 2 - Gourmet Edition",
				"included_appids": []any{float64(440)},
				"best_purchase_option": map[string]any{
					"final_price_in_cents": float64(1499), "package_group": "default",
				},
			}))
		}
		kinds = append(kinds, "bundleid")
		return http.StatusOK, itemsPayload()
	})

	outcome, err := svc.ImportBundle(context.Background(), "https://store.steampowered.com/sub/1066582/")
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.OK || outcome.BundleID != 1066582 || outcome.Kind != "sub" ||
		outcome.Existed || outcome.RegionPrices != 1 ||
		outcome.Name != "Overcooked! 2 - Gourmet Edition" {
		t.Fatalf("outcome=%+v", outcome)
	}
	if len(kinds) != 1 || kinds[0] != "packageid" {
		t.Fatalf("kinds=%v", kinds)
	}
	if got := mustInt(t, db, `SELECT count(*) FROM bundles WHERE bundle_id=1066582`); got != 1 {
		t.Fatalf("bundles=%d", got)
	}
	if got := mustInt(t, db, `SELECT must_purchase_as_set FROM bundles WHERE bundle_id=1066582`); got != 1 {
		t.Fatalf("mps=%d", got)
	}
	if got := mustInt(t, db, `SELECT item_kind FROM bundles WHERE bundle_id=1066582`); got != 1 {
		t.Fatalf("kind=%d", got)
	}
	var price, cny sql.NullInt64
	if err := db.QueryRow(`SELECT price,cny_fen FROM bundle_region_prices
		WHERE bundle_id=1066582 AND region_code='us'`).Scan(&price, &cny); err != nil {
		t.Fatal(err)
	}
	if !price.Valid || price.Int64 != 1499 || !cny.Valid || cny.Int64 != 10868 {
		t.Fatalf("price=%v cny=%v（round(1499×7.25)）", price, cny)
	}
	var source string
	if err := db.QueryRow(`SELECT source FROM monitor_sources WHERE target_type='bundle'
		AND target_id=1066582 AND active=1`).Scan(&source); err != nil {
		t.Fatalf("import 来源未登记: %v", err)
	}
	if source != "import" {
		t.Fatalf("source=%q", source)
	}
}

func TestImportBundleLinkProbesBothKinds(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedCrawlRegion(t, db, "cn", true, 1)
	var kinds []string
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		if len(specIDs(t, spec, "packageid")) > 0 {
			kinds = append(kinds, "packageid")
			return http.StatusOK, itemsPayload()
		}
		kinds = append(kinds, "bundleid")
		return http.StatusOK, itemsPayload(stubItem(21478, nil))
	})
	outcome, err := svc.ImportBundle(context.Background(), "https://store.steampowered.com/bundle/21478/")
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.OK || outcome.Kind != "bundle" || outcome.RegionPrices != 1 {
		t.Fatalf("outcome=%+v", outcome)
	}
	if len(kinds) != 1 || kinds[0] != "bundleid" {
		t.Fatalf("kinds=%v", kinds)
	}
}

func TestImportEmptyFetchFailsWith400FamilyError(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedCrawlRegion(t, db, "cn", true, 1)
	stubBrowse(t, func(q string) (int, any) {
		return http.StatusOK, itemsPayload()
	})
	_, err := svc.ImportBundle(context.Background(), "https://store.steampowered.com/bundle/13608/")
	if !errors.Is(err, ErrImportFetchFailed) {
		t.Fatalf("err=%v", err)
	}
	if want := "捆绑包 13608 抓取失败（不存在/限速/网络）——稍后重试或检查链接"; err.Error() != want {
		t.Fatalf("msg=%q want %q", err.Error(), want)
	}
	_, err = svc.ImportBundle(context.Background(), "https://store.steampowered.com/sub/61597/")
	if !errors.Is(err, ErrImportFetchFailed) {
		t.Fatalf("sub err=%v", err)
	}
	if want := "Sub 61597 抓取失败（不存在/限速/网络）——稍后重试或检查链接"; err.Error() != want {
		t.Fatalf("sub msg=%q want %q", err.Error(), want)
	}
}

func TestImportUnrecognizedRef(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	stubBrowse(t, func(q string) (int, any) {
		t.Errorf("识别失败不应出网: %s", q)
		return http.StatusOK, itemsPayload()
	})
	for _, text := range []string{"", "abc", "https://example.com/bundle/123"} {
		if _, err := svc.ImportBundle(context.Background(), text); !errors.Is(err, ErrUnrecognizedRef) {
			t.Fatalf("parse(%q) err=%v", text, err)
		}
	}
}

func TestImportAllowsSingletonSub(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedCrawlRegion(t, db, "cn", true, 1)
	stubBrowse(t, func(q string) (int, any) {
		return http.StatusOK, itemsPayload(stubItem(1066582, map[string]any{
			"item_type":       float64(1),
			"name":            "Prime Status Upgrade",
			"included_appids": []any{float64(440)},
			"best_purchase_option": map[string]any{
				"final_price_in_cents": float64(1499), "package_group": "default",
			},
		}))
	})
	outcome, err := svc.ImportBundle(context.Background(), "1066582")
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.OK || outcome.Kind != "bundle" || outcome.Name != "Prime Status Upgrade" {
		t.Fatalf("outcome=%+v（裸数字默认 bundle，双轨自动落 sub）", outcome)
	}
	if got := mustInt(t, db, `SELECT count(*) FROM bundle_region_prices WHERE bundle_id=1066582`); got != 1 {
		t.Fatalf("rows=%d（手动导入单 appid sub 例外保留）", got)
	}
}

func TestImportExistingBundleMarked(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedCrawlRegion(t, db, "cn", true, 1)
	seedBundleRow(t, db, 21478, "旧档", 0, 0)
	stubBrowse(t, func(q string) (int, any) {
		return http.StatusOK, itemsPayload(stubItem(21478, nil))
	})
	outcome, err := svc.ImportBundle(context.Background(), "https://store.steampowered.com/bundle/21478/")
	if err != nil {
		t.Fatal(err)
	}
	if !outcome.Existed {
		t.Fatalf("existed=%v", outcome.Existed)
	}
	var name string
	if err := db.QueryRow(`SELECT name FROM bundles WHERE bundle_id=21478`).Scan(&name); err != nil {
		t.Fatal(err)
	}
	if name != "Overcooked! 2 - Complete the Set" {
		t.Fatalf("name=%q", name)
	}
}

func TestImportSortCacheFailureDoesNotBreakImport(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedCrawlRegion(t, db, "cn", true, 1)
	stubBrowse(t, func(q string) (int, any) {
		return http.StatusOK, itemsPayload(stubItem(21478, nil))
	})
	if _, err := db.Exec(`DROP TABLE games`); err != nil {
		t.Fatal(err)
	}
	outcome, err := svc.ImportBundle(context.Background(), "https://store.steampowered.com/bundle/21478/")
	if err != nil {
		t.Fatalf("快照失败不该拖垮导入: %v", err)
	}
	if !outcome.OK || outcome.RegionPrices != 1 {
		t.Fatalf("outcome=%+v", outcome)
	}
}

func TestImportDisabledRegionsErrors(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	stubBrowse(t, func(q string) (int, any) {
		t.Errorf("全禁用区不应出网: %s", q)
		return http.StatusOK, itemsPayload()
	})
	_, err := svc.ImportBundle(context.Background(), "https://store.steampowered.com/bundle/21478/")
	if !errors.Is(err, ErrRegionsDisabled) {
		t.Fatalf("err=%v", err)
	}
}
