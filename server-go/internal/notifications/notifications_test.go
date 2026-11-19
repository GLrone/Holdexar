package notifications

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"

	_ "modernc.org/sqlite"

	"holdexar/server-go/internal/secretbox"
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
CREATE TABLE notification_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_kind VARCHAR(16),
    event_id INTEGER,
    cycle_id INTEGER,
    appid INTEGER,
    region_code VARCHAR(8),
    event_type VARCHAR(32),
    category VARCHAR(16),
    recipient VARCHAR(200),
    status VARCHAR(12),
    reason TEXT,
    attempts INTEGER,
    last_error TEXT,
    created_at DATETIME,
    updated_at DATETIME,
    delivered_at DATETIME,
    delivery_history_json JSON,
    payload_json JSON,
    due_at DATETIME
);
CREATE TABLE fact_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source VARCHAR(24),
    kind VARCHAR(24),
    fact_key TEXT,
    data JSON,
    occurred_at DATETIME
);
CREATE TABLE games (appid INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE price_events (id INTEGER PRIMARY KEY, current_json JSON);
CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json JSON, updated_at DATETIME);`
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("建表失败：%v", err)
	}
	return db
}

func newServer(t *testing.T, db *sql.DB) http.Handler {
	t.Helper()
	mux := http.NewServeMux()
	Register(mux, db)
	return mux
}

func doGet(t *testing.T, h http.Handler, target string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, target, nil)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func decodeBody(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	dec := json.NewDecoder(rec.Body)
	dec.UseNumber()
	var out map[string]any
	if err := dec.Decode(&out); err != nil {
		t.Fatalf("响应体解析失败：%v", err)
	}
	return out
}

func obj(t *testing.T, m map[string]any, key string) map[string]any {
	t.Helper()
	v, ok := m[key].(map[string]any)
	if !ok {
		t.Fatalf("字段 %q 不是对象：%v", key, m[key])
	}
	return v
}

func arr(t *testing.T, m map[string]any, key string) []any {
	t.Helper()
	v, ok := m[key].([]any)
	if !ok {
		t.Fatalf("字段 %q 不是数组：%v", key, m[key])
	}
	return v
}

func wantStr(t *testing.T, m map[string]any, key, value string) {
	t.Helper()
	got, ok := m[key].(string)
	if !ok || got != value {
		t.Fatalf("字段 %q = %v，期望 %q", key, m[key], value)
	}
}

func wantNum(t *testing.T, m map[string]any, key, value string) {
	t.Helper()
	got, ok := m[key].(json.Number)
	if !ok || got.String() != value {
		t.Fatalf("字段 %q = %v，期望数值 %s", key, m[key], value)
	}
}

func wantBool(t *testing.T, m map[string]any, key string, value bool) {
	t.Helper()
	got, ok := m[key].(bool)
	if !ok || got != value {
		t.Fatalf("字段 %q = %v，期望 %v", key, m[key], value)
	}
}

func wantNil(t *testing.T, m map[string]any, key string) {
	t.Helper()
	if v, exists := m[key]; v != nil || !exists {
		t.Fatalf("字段 %q = %v，期望 null", key, v)
	}
}

func want422(t *testing.T, rec *httptest.ResponseRecorder, field, errType, msg string) {
	t.Helper()
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("状态码 = %d，期望 422", rec.Code)
	}
	body := decodeBody(t, rec)
	details, ok := body["detail"].([]any)
	if !ok || len(details) != 1 {
		t.Fatalf("detail 形态不符：%v", body["detail"])
	}
	item, _ := details[0].(map[string]any)
	if item["type"] != errType {
		t.Fatalf("校验类型 = %v，期望 %s", item["type"], errType)
	}
	wantStr(t, item, "msg", msg)
	loc, _ := item["loc"].([]any)
	if len(loc) != 2 || loc[0] != "query" || loc[1] != field {
		t.Fatalf("loc = %v，期望 [query %s]", loc, field)
	}
}

type candSeed struct {
	sourceKind  string
	eventID     int64
	cycleID     int64
	appid       int64
	hasAppid    bool
	noRegion    bool
	region      string
	eventType   string
	category    string
	recipient   string
	status      string
	reason      string
	attempts    int64
	hasAttempts bool
	lastError   string
	createdAt   string
	updatedAt   string
	deliveredAt string
	history     string
	payload     string
	dueAt       string
}

func nullIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func seedCandidates(t *testing.T, db *sql.DB, seeds []candSeed) []int64 {
	t.Helper()
	ids := make([]int64, 0, len(seeds))
	for _, s := range seeds {
		var appid, attempts any
		if s.hasAppid {
			appid = s.appid
		}
		if s.hasAttempts {
			attempts = s.attempts
		}
		region := any(s.region)
		if s.noRegion {
			region = nil
		}
		reason, lastError := nullIfEmpty(s.reason), nullIfEmpty(s.lastError)
		createdAt, updatedAt := nullIfEmpty(s.createdAt), nullIfEmpty(s.updatedAt)
		deliveredAt, dueAt := nullIfEmpty(s.deliveredAt), nullIfEmpty(s.dueAt)
		res, err := db.Exec(
			"INSERT INTO notification_candidates (source_kind, event_id, cycle_id, appid,"+
				" region_code, event_type, category, recipient, status, reason, attempts,"+
				" last_error, created_at, updated_at, delivered_at, delivery_history_json,"+
				" payload_json, due_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
			s.sourceKind, s.eventID, s.cycleID, appid, region, s.eventType, s.category,
			s.recipient, s.status, reason, attempts, lastError, createdAt,
			updatedAt, deliveredAt, s.history, s.payload, dueAt)
		if err != nil {
			t.Fatalf("候选种子写入失败：%v", err)
		}
		id, _ := res.LastInsertId()
		ids = append(ids, id)
	}
	return ids
}

func TestListCandidatesShapeAndStates(t *testing.T) {
	db := newTestDB(t)
	if _, err := db.Exec(
		"INSERT INTO games (appid, name) VALUES (10, 'GameA'), (30, 'BundleGame')"); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(
		"INSERT INTO price_events (id, current_json) VALUES (100, '{\"price\": 5000, \"currency\": \"CNY\", \"originalPrice\": 10000, \"discount\": 50, \"observedAt\": \"2026-01-01T10:00:00\", \"at\": \"2026-01-01T09:59:00\"}')"); err != nil {
		t.Fatal(err)
	}
	seedCandidates(t, db, []candSeed{
		{sourceKind: "price_event", eventID: 100, cycleID: 7, appid: 10, hasAppid: true,
			region: "CN", eventType: "PRICE_DROP", category: "price", recipient: "a@b.c",
			status: "delivered", attempts: 1, hasAttempts: true,
			createdAt: "2026-01-01 09:00:00", updatedAt: "2026-01-02 03:04:05",
			deliveredAt: "2026-01-02 03:04:05.123456", dueAt: "2026-01-01 09:00:00",
			history: `[{"recipient": "old@b.c", "status": "pending", "attempts": 0}]`,
			payload: `{"note": "price branch ignores payload detail"}`},
		{sourceKind: "price_event", eventID: 999, cycleID: 7, appid: 10, hasAppid: true,
			region: "CN", eventType: "PRICE_INCREASE", category: "price", recipient: "a@b.c",
			status: "failed", reason: "smtp: 550 rejected", attempts: 3, hasAttempts: true,
			lastError: "SMTPRejected: 550 mailbox unavailable",
			createdAt: "2026-01-01 09:00:00", updatedAt: "2026-01-02 03:04:06"},
		{sourceKind: "alert_event", eventID: 55, cycleID: 0, appid: 30, hasAppid: true,
			region: "US", eventType: "RULE_PRICE", category: "rules", recipient: "a@b.c",
			status: "pending", attempts: 0, hasAttempts: true,
			createdAt: "2026-01-01 09:00:00", updatedAt: "2026-01-02 03:04:07",
			payload: `{"gameName": "", "title": "T3", "price": 100, "currency": "USD", "labels": [], "summary": null}`},
		{sourceKind: "fact_notice", eventID: 9, cycleID: 0, appid: 20, hasAppid: true,
			region: "", eventType: "EPIC_FREE", category: "content", recipient: "a@b.c",
			status: "unknown", reason: "投递中断，外部是否已接收无法确认", attempts: 1, hasAttempts: true,
			lastError: "sending 超时，投递结果未知",
			createdAt: "2026-01-01 09:00:00", updatedAt: "2026-01-02 03:04:08",
			payload: `{"title": "T4", "labels": ["限时免费"]}`},
		{sourceKind: "price_event", eventID: 101, cycleID: 7, appid: 0, hasAppid: true,
			region: "", eventType: "WEIRD_TYPE", category: "price", recipient: "a@b.c",
			status: "suppressed", reason: "quiet", attempts: 0, hasAttempts: true,
			createdAt: "2026-01-01 09:00:00", updatedAt: "2026-01-02 03:04:09",
			payload: `{}`},
		{sourceKind: "price_event", eventID: 102, cycleID: 7, appid: 0, noRegion: true,
			region: "", eventType: "FREE_PROMO", category: "lifecycle", recipient: "a@b.c",
			status: "sending", attempts: 1, hasAttempts: true,
			createdAt: "2026-01-01 09:00:00", updatedAt: "2026-01-02 03:04:10"},
	})

	h := newServer(t, db)
	rec := doGet(t, h, "/api/v1/notifications/candidates")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d，体 = %s", rec.Code, rec.Body.String())
	}
	rawBody := rec.Body.String()
	body := decodeBody(t, rec)
	items := arr(t, body, "items")
	if len(items) != 6 {
		t.Fatalf("items 数 = %d，期望 6", len(items))
	}

	statuses := map[string]bool{}
	for _, it := range items {
		statuses[it.(map[string]any)["status"].(string)] = true
	}
	for _, s := range []string{"pending", "suppressed", "sending", "unknown", "delivered", "failed"} {
		if !statuses[s] {
			t.Fatalf("状态 %s 未出现在账本", s)
		}
	}

	item1 := items[0].(map[string]any)
	wantNum(t, item1, "id", "6")

	c1 := items[5].(map[string]any)
	wantNum(t, c1, "id", "1")
	wantNum(t, c1, "eventId", "100")
	wantNum(t, c1, "cycleId", "7")
	wantNum(t, c1, "appid", "10")
	wantStr(t, c1, "region", "CN")
	wantStr(t, c1, "sourceKind", "price_event")
	wantStr(t, c1, "eventType", "PRICE_DROP")
	wantStr(t, c1, "eventLabel", "价格下降")
	wantStr(t, c1, "category", "price")
	wantStr(t, c1, "recipient", "a@b.c")
	wantStr(t, c1, "status", "delivered")
	wantNil(t, c1, "reason")
	wantNum(t, c1, "attempts", "1")
	wantStr(t, c1, "deliveredAt", "2026-01-02T03:04:05.123456")
	wantStr(t, c1, "createdAt", "2026-01-01T09:00:00")
	wantStr(t, c1, "dueAt", "2026-01-01T09:00:00")
	d1 := obj(t, c1, "detail")
	wantStr(t, d1, "title", "GameA")
	wantNum(t, d1, "price", "5000")
	wantStr(t, d1, "currency", "CNY")
	wantNum(t, d1, "originalPrice", "10000")
	wantNum(t, d1, "discount", "50")
	wantStr(t, d1, "observedAt", "2026-01-01T10:00:00")
	if labels := arr(t, d1, "labels"); len(labels) != 0 {
		t.Fatalf("price_event labels 应为空数组：%v", labels)
	}
	wantNil(t, d1, "summary")

	c2 := items[4].(map[string]any)
	wantStr(t, c2, "eventLabel", "价格上涨")
	d2 := obj(t, c2, "detail")
	wantStr(t, d2, "title", "GameA")
	wantNil(t, d2, "price")
	wantNil(t, d2, "currency")
	wantNil(t, d2, "observedAt")
	wantNum(t, c2, "attempts", "3")

	c3 := items[3].(map[string]any)
	d3 := obj(t, c3, "detail")
	wantStr(t, d3, "title", "T3")
	wantNum(t, d3, "price", "100")
	wantStr(t, d3, "currency", "USD")
	wantStr(t, c3, "eventLabel", "价格变化")

	c4 := items[2].(map[string]any)
	wantStr(t, c4, "region", "")
	d4 := obj(t, c4, "detail")
	wantStr(t, d4, "title", "T4")
	if labels := arr(t, d4, "labels"); len(labels) != 1 || labels[0] != "限时免费" {
		t.Fatalf("labels = %v", labels)
	}
	if hist := arr(t, c4, "deliveryHistory"); len(hist) != 0 {
		t.Fatalf("deliveryHistory 应为空数组：%v", hist)
	}
	wantNil(t, c4, "dueAt")

	c5 := items[1].(map[string]any)
	wantStr(t, c5, "eventLabel", "价格变化")
	d5 := obj(t, c5, "detail")
	wantNil(t, d5, "title")

	c6 := items[0].(map[string]any)
	wantNil(t, c6, "appid")
	wantNil(t, c6, "region")
	wantStr(t, c6, "status", "sending")

	if !strings.Contains(rawBody, `{"recipient":"old@b.c","status":"pending","attempts":0}`) {
		t.Fatalf("deliveryHistory 未紧凑化：%s", rawBody)
	}
	if strings.Contains(rawBody, `"note": "price`) {
		t.Fatalf("payload 未紧凑化")
	}
}

