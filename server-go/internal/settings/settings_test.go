package settings

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"holdexar/server-go/internal/store"
)

// newTestEnv 真实临时库 + 真实写闸 + 已装配路由。
func newTestEnv(t *testing.T) (*sql.DB, http.Handler) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatalf("打开临时库失败：%v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(`CREATE TABLE app_settings (
    key VARCHAR(64) NOT NULL PRIMARY KEY,
    value_json TEXT,
    updated_at DATETIME
)`); err != nil {
		t.Fatalf("建表失败：%v", err)
	}
	gate := store.NewWriteGate(db, path)
	mux := http.NewServeMux()
	Register(mux, db, gate)
	return db, mux
}

func do(t *testing.T, h http.Handler, method, target, body string) *httptest.ResponseRecorder {
	t.Helper()
	var reader io.Reader
	if body != "" {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, target, reader)
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func seedKV(t *testing.T, db *sql.DB, key, jsonText string) {
	t.Helper()
	if _, err := db.Exec(
		"INSERT INTO app_settings (key, value_json, updated_at) VALUES (?, ?, NULL)",
		key, jsonText); err != nil {
		t.Fatalf("种子写入失败：%v", err)
	}
}

const defaultBody = `{"account":{"steam_id":"","steam_api_key":"","has_api_key":false},` +
	`"onboarding_done":false,"auto_price":true,"backup_auto":true,"update_notified":"",` +
	`"update_skipped":"","update_notify":true,"update_auto":false,"direct_notice_seen":false,` +
	`"theme":"dark","mcp_enabled":false,"mcp_running":false,"mcp_error":"","mcp_forced_by_env":false}`

func TestGetSettingsDefaults(t *testing.T) {
	_, h := newTestEnv(t)
	rec := do(t, h, http.MethodGet, "/api/v1/settings", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	if got := rec.Body.String(); got != defaultBody {
		t.Fatalf("缺省形状不一致\n got: %s\nwant: %s", got, defaultBody)
	}
}

func TestGetSettingsSeeded(t *testing.T) {
	db, h := newTestEnv(t)
	seedKV(t, db, "account.steam_id", `"76561198000000000"`)
	seedKV(t, db, "account.steam_api_key", `"abcd1234efgh"`)
	seedKV(t, db, "ui.onboarding_done", `true`)
	seedKV(t, db, "crawl.auto_price", `"false"`) // 字符串 "false" 是 Python truthy
	seedKV(t, db, "ui.update_notified", `"1.2.3"`)
	seedKV(t, db, "ui.theme", `"light"`)
	seedKV(t, db, "mcp.enabled", `true`)
	rec := do(t, h, http.MethodGet, "/api/v1/settings", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `{"account":{"steam_id":"76561198000000000","steam_api_key":"****efgh","has_api_key":true},` +
		`"onboarding_done":true,"auto_price":true,"backup_auto":true,"update_notified":"1.2.3",` +
		`"update_skipped":"","update_notify":true,"update_auto":false,"direct_notice_seen":false,` +
		`"theme":"light","mcp_enabled":true,"mcp_running":false,"mcp_error":"","mcp_forced_by_env":false}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("种子形状不一致\n got: %s\nwant: %s", got, want)
	}
}

func TestGetSettingsEncryptedKeyDegrades(t *testing.T) {
	db, h := newTestEnv(t)
	seedKV(t, db, "account.steam_api_key", `"enc1:QUJDREVGR0g"`)
	rec := do(t, h, http.MethodGet, "/api/v1/settings", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	var resp settingsResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败：%v", err)
	}
	// 凭据在场事实保留；掩码因解密能力未迁移退化为全星。
	if !resp.Account.HasAPIKey || resp.Account.SteamAPIKey != "****" {
		t.Fatalf("密文凭据降级形状不符：%+v", resp.Account)
	}
}

