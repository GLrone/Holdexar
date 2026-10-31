package rates

import (
	"database/sql"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	_ "modernc.org/sqlite"
)

func newTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatalf("打开测试库失败：%v", err)
	}
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { db.Close() })
	schema := `
CREATE TABLE fx_rates (
    currency_code VARCHAR(10) NOT NULL PRIMARY KEY,
    rate_to_cny FLOAT NOT NULL,
    fetched_at DATETIME
);
CREATE TABLE fx_rate_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currency_code VARCHAR(10) NOT NULL,
    rate_to_cny FLOAT NOT NULL,
    source VARCHAR(30),
    fetched_at DATETIME,
    rate_date DATE,
    source_kind VARCHAR(12),
    observed_at DATETIME
);
CREATE UNIQUE INDEX ux_frh_currency_date ON fx_rate_history (currency_code, rate_date);`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("建表失败：%v", err)
	}
	return db
}

// historyRow 历史行种子；空串字段写入 NULL。
type historyRow struct {
	Code      string
	Rate      float64
	RateDate  string
	FetchedAt string
	Kind      string
	Source    string
}

func seedHistory(t *testing.T, db *sql.DB, rows []historyRow) {
	t.Helper()
	for _, r := range rows {
		rate := r.Rate
		if rate == 0 {
			rate = 1.5
		}
		var rateDate, fetchedAt, kind, source any
		if r.RateDate != "" {
			rateDate = r.RateDate
		}
		if r.FetchedAt != "" {
			fetchedAt = r.FetchedAt
		}
		if r.Kind != "" {
			kind = r.Kind
		}
		if r.Source != "" {
			source = r.Source
		}
		if _, err := db.Exec(
			"INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at, rate_date, source_kind)"+
				" VALUES (?, ?, ?, ?, ?, ?)",
			r.Code, rate, source, fetchedAt, rateDate, kind); err != nil {
			t.Fatalf("种子写入失败：%v", err)
		}
	}
}