func TestListCandidatesPagination(t *testing.T) {
	db := newTestDB(t)
	seeds := make([]candSeed, 0, 60)
	for i := 0; i < 60; i++ {
		seeds = append(seeds, candSeed{sourceKind: "price_event", eventID: int64(i + 1),
			cycleID: 1, appid: 0, eventType: "PRICE_DROP", category: "price",
			recipient: "a@b.c", status: "pending", attempts: 0, hasAttempts: true})
	}
	seedCandidates(t, db, seeds)
	h := newServer(t, db)

	rec := doGet(t, h, "/api/v1/notifications/candidates")
	items := arr(t, decodeBody(t, rec), "items")
	if len(items) != 50 {
		t.Fatalf("缺省 limit 应为 50，实得 %d", len(items))
	}
	first := items[0].(map[string]any)
	wantNum(t, first, "eventId", "60")

	rec = doGet(t, h, "/api/v1/notifications/candidates?limit=2")
	if got := len(arr(t, decodeBody(t, rec), "items")); got != 2 {
		t.Fatalf("limit=2 截断失败：%d", got)
	}
	rec = doGet(t, h, "/api/v1/notifications/candidates?limit=500")
	if got := len(arr(t, decodeBody(t, rec), "items")); got != 60 {
		t.Fatalf("limit=500 应返回全部：%d", got)
	}
	want422(t, doGet(t, h, "/api/v1/notifications/candidates?limit=0"),
		"limit", "greater_than_equal", "Input should be greater than or equal to 1")
	want422(t, doGet(t, h, "/api/v1/notifications/candidates?limit=501"),
		"limit", "less_than_equal", "Input should be less than or equal to 500")
	want422(t, doGet(t, h, "/api/v1/notifications/candidates?limit=abc"),
		"limit", "int_parsing", "Input should be a valid integer, unable to parse string as an integer")

	req := httptest.NewRequest(http.MethodPost, "/api/v1/notifications/candidates", nil)
	recorder := httptest.NewRecorder()
	newServer(t, db).ServeHTTP(recorder, req)
	if recorder.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST candidates 状态码 = %d，期望 405", recorder.Code)
	}
}

