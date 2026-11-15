// redeem_test.go 契约测试：复刻 Python tests/test_redeem.py 基线 13 案 +
// 分桶计数与 account 注入回归 2 案，另加 HTTP 层 422/400 形状守护。
// 网络全隔离：Steam 端点经包级 registerKeyURL 打桩到 httptest。
package redeem

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/store"
)

const primarySteamID = "76561198000000001"
const otherSteamID = "76561198000000002"

// newFixture 临时库 + 账号服务（钱包/资料请求全部 429 快速失败，不影响绑定）
// + redeem 服务（RedeemUsed 注入接线，与生产装配同构）。
func newFixture(t *testing.T) (*Service, *account.Service, *sql.DB) {
	t.Helper()
	data := t.TempDir()
	path := filepath.Join(data, "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err = store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(429) }))
	t.Cleanup(srv.Close)
	client := account.NewSteamClient(account.SteamOptions{HTTPClient: srv.Client(), AccountURL: srv.URL + "/account/", ProfileURL: srv.URL + "/miniprofile/%s/json"})
	gate := store.NewWriteGate(db, path)
	var svc *Service
	accounts := account.NewService(context.Background(), db, gate, account.ServiceOptions{DataDir: data, Steam: client, RedeemUsed: func(steamID string) int {
		if svc == nil {
			return 0
		}
		return svc.AccountUsage(steamID)
	}})
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if err := accounts.Close(ctx); err != nil {
			t.Error(err)
		}
	})
	svc = NewService(db, gate, ServiceOptions{Accounts: accounts})
	return svc, accounts, db
}

// stubSteam 伪 Steam 激活端点（覆盖包级出网 URL）。
func stubSteam(t *testing.T, handler http.HandlerFunc) {
	t.Helper()
	srv := httptest.NewServer(handler)
	old := registerKeyURL
	registerKeyURL = srv.URL + "/account/ajaxregisterkey/"
	t.Cleanup(func() { registerKeyURL = old; srv.Close() })
}

// bindCookie 绑定账号（sessionid 可为空串模拟缺失）。
func bindCookie(t *testing.T, accounts *account.Service, steamID, sessionID string) {
	t.Helper()
	cookie := "steamLoginSecure=" + steamID + "||jwt-token"
	if sessionID != "" {
		cookie = "sessionid=" + sessionID + "; " + cookie
	}
	if _, err := accounts.BindCookies(context.Background(), cookie); err != nil {
		t.Fatal(err)
	}
}

type steamCall struct {
	form    url.Values
	cookie  string
	referer string
	ua      string
}

func captureCall(t *testing.T, call *steamCall, payload map[string]any) {
	t.Helper()
	stubSteam(t, func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		*call = steamCall{form: r.Form, cookie: r.Header.Get("Cookie"), referer: r.Header.Get("Referer"), ua: r.Header.Get("User-Agent")}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(payload)
	})
}

func ledgerRows(t *testing.T, db *sql.DB) []map[string]any {
	t.Helper()
	rows, err := db.Query(`SELECT id,kind,status,product_key,subid,sub_name,detail,raw,steamid,CAST(created_at AS TEXT) FROM redeem_activations ORDER BY id`)
	if err != nil {
		t.Fatal(err)
	}
	defer rows.Close()
	out := []map[string]any{}
	for rows.Next() {
		var id int64
		var kind, status, key, subid, subName, detail, raw, steamID, created string
		if err := rows.Scan(&id, &kind, &status, &key, &subid, &subName, &detail, &raw, &steamID, &created); err != nil {
			t.Fatal(err)
		}
		out = append(out, map[string]any{"id": id, "kind": kind, "status": status, "product_key": key, "subid": subid, "sub_name": subName, "detail": detail, "raw": raw, "steamid": steamID, "created_at": created})
	}
	if err := rows.Err(); err != nil {
		t.Fatal(err)
	}
	return out
}

