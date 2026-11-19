package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/crawler"
	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/steamhttp"
	"holdexar/server-go/internal/store"
)

func openRefreshDB(t *testing.T) (*sql.DB, *store.WriteGate) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "bundles-refresh.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	for _, q := range []string{
		`CREATE TABLE bundles(bundle_id INTEGER PRIMARY KEY,name TEXT NOT NULL DEFAULT '',
			must_purchase_as_set INTEGER NOT NULL DEFAULT -1,item_kind INTEGER NOT NULL DEFAULT -1,
			header_image TEXT,is_lowest INTEGER NOT NULL DEFAULT 0,min_cny_fen INTEGER,
			diff_fen INTEGER NOT NULL DEFAULT 0,smart_score REAL,url TEXT,app_ids JSON,
			view_count INTEGER NOT NULL DEFAULT 0,updated_at DATETIME)`,
		`CREATE TABLE bundle_region_prices(id INTEGER PRIMARY KEY AUTOINCREMENT,bundle_id INTEGER NOT NULL,
			region_code TEXT NOT NULL,currency TEXT,price INTEGER,original_price INTEGER,
			discount_percent INTEGER NOT NULL DEFAULT 0,bundle_base_discount INTEGER NOT NULL DEFAULT 0,
			price_status TEXT NOT NULL DEFAULT 'ok',cny_fen INTEGER,discount_end_ts INTEGER,app_ids JSON,crawled_at DATETIME)`,
		`CREATE TABLE games(appid INTEGER PRIMARY KEY,name TEXT,header_image TEXT,positive_rate INTEGER,
			review_count INTEGER,release_date TEXT,type TEXT)`,
		`CREATE TABLE crawl_regions(code TEXT PRIMARY KEY,name TEXT,currency TEXT NOT NULL DEFAULT '',
			enabled INTEGER NOT NULL DEFAULT 0,sort INTEGER NOT NULL DEFAULT 0,updated_at DATETIME)`,
		`CREATE TABLE fx_rates(currency_code TEXT PRIMARY KEY,rate_to_cny REAL,fetched_at DATETIME)`,
		`CREATE TABLE monitor_targets(target_type TEXT,target_id INTEGER,created_at DATETIME,updated_at DATETIME,
			activated_at DATETIME,released_at DATETIME,excluded_at DATETIME,PRIMARY KEY(target_type,target_id))`,
		`CREATE TABLE monitor_sources(target_type TEXT,target_id INTEGER,source TEXT,priority INTEGER,
			active INTEGER,created_at DATETIME,updated_at DATETIME,PRIMARY KEY(target_type,target_id,source))`,
		`CREATE TABLE monitor_exclusions(target_type TEXT,target_id INTEGER,reason TEXT,active INTEGER,
			created_at DATETIME,cleared_at DATETIME)`,
		`CREATE UNIQUE INDEX ux_monitor_source ON monitor_sources(target_type,target_id,source)`,
		`CREATE UNIQUE INDEX ux_monitor_exclusion_active ON monitor_exclusions(target_type,target_id) WHERE active=1`,
	} {
		if _, err := db.Exec(q); err != nil {
			t.Fatalf("seed %s: %v", q, err)
		}
	}
	return db, store.NewWriteGate(db, path)
}

func newRefreshService(t *testing.T, db *sql.DB, gate *store.WriteGate) *Service {
	t.Helper()
	mon := monitoring.New(db, gate)
	svc := NewService(db, gate, mon)
	svc.Now = func() time.Time { return time.Date(2026, 11, 14, 12, 0, 0, 0, time.UTC) }
	svc.client = steamhttp.New(steamhttp.Config{
		Timeout:          2 * time.Second,
		MaxRetries:       1,
		BackoffOtherBase: time.Millisecond,
		Backoff429Base:   time.Millisecond,
		JitterMax:        -1,
		RateLimits:       crawler.NewExitRateLimits(nil, 200, 300*time.Second, crawler.NewRateLimiter(200, 300*time.Second)),
		Breakers:         crawler.NewExitBreakers(nil, crawler.NewBreaker429()),
	})
	return svc
}

type browseStub struct {
	mu      sync.Mutex
	queries []string
	respond func(q string) (int, any)
}

func stubBrowse(t *testing.T, respond func(q string) (int, any)) *browseStub {
	t.Helper()
	st := &browseStub{respond: respond}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query().Get("input_json")
		st.mu.Lock()
		st.queries = append(st.queries, q)
		respond := st.respond
		st.mu.Unlock()
		status, payload := respond(q)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(payload)
	}))
	t.Cleanup(srv.Close)
	old := browseEndpoint
	browseEndpoint = srv.URL
	t.Cleanup(func() { browseEndpoint = old })
	return st
}