func TestGetStats(t *testing.T) {
	db := newTestDB(t)
	seedCandidates(t, db, []candSeed{
		{sourceKind: "price_event", eventID: 1, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "delivered",
			attempts: 2, hasAttempts: true},
		{sourceKind: "price_event", eventID: 2, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "pending", attempts: 0, hasAttempts: true},
		{sourceKind: "price_event", eventID: 3, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "suppressed"},
		{sourceKind: "price_event", eventID: 4, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "sending"},
		{sourceKind: "price_event", eventID: 5, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "unknown"},
		{sourceKind: "price_event", eventID: 6, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "failed",
			attempts: 2, hasAttempts: true, lastError: "SMTPRejected: 450 try later"},
		{sourceKind: "price_event", eventID: 7, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "failed",
			attempts: 1, hasAttempts: true, lastError: "SMTPAuthenticationError: 535 bad credentials"},
		{sourceKind: "price_event", eventID: 8, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "failed",
			attempts: 1, hasAttempts: true, lastError: "SMTPDeliveryUnknown: TimeoutError: read"},
		{sourceKind: "price_event", eventID: 9, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "failed",
			attempts: 3, hasAttempts: true, lastError: "SMTPRejected: 450 try later"},
	})

	rec := doGet(t, newServer(t, db), "/api/v1/notifications/stats")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d，体 = %s", rec.Code, rec.Body.String())
	}
	body := decodeBody(t, rec)
	wantNum(t, body, "total", "9")
	wantNum(t, body, "delivered", "1")
	wantNum(t, body, "pending", "1")
	wantNum(t, body, "suppressed", "1")
	wantNum(t, body, "sending", "1")
	wantNum(t, body, "unknown", "1")
	wantNum(t, body, "failed", "4")
	wantNum(t, body, "retryable", "1")
	wantNum(t, body, "permanent", "3")
	wantNum(t, body, "attempts", "9")
	wantNum(t, body, "maxAttempts", "3")
	byStatus := obj(t, body, "byStatus")
	for status, count := range map[string]string{
		"delivered": "1", "pending": "1", "suppressed": "1", "sending": "1",
		"unknown": "1", "failed": "4",
	} {
		wantNum(t, obj(t, byStatus, status), "count", count)
	}
	wantNum(t, obj(t, byStatus, "failed"), "attempts", "7")
}