// 1. test_activate_key_success_receipt：success=1 → ok，回执提取 subId/subName。
func TestActivateKeySuccessReceipt(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	var call steamCall
	captureCall(t, &call, map[string]any{
		"success":               1,
		"purchase_receipt_info": map[string]any{"line_items": []any{map[string]any{"packageid": 307574, "line_item_description": "Dark Envoy"}}},
	})
	res, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC")
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != "ok" || res.Detail != "激活成功" || res.SubID != "307574" || res.SubName != "Dark Envoy" {
		t.Fatalf("结果不符: %+v", res)
	}
	if !strings.Contains(res.Raw, "purchase_receipt_info") {
		t.Fatalf("raw 缺回执原文: %q", res.Raw)
	}
	if got := call.form.Get("product_key"); got != "AAAAA-BBBBB-CCCCC" {
		t.Fatalf("表单 product_key 不符: %q", got)
	}
	if got := call.form.Get("sessionid"); got != "abc123" {
		t.Fatalf("表单 sessionid 不符: %q", got)
	}
	if call.ua != "Mozilla/5.0 (Windows NT 10.0; Win64; x64)" || !strings.Contains(call.referer, "registerkey") {
		t.Fatalf("请求头不符: UA=%q Referer=%q", call.ua, call.referer)
	}
	if !strings.Contains(call.cookie, "steamLoginSecure=") || !strings.Contains(call.cookie, "sessionid=abc123") {
		t.Fatalf("Cookie 注入不符: %q", call.cookie)
	}
}

// 2. test_activate_key_own_receipt_keeps_sub：已拥有回执带 line_items 同样提取。
func TestActivateKeyOwnReceiptKeepsSub(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	captureCall(t, &steamCall{}, map[string]any{
		"success":                2,
		"purchase_result_details": 9,
		"purchase_receipt_info":  map[string]any{"line_items": []any{map[string]any{"packageid": 52524, "line_item_description": "Counter-Strike 2"}}},
	})
	res, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC")
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != "own" || res.SubID != "52524" || res.SubName != "Counter-Strike 2" {
		t.Fatalf("结果不符: %+v", res)
	}
	if !strings.Contains(res.Raw, "purchase_result_details") {
		t.Fatalf("raw 缺回执原文: %q", res.Raw)
	}
}

// 3. test_activate_key_already_owned：错误码 9 → own。
func TestActivateKeyAlreadyOwned(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	captureCall(t, &steamCall{}, map[string]any{"success": 2, "purchase_result_details": 9})
	res, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC")
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != "own" || res.Detail != "已拥有" {
		t.Fatalf("结果不符: %+v", res)
	}
}

// 4. test_activate_key_rate_limited：错误码 53 → fail 次数上限语义。
func TestActivateKeyRateLimited(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	captureCall(t, &steamCall{}, map[string]any{"success": 2, "purchase_result_details": 53})
	res, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC")
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != "fail" || !strings.Contains(res.Detail, "上限") {
		t.Fatalf("结果不符: %+v", res)
	}
}

// 5. test_activate_key_invalid_code_passthrough：未知错误码 → 原码回传。
func TestActivateKeyInvalidCodePassthrough(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	captureCall(t, &steamCall{}, map[string]any{"success": 2, "purchase_result_details": 87})
	res, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC")
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != "fail" || !strings.Contains(res.Detail, "87") {
		t.Fatalf("结果不符: %+v", res)
	}
}

// 6. test_activate_batch_aggregation：批量汇总计数与逐码结果顺序保持。
func TestActivateBatchAggregation(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	stubSteam(t, func(w http.ResponseWriter, r *http.Request) {
		_ = r.ParseForm()
		var payload map[string]any
		switch r.Form.Get("product_key") {
		case "K1":
			payload = map[string]any{"success": 1, "purchase_receipt_info": map[string]any{"line_items": []any{map[string]any{"packageid": 1, "line_item_description": "A"}}}}
		case "K2":
			payload = map[string]any{"success": 2, "purchase_result_details": 9}
		default:
			payload = map[string]any{"success": 2, "purchase_result_details": 14}
		}
		_ = json.NewEncoder(w).Encode(payload)
	})
	out, err := svc.ActivateBatch(context.Background(), []string{"K1", "K2", "K3"})
	if err != nil {
		t.Fatal(err)
	}
	if out.OK != 1 || out.Own != 1 || out.Fail != 1 {
		t.Fatalf("汇总计数不符: %+v", out)
	}
	codes := []string{}
	for _, row := range out.Results {
		codes = append(codes, row.Code)
	}
	if strings.Join(codes, ",") != "K1,K2,K3" {
		t.Fatalf("结果顺序不符: %v", codes)
	}
}

