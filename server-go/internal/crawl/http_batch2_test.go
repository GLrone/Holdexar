package crawl

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	"holdexar/server-go/internal/games"
)

func serve(t *testing.T, r *Runtime) (*httptest.ResponseRecorder, func(method, path, body string) *httptest.ResponseRecorder) {
	t.Helper()
	mux := http.NewServeMux()
	r.Register(mux, context.Background())
	do := func(method, path, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(method, path, strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		rec := httptest.NewRecorder()
		mux.ServeHTTP(rec, req)
		return rec
	}
	return nil, do
}

func decodeJSON(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应不是 JSON：%v（%s）", err, rec.Body.String())
	}
	return out
}

func freshQuery(ids ...string) string {
	return "/api/v1/crawl/freshness?" + joinAppids(ids)
}

func joinAppids(ids []string) string {
	parts := make([]string, len(ids))
	for i, id := range ids {
		parts[i] = "appid=" + id
	}
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += "&"
		}
		out += p
	}
	return out
}

func TestFreshnessHTTPGuards(t *testing.T) {
	r, _ := readFixture(t)
	_, do := serve(t, r)
	rec := do("GET", "/api/v1/crawl/freshness", "")
	if rec.Code != 400 {
		t.Fatalf("空参应 400：%d %s", rec.Code, rec.Body.String())
	}
	if !strings.Contains(rec.Body.String(), "缺少 appid 参数") {
		t.Fatal(rec.Body.String())
	}
	rec = do("GET", "/api/v1/crawl/freshness?appid=abc", "")
	if rec.Code != 422 {
		t.Fatalf("非整 appid 应 422：%d", rec.Code)
	}
	detail, _ := decodeJSON(t, rec)["detail"].([]any)
	first, _ := detail[0].(map[string]any)
	if first["type"] != "int_parsing" {
		t.Fatal(first)
	}
	if loc, _ := first["loc"].([]any); len(loc) != 3 || loc[0] != "query" || loc[1] != "appid" || loc[2] != float64(0) {
		t.Fatal(first["loc"])
	}
	many := make([]string, 201)
	for i := range many {
		many[i] = strconv.Itoa(i + 1)
	}
	rec = do("GET", freshQuery(many...), "")
	if rec.Code != 400 || !strings.Contains(rec.Body.String(), "一次最多查询 200 个 appid") {
		t.Fatalf("201 个应 400：%d %s", rec.Code, rec.Body.String())
	}
}

func TestFreshnessHTTPDedupOrder(t *testing.T) {
	r, db := readFixture(t)
	readExec(t, db, `INSERT INTO game_current_prices(appid,region_code,discount_percent,fail_count,price_status,attempt_outcome,steam_answer,last_success_at,updated_at)
		VALUES(10,'CN',0,0,'ok','success','ok','2026-11-06 10:00:00','2026-11-06 10:00:00')`)
	_, do := serve(t, r)
	rec := do("GET", freshQuery("10", "20", "10"), "")
	if rec.Code != 200 {
		t.Fatalf("重复参数应 200：%d %s", rec.Code, rec.Body.String())
	}
	var payload struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if len(payload.Items) != 2 {
		t.Fatalf("应去重为 2 项：%s", rec.Body.String())
	}
	if payload.Items[0]["appid"] != float64(10) || payload.Items[1]["appid"] != float64(20) {
		t.Fatal("去重必须保序")
	}
	item := payload.Items[0]
	if _, ok := item["regions"].(map[string]any); !ok {
		t.Fatal("regions 明细缺失")
	}
}

func TestImportHTTPValidation(t *testing.T) {
	r, _ := readFixture(t)
	_, do := serve(t, r)
	cases := []struct {
		name string
		body string
		want int
		kind string
	}{
		{"空列表", `{"appids": []}`, 422, "too_short"},
		{"101 个", `{"appids": [` + stringOf101() + `]}`, 422, "too_long"},
		{"非整元素", `{"appids": ["abc"]}`, 422, "int_parsing"},
		{"带小数", `{"appids": [2.9]}`, 422, "int_from_float"},
		{"缺字段", `{}`, 422, "missing"},
		{"顶层非对象", `[1]`, 422, "model_attributes_type"},
		{"列表类型", `{"appids": "abc"}`, 422, "list_type"},
	}
	for _, tc := range cases {
		rec := do("POST", "/api/v1/crawl/import", tc.body)
		if rec.Code != tc.want {
			t.Fatalf("%s 应 %d：%d %s", tc.name, tc.want, rec.Code, rec.Body.String())
		}
		detail, _ := decodeJSON(t, rec)["detail"].([]any)
		first, _ := detail[0].(map[string]any)
		if first["type"] != tc.kind {
			t.Fatalf("%s 错误类型应 %s：%v", tc.name, tc.kind, first)
		}
	}
}

func stringOf101() string {
	parts := make([]string, 101)
	for i := range parts {
		parts[i] = "1"
	}
	return joinStrings(parts, ",")
}

func joinStrings(parts []string, sep string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += sep
		}
		out += p
	}
	return out
}

func TestImportHTTPClassification(t *testing.T) {
	r, db := readFixture(t)
	r.ImportAppids = func(ctx context.Context, appids []any) (map[string]any, error) {
		return games.ImportAppids(ctx, db, appids)
	}
	readExec(t, db, `INSERT INTO games(appid,name,type,is_epic,is_hb,hl_flag,pp_flag,diff_fen,removed_strikes,view_count,
		family_sharing,trading_cards,is_adult,is_visual_novel,positive_reviews,review_count,updated_at)
		VALUES(220,'Fixture','game',0,0,0,0,0,0,0,0,0,0,0,0,0,'2026-11-06 10:00:00')`)
	_, do := serve(t, r)
	rec := do("POST", "/api/v1/crawl/import", `{"appids": [0, 220, -5]}`)
	if rec.Code != 200 {
		t.Fatalf("有效列表应 200：%d %s", rec.Code, rec.Body.String())
	}
	body := decodeJSON(t, rec)
	if body["fail"] != float64(2) || body["own"] != float64(1) || body["ok"] != float64(0) {
		t.Fatal(body)
	}
	results, _ := body["results"].([]any)
	first, _ := results[0].(map[string]any)
	if first["appid"] != float64(0) || first["status"] != "fail" {
		t.Fatal(first)
	}
	rec = do("POST", "/api/v1/crawl/import", `{"appids": [true, " 7 "]}`)
	if rec.Code != 200 {
		t.Fatalf("宽松强转应 200：%d %s", rec.Code, rec.Body.String())
	}
}