func TestGetStatsEmpty(t *testing.T) {
	db := newTestDB(t)
	rec := doGet(t, newServer(t, db), "/api/v1/notifications/stats")
	body := decodeBody(t, rec)
	wantNum(t, body, "total", "0")
	wantNum(t, body, "retryable", "0")
	wantNum(t, body, "permanent", "0")
	byStatus, ok := body["byStatus"].(map[string]any)
	if !ok || len(byStatus) != 0 {
		t.Fatalf("空库 byStatus 应为空对象：%v", body["byStatus"])
	}
}

func seedSetting(t *testing.T, db *sql.DB, key, value string) {
	t.Helper()
	var v any
	if value != "" {
		v = value
	}
	if _, err := db.Exec(
		"INSERT INTO app_settings (key, value_json) VALUES (?, ?)", key, v); err != nil {
		t.Fatal(err)
	}
}

func TestGetPrefsDefaults(t *testing.T) {
	db := newTestDB(t)
	rec := doGet(t, newServer(t, db), "/api/v1/notifications/prefs")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d，体 = %s", rec.Code, rec.Body.String())
	}
	body := decodeBody(t, rec)
	wantBool(t, body, "enabled", false)
	wantBool(t, body, "quietEnabled", false)
	wantStr(t, body, "quietStart", "23:00")
	wantStr(t, body, "quietEnd", "08:00")
	wantBool(t, body, "includeDetails", true)
	wantNum(t, body, "maxAttempts", "3")
	wantNil(t, body, "manualRegion")
	wantStr(t, body, "digestTime", "08:00")
	wantNum(t, body, "digestWeekday", "0")
	wantNil(t, body, "lastDelivery")

	region := obj(t, body, "region")
	wantNil(t, region, "region")
	wantStr(t, region, "source", "unknown")
	wantNil(t, region, "currency")
	wantStr(t, region, "currencySource", "unknown")

	smtp := obj(t, body, "smtp")
	wantBool(t, smtp, "configured", false)
	wantStr(t, smtp, "host", "")
	wantNum(t, smtp, "port", "465")
	wantStr(t, smtp, "userMasked", "")
	wantBool(t, smtp, "hasPassword", false)
	wantBool(t, smtp, "useSsl", true)

	types := arr(t, body, "types")
	if len(types) != 20 {
		t.Fatalf("types 数 = %d，期望 20", len(types))
	}
	first := types[0].(map[string]any)
	wantStr(t, first, "key", "PRICE_DROP")
	wantStr(t, first, "label", "价格下降")
	wantStr(t, first, "category", "price")
	wantBool(t, first, "enabled", true)
	wantStr(t, first, "frequency", "immediate")

	categories := arr(t, body, "categories")
	if len(categories) != 8 {
		t.Fatalf("categories 数 = %d，期望 8", len(categories))
	}
	wantStr(t, categories[0].(map[string]any), "key", "price")
	wantNum(t, categories[0].(map[string]any), "eventTypes", "3")
	wantStr(t, categories[0].(map[string]any), "label", "价格变化")

	featured := obj(t, body, "featured")
	wantNum(t, featured, "minDiscount", "50")
	if scope := arr(t, featured, "scope"); len(scope) != 2 || scope[0] != "followed" {
		t.Fatalf("featured.scope 默认不符：%v", scope)
	}
	wantStr(t, featured, "lowMode", "any")
	wantNil(t, featured, "minSavings")
	wantStr(t, featured, "missingReviews", "include")
}