// 7/8. test_activate_key_no_cookie / no_sessionid：登录态缺失 → InputError（路由 400）。
func TestActivateKeyNoCookie(t *testing.T) {
	svc, _, _ := newFixture(t)
	_, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC")
	var input *InputError
	if !errors.As(err, &input) || !strings.Contains(err.Error(), "尚未绑定") {
		t.Fatalf("应报尚未绑定: %v", err)
	}
}

func TestActivateKeyNoSessionid(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "")
	_, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC")
	var input *InputError
	if !errors.As(err, &input) || !strings.Contains(err.Error(), "sessionid") {
		t.Fatalf("应报缺 sessionid: %v", err)
	}
}

// 9. test_quota_status：无 Cookie 五字段精确等值；绑定后前提齐备。
func TestQuotaStatus(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	quota, err := svc.QuotaStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if quota.HasCookie || quota.HasSessionID || quota.SteamID != "" || quota.Used != 0 || quota.Limit != 10 {
		t.Fatalf("无 Cookie 响应不符: %+v", quota)
	}
	bindCookie(t, accounts, primarySteamID, "abc123")
	quota, err = svc.QuotaStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if !quota.HasCookie || !quota.HasSessionID || quota.SteamID != primarySteamID || quota.Used != 0 || quota.Limit != 10 {
		t.Fatalf("绑定后响应不符: %+v", quota)
	}
}

// 10. test_activation_count_per_account：按账号计数，换绑清零。
func TestActivationCountPerAccount(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	captureCall(t, &steamCall{}, map[string]any{"success": 2, "purchase_result_details": 9})
	for _, key := range []string{"AAAAA-BBBBB-CCCCC", "BBBBB-CCCCC-DDDDD"} {
		if _, err := svc.ActivateKey(context.Background(), key); err != nil {
			t.Fatal(err)
		}
	}
	quota, err := svc.QuotaStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if quota.SteamID != primarySteamID || quota.Used != 2 {
		t.Fatalf("计数不符: %+v", quota)
	}
	bindCookie(t, accounts, otherSteamID, "xyz")
	quota, err = svc.QuotaStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if quota.SteamID != otherSteamID || quota.Used != 0 {
		t.Fatalf("换绑后应清零: %+v", quota)
	}
}

// 11. test_activation_ledger_rows_for_each_status：ok/own/fail 各落一行 + 掩码。
func TestActivationLedgerRowsForEachStatus(t *testing.T) {
	svc, accounts, db := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	cases := []struct {
		payload map[string]any
		status  string
		subid   string
	}{
		{map[string]any{"success": 1, "purchase_receipt_info": map[string]any{"line_items": []any{map[string]any{"packageid": 307574, "line_item_description": "Dark Envoy"}}}}, "ok", "307574"},
		{map[string]any{"success": 2, "purchase_result_details": 9}, "own", ""},
		{map[string]any{"success": 2, "purchase_result_details": 15}, "fail", ""},
	}
	for _, tc := range cases {
		captureCall(t, &steamCall{}, tc.payload)
		if _, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC"); err != nil {
			t.Fatal(err)
		}
	}
	rows := ledgerRows(t, db)
	if len(rows) != 3 {
		t.Fatalf("应落 3 行: %d", len(rows))
	}
	statuses := []string{}
	for _, row := range rows {
		statuses = append(statuses, row["status"].(string))
		if row["kind"] != "cdk" || row["product_key"] != "AAAAA-BBBBB-CCCCC" {
			t.Fatalf("行形状不符: %+v", row)
		}
	}
	if strings.Join(statuses, ",") != "ok,own,fail" {
		t.Fatalf("状态序列不符: %v", statuses)
	}
	first := rows[0]
	if first["subid"] != "307574" || first["sub_name"] != "Dark Envoy" || first["detail"] != "激活成功" || first["steamid"] != primarySteamID {
		t.Fatalf("首行回执不符: %+v", first)
	}
	if !strings.Contains(first["raw"].(string), "purchase_receipt_info") {
		t.Fatalf("首行 raw 缺原文: %+v", first)
	}
	if created := first["created_at"].(string); len(created) < 19 || created[10] != ' ' {
		t.Fatalf("created_at 格式不符: %q", created)
	}
	if got := maskKey("ABCDE-FGHIJ-KLMNP"); got != "ABCDE**********NP" {
		t.Fatalf("掩码不符: %q", got)
	}
}

