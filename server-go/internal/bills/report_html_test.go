package bills

import (
	"reflect"
	"testing"
)

func TestHistoryHTMLFieldsAndRepeatedRows(t *testing.T) {
	const row = `<tr class='row wallet_table_row'>
		<td class='wht_date'>2026 年 <span>9 月</span> 10 日</td>
		<td class='wht_items'>
			<div><a>Game &amp; <b>DLC</b></a><span>礼物已发送给 <a data-miniprofile='7'><b>Alice</b> &amp; Bob</a></span><div class='wth_item_refunded'>退款</div></div>
			<div>Bonus</div><div>Bonus</div><div class='wth_payment'>付款方式</div>
		</td>
		<td class='wht_type'><div>礼物购买</div><div>钱包</div><div class='wth_payment'>重复支付标签</div><div><b>万事达卡</b> **52</div></td>
		<td class='wht_total'><del>¥120</del> <span>¥30</span><span class='wht_discount_pct'><b>-75%</b></span></td>
		<td class='wht_wallet_change'>-¥10</td><td class='wht_wallet_balance'>¥&nbsp;50</td>
		<td class='wht_base_price'>¥35</td>
	</tr>`
	const unrelated = `<table><tr class='wallet_table_row'><td class='wht_date'>2026-01-01</td></tr></table>`
	got, err := parseHistoryHTML(unrelated+`<table class='wallet_history_table'><tbody>`+row+row+`</tbody></table>`, true)
	if err != nil {
		t.Fatal(err)
	}
	want := historyRow{
		Date: "2026 年 9 月 10 日", Item: "Game & DLC 礼物已发送给 Alice & Bob, Bonus",
		Type: "礼物购买", Payment: "钱包 + 万事达卡 **52", Total: "¥30", WalletChange: "-¥10", WalletBalance: "¥\u00a050",
		BasePrice: "¥35", OriginalPrice: "¥120", Discount: "-75%", TypeCount: 1, GiftRecipients: []string{"Alice & Bob"},
	}
	if !reflect.DeepEqual(got, []historyRow{want, want}) {
		t.Fatalf("history = %+v, want two copies of %+v", got, want)
	}
}

func TestHistoryHTMLFragment(t *testing.T) {
	const topup = `<tr class="wallet_table_row">
		<td class="wht_date">2026-09-09</td>
		<td class="wht_items">已购买 A$ 35.00 钱包资金 <div class="wth_payment">不计入物品</div></td>
		<td class="wht_type"><div>购买</div><div>支付宝</div></td><td class="wht_base_price">A$ 35.00</td>
	</tr>`
	const market = `<tr class="wallet_table_row"><td class="wht_date">2026-09-08</td>
		<td class="wht_type"><div>6&nbsp;Market Transactions</div></td>
		<td class="wht_total"><s>¥75</s><span class="wht_original_price"><b>¥80</b></span>¥60<span class="wht_discount">-25%</span></td>
	</tr>`
	for _, page := range []string{topup + market, `<tbody>` + topup + market + `</tbody>`, `<table>` + topup + market + `</table>`} {
		got, err := parseHistoryHTML(page, false)
		want := []historyRow{
			{Date: "2026-09-09", Item: "已购买 A$ 35.00 钱包资金", Type: "购买", Payment: "支付宝", BasePrice: "A$ 35.00", TypeCount: 1},
			{Date: "2026-09-08", Type: "6\u00a0Market Transactions", TypeCount: 6, Total: "¥60", OriginalPrice: "¥80", Discount: "-25%"},
		}
		if err != nil || !reflect.DeepEqual(got, want) {
			t.Fatalf("fragment history = %+v, %v, want %+v", got, err, want)
		}
	}
}