func TestGetPrefsPatched(t *testing.T) {
	db := newTestDB(t)
	seedSetting(t, db, prefsKey,
		`{"enabled": true, "manualRegion": "cn", "digestTime": "09:30", "digestWeekday": 5,
		  "types": {"PRICE_DROP": {"enabled": false, "frequency": "daily"}},
		  "categories": {"price": false},
		  "featured": {"minDiscount": 60, "minSavings": 200}}`)
	rec := doGet(t, newServer(t, db), "/api/v1/notifications/prefs")
	body := decodeBody(t, rec)
	wantBool(t, body, "enabled", true)
	wantStr(t, body, "manualRegion", "cn")
	wantStr(t, body, "digestTime", "09:30")
	wantNum(t, body, "digestWeekday", "5")

	region := obj(t, body, "region")
	wantStr(t, region, "region", "CN")
	wantStr(t, region, "source", "manual")
	wantStr(t, region, "currency", "CNY")
	wantStr(t, region, "currencySource", "region_mapping")

	drop := arr(t, body, "types")[0].(map[string]any)
	wantBool(t, drop, "enabled", false)
	wantStr(t, drop, "frequency", "daily")
	rise := arr(t, body, "types")[1].(map[string]any)
	wantBool(t, rise, "enabled", true)
	wantStr(t, rise, "frequency", "immediate")

	price := arr(t, body, "categories")[0].(map[string]any)
	wantBool(t, price, "enabled", false)
	low := arr(t, body, "categories")[1].(map[string]any)
	wantBool(t, low, "enabled", true)

	featured := obj(t, body, "featured")
	wantNum(t, featured, "minDiscount", "60")
	wantNum(t, featured, "minSavings", "200")
	wantStr(t, featured, "lowMode", "any")
}