// 12. test_list_activations_order_and_paging：倒序 + 分页 + 夹取。
func TestListActivationsOrderAndPaging(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	captureCall(t, &steamCall{}, map[string]any{"success": 2, "purchase_result_details": 15})
	keys := []string{"AAAAA-BBBBB-CCCC1", "AAAAA-BBBBB-CCCC2", "AAAAA-BBBBB-CCCC3"}
	for _, key := range keys {
		if _, err := svc.ActivateKey(context.Background(), key); err != nil {
			t.Fatal(err)
		}
	}
	page1, err := svc.ListActivations(context.Background(), 2, 0)
	if err != nil {
		t.Fatal(err)
	}
	if page1.Total != 3 {
		t.Fatalf("total 不符: %d", page1.Total)
	}
	if page1.Rows[0].Key != keys[2] || page1.Rows[1].Key != keys[1] {
		t.Fatalf("倒序不符: %+v", page1.Rows)
	}
	page2, err := svc.ListActivations(context.Background(), 2, 2)
	if err != nil {
		t.Fatal(err)
	}
	if len(page2.Rows) != 1 || page2.Rows[0].Key != keys[0] {
		t.Fatalf("第二页不符: %+v", page2.Rows)
	}
	over, err := svc.ListActivations(context.Background(), 99999, -5)
	if err != nil {
		t.Fatal(err)
	}
	if len(over.Rows) != 3 {
		t.Fatalf("夹取后应取全部: %d", len(over.Rows))
	}
	row := page1.Rows[0]
	if row.Kind != "cdk" || row.Status != "fail" || row.SubID != "" || row.SteamID != primarySteamID {
		t.Fatalf("行投影不符: %+v", row)
	}
	if len(row.CreatedAt) != 19 || row.CreatedAt[4] != '-' || row.CreatedAt[10] != ' ' {
		t.Fatalf("createdAt 格式不符: %q", row.CreatedAt)
	}
}

// TestSearchActivationsFilterAndPaging 检索函数：status/key 精确过滤 + q 子串搜索 + 分页。
func TestSearchActivationsFilterAndPaging(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	// ok 回执带版本名，两条失败行无版本名
	captureCall(t, &steamCall{}, map[string]any{"success": 1, "purchase_receipt_info": map[string]any{
		"line_items": []any{map[string]any{"packageid": 307574, "line_item_description": "Dark Envoy"}}}})
	if _, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCC1"); err != nil {
		t.Fatal(err)
	}
	captureCall(t, &steamCall{}, map[string]any{"success": 2, "purchase_result_details": 13})
	for _, key := range []string{"AAAAA-BBBBB-CCCC2", "EEEEE-FFFFF-GGGG1"} {
		if _, err := svc.ActivateKey(context.Background(), key); err != nil {
			t.Fatal(err)
		}
	}

	okPage, err := svc.SearchActivations(context.Background(), "ok", "", "", 200, 0)
	if err != nil {
		t.Fatal(err)
	}
	if okPage.Total != 1 || okPage.Rows[0].Key != "AAAAA-BBBBB-CCCC1" || okPage.Rows[0].SubName != "Dark Envoy" {
		t.Fatalf("ok 过滤不符: %+v", okPage)
	}
	failPage, err := svc.SearchActivations(context.Background(), "fail", "", "", 200, 0)
	if err != nil {
		t.Fatal(err)
	}
	if failPage.Total != 2 {
		t.Fatalf("fail 过滤不符: %d", failPage.Total)
	}

	byKey, err := svc.SearchActivations(context.Background(), "ok", "", "AAAAA-BBBBB-CCCC1", 5, 0)
	if err != nil {
		t.Fatal(err)
	}
	if byKey.Total != 1 || byKey.Rows[0].Detail != "激活成功" {
		t.Fatalf("按码回查不符: %+v", byKey)
	}
	none, err := svc.SearchActivations(context.Background(), "ok", "", "EEEEE-FFFFF-GGGG1", 5, 0)
	if err != nil {
		t.Fatal(err)
	}
	if none.Total != 0 {
		t.Fatalf("无成功历史的码不应命中: %d", none.Total)
	}

	hit, err := svc.SearchActivations(context.Background(), "", "dark envoy", "", 200, 0)
	if err != nil {
		t.Fatal(err)
	}
	if hit.Total != 1 || hit.Rows[0].Key != "AAAAA-BBBBB-CCCC1" {
		t.Fatalf("q 搜索不符: %+v", hit)
	}
	// 时间倒序：后激活的在前；分页切片一致
	page0, err := svc.SearchActivations(context.Background(), "", "aaaa", "", 1, 0)
	if err != nil {
		t.Fatal(err)
	}
	page1, err := svc.SearchActivations(context.Background(), "", "aaaa", "", 1, 1)
	if err != nil {
		t.Fatal(err)
	}
	if page0.Rows[0].Key != "AAAAA-BBBBB-CCCC2" || page1.Rows[0].Key != "AAAAA-BBBBB-CCCC1" {
		t.Fatalf("分页不符: %+v / %+v", page0.Rows, page1.Rows)
	}
	if _, err := svc.SearchActivations(context.Background(), "bogus", "", "", 200, 0); err == nil {
		t.Fatal("非法 status 应报错")
	}
}