func TestLicenseHTMLFields(t *testing.T) {
	const first = `<tr>
		<td class="license_date_col">2026 年 1 月 6 日</td>
		<td class="steamdb_license_id_col">987654</td>
		<td><a href="https://store.steampowered.com/app/440/"><b>Team Fortress</b> 2</a>
			<a class="free_license_remove_link" href="/app/1234/">移除</a></td>
		<td class="license_acquisition_col">免费赠送</td>
	</tr>`
	const second = `<tr><td class="license_date_col">2026 年 1 月 7 日</td>
		<td><a href="/sub/456/">Some Retail Key</a><a href="https://example.com/app/789/"></a></td>
		<td class="license_acquisition_col">零售</td></tr>`
	const next = `<a href="?continuationToken=a&amp;page=2" class="license_paginator_next enabled">Next</a>`
	for _, table := range []string{`<table class="account_table">`, `<table>`} {
		page := table + `<tr><th>Date</th></tr>` + first + second + `<tr><td colspan="3">No licenses found</td></tr></table>` + next
		got, gotNext, err := parseLicenseHTML(page)
		var appID int64 = 440
		want := []licenseRow{{Date: "2026 年 1 月 6 日", Item: "Team Fortress 2", Method: "免费赠送", AppID: &appID}, {Date: "2026 年 1 月 7 日", Item: "Some Retail Key", Method: "零售"}}
		if err != nil || !reflect.DeepEqual(got, want) || gotNext != "?continuationToken=a&page=2" {
			t.Fatalf("licenses = %+v, next = %q, err = %v, want %+v and a decoded next href", got, gotNext, err, want)
		}
	}
}

func TestEmptyAndMalformedReportHTML(t *testing.T) {
	for _, tc := range []struct {
		page      string
		firstPage bool
		wantError bool
	}{
		{`<table class="wallet_history_table"><tr><th>Date</th></tr></table>`, true, false},
		{`<html><p>Unrelated page</p></html>`, true, true},
		{`<table class="wallet_history_table"><tr class="wallet_table_row"><td>Missing date class</td></tr></table>`, true, true},
		{"", false, false},
	} {
		got, err := parseHistoryHTML(tc.page, tc.firstPage)
		if (err != nil) != tc.wantError || len(got) != 0 {
			t.Fatalf("history for %q = %+v, %v, want empty and error=%v", tc.page, got, err, tc.wantError)
		}
	}
	for _, tc := range []struct {
		page      string
		wantError bool
	}{
		{`<table class="account_table"><tr><td colspan="3">No licenses found</td></tr></table>`, false},
		{`<table><tr><th>Date</th></tr></table>`, true},
		{`<table class="account_table"><tr><td class="license_date_col">2026-01-01</td><td>Missing method cell</td></tr></table>`, true},
	} {
		got, next, err := parseLicenseHTML(tc.page)
		if (err != nil) != tc.wantError || len(got) != 0 || next != "" {
			t.Fatalf("licenses for %q = %+v, %q, %v, want empty and error=%v", tc.page, got, next, err, tc.wantError)
		}
	}
}

func TestReportRejectsMalformedSiblingOfValidRow(t *testing.T) {
	history := `<tr class="wallet_table_row"><td class="wht_date">2026-01-02</td><td class="wht_type"><div>购买</div></td><td class="wht_total">¥12</td></tr>`
	for _, broken := range []string{`<tr class="wallet_table_row"><td>Truncated</td></tr>`, `<tr class="wallet_table_row"><td class="wht_date">2026-01-01</td></tr>`} {
		if _, err := parseHistoryHTML(history+broken, false); err == nil {
			t.Fatal("history accepted a partial page")
		}
	}
	license := `<tr><td class="license_date_col">2026-01-02</td><td>Key</td><td class="license_acquisition_col">Retail</td></tr>`
	broken := `<tr><td class="license_date_col">2026-01-01</td><td>Truncated</td></tr>`
	for _, table := range []string{`<table class="account_table">`, `<table>`} {
		if _, _, err := parseLicenseHTML(table + license + broken + `</table>`); err == nil {
			t.Fatal("licenses accepted a partial page")
		}
	}
}

func TestIdentityHTMLAvatarDomains(t *testing.T) {
	for _, avatar := range []string{
		"https://avatars.fastly.steamstatic.com/abc_full.jpg",
		"https://avatars.akamaized.net/abc_full.jpg",
		"https://avatars.steamstatic.com/abc.png",
		"",
	} {
		page := `<div id='account_pulldown'><span>测试昵称</span> &amp; A</div><img src='https://store.steampowered.com/logo.jpg'><img src='` + avatar + `'>`
		if avatar != "" {
			page += `<img src='https://avatars.steamstatic.com/other.png'>`
		}
		nick, got, err := parseIdentityHTML(page)
		if err != nil || nick != "测试昵称 & A" || got != avatar {
			t.Fatalf("identity = %q, %q, %v, want nickname and avatar %q", nick, got, err, avatar)
		}
	}
}