func TestPutSettingsRoundTrip(t *testing.T) {
	db, h := newTestEnv(t)
	body := `{"account":{"steam_id":" 76561198 ","steam_api_key":"sk-real-9988"},` +
		`"onboarding_done":true,"auto_price":false,"backup_auto":false,` +
		`"update_notified":" 1.2.3 ","update_skipped":" 9.9.9 ",` +
		`"update_notify":false,"update_auto":true,"direct_notice_seen":true,"theme":"light"}`
	rec := do(t, h, http.MethodPut, "/api/v1/settings", body)
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, body %s", rec.Code, rec.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败：%v", err)
	}
	acct := resp["account"].(map[string]any)
	if acct["steam_id"] != "76561198" {
		t.Fatalf("steam_id = %v, want 76561198（去空白）", acct["steam_id"])
	}
	// 凭据写未迁移：不落库，掩码维持空。
	if acct["steam_api_key"] != "" || acct["has_api_key"] != false {
		t.Fatalf("凭据字段应维持未配置：%+v", acct)
	}
	checks := []struct {
		key  string
		want any
	}{
		{"onboarding_done", true}, {"auto_price", false}, {"backup_auto", false},
		{"update_notified", "1.2.3"}, {"update_skipped", "9.9.9"},
		{"update_notify", false}, {"update_auto", true},
		{"direct_notice_seen", true}, {"theme", "light"},
	}
	for _, c := range checks {
		if got := resp[c.key]; got != c.want {
			t.Fatalf("%s = %v, want %v", c.key, got, c.want)
		}
	}

	// 落库文本与 updated_at 快照核对（CAST 绕开驱动对 DATETIME 声明列的
	// 扫描侧 time.Time 转换，直读存储字节）。
	var raw, updated string
	if err := db.QueryRow(
		"SELECT value_json, CAST(updated_at AS TEXT) FROM app_settings WHERE key = ?", keyTheme).
		Scan(&raw, &updated); err != nil {
		t.Fatalf("读回失败：%v", err)
	}
	if raw != `"light"` {
		t.Fatalf("value_json = %s, want \"light\"", raw)
	}
	if len(updated) != 26 || updated[10] != ' ' || updated[19] != '.' {
		t.Fatalf("updated_at = %q, want 北京裸时刻 YYYY-MM-DD HH:MM:SS.ffffff", updated)
	}

	// 二次 GET 与写后读完全一致。
	rec2 := do(t, h, http.MethodGet, "/api/v1/settings", "")
	if rec2.Body.String() != rec.Body.String() {
		t.Fatalf("写后读与二次 GET 不一致\n after: %s\n again: %s", rec.Body.String(), rec2.Body.String())
	}
}

func TestPutSkipsGateWhenNothingToWrite(t *testing.T) {
	db, h := newTestEnv(t)
	for _, body := range []string{`{}`, `{"theme":null,"onboarding_done":null}`} {
		rec := do(t, h, http.MethodPut, "/api/v1/settings", body)
		if rec.Code != http.StatusOK {
			t.Fatalf("body %s: 状态码 = %d", body, rec.Code)
		}
	}
	var n int
	if err := db.QueryRow("SELECT COUNT(*) FROM app_settings").Scan(&n); err != nil {
		t.Fatalf("计数失败：%v", err)
	}
	if n != 0 {
		t.Fatalf("空写入不应落行，实际 %d 行", n)
	}
	if got := do(t, h, http.MethodGet, "/api/v1/settings", "").Body.String(); got != defaultBody {
		t.Fatalf("空写入后形状应仍为缺省：%s", got)
	}
}

