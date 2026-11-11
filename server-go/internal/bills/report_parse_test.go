package bills

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

func TestReportMoney(t *testing.T) {
	for _, tc := range []struct {
		raw      string
		amount   float64
		currency string
	}{
		{"790₸", 790, "KZT"},
		{"960,70₸ 资金", 960.7, "KZT"},
		{"$0.02 USD Credit", 0.02, "USD"},
		{"20,--€", 20, "EUR"},
		{"A$ 35.00", 35, "AUD"},
		{"NT$ 650", 650, "TWD"},
		{"¥ 60.00", 60, "CNY"},
		{"-790₸", -790, "KZT"},
		{"CDN$ 49.99", 49.99, "CAD"},
		{"1 365₸", 1365, "KZT"},
		{"-CHF\u202f1,235.60", -1235.6, "CHF"},
		{"CHF 12,50 钱包资金", 12.5, "CHF"},
		{"1\u00a0235,60 CHF", 1235.6, "CHF"},
		{"CLP$ 3.999", 3.999, "CLP"},
		{"123", 0, ""},
		{"CHF 0.00", 0, "CHF"},
	} {
		t.Run(tc.raw, func(t *testing.T) {
			amount, currency := parseReportMoney(tc.raw)
			if amount != tc.amount || currency != tc.currency {
				t.Fatalf("got (%v, %q), want (%v, %q)", amount, currency, tc.amount, tc.currency)
			}
		})
	}
}

func TestReportDate(t *testing.T) {
	for raw, want := range map[string]string{
		"2026 年 3 月 17 日":        "2026-03-17",
		"结算 2026 年\u30003 月 7 日": "2026-03-07",
		"23 fEB, 2026":           "2026-02-23",
		"2026-03-17 18:25":       "2026-03-17",
		"  Steam  Date ":         "Steam  Date",
		"23 FOO, 2026":           "23 FOO, 2026",
		"2026 年 13 月 35 日":       "2026-13-35",
		"":                       "",
	} {
		if got := parseDate(raw); got != want {
			t.Errorf("parseDate(%q) = %q, want %q", raw, got, want)
		}
	}
}

func TestReportWalletAmountPriority(t *testing.T) {
	report := parseReport(steamReport{History: []historyRow{
		{Date: "10 Aug, 2025", Item: "Purchased Wallet Credit", Type: "Purchase", Total: "CHF 5,50", WalletChange: "+CHF 6", BasePrice: "CHF 8"},
		{Date: "10 Aug, 2025", Item: "Cross Currency Wallet Credit", Type: "Purchase", Total: "$10", WalletChange: "+€9", BasePrice: "A$ 16"},
		{Date: "10 Aug, 2025", Item: "已购买钱包资金", Type: "购买", WalletChange: "+€11", BasePrice: "A$ 16"},
		{Date: "10 Aug, 2025", Item: "已购买 A$ 35.00 钱包资金", Type: "购买", BasePrice: "A$ 35.00"},
		{Date: "11 Aug, 2025", Item: "Wallet Credit 退款", Type: "Refund", WalletChange: "-CHF 10"},
		{Date: "12 Aug, 2025", Item: "钱包资金", Type: "购买", Total: "¥0", WalletChange: "+¥3"},
		{Date: "12 Aug, 2025", Item: "Wallet Credit", Type: "Conversion", Total: "$15"},
		{Date: "12 Aug, 2025", Item: "Wallet Credit", Type: "Recent Purchase", Total: "$15"},
		{Date: "13 Aug, 2025", Total: "$0.02 USD Credit", Payment: "  Other\u3000Payment "},
	}})
	want := []topupTx{
		{Date: "2025-08-13", Desc: "钱包充值", Amount: 0.02, Sign: 1, Currency: "USD", Payment: "Other Payment", Type: "钱包充值"},
		{Date: "2025-08-11", Desc: "Wallet Credit 退款", Amount: 10, Sign: -1, Currency: "CHF", Type: "充值退款", Refund: true},
		{Date: "2025-08-10", Desc: "Purchased Wallet Credit", Amount: 5.5, Sign: 1, Currency: "CHF", Type: "钱包充值"},
		{Date: "2025-08-10", Desc: "Cross Currency Wallet Credit", Amount: 10, Sign: 1, Currency: "USD", Type: "钱包充值"},
		{Date: "2025-08-10", Desc: "已购买钱包资金", Amount: 11, Sign: 1, Currency: "EUR", Type: "钱包充值"},
		{Date: "2025-08-10", Desc: "已购买 A$ 35.00 钱包资金", Amount: 35, Sign: 1, Currency: "AUD", Type: "钱包充值"},
	}
	if !reflect.DeepEqual(report.Topup, want) || len(report.Game) != 0 {
		t.Fatalf("wallet normalization got topups %+v, games %+v", report.Topup, report.Game)
	}
}

