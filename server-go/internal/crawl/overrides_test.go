package crawl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/storebrowse"
)

// overridesItem 构造单品批量响应（带一个标准版购买选项）。
func overridesItem(appid int64, name string) storebrowse.Item {
	return storebrowse.Item{"appid": appid, "success": 1, "visible": true, "type": 0, "name": name,
		"purchase_options": []any{map[string]any{
			"packageid": "39", "purchase_option_name": name,
			"final_price_in_cents": "1999", "original_price_in_cents": "1999",
		}}}
}

// TestRuntimeWriterLoadsSeriesOverrides B7：爬取期建 Writer 装填数据目录的
// series_overrides.json——写批引发系列联动时覆盖表生效（新占位行按覆盖表落位）。
func TestRuntimeWriterLoadsSeriesOverrides(t *testing.T) {
	db, gate := runtimeFixture(t)
	dataDir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dataDir, "series_overrides.json"),
		[]byte(`{"命运系列": [220]}`), 0o600); err != nil {
		t.Fatal(err)
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_ = json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"store_items": []any{overridesItem(220, "Elden Ring")}}})
	}))
	defer upstream.Close()
	r := New(db, gate, nil, Options{DataDir: dataDir, Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		out := map[int64]storebrowse.Item{}
		for _, id := range ids {
			out[id] = overridesItem(id, "Elden Ring")
		}
		return out, nil
	}})
	if _, err := r.Run(context.Background(), "manual", []Spec{{Scope: "appids", AppIDs: []int64{220}}}); err != nil {
		t.Fatal(err)
	}
	var series string
	if err := db.QueryRow("SELECT COALESCE(series_id,'') FROM games WHERE appid=220").Scan(&series); err != nil {
		t.Fatal(err)
	}
	if series != "命运系列" {
		t.Fatalf("覆盖表未落位：series_id=%q", series)
	}
}

// B5④ 端到端：coming_soon 且该区无购买答复 → COMING_SOON 暂缓行落库，
// 复查轮（comingsoon_retry）冷却期满后能选到该行。
func TestRuntimeComingSoonDefersAndRecheckPicksIt(t *testing.T) {
	db, gate := runtimeFixture(t)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		item := storebrowse.Item{"appid": 220, "success": 1, "visible": true, "type": 0, "name": "Future Game", "is_coming_soon": true}
		_ = json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"store_items": []any{item}}})
	}))
	defer upstream.Close()
	r := New(db, gate, nil, Options{Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		out := map[int64]storebrowse.Item{}
		for _, id := range ids {
			out[id] = storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": "Future Game", "is_coming_soon": true}
		}
		return out, nil
	}})
	if _, err := r.Run(context.Background(), "manual", []Spec{{Scope: "appids", AppIDs: []int64{220}}}); err != nil {
		t.Fatal(err)
	}
	var typ, name string
	if err := db.QueryRow(`SELECT type, name FROM games WHERE appid=220`).Scan(&typ, &name); err != nil {
		t.Fatal(err)
	}
	if typ != "COMING_SOON" || name != "AppID_220" {
		t.Fatalf("暂缓行不符: type=%q name=%q", typ, name)
	}
	var missingRows int
	if err := db.QueryRow(`SELECT COUNT(*) FROM game_current_prices WHERE appid=220`).Scan(&missingRows); err != nil {
		t.Fatal(err)
	}
	if missingRows != 0 {
		t.Fatalf("暂缓分支应清账不落欠账: %d", missingRows)
	}
	// 复查轮选取：冷却期满（updated_at 回拨 15 天）后能选到该行
	if _, err := db.Exec(`UPDATE games SET updated_at=? WHERE appid=220`, timestamp(r.now().AddDate(0, 0, -15))); err != nil {
		t.Fatal(err)
	}
	ids, err := r.recheckCandidates(context.Background(), "comingsoon_retry")
	if err != nil || len(ids) != 1 || ids[0] != 220 {
		t.Fatalf("复查轮应选到暂缓行: %v %v", ids, err)
	}
}

