package bundles

// HTTP 契约测试（对照 Python test_bundles_api.py 的 HTTP 断言 + 生命周期
// test_bundle_follow_remove.py）：9 条端点形状、FastAPI 422 复刻、生命周期
// 状态机、空关注集刷新零键形状。

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"holdexar/server-go/internal/monitoring"
)

func newHTTPFixture(t *testing.T) (*Service, *sql.DB, http.Handler) {
	t.Helper()
	svc, db := newHTTPService(t)
	mux := http.NewServeMux()
	svc.Register(mux, RegisterOptions{})
	return svc, db, mux
}

func newHTTPService(t *testing.T) (*Service, *sql.DB) {
	t.Helper()
	db, gate := openRefreshDB(t)
	if _, err := db.Exec(`CREATE TABLE steam_accounts(steam_id TEXT PRIMARY KEY,persona_name TEXT,
		is_active INTEGER,bound_at DATETIME)`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE app_settings(key TEXT PRIMARY KEY,value_json JSON)`); err != nil {
		t.Fatal(err)
	}
	svc := newRefreshService(t, db, gate)
	return svc, db
}

func callJSON(t *testing.T, handler http.Handler, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	var reader *strings.Reader
	if body == "" {
		reader = strings.NewReader("")
	} else {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, path, reader)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	return rec
}

func decodeBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var payload map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatalf("响应非 JSON：%v │ %s", err, rec.Body.String())
	}
	return payload
}

func TestBundlesListValidation422(t *testing.T) {
	_, _, handler := newHTTPFixture(t)
	cases := []struct {
		query string
		name  string
		typ   string
	}{
		{"limit=201", "limit", "less_than_equal"},
		{"limit=0", "limit", "greater_than_equal"},
		{"limit=abc", "limit", "int_parsing"},
		{"offset=-1", "offset", "greater_than_equal"},
		{"offset=zz", "offset", "int_parsing"},
		{"price_min=nan", "price_min", "finite_number"},
		{"price_max=inf", "price_max", "finite_number"},
		{"diff_min=abc", "diff_min", "float_parsing"},
		{"hide_owned=maybe", "hide_owned", "bool_parsing"},
	}
	for _, tc := range cases {
		rec := callJSON(t, handler, "GET", "/api/v1/bundles?"+tc.query, "")
		if rec.Code != 422 {
			t.Fatalf("%s → %d，应为 422 │ %s", tc.query, rec.Code, rec.Body.String())
		}
		payload := decodeBody(t, rec)
		issues, _ := payload["detail"].([]any)
		if len(issues) == 0 {
			t.Fatalf("%s 无校验问题列表 │ %s", tc.query, rec.Body.String())
		}
		first, _ := issues[0].(map[string]any)
		if first["type"] != tc.typ {
			t.Fatalf("%s type=%v，应为 %s", tc.query, first["type"], tc.typ)
		}
		loc, _ := first["loc"].([]any)
		if len(loc) < 2 || loc[0] != "query" || loc[1] != tc.name {
			t.Fatalf("%s loc 形状不符 │ %s", tc.query, rec.Body.String())
		}
	}
}

func TestBundlesListEmptyShape(t *testing.T) {
	_, _, handler := newHTTPFixture(t)
	rec := callJSON(t, handler, "GET", "/api/v1/bundles", "")
	if rec.Code != 200 {
		t.Fatalf("status=%d │ %s", rec.Code, rec.Body.String())
	}
	payload := decodeBody(t, rec)
	for _, key := range []string{"bundles", "total", "completableCount", "offset", "limit"} {
		if _, ok := payload[key]; !ok {
			t.Fatalf("缺键 %s │ %s", key, rec.Body.String())
		}
	}
	if payload["limit"].(float64) != 120 {
		t.Fatalf("默认 limit=%v，应为 120", payload["limit"])
	}
}

func TestBundlesLifecycleFlow(t *testing.T) {
	svc, db, handler := newHTTPFixture(t)
	seedBundleRow(t, db, 10, "测试包", 0, 0)

	rec := callJSON(t, handler, "PUT", "/api/v1/bundles/10/follow", "")
	if rec.Code != 200 {
		t.Fatalf("follow → %d │ %s", rec.Code, rec.Body.String())
	}
	if got := decodeBody(t, rec); got["followed"] != true || got["bundleId"].(float64) != 10 {
		t.Fatalf("follow 响应不符：%s", rec.Body.String())
	}
	rec = callJSON(t, handler, "GET", "/api/v1/bundles/follows", "")
	if ids := decodeBody(t, rec)["bundleIds"].([]any); len(ids) != 1 || ids[0].(float64) != 10 {
		t.Fatalf("follows=%s", rec.Body.String())
	}
	// 关注再导入（来源共存）：import 来源在场时取关只摘星不遗忘
	if _, err := svc.Mon.Apply(context.Background(), "bundle", []int64{10},
		lifecycleTestChange()); err != nil {
		t.Fatal(err)
	}
	rec = callJSON(t, handler, "DELETE", "/api/v1/bundles/10/follow", "")
	if got := decodeBody(t, rec); got["followed"] != false {
		t.Fatalf("unfollow 响应不符：%s", rec.Body.String())
	}
	rec = callJSON(t, handler, "GET", "/api/v1/bundles/follows", "")
	if ids := decodeBody(t, rec)["bundleIds"].([]any); len(ids) != 0 {
		t.Fatalf("取关后 follows 应为空（favorite 已摘）：%s", rec.Body.String())
	}
	var sources int
	if err := db.QueryRow("SELECT count(*) FROM monitor_sources WHERE target_type='bundle' AND target_id=10 AND active=1").Scan(&sources); err != nil {
		t.Fatal(err)
	}
	if sources != 1 {
		t.Fatalf("导入来源应保留，active sources=%d", sources)
	}
	// 星标是唯一来源时取关 → forget 基线
	if _, err := svc.Mon.Apply(context.Background(), "bundle", []int64{10}, lifecycleTestChange()); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Mon.Apply(context.Background(), "bundle", []int64{10},
		lifecycleDropImport()); err != nil {
		t.Fatal(err)
	}
	callJSON(t, handler, "PUT", "/api/v1/bundles/10/follow", "")
	rec = callJSON(t, handler, "DELETE", "/api/v1/bundles/10/follow", "")
	if rec.Code != 200 {
		t.Fatalf("unfollow → %d", rec.Code)
	}
	var targets int
	if err := db.QueryRow("SELECT count(*) FROM monitor_targets WHERE target_type='bundle' AND target_id=10").Scan(&targets); err != nil {
		t.Fatal(err)
	}
	if targets != 0 {
		t.Fatalf("唯一来源取关后应遗忘目标，targets=%d", targets)
	}
	// remove → 404 语义 / restore 状态机
	callJSON(t, handler, "PUT", "/api/v1/bundles/10/follow", "")
	rec = callJSON(t, handler, "POST", "/api/v1/bundles/10/remove", "")
	if got := decodeBody(t, rec); got["removed"] != true {
		t.Fatalf("remove 响应不符：%s", rec.Body.String())
	}
	rec = callJSON(t, handler, "POST", "/api/v1/bundles/999/remove", "")
	if rec.Code != 404 {
		t.Fatalf("remove 不存在 → %d，应为 404", rec.Code)
	}
	if !strings.Contains(rec.Body.String(), "捆绑包 999 不存在") {
		t.Fatalf("404 文案不符：%s", rec.Body.String())
	}
	rec = callJSON(t, handler, "POST", "/api/v1/bundles/10/restore", "")
	if got := decodeBody(t, rec); got["restored"] != true {
		t.Fatalf("restore 响应不符：%s", rec.Body.String())
	}
	rec = callJSON(t, handler, "POST", "/api/v1/bundles/10/restore", "")
	if got := decodeBody(t, rec); got["restored"] != false {
		t.Fatalf("二次 restore 应 restored=false（排除已清）：%s", rec.Body.String())
	}
}

func TestBundlesDetail404AndPathValidation(t *testing.T) {
	_, _, handler := newHTTPFixture(t)
	rec := callJSON(t, handler, "GET", "/api/v1/bundles/not-an-id", "")
	if rec.Code != 422 {
		t.Fatalf("路径非整数 → %d，应为 422", rec.Code)
	}
	payload := decodeBody(t, rec)
	issues := payload["detail"].([]any)
	first, _ := issues[0].(map[string]any)
	loc, _ := first["loc"].([]any)
	if first["type"] != "int_parsing" || loc[0] != "path" || loc[1] != "bundle_id" {
		t.Fatalf("路径参数校验形状不符：%s", rec.Body.String())
	}
	rec = callJSON(t, handler, "GET", "/api/v1/bundles/42", "")
	if rec.Code != 404 || !strings.Contains(rec.Body.String(), "bundle not found") {
		t.Fatalf("详情不存在 → %d │ %s", rec.Code, rec.Body.String())
	}
}

func TestBundlesRefreshEmptyWatchSet(t *testing.T) {
	_, _, handler := newHTTPFixture(t)
	rec := callJSON(t, handler, "POST", "/api/v1/bundles/refresh", "")
	if rec.Code != 200 {
		t.Fatalf("refresh → %d │ %s", rec.Code, rec.Body.String())
	}
	body := rec.Body.String()
	if !strings.Contains(body, `"ok":true`) || !strings.Contains(body, `"total":0`) {
		t.Fatalf("空关注集刷新形状不符：%s", body)
	}
	if strings.Contains(body, "droppedSingletons") {
		t.Fatalf("跳过分支不应有 droppedSingletons 键：%s", body)
	}
}

func TestBundlesImportValidation(t *testing.T) {
	_, db, handler := newHTTPFixture(t)
	if _, err := db.Exec(`INSERT INTO crawl_regions(code,name,currency,enabled,sort)
		VALUES('CN','中国','CNY',1,0)`); err != nil {
		t.Fatal(err)
	}
	rec := callJSON(t, handler, "POST", "/api/v1/bundles/import", `{"text":"完全不是链接"}`)
	if rec.Code != 400 {
		t.Fatalf("无法识别的导入文本 → %d，应为 400 │ %s", rec.Code, rec.Body.String())
	}
	rec = callJSON(t, handler, "POST", "/api/v1/bundles/import", `not-json`)
	if rec.Code != 400 {
		t.Fatalf("非 dict 载荷按空文本 → 识别失败 400，得 %d │ %s", rec.Code, rec.Body.String())
	}
	// 裸数字识别成功但抓取全空 → 400 抓取失败（browse 打桩返回空响应）
	stub := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"response":{}}`))
	}))
	defer stub.Close()
	prev := browseEndpoint
	browseEndpoint = stub.URL + "/api"
	t.Cleanup(func() { browseEndpoint = prev })
	rec = callJSON(t, handler, "POST", "/api/v1/bundles/import", `{"text":"https://store.steampowered.com/bundle/12345/"}`)
	if rec.Code != 400 || !strings.Contains(rec.Body.String(), "抓取失败") {
		t.Fatalf("抓取空响应 → 400 │ %d %s", rec.Code, rec.Body.String())
	}
}

