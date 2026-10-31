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

	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

// newTestEnv 真实临时库 + 真实写闸 + 已装配路由；凭据主密钥托管指向
// 同一临时数据目录（测试隔离，不触真实数据目录）。
func newTestEnv(t *testing.T) (*sql.DB, http.Handler) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "holdexar.db")
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
	prev := resolveDataDir
	resolveDataDir = func() string { return dir }
	t.Cleanup(func() { resolveDataDir = prev })
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

func TestGetSettingsUndecryptableKeyDegrades(t *testing.T) {
	db, h := newTestEnv(t)
	seedKV(t, db, "account.steam_api_key", `"enc1:QUJDREVGR0g"`) // 伪密文，解密必失败
	rec := do(t, h, http.MethodGet, "/api/v1/settings", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	var resp settingsResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败：%v", err)
	}
	// 解密失败按无凭据降级（Python SecretBoxError → default 同径）。
	if resp.Account.HasAPIKey || resp.Account.SteamAPIKey != "" {
		t.Fatalf("解密失败降级形状不符：%+v", resp.Account)
	}
}

// TestPutSecretKeyRoundTrip 凭据写读全链：PUT 明文 → 库内 enc1: 密文 →
// GET 掩码回显 → 同数据目录 secretbox 解密还原明文；掩码回显与空串不落库。
func TestPutSecretKeyRoundTrip(t *testing.T) {
	db, h := newTestEnv(t)
	rec := do(t, h, http.MethodPut, "/api/v1/settings", `{"account":{"steam_api_key":" sk-test-abcd1234 "}}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, body %s", rec.Code, rec.Body.String())
	}

	// 库内是 enc1: 前缀密文（JSON 字符串），不是明文。
	var stored string
	if err := db.QueryRow(
		"SELECT value_json FROM app_settings WHERE key = ?", keySteamAPIKey).Scan(&stored); err != nil {
		t.Fatalf("读回失败：%v", err)
	}
	if !strings.HasPrefix(stored, `"enc1:`) {
		t.Fatalf("库内应为 enc1: 密文，实际 %s", stored)
	}
	token := strings.Trim(stored, `"`)

	// GET 掩码回显（**** + 末 4 字符）。
	var resp settingsResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败：%v", err)
	}
	if resp.Account.SteamAPIKey != "****1234" || !resp.Account.HasAPIKey {
		t.Fatalf("掩码回显不符：%+v", resp.Account)
	}

	// 同数据目录下 secretbox 解密还原明文（purpose 与 Python _secret_purpose 同径）。
	plain, err := secretbox.DecryptSecretIn(resolveDataDir(), token, secretbox.SecretPurpose(keySteamAPIKey))
	if err != nil {
		t.Fatalf("密文解密失败：%v", err)
	}
	if plain != "sk-test-abcd1234" {
		t.Fatalf("还原明文不符：%q", plain)
	}

	// 掩码回显不落库：库内密文不变。
	before := stored
	if rec := do(t, h, http.MethodPut, "/api/v1/settings", `{"account":{"steam_api_key":"****1234"}}`); rec.Code != http.StatusOK {
		t.Fatalf("掩码回显请求状态码 = %d", rec.Code)
	}
	var after string
	if err := db.QueryRow(
		"SELECT value_json FROM app_settings WHERE key = ?", keySteamAPIKey).Scan(&after); err != nil {
		t.Fatalf("读回失败：%v", err)
	}
	if after != before {
		t.Fatalf("掩码回显不应落库：%s -> %s", before, after)
	}

	// 空串不落库（Python `if key` 短路同径）。
	if rec := do(t, h, http.MethodPut, "/api/v1/settings", `{"account":{"steam_api_key":"  "}}`); rec.Code != http.StatusOK {
		t.Fatalf("空串请求状态码 = %d", rec.Code)
	}
	if err := db.QueryRow(
		"SELECT value_json FROM app_settings WHERE key = ?", keySteamAPIKey).Scan(&after); err != nil {
		t.Fatalf("读回失败：%v", err)
	}
	if after != before {
		t.Fatalf("空串不应落库：%s -> %s", before, after)
	}
}

// TestGetSeededRealTokenMask 存量密文行（先于 Go 写入存在）GET 解密掩码。
func TestGetSeededRealTokenMask(t *testing.T) {
	db, h := newTestEnv(t)
	token, err := secretbox.EncryptSecretIn(resolveDataDir(), "seedkey9999", secretbox.SecretPurpose(keySteamAPIKey))
	if err != nil {
		t.Fatalf("种子密文生成失败：%v", err)
	}
	seedKV(t, db, keySteamAPIKey, fmt.Sprintf("%q", token))
	rec := do(t, h, http.MethodGet, "/api/v1/settings", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	var resp settingsResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("响应解析失败：%v", err)
	}
	if resp.Account.SteamAPIKey != "****9999" || !resp.Account.HasAPIKey {
		t.Fatalf("存量密文掩码不符：%+v", resp.Account)
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
	// 凭据已加密落库：掩码回显 + 在场标记。
	if acct["steam_api_key"] != "****9988" || acct["has_api_key"] != true {
		t.Fatalf("凭据掩码回显不符：%+v", acct)
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

// TestPutMCPEnabledTriggersHook mcp_enabled 落库成功后触发运行时重载回调；
// 未携带该字段时不触发（Python 写入口 reapply 同径）。
func TestPutMCPEnabledTriggersHook(t *testing.T) {
	_, h := newTestEnv(t)
	var fired int
	prev := OnMCPEnabledWrite
	OnMCPEnabledWrite = func() { fired++ }
	t.Cleanup(func() { OnMCPEnabledWrite = prev })

	if rec := do(t, h, http.MethodPut, "/api/v1/settings", `{"mcp_enabled":true}`); rec.Code != http.StatusOK {
		t.Fatalf("PUT 状态码 = %d, want 200", rec.Code)
	}
	if fired != 1 {
		t.Fatalf("携带 mcp_enabled 应触发一次重载回调：%d", fired)
	}
	do(t, h, http.MethodPut, "/api/v1/settings", `{"theme":"light"}`)
	if fired != 1 {
		t.Fatalf("未携带 mcp_enabled 不应触发回调：%d", fired)
	}
}

// TestGetMCPStatusProjection 注入运行态快照后 GET 的三个运行态字段跟随投影。
func TestGetMCPStatusProjection(t *testing.T) {
	_, h := newTestEnv(t)
	prev := MCPStatus
	MCPStatus = func() (bool, string, bool) { return true, "启用失败：注入", true }
	t.Cleanup(func() { MCPStatus = prev })

	rec := do(t, h, http.MethodGet, "/api/v1/settings", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	body := rec.Body.String()
	for _, want := range []string{`"mcp_running":true`, `"mcp_error":"启用失败：注入"`, `"mcp_forced_by_env":true`} {
		if !strings.Contains(body, want) {
			t.Fatalf("运行态投影缺 %s：%s", want, body)
		}
	}
}