func TestGetPrefsSmtpAndLastDelivery(t *testing.T) {
	dir := t.TempDir()
	overrideDataDir(t, dir)
	token, err := secretbox.EncryptSecretIn(dir, "auth-code-123",
		secretbox.SecretPurpose(smtpPasswordKey))
	if err != nil {
		t.Fatalf("密文准备失败：%v", err)
	}
	db := newTestDB(t)
	seedSetting(t, db, "smtp.host", `"smtp.example.com"`)
	seedSetting(t, db, "smtp.port", `465`)
	seedSetting(t, db, "smtp.user", `"zhangsan@example.com"`)
	seedSetting(t, db, smtpPasswordKey, strconv.Quote(token))
	seedSetting(t, db, "smtp.to_addr", `"me@example.com"`)
	seedSetting(t, db, "smtp.use_ssl", `false`)
	seedCandidates(t, db, []candSeed{
		{sourceKind: "price_event", eventID: 1, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "delivered",
			updatedAt: "2026-01-01 00:00:00", deliveredAt: "2026-01-01 00:00:00"},
		{sourceKind: "price_event", eventID: 2, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "failed",
			reason: "smtp: timeout", updatedAt: "2026-01-05 06:07:08",
			lastError: "TimeoutError: timed out"},
		{sourceKind: "price_event", eventID: 3, cycleID: 1, appid: 0, eventType: "PRICE_DROP",
			category: "price", recipient: "a@b.c", status: "pending",
			updatedAt: "2026-01-06 00:00:00"},
	})

	rec := doGet(t, newServer(t, db), "/api/v1/notifications/prefs")
	body := decodeBody(t, rec)
	smtp := obj(t, body, "smtp")
	wantBool(t, smtp, "configured", true)
	wantStr(t, smtp, "host", "smtp.example.com")
	wantNum(t, smtp, "port", "465")
	wantStr(t, smtp, "userMasked", "z***n@example.com")
	wantBool(t, smtp, "hasPassword", true)
	wantBool(t, smtp, "useSsl", false)

	last := obj(t, body, "lastDelivery")
	wantStr(t, last, "status", "failed")
	wantStr(t, last, "at", "2026-01-05T06:07:08")
	wantNil(t, last, "attempts")
	wantStr(t, last, "reason", "smtp: timeout")
	wantBool(t, last, "retryable", true)
}