func TestBundlesListPrimarySteamID(t *testing.T) {
	_, db, _ := newHTTPFixture(t)
	if _, err := db.Exec(`INSERT INTO steam_accounts(steam_id,persona_name,is_active,bound_at)
		VALUES('76561198000000001','甲',1,'2026-01-01 00:00:00'),
		('76561198000000002','乙',0,'2026-02-01 00:00:00')`); err != nil {
		t.Fatal(err)
	}
	if got := primarySteamID(context.Background(), db); got != "76561198000000001" {
		t.Fatalf("主账号=%s", got)
	}
	if _, err := db.Exec("DELETE FROM steam_accounts"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('account.steam_id','"76561198000000009"')`); err != nil {
		t.Fatal(err)
	}
	if got := primarySteamID(context.Background(), db); got != "76561198000000009" {
		t.Fatalf("设置回退=%s", got)
	}
	if _, err := db.Exec("DELETE FROM app_settings"); err != nil {
		t.Fatal(err)
	}
	if got := primarySteamID(context.Background(), db); got != "" {
		t.Fatalf("无绑定应空串，得 %s", got)
	}
}

func TestBundlesHookUsesTrackedRegions(t *testing.T) {
	svc, db, _ := newHTTPFixture(t)
	seedBundleRow(t, db, 7, "钩子包", 0, 0)
	if _, err := db.Exec(`INSERT INTO crawl_regions(code,name,currency,enabled,sort)
		VALUES('CN','中国','CNY',1,0),('JP','日本','JPY',0,1)`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO bundle_region_prices(bundle_id,region_code,currency,price,
		price_status,cny_fen,crawled_at,app_ids) VALUES
		(7,'CN','CNY',10000,'ok',10000,'2026-11-14 00:00:00.000000','[1,2]')`); err != nil {
		t.Fatal(err)
	}
	var conn *sql.Conn
	conn, err := db.Conn(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	if err := svc.RefreshSortCacheHook(context.Background(), conn, []int64{7}); err != nil {
		t.Fatal(err)
	}
	var diff int64
	if err := db.QueryRow("SELECT diff_fen FROM bundles WHERE bundle_id=7").Scan(&diff); err != nil {
		t.Fatal(err)
	}
	if diff != 0 {
		t.Fatalf("仅 CN 有价时 diff 应为 0，得 %d", diff)
	}
}

// 生命周期测试辅助：直接构造 monitoring.Change 的两态。
func lifecycleTestChange() monitoring.Change {
	on := true
	return monitoring.Change{Sources: map[int64][]string{10: {"import"}}, Excluded: &on}
}

func lifecycleDropImport() monitoring.Change {
	return monitoring.Change{Sources: map[int64][]string{10: {}}}
}