func (st *browseStub) count() int {
	st.mu.Lock()
	defer st.mu.Unlock()
	return len(st.queries)
}

type stubSpec struct {
	IDs []struct {
		AppID     int64 `json:"appid"`
		BundleID  int64 `json:"bundleid"`
		PackageID int64 `json:"packageid"`
	} `json:"ids"`
	Context struct {
		Country string `json:"country_code"`
	} `json:"context"`
}

func parseSpec(t *testing.T, raw string) stubSpec {
	t.Helper()
	var spec stubSpec
	if err := json.Unmarshal([]byte(raw), &spec); err != nil {
		t.Fatalf("parse input_json %q: %v", raw, err)
	}
	return spec
}

func specIDs(t *testing.T, spec stubSpec, kind string) []int64 {
	t.Helper()
	out := make([]int64, 0, len(spec.IDs))
	for _, id := range spec.IDs {
		if kind == "packageid" {
			if id.PackageID != 0 {
				out = append(out, id.PackageID)
			}
			continue
		}
		if id.BundleID != 0 {
			out = append(out, id.BundleID)
		}
	}
	return out
}

func stubItem(id int64, kw map[string]any) map[string]any {
	base := map[string]any{
		"id":              float64(id),
		"item_type":       float64(2),
		"name":            "Overcooked! 2 - Complete the Set",
		"store_url_slug":  "Overcooked_2_Complete_the_Set",
		"success":         float64(1),
		"visible":         true,
		"included_appids": []any{float64(728880), float64(858240)},
		"best_purchase_option": map[string]any{
			"final_price_in_cents":    float64(4856),
			"original_price_in_cents": float64(5396),
			"discount_pct":            float64(10),
			"bundle_discount_pct":     float64(10),
			"package_group":           "default",
		},
		"assets": map[string]any{
			"asset_url_format": "steam/bundles/13608/${FILENAME}",
			"header":           "header.jpg",
		},
	}
	for k, v := range kw {
		base[k] = v
	}
	return base
}

func itemsPayload(items ...map[string]any) any {
	if items == nil {
		items = []map[string]any{}
	}
	return map[string]any{"response": map[string]any{"store_items": items}}
}

func iptr(v int64) *int64 { return &v }

func seedCrawlRegion(t *testing.T, db *sql.DB, code string, enabled bool, sort int) {
	t.Helper()
	on := 0
	if enabled {
		on = 1
	}
	if _, err := db.Exec(`INSERT INTO crawl_regions(code,currency,enabled,sort) VALUES(?,?,?,?)
		ON CONFLICT(code) DO UPDATE SET enabled=excluded.enabled,sort=excluded.sort`,
		code, "CNY", on, sort); err != nil {
		t.Fatal(err)
	}
}