// B5③ 库内已知行未发售（release_date 在未来）且判定 missing：只清账不标记、
// 不落欠账。
func TestRuntimeFutureReleaseOnlyClearsLedger(t *testing.T) {
	db, gate := runtimeFixture(t)
	kind := "GAME"
	future := "2999-01-01"
	name := "Not Yet Released"
	writer := dbwriter.NewWriter(db, gate)
	writer.Now = func() time.Time { return time.Now().In(beijing).Add(-48 * time.Hour) }
	if _, err := writer.UpsertTaskBatch(context.Background(), []dbwriter.BatchEntry{{Game: dbwriter.GameData{
		AppID: 220, Name: &name, NameEn: &name, Type: &kind, ReleaseDate: &future,
	}}}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO game_current_prices(appid,region_code,price_status,updated_at,discount_percent,fail_count) VALUES
 (220,'CN','missing','2020-01-01 00:00:00.000000',0,3)`); err != nil {
		t.Fatal(err)
	}
	r := New(db, gate, nil, Options{Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		out := map[int64]storebrowse.Item{}
		for _, id := range ids {
			// 赠送态但响应里拿不到 0 价赠送选项 → 判定 missing
			out[id] = storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": name, "is_free_temporarily": true}
		}
		return out, nil
	}})
	if _, err := r.Run(context.Background(), "manual", []Spec{{Scope: "appids", AppIDs: []int64{220}}}); err != nil {
		t.Fatal(err)
	}
	var typ string
	if err := db.QueryRow(`SELECT type FROM games WHERE appid=220`).Scan(&typ); err != nil {
		t.Fatal(err)
	}
	if typ != "GAME" {
		t.Fatalf("未发售已知行不得改写为暂缓: %q", typ)
	}
	var rows int
	if err := db.QueryRow(`SELECT COUNT(*) FROM game_current_prices WHERE appid=220`).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 0 {
		t.Fatalf("误标 missing 陈欠行应一并清除: %d", rows)
	}
}

// B6 端到端：价格保存成功的批次白送的捆绑包发现落库（无价桩 + 单种子）。
func TestRuntimeRecordsBundleDiscoveries(t *testing.T) {
	db, gate := runtimeFixture(t)
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		item := storebrowse.Item{"appid": 220, "success": 1, "visible": true, "type": 0, "name": "Priced Game",
			"purchase_options": []any{
				map[string]any{"packageid": "39", "purchase_option_name": "Priced Game", "final_price_in_cents": "1999", "original_price_in_cents": "1999"},
				map[string]any{"bundleid": "333", "purchase_option_name": "Discovered Bundle", "must_purchase_as_set": false},
			}}
		_ = json.NewEncoder(w).Encode(map[string]any{"response": map[string]any{"store_items": []any{item}}})
	}))
	defer upstream.Close()
	r := New(db, gate, nil, Options{Fetch: func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		out := map[int64]storebrowse.Item{}
		for _, id := range ids {
			out[id] = storebrowse.Item{"appid": id, "success": 1, "visible": true, "type": 0, "name": "Priced Game",
				"purchase_options": []any{
					map[string]any{"packageid": "39", "purchase_option_name": "Priced Game", "final_price_in_cents": "1999", "original_price_in_cents": "1999"},
					map[string]any{"bundleid": "333", "purchase_option_name": "Discovered Bundle", "must_purchase_as_set": false},
				}}
		}
		return out, nil
	}})
	if _, err := r.Run(context.Background(), "manual", []Spec{{Scope: "appids", AppIDs: []int64{220}}}); err != nil {
		t.Fatal(err)
	}
	var name, appIDs string
	var updatedAt any
	var mps int64
	if err := db.QueryRow(`SELECT name, app_ids, updated_at, must_purchase_as_set FROM bundles WHERE bundle_id=333`).Scan(&name, &appIDs, &updatedAt, &mps); err != nil {
		t.Fatal(err)
	}
	if name != "Discovered Bundle" || appIDs != "[220]" || updatedAt != nil || mps != 0 {
		t.Fatalf("发现桩不符: %q %q %v %v", name, appIDs, updatedAt, mps)
	}
}