func do(t *testing.T, h http.Handler, method, target string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, target, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

// overrideToday 固定扫描/窗口过滤用的「今天」。
func overrideToday(t *testing.T, day string) {
	t.Helper()
	parsed, err := time.Parse("2006-01-02", day)
	if err != nil {
		t.Fatalf("测试日期非法：%v", err)
	}
	orig := beijingTodayFn
	beijingTodayFn = func() time.Time { return parsed }
	t.Cleanup(func() { beijingTodayFn = orig })
}

func newMux(t *testing.T) (*sql.DB, http.Handler) {
	t.Helper()
	db := newTestDB(t)
	mux := http.NewServeMux()
	Register(mux, db)
	return db, mux
}

func TestListRatesShape(t *testing.T) {
	db, mux := newMux(t)
	seeds := [][]any{
		{"USD", 0.14, "2026-10-31 12:00:00.123456"},
		{"CNY", 1.0, "2026-10-31 12:00:00.000000"},
		{"EUR", 0.85, nil},
	}
	for _, s := range seeds {
		if _, err := db.Exec(
			"INSERT INTO fx_rates (currency_code, rate_to_cny, fetched_at) VALUES (?, ?, ?)", s...); err != nil {
			t.Fatalf("种子写入失败：%v", err)
		}
	}
	rec := do(t, mux, http.MethodGet, "/api/v1/rates")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `{"rates":[` +
		`{"currency":"CNY","rateToCny":1.0,"fetchedAt":"2026-10-31T12:00:00"},` +
		`{"currency":"EUR","rateToCny":0.85,"fetchedAt":null},` +
		`{"currency":"USD","rateToCny":0.14,"fetchedAt":"2026-10-31T12:00:00.123456"}]}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("响应体不一致\n got: %s\nwant: %s", got, want)
	}
}

func TestListRatesEmpty(t *testing.T) {
	_, mux := newMux(t)
	rec := do(t, mux, http.MethodGet, "/api/v1/rates")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	if got := rec.Body.String(); got != `{"rates":[]}` {
		t.Fatalf("响应体 = %s", got)
	}
}

func seedHistoryDefaults(t *testing.T, db *sql.DB) {
	t.Helper()
	seedHistory(t, db, []historyRow{
		{Code: "USD", Rate: 0.14, RateDate: "2026-10-01", Kind: "observed",
			Source: "augmentedsteam", FetchedAt: "2026-10-01 05:00:00.000000"},
		{Code: "USD", Rate: 0.139, RateDate: "2026-10-02", Kind: "carried", Source: "backfill"},
		{Code: "USD", Rate: 0.138, RateDate: "2026-10-05", Kind: "derived",
			Source: "frankfurter", FetchedAt: "2026-10-05 06:00:00.000000"},
		{Code: "EUR", Rate: 0.85, RateDate: "2026-10-01", Kind: "observed", Source: "augmentedsteam"},
		{Code: "USD", Rate: 0.2, FetchedAt: "2026-10-03 08:00:00.000000"}, // rate_date NULL，不进日线
	})
}

func TestRateHistoryDefault(t *testing.T) {
	db, mux := newMux(t)
	seedHistoryDefaults(t, db)
	rec := do(t, mux, http.MethodGet, "/api/v1/rates/history")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `[` +
		`{"date":"2026-10-01","rateToCny":0.14,"source":"augmentedsteam","sourceKind":"observed","fetchedAt":"2026-10-01T05:00:00"},` +
		`{"date":"2026-10-02","rateToCny":0.139,"source":"backfill","sourceKind":"carried","fetchedAt":null},` +
		`{"date":"2026-10-05","rateToCny":0.138,"source":"frankfurter","sourceKind":"derived","fetchedAt":"2026-10-05T06:00:00"}]`
	if got := rec.Body.String(); got != want {
		t.Fatalf("响应体不一致\n got: %s\nwant: %s", got, want)
	}
}

func TestRateHistoryLimitAndCurrency(t *testing.T) {
	db, mux := newMux(t)
	seedHistoryDefaults(t, db)

	rec := do(t, mux, http.MethodGet, "/api/v1/rates/history?limit=2")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `[` +
		`{"date":"2026-10-02","rateToCny":0.139,"source":"backfill","sourceKind":"carried","fetchedAt":null},` +
		`{"date":"2026-10-05","rateToCny":0.138,"source":"frankfurter","sourceKind":"derived","fetchedAt":"2026-10-05T06:00:00"}]`
	if got := rec.Body.String(); got != want {
		t.Fatalf("limit=2 响应体\n got: %s\nwant: %s", got, want)
	}

	rec = do(t, mux, http.MethodGet, "/api/v1/rates/history?currency=eur")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	if got := rec.Body.String(); got !=
		`[{"date":"2026-10-01","rateToCny":0.85,"source":"augmentedsteam","sourceKind":"observed","fetchedAt":null}]` {
		t.Fatalf("currency=eur 响应体 = %s", got)
	}

	rec = do(t, mux, http.MethodGet, "/api/v1/rates/history?currency=JPY")
	if got := rec.Body.String(); got != `[]` {
		t.Fatalf("无行币种应为空数组：%s", got)
	}
}

func TestRateHistoryRange(t *testing.T) {
	db, mux := newMux(t)
	overrideToday(t, "2026-10-31")
	seedHistory(t, db, []historyRow{
		{Code: "USD", Rate: 0.14, RateDate: "2026-09-15", Kind: "observed", Source: "augmentedsteam"},
		{Code: "USD", Rate: 0.14, RateDate: "2026-10-01", Kind: "observed", Source: "augmentedsteam"},
		{Code: "USD", Rate: 0.14, RateDate: "2026-10-05", Kind: "observed", Source: "augmentedsteam"},
	})

	rec := do(t, mux, http.MethodGet, "/api/v1/rates/history?range=1mo")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want1mo := `[` +
		`{"date":"2026-10-01","rateToCny":0.14,"source":"augmentedsteam","sourceKind":"observed","fetchedAt":null},` +
		`{"date":"2026-10-05","rateToCny":0.14,"source":"augmentedsteam","sourceKind":"observed","fetchedAt":null}]`
	if got := rec.Body.String(); got != want1mo {
		t.Fatalf("range=1mo 响应体\n got: %s\nwant: %s", got, want1mo)
	}

	rec = do(t, mux, http.MethodGet, "/api/v1/rates/history?range=ALL")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	if got := rec.Body.String(); !strings.HasPrefix(got, `[{"date":"2026-09-15"`) {
		t.Fatalf("range=ALL（大小写不敏感）应保留全部：%s", got)
	}

	rec = do(t, mux, http.MethodGet, "/api/v1/rates/history?range=2w")
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("状态码 = %d, want 400", rec.Code)
	}
	if got := rec.Body.String(); got != `{"detail":"无效的时间范围: 2w"}` {
		t.Fatalf("无效范围响应体 = %s", got)
	}
}

func TestRateHistoryLimitInvalid(t *testing.T) {
	_, mux := newMux(t)
	rec := do(t, mux, http.MethodGet, "/api/v1/rates/history?limit=abc")
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("状态码 = %d, want 422", rec.Code)
	}
	want := `{"detail":[{"type":"int_parsing","loc":["query","limit"],` +
		`"msg":"Input should be a valid integer, unable to parse string as an integer","input":"abc"}]}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("422 响应体 = %s", got)
	}
}

func TestMethodFallbacksAndExcludedRoutes(t *testing.T) {
	_, mux := newMux(t)
	for _, path := range []string{"/api/v1/rates", "/api/v1/rates/history", "/api/v1/rates/history/gaps"} {
		rec := do(t, mux, http.MethodPost, path)
		if rec.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%s POST 状态码 = %d, want 405", path, rec.Code)
		}
		if allow := rec.Header().Get("Allow"); allow != "GET" {
			t.Fatalf("%s Allow = %q", path, allow)
		}
	}
	// 刷新/修复为写端点，未注册：路由不存在。
	if rec := do(t, mux, http.MethodPost, "/api/v1/rates/refresh"); rec.Code != http.StatusNotFound {
		t.Fatalf("refresh 未迁移应为 404, got %d", rec.Code)
	}
}

func TestFormatPyFloat(t *testing.T) {
	cases := map[float64]string{
		1.0:    "1.0",
		0.85:   "0.85",
		0.14:   "0.14",
		0:      "0.0",
		-1.5:   "-1.5",
		123.0:  "123.0",
		1e15:   "1000000000000000.0",
		1e16:   "1e+16",
		1e-4:   "0.0001",
		2.8e-5: "2.8e-05",
	}
	for in, want := range cases {
		if got := formatPyFloat(in); got != want {
			t.Fatalf("formatPyFloat(%v) = %s, want %s", in, got, want)
		}
	}
}