func overrideDataDir(t *testing.T, dir string) {
	t.Helper()
	orig := dataDirResolver
	dataDirResolver = func() (string, error) { return dir, nil }
	t.Cleanup(func() { dataDirResolver = orig })
}

func TestGetPrefsHasPasswordStates(t *testing.T) {
	purpose := secretbox.SecretPurpose(smtpPasswordKey)

	t.Run("decryptable", func(t *testing.T) {
		dir := t.TempDir()
		overrideDataDir(t, dir)
		token, err := secretbox.EncryptSecretIn(dir, "auth-code-123", purpose)
		if err != nil {
			t.Fatalf("密文准备失败：%v", err)
		}
		db := newTestDB(t)
		seedSetting(t, db, smtpPasswordKey, strconv.Quote(token))
		body := decodeBody(t, doGet(t, newServer(t, db), "/api/v1/notifications/prefs"))
		wantBool(t, obj(t, body, "smtp"), "hasPassword", true)
	})

	t.Run("undecryptable wrong purpose", func(t *testing.T) {
		dir := t.TempDir()
		overrideDataDir(t, dir)
		token, err := secretbox.EncryptSecretIn(dir, "auth-code-123", "kv:smtp.to_addr")
		if err != nil {
			t.Fatalf("密文准备失败：%v", err)
		}
		db := newTestDB(t)
		seedSetting(t, db, smtpPasswordKey, strconv.Quote(token))
		body := decodeBody(t, doGet(t, newServer(t, db), "/api/v1/notifications/prefs"))
		wantBool(t, obj(t, body, "smtp"), "hasPassword", false)
	})

	t.Run("passphrase locked", func(t *testing.T) {
		dir := t.TempDir()
		overrideDataDir(t, dir)
		token, err := secretbox.EncryptSecretIn(dir, "auth-code-123", purpose)
		if err != nil {
			t.Fatalf("密文准备失败：%v", err)
		}
		master, err := secretbox.LegacyMaster(dir)
		if err != nil {
			t.Fatalf("legacy 主密钥准备失败：%v", err)
		}
		rec, err := secretbox.WrapPassphrase(master, "test-passphrase")
		if err != nil {
			t.Fatalf("口令封装失败：%v", err)
		}
		if err := secretbox.WriteRecord(dir, rec); err != nil {
			t.Fatalf("密钥记录写入失败：%v", err)
		}
		secretbox.Lock()
		db := newTestDB(t)
		seedSetting(t, db, smtpPasswordKey, strconv.Quote(token))
		body := decodeBody(t, doGet(t, newServer(t, db), "/api/v1/notifications/prefs"))
		wantBool(t, obj(t, body, "smtp"), "hasPassword", false)
	})

	t.Run("plaintext passthrough", func(t *testing.T) {
		dir := t.TempDir()
		overrideDataDir(t, dir)
		db := newTestDB(t)
		seedSetting(t, db, smtpPasswordKey, `"legacy-plain"`)
		body := decodeBody(t, doGet(t, newServer(t, db), "/api/v1/notifications/prefs"))
		wantBool(t, obj(t, body, "smtp"), "hasPassword", true)
	})
}

