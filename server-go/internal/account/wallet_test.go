package account

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func walletPage(sid, amount, formatted string, currency int) string {
	return fmt.Sprintf(`<div class="accountRow accountBalance"><div class="accountData price"><a>%s</a></div></div><div data-config="{&quot;currency_code&quot;:%d,&quot;formatted_amount&quot;:&quot;%s&quot;}"></div><a href="https://steamcommunity.com/profiles/%s/" class="user_avatar"></a>`, amount, currency, formatted, sid)
}
func TestWalletParsingLocaleCurrencyAndOwner(t *testing.T) {
	const sid = "76561198000000001"
	for _, tc := range []struct {
		amount, format string
		id             int
		balance        float64
		display        string
	}{
		{"¥1,234.56", `\u00a51,234.56`, 23, 1234.56, "¥1,234.56"},
		{"Rp 3.810,21", "Rp 3.810,21", 10, 3810.21, "Rp3,810.21"},
		{"¥1,234", "¥1,234", 8, 1234, "¥1,234"},
	} {
		w, e := ParseWalletPage(walletPage(sid, tc.amount, tc.format, tc.id), sid)
		if e != nil || w.Balance != tc.balance || w.BalanceDisplay != tc.display || w.SteamID != sid {
			t.Fatalf("%+v %v", w, e)
		}
	}
	for _, page := range []string{walletPage("76561198000000002", "¥1.00", "¥1.00", 23), walletPage(sid, "$1.00", "$1.00", 23), walletPage(sid, "¥1.00", "¥1.00", 999), strings.ReplaceAll(walletPage(sid, "¥1.00", "¥1.00", 23), sid, "")} {
		if _, e := ParseWalletPage(page, sid); e == nil {
			t.Fatal("unverified wallet accepted")
		}
	}
}

func TestWalletHTTPKeepsAccountCookiesSeparateAndClassifiesFailures(t *testing.T) {
	const a = "76561198000000001"
	const b = "76561198000000002"
	mode := "ok"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/login/" {
			fmt.Fprint(w, "login")
			return
		}
		switch mode {
		case "rate":
			w.WriteHeader(429)
			return
		case "expired":
			http.Redirect(w, r, "/login/", 302)
			return
		case "mismatch":
			fmt.Fprint(w, walletPage(b, "¥1.00", "¥1.00", 23))
			return
		}
		sid := CookieSteamID(r.Header.Get("Cookie"))
		http.SetCookie(w, &http.Cookie{Name: "steamLoginSecure", Value: b + "%7C%7Creplacement"})
		fmt.Fprint(w, walletPage(sid, "¥12.34", "¥12.34", 23))
	}))
	defer srv.Close()
	c := NewSteamClient(SteamOptions{HTTPClient: srv.Client(), AccountURL: srv.URL + "/account/"})
	for _, sid := range []string{a, b, a} {
		wallet, e := c.FetchWallet(context.Background(), "steamLoginSecure="+sid+"%7C%7Ctoken")
		if e != nil || wallet.SteamID != sid {
			t.Fatalf("crossed account: %+v %v", wallet, e)
		}
	}
	for _, tc := range []struct{ mode, kind string }{{"rate", "rate"}, {"mismatch", "owner"}, {"expired", "expired"}} {
		mode = tc.mode
		_, e := c.FetchWallet(context.Background(), "steamLoginSecure="+a+"%7C%7Ctoken")
		var target *WalletError
		if !errors.As(e, &target) || target.Kind != tc.kind {
			t.Fatalf("%s %v", mode, e)
		}
	}
}

func TestWalletDoesNotBypassCertificateValidation(t *testing.T) {
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { t.Error("untrusted connection delivered credentials") }))
	defer srv.Close()
	c := NewSteamClient(SteamOptions{AccountURL: srv.URL + "/account/"})
	defer c.Close()
	_, e := c.FetchWallet(context.Background(), "steamLoginSecure=76561198000000001||fixture-only")
	var target *WalletError
	if !errors.As(e, &target) || target.Kind != "tls" {
		t.Fatalf("%v", e)
	}
}