// 13. test_ledger_raw_truncated：落账 raw 截到 8000；展示口径先截 2000。
func TestLedgerRawTruncated(t *testing.T) {
	svc, accounts, db := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	ctx := context.Background()
	if err := svc.recordLedger(ctx, ledgerRow{Kind: "cdk", Status: "fail", ProductKey: "AAAAA-BBBBB-CCCCC", Detail: "测试截断", Raw: strings.Repeat("x", rawMax*3), SteamID: primarySteamID}); err != nil {
		t.Fatal(err)
	}
	if err := svc.recordLedger(ctx, ledgerRow{Kind: "cdk", Status: "fail", ProductKey: "BBBBB-CCCCC-DDDDD", Detail: "测试上限", Raw: strings.Repeat("y", 5000), SteamID: primarySteamID}); err != nil {
		t.Fatal(err)
	}
	rows := ledgerRows(t, db)
	if len(rows) != 2 {
		t.Fatalf("应落 2 行: %d", len(rows))
	}
	if len(rows[0]["raw"].(string)) != rawMax {
		t.Fatalf("raw 应截到 %d: %d", rawMax, len(rows[0]["raw"].(string)))
	}
	if len(rows[1]["raw"].(string)) != 5000 {
		t.Fatalf("5000 以内不应截断: %d", len(rows[1]["raw"].(string)))
	}
	// 正常链路：结果 raw 按展示口径先截 2000
	stubSteam(t, func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(strings.Repeat("z", 5000)))
	})
	res, err := svc.ActivateKey(ctx, "CCCCC-DDDDD-EEEEE")
	if err != nil {
		t.Fatal(err)
	}
	if len(res.Raw) != rawDisplayMax {
		t.Fatalf("展示 raw 应截到 %d: %d", rawDisplayMax, len(res.Raw))
	}
	rows = ledgerRows(t, db)
	if len(rows[2]["raw"].(string)) != rawDisplayMax {
		t.Fatalf("落账 raw 应为展示截断值: %d", len(rows[2]["raw"].(string)))
	}
}