func seedBundleRow(t *testing.T, db *sql.DB, id int64, name string, mps, kind int64) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO bundles(bundle_id,name,must_purchase_as_set,item_kind) VALUES(?,?,?,?)`,
		id, name, mps, kind); err != nil {
		t.Fatal(err)
	}
}

func watchBundle(t *testing.T, svc *Service, id int64, source string) {
	t.Helper()
	if _, err := svc.Mon.Apply(context.Background(), "bundle", []int64{id}, monitoring.Change{
		Sources: map[int64][]string{id: {source}},
	}); err != nil {
		t.Fatal(err)
	}
}

func bundleRegionCodes(t *testing.T, db *sql.DB, id int64) map[string]sql.NullString {
	t.Helper()
	rows, err := db.Query(`SELECT region_code,currency FROM bundle_region_prices WHERE bundle_id=?`, id)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := map[string]sql.NullString{}
	for rows.Next() {
		var code string
		var currency sql.NullString
		if err := rows.Scan(&code, &currency); err != nil {
			t.Fatal(err)
		}
		out[code] = currency
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

func mustInt(t *testing.T, db *sql.DB, query string, args ...any) int64 {
	t.Helper()
	var v sql.NullInt64
	if err := db.QueryRow(query, args...).Scan(&v); err != nil {
		t.Fatalf("%s: %v", query, err)
	}
	if !v.Valid {
		t.Fatalf("%s: NULL", query)
	}
	return v.Int64
}

func assertZeroSkip(t *testing.T, res RefreshResult) {
	t.Helper()
	if !res.OK || res.Updated != 0 || res.Total != 0 || res.RegionPrices != 0 ||
		len(res.Failed) != 0 || res.DroppedSingletons != nil {
		t.Fatalf("skip result=%+v", res)
	}
}

const (
	scopeA = int64(99710401)
	scopeB = int64(99710402)
	scopeC = int64(99710403)
)

func seedScopeBundles(t *testing.T, db *sql.DB) {
	t.Helper()
	seedBundleRow(t, db, scopeA, "bundle-scope-probe-0", -1, -1)
	seedBundleRow(t, db, scopeB, "bundle-scope-probe-1", -1, -1)
	seedBundleRow(t, db, scopeC, "bundle-scope-probe-2", -1, -1)
}

func TestRefreshOnlyCoversWatchedBundles(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedScopeBundles(t, db)
	watchBundle(t, svc, scopeA, "favorite")
	watchBundle(t, svc, scopeB, "import")
	seedCrawlRegion(t, db, "cn", true, 1)
	if _, err := db.Exec(`INSERT INTO fx_rates(currency_code,rate_to_cny) VALUES('CNY',1.0)`); err != nil {
		t.Fatal(err)
	}

	asked := map[int64]bool{}
	st := stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		ids := specIDs(t, spec, "bundleid")
		if len(specIDs(t, spec, "packageid")) > 0 {
			t.Errorf("不该按 packageid 请求: %s", q)
		}
		var items []map[string]any
		for _, id := range ids {
			asked[id] = true
			items = append(items, stubItem(id, map[string]any{
				"name":            "bundle-scope-probe-" + strconv.FormatInt(id, 10),
				"included_appids": []any{float64(99710411), float64(99710412)},
				"best_purchase_option": map[string]any{
					"final_price_in_cents": float64(10000), "package_group": "default",
				},
			}))
		}
		return http.StatusOK, itemsPayload(items...)
	})

	res, err := svc.RefreshFollowed(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !res.OK || res.Total != 2 || res.Updated != 2 || res.RegionPrices != 2 ||
		len(res.Failed) != 0 || res.DroppedSingletons == nil || *res.DroppedSingletons != 0 {
		t.Fatalf("result=%+v", res)
	}
	if asked[scopeC] {
		t.Fatal("无监控记录的包不该进刷新集")
	}
	if !asked[scopeA] || !asked[scopeB] {
		t.Fatalf("watched=%v", asked)
	}
	if n := st.count(); n != 1 {
		t.Fatalf("requests=%d", n)
	}
	var name string
	var updated sql.NullString
	if err := db.QueryRow(`SELECT name,CAST(updated_at AS TEXT) FROM bundles WHERE bundle_id=?`, scopeA).Scan(&name, &updated); err != nil {
		t.Fatal(err)
	}
	if name != "bundle-scope-probe-99710401" || updated.String != "2026-11-14 12:00:00.000000" {
		t.Fatalf("A row name=%q updated=%q", name, updated.String)
	}
	codes := bundleRegionCodes(t, db, scopeA)
	if currency, ok := codes["cn"]; !ok || currency.String != "CNY" {
		t.Fatalf("A regions=%v", codes)
	}
	if got := mustInt(t, db, `SELECT cny_fen FROM bundle_region_prices WHERE bundle_id=? AND region_code='cn'`, scopeA); got != 10000 {
		t.Fatalf("A cn cny=%d", got)
	}
	if got := mustInt(t, db, `SELECT must_purchase_as_set FROM bundles WHERE bundle_id=?`, scopeA); got != 0 {
		t.Fatalf("A mps=%d（bundle 形态有选项未标整包 → 可补齐）", got)
	}
	if got := mustInt(t, db, `SELECT diff_fen FROM bundles WHERE bundle_id=?`, scopeA); got != 0 {
		t.Fatalf("A diff=%d", got)
	}
}

func TestRefreshSkipsRoundWithoutWatchedBundles(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedScopeBundles(t, db)
	st := stubBrowse(t, func(q string) (int, any) {
		t.Errorf("无关注包时不应发起抓取: %s", q)
		return http.StatusOK, itemsPayload()
	})
	res, err := svc.RefreshFollowed(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	assertZeroSkip(t, res)
	if n := st.count(); n != 0 {
		t.Fatalf("requests=%d", n)
	}
}

func TestRefreshSkipsWhenAllRegionsDisabled(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedBundleRow(t, db, scopeA, "disabled-region-probe", -1, -1)
	watchBundle(t, svc, scopeA, "favorite")
	st := stubBrowse(t, func(q string) (int, any) {
		t.Errorf("全禁用区不应发起抓取: %s", q)
		return http.StatusOK, itemsPayload()
	})
	res, err := svc.RefreshFollowed(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	assertZeroSkip(t, res)
	if st.count() != 0 {
		t.Fatalf("requests=%d", st.count())
	}
}

func TestRefreshSingletonPurgedAndCounted(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedBundleRow(t, db, scopeA, "桩_单DLC sub", 1, 1)
	if _, err := db.Exec(`INSERT INTO bundle_region_prices(bundle_id,region_code,currency,price,price_status,app_ids)
		VALUES(?,?,?,?,?,?)`, scopeA, "us", "USD", 499, "ok", "[16801]"); err != nil {
		t.Fatal(err)
	}
	watchBundle(t, svc, scopeA, "favorite")
	seedCrawlRegion(t, db, "cn", true, 1)
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		var items []map[string]any
		for _, id := range specIDs(t, spec, "bundleid") {
			items = append(items, stubItem(id, map[string]any{
				"included_appids": []any{float64(16801)},
				"item_type":       float64(1),
				"name":            "单 DLC",
				"best_purchase_option": map[string]any{
					"final_price_in_cents": float64(499), "package_group": "default",
				},
			}))
		}
		return http.StatusOK, itemsPayload(items...)
	})

	res, err := svc.RefreshFollowed(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !res.OK || res.Updated != 0 || res.Total != 1 || res.RegionPrices != 0 ||
		len(res.Failed) != 0 || res.DroppedSingletons == nil || *res.DroppedSingletons != 1 {
		t.Fatalf("result=%+v", res)
	}
	if got := mustInt(t, db, `SELECT count(*) FROM bundles WHERE bundle_id=?`, scopeA); got != 0 {
		t.Fatalf("bundles rows=%d（单品应删净主档）", got)
	}
	if got := mustInt(t, db, `SELECT count(*) FROM bundle_region_prices WHERE bundle_id=?`, scopeA); got != 0 {
		t.Fatalf("price rows=%d（单品应删净区域价）", got)
	}
}

func TestRefreshFlipsShapeWhenPrimaryKindMisses(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedBundleRow(t, db, scopeA, "错形态导入", -1, 0)
	watchBundle(t, svc, scopeA, "import")
	seedCrawlRegion(t, db, "cn", true, 1)
	var bundleidRounds, packageRounds int
	stubBrowse(t, func(q string) (int, any) {
		spec := parseSpec(t, q)
		if len(specIDs(t, spec, "packageid")) > 0 {
			packageRounds++
			return http.StatusOK, itemsPayload(stubItem(scopeA, map[string]any{
				"name":            "真身是 sub",
				"item_type":       float64(1),
				"included_appids": []any{float64(99710411), float64(99710412)},
				"best_purchase_option": map[string]any{
					"final_price_in_cents": float64(1499), "package_group": "default",
				},
			}))
		}
		bundleidRounds++
		return http.StatusOK, itemsPayload()
	})

	res, err := svc.RefreshFollowed(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !res.OK || res.Updated != 1 || res.Total != 1 {
		t.Fatalf("result=%+v", res)
	}
	if bundleidRounds != 1 || packageRounds != 1 {
		t.Fatalf("rounds bundleid=%d packageid=%d", bundleidRounds, packageRounds)
	}
	if got := mustInt(t, db, `SELECT item_kind FROM bundles WHERE bundle_id=?`, scopeA); got != 1 {
		t.Fatalf("item_kind=%d（兜底条目 item_type=1 应写回形态）", got)
	}
}

func TestUpsertStaleCleanupAndLowercaseAuthority(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedBundleRow(t, db, 990301, "脏行包", 0, 0)
	if _, err := db.Exec(`INSERT INTO bundle_region_prices(bundle_id,region_code,currency,price,price_status,cny_fen,app_ids) VALUES
		(990301,'JP','USD',3000,'ok',15000,'[990111]'),
		(990301,'RU','USD',100,'ok',800,'[990111]')`); err != nil {
		t.Fatal(err)
	}
	rateMap := map[string]float64{"USD": 7.25, "CNY": 1.0}
	regions := []regionRow{
		{BundleID: 990301, RegionCode: "jp", Currency: "USD", Price: iptr(2000), AppIDs: []int64{990111, 990112},
			PriceStatus: "ok", MPS: iptr(0), ItemKind: iptr(0), Kind: "bundleid"},
		{BundleID: 990301, RegionCode: "cn", Currency: "CNY", Price: iptr(10000), AppIDs: []int64{990111, 990112},
			PriceStatus: "ok", MPS: iptr(0), ItemKind: iptr(0), Kind: "bundleid"},
	}
	stored, err := svc.upsertBundleRows(context.Background(), 990301, regions, rateMap, "2026-11-14 12:00:00.000000", false)
	if err != nil || !stored {
		t.Fatalf("stored=%v err=%v", stored, err)
	}
	codes := bundleRegionCodes(t, db, 990301)
	if _, ok := codes["jp"]; !ok {
		t.Fatalf("regions=%v（本次小写行应保留）", codes)
	}
	if _, ok := codes["cn"]; !ok {
		t.Fatalf("regions=%v", codes)
	}
	for _, banned := range []string{"JP", "RU"} {
		if _, ok := codes[banned]; ok {
			t.Fatalf("regions=%v（%s 旧行应删：本次未写入/大小写权威）", codes, banned)
		}
	}
	if got := mustInt(t, db, `SELECT cny_fen FROM bundle_region_prices WHERE bundle_id=990301 AND region_code='jp'`); got != 14500 {
		t.Fatalf("jp cny=%d（round(2000×7.25)）", got)
	}
	var appIDs string
	if err := db.QueryRow(`SELECT app_ids FROM bundle_region_prices WHERE bundle_id=990301 AND region_code='cn'`).Scan(&appIDs); err != nil {
		t.Fatal(err)
	}
	if appIDs != "[990111, 990112]" {
		t.Fatalf("cn app_ids=%q（json.dumps 缺省分隔符）", appIDs)
	}
}

func TestUpsertTieVoteKeepsStoredSemantics(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	seedBundleRow(t, db, 990401, "旧名", 1, 0)
	rateMap := map[string]float64{"CNY": 1.0}
	regions := []regionRow{
		{BundleID: 990401, RegionCode: "cn", Currency: "CNY", Price: iptr(10000), AppIDs: []int64{728880, 858240},
			PriceStatus: "ok", MPS: iptr(0), ItemKind: iptr(1), Kind: "bundleid"},
		{BundleID: 990401, RegionCode: "us", Currency: "USD", Price: iptr(5000), AppIDs: []int64{728880, 858240},
			PriceStatus: "ok", MPS: iptr(1), ItemKind: iptr(0), Kind: "bundleid"},
	}
	stored, err := svc.upsertBundleRows(context.Background(), 990401, regions, rateMap, "2026-11-14 12:00:00.000000", false)
	if err != nil || !stored {
		t.Fatalf("stored=%v err=%v", stored, err)
	}
	if got := mustInt(t, db, `SELECT must_purchase_as_set FROM bundles WHERE bundle_id=990401`); got != 1 {
		t.Fatalf("mps=%d（平票不改写）", got)
	}
	if got := mustInt(t, db, `SELECT item_kind FROM bundles WHERE bundle_id=990401`); got != 0 {
		t.Fatalf("kind=%d（平票不改写）", got)
	}
	var name string
	if err := db.QueryRow(`SELECT name FROM bundles WHERE bundle_id=990401`).Scan(&name); err != nil {
		t.Fatal(err)
	}
	if name != "旧名" {
		t.Fatalf("name=%q", name)
	}
}

func TestUpsertMajorityWinsAndNewRowDefaults(t *testing.T) {
	db, gate := openRefreshDB(t)
	svc := newRefreshService(t, db, gate)
	rateMap := map[string]float64{"CNY": 1.0}
	regions := []regionRow{
		{BundleID: 990402, RegionCode: "cn", Currency: "CNY", Price: iptr(10000), AppIDs: []int64{728880, 858240},
			PriceStatus: "locked", ItemKind: iptr(1), Kind: "bundleid"},
		{BundleID: 990402, RegionCode: "us", Currency: "USD", Price: nil,
			PriceStatus: "locked", ItemKind: iptr(1), MPS: iptr(1), Kind: "packageid"},
		{BundleID: 990402, RegionCode: "jp", Currency: "JPY", Price: iptr(499500), AppIDs: []int64{728880, 858240},
			PriceStatus: "ok", MPS: iptr(1), ItemKind: iptr(1), Kind: "packageid"},
	}
	stored, err := svc.upsertBundleRows(context.Background(), 990402, regions, rateMap, "2026-11-14 12:00:00.000000", false)
	if err != nil || !stored {
		t.Fatalf("stored=%v err=%v", stored, err)
	}
	if got := mustInt(t, db, `SELECT item_kind FROM bundles WHERE bundle_id=990402`); got != 1 {
		t.Fatalf("kind=%d（多数决 2:1）", got)
	}
	if got := mustInt(t, db, `SELECT must_purchase_as_set FROM bundles WHERE bundle_id=990402`); got != 1 {
		t.Fatalf("mps=%d", got)
	}
	var currency sql.NullString
	var price, originalPrice, cny sql.NullInt64
	var status string
	if err := db.QueryRow(`SELECT currency,price,original_price,cny_fen,price_status FROM bundle_region_prices
		WHERE bundle_id=990402 AND region_code='cn'`).Scan(&currency, &price, &originalPrice, &cny, &status); err != nil {
		t.Fatal(err)
	}
	if !currency.Valid || currency.String != "CNY" || price.Int64 != 10000 || cny.Int64 != 10000 || status != "locked" {
		t.Fatalf("cn row=%v/%v/%v/%v/%q", currency, price, originalPrice, cny, status)
	}
	if err := db.QueryRow(`SELECT cny_fen FROM bundle_region_prices WHERE bundle_id=990402 AND region_code='us'`).Scan(&cny); err != nil {
		t.Fatal(err)
	}
	if cny.Valid {
		t.Fatalf("us cny=%v（无价行 cny 应 NULL）", cny)
	}
	var appIDs string
	if err := db.QueryRow(`SELECT app_ids FROM bundle_region_prices WHERE bundle_id=990402 AND region_code='us'`).Scan(&appIDs); err != nil {
		t.Fatal(err)
	}
	if appIDs != "[]" {
		t.Fatalf("us app_ids=%q（无 included_appids 落空数组）", appIDs)
	}
	if got := mustInt(t, db, `SELECT is_lowest FROM bundles WHERE bundle_id=990402`); got != 0 {
		t.Fatalf("is_lowest=%d", got)
	}
}

func TestLoadRatesOverlayAndCNY(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	if _, err := db.Exec(`INSERT INTO fx_rates(currency_code,rate_to_cny) VALUES('USD',7.25),('JPY',0.05)`); err != nil {
		t.Fatal(err)
	}
	rates, err := svc.loadRates(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rates["USD"] != 7.25 || rates["JPY"] != 0.05 {
		t.Fatalf("rates us=%v jpy=%v（表值覆盖兜底）", rates["USD"], rates["JPY"])
	}
	if rates["CNY"] != 1.0 {
		t.Fatalf("CNY=%v", rates["CNY"])
	}
	if rates["CHF"] != 8.10 || rates["TRY"] != 0.22 {
		t.Fatalf("fallback chf=%v try=%v", rates["CHF"], rates["TRY"])
	}
}

func TestErrRegionsDisabledSentinel(t *testing.T) {
	db, _ := openRefreshDB(t)
	svc := newRefreshService(t, db, nil)
	_, err := svc.fetchCCs(context.Background())
	if !errors.Is(err, ErrRegionsDisabled) {
		t.Fatalf("err=%v", err)
	}
	if err.Error() != "未启用任何区服：请先在「我」页勾选要爬取的地区" {
		t.Fatalf("msg=%q", err.Error())
	}
}

func TestEnqueueEnsureGameExistsUsesSeriesOverridesFromDataDir(t *testing.T) {
	ctx := context.Background()
	dataDir := t.TempDir()
	dbPath := filepath.Join(dataDir, "holdexar.db")
	db, err := store.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('games.derived_inputs', '{"day":"2026-11-06","series":{}}')
		ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`); err != nil {
		t.Fatal(err)
	}
	svc := NewService(db, store.NewWriteGate(db, dbPath), monitoring.New(db, store.NewWriteGate(db, dbPath)))
	called := 0
	svc.LoadOverrides = func() (map[string][]int64, []string) {
		called++
		return map[string][]int64{"CustomSeries": {777}}, []string{"CustomSeries"}
	}
	if err := svc.ensureGameExists(ctx, 777, "Override Game"); err != nil {
		t.Fatal(err)
	}
	if called != 1 {
		t.Fatalf("expected LoadOverrides to be called once, got %d", called)
	}
	var series string
	if err := db.QueryRow(`SELECT COALESCE(series_id,'') FROM games WHERE appid=777`).Scan(&series); err != nil || series != "CustomSeries" {
		t.Fatalf("series_id=%q err=%v", series, err)
	}
}
