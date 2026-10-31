package proxypool

// L0 探测与共享纯函数的单测：桩控制器覆盖全部响应分档；冷却档位、_to_int
// 语义、IP 提取、L2 目标 URL 形状逐一钉死。

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestProbeNodeStubController(t *testing.T) {
	okServer := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/proxies/n1/delay" {
			t.Errorf("路径不对：%s", r.URL.Path)
		}
		if r.URL.Query().Get("timeout") != "2000" {
			t.Errorf("timeout 参数丢失：%s", r.URL.RawQuery)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer sec" {
			t.Errorf("鉴权头丢失：%q", got)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"delay": 123}`))
	}))
	defer okServer.Close()

	result := ProbeNode(context.Background(), okServer.URL, "sec", "n1", ProbeOptions{TimeoutMS: 2000})
	if !result.OK || !result.Valid || result.DelayMS != 123 || result.Detail != "" {
		t.Fatalf("成功探测结果不符：%+v", result)
	}

	cases := []struct {
		name      string
		handler   http.HandlerFunc
		wantOK    bool
		wantValid bool
	}{
		{"名字没定位到", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(404) }, false, false},
		{"认证失败", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(401) }, false, false},
		{"探测失败", func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(500) }, false, true},
		{"响应不是JSON", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("oops")) }, false, true},
		{"delay非正数", func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte(`{"delay": 0}`)) }, false, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv := httptest.NewServer(tc.handler)
			defer srv.Close()
			got := ProbeNode(context.Background(), srv.URL, "", "n1", ProbeOptions{})
			if got.OK != tc.wantOK || got.Valid != tc.wantValid {
				t.Fatalf("分档不符：want ok=%v valid=%v，got %+v", tc.wantOK, tc.wantValid, got)
			}
			if got.Detail == "" {
				t.Fatalf("失败结论必须带 detail")
			}
		})
	}
}

func TestCooldownMinutesFor(t *testing.T) {
	cases := map[int64]int{
		0: 30, 1: 30, 2: 60, 3: 120, 4: 240, 5: 480, 6: 1440, 7: 1440, 99: 1440,
	}
	for failures, want := range cases {
		if got := CooldownMinutesFor(failures); got != want {
			t.Fatalf("连续失败 %d 次的冷却应为 %d 分钟，实际 %d", failures, want, got)
		}
	}
}

func TestPyToProbeInt(t *testing.T) {
	cases := []struct {
		in     any
		want   int64
		wantOK bool
	}{
		{nil, 0, false},
		{"", 0, false},
		{"999", 999, true},
		{"74000", 74000, true},
		{"1e3", 1000, true},
		{"abc", 0, false},
		{"nan", 0, false},
		{json.Number("999"), 999, true},
		{json.Number("12.9"), 12, true},
		{int64(443), 443, true},
		{float64(7.9), 7, true},
		{true, 1, true},
		{false, 0, true},
	}
	for _, tc := range cases {
		got, ok := pyToProbeInt(tc.in)
		if ok != tc.wantOK || (ok && got != tc.want) {
			t.Fatalf("pyToProbeInt(%#v) = (%d, %v)，期望 (%d, %v)", tc.in, got, ok, tc.want, tc.wantOK)
		}
	}
}

func TestExtractIP(t *testing.T) {
	cases := []struct {
		in   string
		want string
	}{
		{`{"ip":"1.2.3.4"}`, "1.2.3.4"},
		{"1.2.3.4\n", "1.2.3.4"},
		{"2001:db8::1", "2001:db8::1"},
		{`{"ip":"not-an-ip"}`, ""},
		{`{"other":1}`, ""},
		{"not json", ""},
		{"", ""},
	}
	for _, tc := range cases {
		if got := ExtractIP(tc.in); got != tc.want {
			t.Fatalf("ExtractIP(%q) = %q，期望 %q", tc.in, got, tc.want)
		}
	}
}

// L2 目标 URL 的 golden 用 Python 真实 urlencode 产物钉死（换链接即测试红）。
func TestBusinessProbeURLGolden(t *testing.T) {
	want := "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/" +
		`?input_json={"ids":%5B{"appid":220}%5D,"context":{"language":"english",` +
		`"country_code":"US","steam_realm":1},"data_request":` +
		`{"include_basic_info":true,"include_all_purchase_options":true}}`
	if got := BusinessProbeURL(DefaultBusinessAppID, BusinessCC); got != want {
		t.Fatalf("L2 探针 URL 形状漂移：\n got %s\nwant %s", got, want)
	}
}

func TestValidateBusiness(t *testing.T) {
	valid := map[string]any{
		"response": map[string]any{
			"store_items": []any{
				map[string]any{"appid": int64(999), "success": int64(0)},
				map[string]any{
					"appid": int64(220), "success": int64(1),
					"best_purchase_option": map[string]any{"final_price_in_cents": "999"},
				},
			},
		},
	}
	ok, reason, final := ValidateBusiness(valid, 220)
	if !ok || final != 999 || reason != "" {
		t.Fatalf("合法业务响应判定失败：%v %q %d", ok, reason, final)
	}

	// success 是布尔 true 属契约不符（True == 1 会蒙混过关，显式拒绝）
	boolPayload := map[string]any{
		"response": map[string]any{"store_items": []any{
			map[string]any{"appid": int64(220), "success": true,
				"best_purchase_option": map[string]any{"final_price_in_cents": "999"}},
		}},
	}
	if ok, reason, _ := ValidateBusiness(boolPayload, 220); ok {
		t.Fatalf("布尔 success 必须拒绝，实际通过（%s）", reason)
	}

	failures := []struct {
		name    string
		payload any
		reason  string
	}{
		{"不是对象", []any{1}, "响应不是 JSON 对象"},
		{"缺信封", map[string]any{}, "缺少 response 信封"},
		{"items不是list", map[string]any{"response": map[string]any{"store_items": "x"}}, "store_items 不是 list"},
		{"appid未命中", map[string]any{"response": map[string]any{"store_items": []any{}}}, "store_items 里没有 appid=220"},
		{"success非1", map[string]any{"response": map[string]any{"store_items": []any{
			map[string]any{"appid": int64(220), "success": int64(2)}}}}, "success 不是整数 1"},
		{"缺购买项", map[string]any{"response": map[string]any{"store_items": []any{
			map[string]any{"appid": int64(220), "success": int64(1)}}}}, "缺少 best_purchase_option"},
		{"价格不可解析", map[string]any{"response": map[string]any{"store_items": []any{
			map[string]any{"appid": int64(220), "success": int64(1),
				"best_purchase_option": map[string]any{"final_price_in_cents": "x"}}}}},
			"final_price_in_cents 无法按 _to_int 语义解析"},
	}
	for _, tc := range failures {
		t.Run(tc.name, func(t *testing.T) {
			ok, reason, _ := ValidateBusiness(tc.payload, 220)
			if ok {
				t.Fatalf("应判定失败")
			}
			if len(reason) < len(tc.reason) || reason[:len(tc.reason)] != tc.reason {
				t.Fatalf("拒因不符：%q", reason)
			}
		})
	}
}