func TestPutValidationShapes(t *testing.T) {
	_, h := newTestEnv(t)
	cases := []struct {
		name string
		body string
		want string
	}{
		{"非法JSON", `{bad`,
			`{"detail":[{"type":"json_invalid","loc":["body"],"msg":"JSON decode error","input":{}}]}`},
		{"顶层非对象", `[1]`,
			`{"detail":[{"type":"model_attributes_type","loc":["body"],"msg":"Input should be a valid dictionary or object to extract fields from","input":[1]}]}`},
		{"theme数字", `{"theme":123}`,
			`{"detail":[{"type":"string_type","loc":["body","theme"],"msg":"Input should be a valid string","input":123}]}`},
		{"布尔字符串越界", `{"auto_price":"maybe"}`,
			`{"detail":[{"type":"bool_parsing","loc":["body","auto_price"],"msg":"Input should be a valid boolean, unable to interpret input","input":"maybe"}]}`},
		{"布尔整数越界", `{"auto_price":2}`,
			`{"detail":[{"type":"bool_parsing","loc":["body","auto_price"],"msg":"Input should be a valid boolean, unable to interpret input","input":2}]}`},
		{"布尔浮点越界", `{"auto_price":1.5}`,
			`{"detail":[{"type":"bool_type","loc":["body","auto_price"],"msg":"Input should be a valid boolean","input":1.5}]}`},
		{"多错按字段序合并", `{"theme":123,"onboarding_done":"maybe"}`,
			`{"detail":[{"type":"bool_parsing","loc":["body","onboarding_done"],"msg":"Input should be a valid boolean, unable to interpret input","input":"maybe"},` +
				`{"type":"string_type","loc":["body","theme"],"msg":"Input should be a valid string","input":123}]}`},
		{"account非对象", `{"account":"x"}`,
			`{"detail":[{"type":"model_attributes_type","loc":["body","account"],"msg":"Input should be a valid dictionary or object to extract fields from","input":"x"}]}`},
		{"account内层类型", `{"account":{"steam_id":123}}`,
			`{"detail":[{"type":"string_type","loc":["body","account","steam_id"],"msg":"Input should be a valid string","input":123}]}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := do(t, h, http.MethodPut, "/api/v1/settings", tc.body)
			if rec.Code != http.StatusUnprocessableEntity {
				t.Fatalf("状态码 = %d, want 422", rec.Code)
			}
			if got := rec.Body.String(); got != tc.want {
				t.Fatalf("校验形状不一致\n got: %s\nwant: %s", got, tc.want)
			}
		})
	}

	rec := do(t, h, http.MethodPut, "/api/v1/settings", "")
	if rec.Code != http.StatusUnprocessableEntity {
		t.Fatalf("空 body 状态码 = %d, want 422", rec.Code)
	}
	want := `{"detail":[{"type":"missing","loc":["body"],"msg":"Field required","input":null}]}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("空 body 形状不一致\n got: %s\nwant: %s", got, want)
	}
}

func TestPutLaxBoolAndNullSemantics(t *testing.T) {
	db, h := newTestEnv(t)
	cases := []struct {
		body string
		want string // value_json 读回值
	}{
		{`{"onboarding_done":"TRUE"}`, `true`},
		{`{"onboarding_done":0}`, `false`},
		{`{"onboarding_done":"Off"}`, `false`},
	}
	for i, tc := range cases {
		rec := do(t, h, http.MethodPut, "/api/v1/settings", tc.body)
		if rec.Code != http.StatusOK {
			t.Fatalf("case %d: 状态码 = %d, body %s", i, rec.Code, rec.Body.String())
		}
		var raw string
		if err := db.QueryRow(
			"SELECT value_json FROM app_settings WHERE key = ?", keyOnboardingDone).Scan(&raw); err != nil {
			t.Fatalf("case %d: 读回失败：%v", i, err)
		}
		if raw != tc.want {
			t.Fatalf("case %d: value_json = %s, want %s", i, raw, tc.want)
		}
	}
}

func TestPutExplicitNullWritesNothing(t *testing.T) {
	db, h := newTestEnv(t)
	rec := do(t, h, http.MethodPut, "/api/v1/settings", `{"onboarding_done":null,"theme":null}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, body %s", rec.Code, rec.Body.String())
	}
	var n int
	if err := db.QueryRow("SELECT COUNT(*) FROM app_settings").Scan(&n); err != nil {
		t.Fatalf("计数失败：%v", err)
	}
	if n != 0 {
		t.Fatalf("显式 null 不应落行，实际 %d 行", n)
	}
}

func TestPutInvalidThemeIgnored(t *testing.T) {
	_, h := newTestEnv(t)
	rec := do(t, h, http.MethodPut, "/api/v1/settings", `{"theme":"blue"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	var resp settingsResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败：%v", err)
	}
	if resp.Theme != "dark" {
		t.Fatalf("异常主题应被忽略，theme = %q", resp.Theme)
	}
}

// TestPutConcurrentNoDeadlock 真实写闸下并发 PUT：各写独立字段互不覆盖，
// 主题并发只落两值之一，全部请求 200 且不挂死。
func TestPutConcurrentNoDeadlock(t *testing.T) {
	_, h := newTestEnv(t)
	srv := httptest.NewServer(h)
	defer srv.Close()

	boolFields := []string{"onboarding_done", "auto_price", "backup_auto", "update_notify", "update_auto", "direct_notice_seen"}
	var wg sync.WaitGroup
	errCh := make(chan error, 64)
	for _, field := range boolFields {
		wg.Add(1)
		go func(field string) {
			defer wg.Done()
			resp, err := srv.Client().Do(mustRequest(field, srv.URL+"/api/v1/settings", `{"`+field+`":true}`))
			if err != nil {
				errCh <- fmt.Errorf("%s: %w", field, err)
				return
			}
			resp.Body.Close()
			if resp.StatusCode != http.StatusOK {
				errCh <- fmt.Errorf("%s: 状态码 %d", field, resp.StatusCode)
			}
		}(field)
	}
	for i := 0; i < 16; i++ {
		theme := "dark"
		if i%2 == 1 {
			theme = "light"
		}
		wg.Add(1)
		go func(theme string) {
			defer wg.Done()
			resp, err := srv.Client().Do(mustRequest("theme", srv.URL+"/api/v1/settings", `{"theme":"`+theme+`"}`))
			if err != nil {
				errCh <- fmt.Errorf("theme: %w", err)
				return
			}
			resp.Body.Close()
			if resp.StatusCode != http.StatusOK {
				errCh <- fmt.Errorf("theme: 状态码 %d", resp.StatusCode)
			}
		}(theme)
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		resp, err := srv.Client().Do(mustRequest("mcp", srv.URL+"/api/v1/settings", `{"mcp_enabled":true,"account":{"steam_id":"76561198000000001"}}`))
		if err != nil {
			errCh <- fmt.Errorf("mcp: %w", err)
			return
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			errCh <- fmt.Errorf("mcp: 状态码 %d", resp.StatusCode)
		}
	}()
	wg.Wait()
	close(errCh)
	for err := range errCh {
		t.Fatal(err)
	}

	rec := do(t, h, http.MethodGet, "/api/v1/settings", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("终态读取状态码 = %d", rec.Code)
	}
	var resp settingsResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败：%v", err)
	}
	values := []bool{resp.OnboardingDone, resp.AutoPrice, resp.BackupAuto, resp.UpdateNotify, resp.UpdateAuto, resp.DirectNoticeSeen}
	if !values[0] || !values[1] || !values[2] || !values[3] || !values[4] || !values[5] {
		t.Fatalf("独立字段写入有丢失：%+v", resp)
	}
	if resp.Theme != "dark" && resp.Theme != "light" {
		t.Fatalf("theme 越界：%q", resp.Theme)
	}
	if !resp.MCPEnabled {
		t.Fatalf("mcp_enabled 丢失")
	}
}

func mustRequest(name, url, body string) *http.Request {
	req, err := http.NewRequest(http.MethodPut, url, strings.NewReader(body))
	if err != nil {
		panic(fmt.Sprintf("%s: %v", name, err))
	}
	req.Header.Set("Content-Type", "application/json")
	return req
}