// 14. test_transport_failure_ledger_row：传输失败落 fail 行且不计次数。
func TestTransportFailureLedgerRow(t *testing.T) {
	svc, accounts, db := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL + "/account/ajaxregisterkey/"
	srv.Close()
	old := registerKeyURL
	registerKeyURL = url
	t.Cleanup(func() { registerKeyURL = old })
	res, err := svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC")
	if err != nil {
		t.Fatal(err)
	}
	if res.Status != "fail" || !strings.Contains(res.Detail, "激活请求失败") {
		t.Fatalf("结果不符: %+v", res)
	}
	quota, err := svc.QuotaStatus(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if quota.Used != 0 {
		t.Fatalf("传输失败不应计数: %d", quota.Used)
	}
	rows := ledgerRows(t, db)
	if len(rows) != 1 {
		t.Fatalf("应落 1 行: %d", len(rows))
	}
	row := rows[0]
	if row["status"] != "fail" || row["product_key"] != "AAAAA-BBBBB-CCCCC" || row["raw"] != "" {
		t.Fatalf("落账行不符: %+v", row)
	}
}

// +1（test_account_multi 分桶 3/1）：计数按账号独立分桶 + 过期戳清出窗口。
func TestActivationUsageIsolatedPerAccount(t *testing.T) {
	now := time.Now()
	c := newCounter(ACTWindowSeconds*time.Second, func() time.Time { return now })
	for i := 0; i < 3; i++ {
		c.record(primarySteamID)
	}
	c.record(otherSteamID)
	if got := c.usage(primarySteamID); got != 3 {
		t.Fatalf("A 桶计数不符: %d", got)
	}
	if got := c.usage(otherSteamID); got != 1 {
		t.Fatalf("B 桶计数不符: %d", got)
	}
	now = now.Add(31 * time.Minute)
	if got := c.usage(primarySteamID); got != 0 {
		t.Fatalf("窗口过期应清零: %d", got)
	}
}

// +2（account 注入回归）：写死 0 → 注入后账号 views 反映计数。
func TestAccountViewsReflectRedeemCounter(t *testing.T) {
	svc, accounts, _ := newFixture(t)
	bindCookie(t, accounts, primarySteamID, "abc123")
	captureCall(t, &steamCall{}, map[string]any{"success": 2, "purchase_result_details": 9})
	views, err := accounts.Accounts(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(views) != 1 || views[0]["redeem_used"] != 0 {
		t.Fatalf("激活前应为 0: %+v", views)
	}
	if _, err = svc.ActivateKey(context.Background(), "AAAAA-BBBBB-CCCCC"); err != nil {
		t.Fatal(err)
	}
	if _, err = svc.ActivateKey(context.Background(), "BBBBB-CCCCC-DDDDD"); err != nil {
		t.Fatal(err)
	}
	views, err = accounts.Accounts(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if views[0]["redeem_used"] != 2 {
		t.Fatalf("注入后 views 应反映计数 2: %v", views[0]["redeem_used"])
	}
}

// ── HTTP 层形状守护（FastAPI 复刻） ─────────────────────────────────

func httpFixture(t *testing.T) (*Service, *account.Service, *http.ServeMux) {
	t.Helper()
	svc, accounts, _ := newFixture(t)
	mux := http.NewServeMux()
	svc.Register(mux)
	return svc, accounts, mux
}

func doJSON(t *testing.T, mux *http.ServeMux, method, target, body string) *httptest.ResponseRecorder {
	t.Helper()
	var reader *strings.Reader
	if body == "" {
		reader = strings.NewReader("")
	} else {
		reader = strings.NewReader(body)
	}
	req := httptest.NewRequest(method, target, reader)
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, req)
	return rec
}

func decode(t *testing.T, rec *httptest.ResponseRecorder) map[string]any {
	t.Helper()
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("响应非 JSON: %s", rec.Body.String())
	}
	return out
}

func TestHTTPActivateKeysValidation(t *testing.T) {
	_, _, mux := httpFixture(t)
	cases := []struct {
		name   string
		body   string
		status int
		issue  int // 期望 detail 数组长度（0 = 非数组信封）
		check  func(t *testing.T, out map[string]any)
	}{
		{"empty list too_short", `{"keys": []}`, 422, 1, func(t *testing.T, out map[string]any) {
			issue := out["detail"].([]any)[0].(map[string]any)
			if issue["type"] != "too_short" || !strings.Contains(issue["msg"].(string), "at least 1 item") {
				t.Fatalf("too_short 形状不符: %v", issue)
			}
		}},
		{"non-list list_type", `{"keys": 5}`, 422, 1, func(t *testing.T, out map[string]any) {
			issue := out["detail"].([]any)[0].(map[string]any)
			if issue["type"] != "list_type" {
				t.Fatalf("list_type 形状不符: %v", issue)
			}
		}},
		{"missing field", `{}`, 422, 1, func(t *testing.T, out map[string]any) {
			issue := out["detail"].([]any)[0].(map[string]any)
			if issue["type"] != "missing" || issue["msg"] != "Field required" {
				t.Fatalf("missing 形状不符: %v", issue)
			}
		}},
		{"invalid json", `not-json`, 422, 1, func(t *testing.T, out map[string]any) {
			issue := out["detail"].([]any)[0].(map[string]any)
			if issue["type"] != "json_invalid" {
				t.Fatalf("json_invalid 形状不符: %v", issue)
			}
		}},
		{"item type", `{"keys": [1, 2]}`, 422, 2, func(t *testing.T, out map[string]any) {
			issue := out["detail"].([]any)[0].(map[string]any)
			if issue["type"] != "string_type" {
				t.Fatalf("string_type 形状不符: %v", issue)
			}
			loc := issue["loc"].([]any)
			if fmt.Sprint(loc[len(loc)-1]) != "0" {
				t.Fatalf("loc 下标不符: %v", loc)
			}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := doJSON(t, mux, "POST", "/api/v1/redeem/keys", tc.body)
			if rec.Code != tc.status {
				t.Fatalf("状态码不符: %d %s", rec.Code, rec.Body.String())
			}
			out := decode(t, rec)
			issues := out["detail"].([]any)
			if len(issues) != tc.issue {
				t.Fatalf("detail 数量不符: %d", len(issues))
			}
			tc.check(t, out)
		})
	}
	// max100：101 个元素 → too_long
	items := make([]string, 101)
	for i := range items {
		items[i] = `"K"`
	}
	rec := doJSON(t, mux, "POST", "/api/v1/redeem/keys", `{"keys": [`+strings.Join(items, ",")+`]}`)
	if rec.Code != 422 {
		t.Fatalf("max100 应 422: %d", rec.Code)
	}
	issue := decode(t, rec)["detail"].([]any)[0].(map[string]any)
	if issue["type"] != "too_long" || !strings.Contains(issue["msg"].(string), "not 101") {
		t.Fatalf("too_long 形状不符: %v", issue)
	}
}

func TestHTTPActivateKeysEnvelopes(t *testing.T) {
	_, _, mux := httpFixture(t)
	// 全空白码过滤后为空 → 400 激活码列表为空
	rec := doJSON(t, mux, "POST", "/api/v1/redeem/keys", `{"keys": ["   ", "  "]}`)
	if rec.Code != 400 {
		t.Fatalf("空白码应 400: %d %s", rec.Code, rec.Body.String())
	}
	if got := decode(t, rec)["detail"]; got != "激活码列表为空" {
		t.Fatalf("detail 不符: %v", got)
	}
	// 未绑定 Cookie → 400 尚未绑定
	rec = doJSON(t, mux, "POST", "/api/v1/redeem/keys", `{"keys": ["AAAAA-BBBBB-CCCCC"]}`)
	if rec.Code != 400 {
		t.Fatalf("无 Cookie 应 400: %d %s", rec.Code, rec.Body.String())
	}
	if got := decode(t, rec)["detail"]; !strings.Contains(got.(string), "尚未绑定") {
		t.Fatalf("detail 不符: %v", got)
	}
}

func TestHTTPQuotaAndActivations(t *testing.T) {
	svc, accounts, mux := httpFixture(t)
	rec := doJSON(t, mux, "GET", "/api/v1/redeem/quota", "")
	if rec.Code != 200 {
		t.Fatalf("quota 应 200: %d", rec.Code)
	}
	out := decode(t, rec)
	want := map[string]any{"hasCookie": false, "hasSessionId": false, "steamId": "", "used": float64(0), "limit": float64(10)}
	for key, value := range want {
		if out[key] != value {
			t.Fatalf("quota 字段 %s 不符: %v", key, out[key])
		}
	}
	// 非法 limit → 422 int_parsing
	rec = doJSON(t, mux, "GET", "/api/v1/redeem/activations?limit=abc", "")
	if rec.Code != 422 {
		t.Fatalf("limit=abc 应 422: %d", rec.Code)
	}
	issue := decode(t, rec)["detail"].([]any)[0].(map[string]any)
	if issue["type"] != "int_parsing" || issue["loc"].([]any)[1] != "limit" {
		t.Fatalf("int_parsing 形状不符: %v", issue)
	}
	// 落两行后分页读口
	bindCookie(t, accounts, primarySteamID, "abc123")
	ctx := context.Background()
	for _, key := range []string{"K1", "K2"} {
		if err := svc.recordLedger(ctx, ledgerRow{Kind: "cdk", Status: "fail", ProductKey: key, Detail: "d", SteamID: primarySteamID}); err != nil {
			t.Fatal(err)
		}
	}
	rec = doJSON(t, mux, "GET", "/api/v1/redeem/activations?limit=1&offset=1", "")
	if rec.Code != 200 {
		t.Fatalf("activations 应 200: %d", rec.Code)
	}
	out = decode(t, rec)
	if out["total"] != float64(2) {
		t.Fatalf("total 不符: %v", out["total"])
	}
	rows := out["rows"].([]any)
	if len(rows) != 1 {
		t.Fatalf("分页不符: %v", rows)
	}
	row := rows[0].(map[string]any)
	if row["key"] != "K1" || row["subId"] != "" || row["createdAt"] == "" {
		t.Fatalf("行投影不符: %v", row)
	}
	if _, exists := row["raw"]; exists {
		t.Fatal("raw 不应回传")
	}
}