func TestReportGiftDedupAndMixedWarning(t *testing.T) {
	report := parseReport(steamReport{Nickname: "  Sample\u3000Name  ", Avatar: "raw", History: []historyRow{
		{Date: "2026-01-02", Item: "Game A, Game B", Type: "Purchase", Total: "¥9.99999"},
		{Date: "2026-01-02", Item: "game a，Game B 礼物已发送给 fallback recipient", Type: "禮物購買", Payment: "  Pay  Pal ", Total: "€10",
			GiftRecipients: []string{"  чьё-то\u00a0имя ", " "}, WalletBalance: "€ 25", BasePrice: "€ 20", OriginalPrice: "20,00", Discount: "-50%", PaymentParts: json.RawMessage(`[{"pm":"card","amount":"€10"}]`)},
		{Date: "2026-01-02", Item: "Game A, Game B", Type: "Gift Purchase", Total: "€10"},
		{Date: "2026-01-02", Item: "Other", Type: "游戏内物品购买", Total: "¥2"},
		{Date: "2026-01-03", Item: "Game A, Game B 退款", Type: "Refund", Total: "$10"},
		{Date: "2026-01-03", Item: "GAME A, GAME B", Type: "Refund", Total: "¥10"},
		{Date: "2026-01-03", Item: "Skin", Type: "6 Market Transactions", Total: "¥10"},
		{Date: "2026-01-03", Item: "Skin", Type: "市场交易", Total: "¥10"},
		{Date: "2026-01-03", Item: "Unpaid", Type: "Purchase", Total: "¥0"},
	}})
	if report.Nickname != "Sample Name" || report.Avatar != "raw" {
		t.Fatalf("account got (%q, %q)", report.Nickname, report.Avatar)
	}
	if len(report.Game) != 3 {
		t.Fatalf("got game transactions %+v", report.Game)
	}
	refund, ingame, gift := report.Game[0], report.Game[1], report.Game[2]
	if refund.RawIdx != 4 || !refund.Refund || !refund.OrigGift || refund.Gift || refund.Sign != -1 || refund.Currency != "USD" {
		t.Fatalf("refund got %+v", refund)
	}
	if ingame.RawIdx != 3 || ingame.Type != "游戏内购买" {
		t.Fatalf("in-game transaction got %+v", ingame)
	}
	if gift.RawIdx != 1 || !gift.Gift || gift.Refund || gift.Sign != 1 || gift.Amount != 10 || gift.Currency != "EUR" ||
		!reflect.DeepEqual(gift.Items, []string{"game a", "Game B"}) || !reflect.DeepEqual(gift.GiftTo, []string{"чьё-то имя"}) ||
		gift.OrigPrice == nil || *gift.OrigPrice != 20 || gift.WalletBalance != "€ 25" || gift.BasePrice != "€ 20" ||
		gift.Payment != "Pay Pal" || gift.Discount != "-50%" || string(gift.PaymentParts) != `[{"pm":"card","amount":"€10"}]` {
		t.Fatalf("gift got %+v", gift)
	}
	if len(report.Warnings) != 1 || !strings.Contains(report.Warnings[0], "2026-01-02 · game a等") {
		t.Fatalf("mixed gift warnings got %+v", report.Warnings)
	}
}

func TestReportRefundConsumesNearestPriorOrLatestFuture(t *testing.T) {
	report := parseReport(steamReport{History: []historyRow{
		{Date: "2026-01-01", Item: "Sample", Type: "Refund", Total: "¥5"},
		{Date: "2026-01-02", Item: "Sample", Type: "Purchase", Total: "¥5"},
		{Date: "2026-01-03", Item: "Sample", Type: "Refund", Total: "¥5"},
		{Date: "2026-01-04", Item: "Sample 礼物已发送给 two words", Type: "Gift Purchase", Total: "¥5"},
		{Date: "2026-01-05", Item: "Sample", Type: "Refund", Total: "¥5"},
		{Date: "2026-01-06", Item: "Other", Type: "Gift Purchase", Total: "¥5"},
		{Date: "2026-01-06", Item: "Other 退款", Type: "Refund", Total: "¥5"},
	}})
	if len(report.Game) != 7 {
		t.Fatalf("got game transactions %+v", report.Game)
	}
	for _, tx := range report.Game {
		switch tx.RawIdx {
		case 0, 6:
			if !tx.OrigGift {
				t.Errorf("refund didn't match gift: %+v", tx)
			}
		case 2, 4:
			if tx.OrigGift {
				t.Errorf("refund incorrectly matched gift: %+v", tx)
			}
		case 3:
			if !reflect.DeepEqual(tx.GiftTo, []string{"two words"}) {
				t.Errorf("gift recipient fallback got %+v", tx.GiftTo)
			}
		}
	}
}

func TestReportIncludesRetailGiftAndFreeLicenses(t *testing.T) {
	appID := int64(440)
	report := parseReport(steamReport{Licenses: []licenseRow{
		{Date: "2026-01-01", Item: "Store", Method: "Steam Store Retail"},
		{Date: "2026-01-02", Item: "Free", Method: "comPLIMENTARY gift"},
		{Date: "2026-01-03", Item: " Retail\u3000Key ", Method: "零售", AppID: &appID},
		{Date: "2026-01-04", Item: "Gift", Method: "Guest\u3000Pass"},
		{Date: "2026-01-05", Item: "Unknown", Method: "Unknown"},
	}})
	want := []licenseTx{
		{Name: "Free", Date: "2026-01-02", Acq: "free", AcqLabel: "免费赠送"},
		{Name: "Retail Key", Date: "2026-01-03", Acq: "cdk", AcqLabel: "零售", AppID: &appID},
		{Name: "Gift", Date: "2026-01-04", Acq: "gift", AcqLabel: "礼物/玩家通行证"},
	}
	if !reflect.DeepEqual(report.Licenses, want) {
		t.Fatalf("got licenses %+v", report.Licenses)
	}
}