func TestListFacts(t *testing.T) {
	db := newTestDB(t)
	seeds := []struct {
		source, kind, key, data, occurredAt string
	}{
		{"epic_free", "free_rotation", "epic:1", `{"appid": 1, "title": "F1"}`, "2026-01-02 03:04:05"},
		{"hb_choice", "bundle_changed", "hb:1", "", "2026-01-02 03:04:05.5"},
		{"epic_free", "free_rotation", "epic:2", `null`, "2026-01-03 00:00:00"},
	}
	for _, s := range seeds {
		var data any
		if s.data != "" {
			data = s.data
		}
		if _, err := db.Exec(
			"INSERT INTO fact_notices (source, kind, fact_key, data, occurred_at) VALUES (?, ?, ?, ?, ?)",
			s.source, s.kind, s.key, data, s.occurredAt); err != nil {
			t.Fatal(err)
		}
	}
	h := newServer(t, db)

	rec := doGet(t, h, "/api/v1/notifications/facts")
	body := decodeBody(t, rec)
	wantNum(t, body, "latestId", "3")
	if items := arr(t, body, "items"); len(items) != 0 {
		t.Fatalf("对齐轮应回空增量：%v", items)
	}

	rec = doGet(t, h, "/api/v1/notifications/facts?afterId=1&limit=2")
	body = decodeBody(t, rec)
	items := arr(t, body, "items")
	if len(items) != 2 {
		t.Fatalf("增量数 = %d，期望 2", len(items))
	}
	i1 := items[0].(map[string]any)
	wantNum(t, i1, "id", "2")
	wantStr(t, i1, "source", "hb_choice")
	wantStr(t, i1, "kind", "bundle_changed")
	wantNil(t, i1, "data")
	wantStr(t, i1, "occurredAt", "2026-01-02T03:04:05.500000")
	i2 := items[1].(map[string]any)
	wantNum(t, i2, "id", "3")
	wantNil(t, i2, "data")
	wantStr(t, i2, "occurredAt", "2026-01-03T00:00:00")

	rec = doGet(t, h, "/api/v1/notifications/facts?afterId=0")
	if got := len(arr(t, decodeBody(t, rec), "items")); got != 3 {
		t.Fatalf("afterId=0 应回全量：%d", got)
	}
	rec = doGet(t, h, "/api/v1/notifications/facts?afterId=3")
	if got := len(arr(t, decodeBody(t, rec), "items")); got != 0 {
		t.Fatalf("游标在末尾应回空：%d", got)
	}
	if _, err := db.Exec(
		"INSERT INTO fact_notices (source, kind, fact_key, data, occurred_at) VALUES ('epic_free', 'free_rotation', 'epic:3', '{\"appid\": 7, \"items\": [\"a\", \"b\"]}', '2026-01-04 00:00:00')"); err != nil {
		t.Fatal(err)
	}
	rec = doGet(t, h, "/api/v1/notifications/facts?afterId=3")
	items = arr(t, decodeBody(t, rec), "items")
	d := obj(t, items[0].(map[string]any), "data")
	wantNum(t, d, "appid", "7")

	want422(t, doGet(t, h, "/api/v1/notifications/facts?afterId=-1"),
		"afterId", "greater_than_equal", "Input should be greater than or equal to 0")
	want422(t, doGet(t, h, "/api/v1/notifications/facts?afterId=abc"),
		"afterId", "int_parsing", "Input should be a valid integer, unable to parse string as an integer")
	want422(t, doGet(t, h, "/api/v1/notifications/facts?limit=0"),
		"limit", "greater_than_equal", "Input should be greater than or equal to 1")
	want422(t, doGet(t, h, "/api/v1/notifications/facts?limit=101"),
		"limit", "less_than_equal", "Input should be less than or equal to 100")
}

func TestIsRetryable(t *testing.T) {
	cases := []struct {
		err  string
		want bool
	}{
		{"", false},
		{"SMTPDeliveryUnknown: TimeoutError: read", false},
		{"SMTPRejected: 450 busy", true},
		{"SMTPRejected: 550 no such user", false},
		{"SMTPRejected: 4x0 weird", false},
		{"SMTPRejected: 4500 weird", true},
		{"SMTPAuthenticationError: 535 no", false},
		{"TimeoutError: timed out", true},
		{"连接超时", true},
		{"密码未配置", false},
		{"mystery error", false},
	}
	for _, c := range cases {
		if got := isRetryable(c.err); got != c.want {
			t.Fatalf("isRetryable(%q) = %v，期望 %v", c.err, got, c.want)
		}
	}
}
