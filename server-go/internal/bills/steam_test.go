package bills

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"
)

const firstBill = `<table class="wallet_history_table"><tr class="wallet_table_row"><td class="wht_date">2026 年 1 月 6 日</td><td class="wht_items"><div>Sample game</div></td><td class="wht_type"><div>购买</div></td><td class="wht_total">¥12.00</td></tr></table>`
const firstLicense = `<table class="account_table"><tr><td class="license_date_col">2026 年 1 月 5 日</td><td><a href="https://store.steampowered.com/app/123/">Sample license</a></td><td class="license_acquisition_col">零售</td></tr></table>`

func TestSteamBillsTransportUsesCurrentStoreSessionAndBothPaginators(t *testing.T) {
	var first, more, licenses atomic.Int32
	store := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		cookie, err := r.Cookie("steamLoginSecure")
		if err != nil || cookie.Value != "76561198000000001%7C%7Ctoken" {
			t.Errorf("account cookie not scoped to test owner")
			http.Error(w, "wrong account", 403)
			return
		}
		switch r.URL.Path {
		case "/account/":
			fmt.Fprint(w, `<a id="account_pulldown">Sample owner</a>`)
		case historyPath:
			first.Add(1)
			http.SetCookie(w, &http.Cookie{Name: "sessionid", Value: "rotated", Path: "/"})
			fmt.Fprint(w, firstBill+`<script>g_historyCursor = {"wallet_txnid": 9223372036854775807, "lasttime": 12};</script>`)
		case historyMorePath:
			more.Add(1)
			if r.Method != http.MethodPost || r.FormValue("cursor[wallet_txnid]") != "9223372036854775807" || r.FormValue("cursor[lasttime]") != "12" || r.FormValue("sessionid") != "rotated" {
				t.Errorf("history did not send exact deep-form cursor and session")
			}
			fmt.Fprint(w, `{"html":"<tr class=\"wallet_table_row\"><td class=\"wht_date\">2026 年 1 月 5 日</td><td class=\"wht_type\">购买</td></tr>","cursor":null}`)
		case licensesPath:
			licenses.Add(1)
			fmt.Fprint(w, firstLicense)
			if r.URL.RawQuery == "" {
				fmt.Fprint(w, `<a class="license_paginator_next" href="?continuationToken=a&amp;x=1">next</a>`)
			} else if r.URL.Query().Get("continuationToken") != "a" || r.URL.Query().Get("x") != "1" {
				t.Errorf("license continuation not decoded: %s", r.URL.RawQuery)
			}
		default:
			http.NotFound(w, r)
		}
	}))
	defer store.Close()
	ctx := context.Background()
	base, _ := url.Parse(store.URL)
	c, err := newBillSteamClient(base, store.Client(), "steamLoginSecure=76561198000000001%7C%7Ctoken; sessionid=original", func(context.Context, time.Duration) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	page, unchanged, err := c.probe(ctx, "2026-01-05")
	if unchanged || err != nil || page == "" {
		t.Fatalf("new first page: %v %v", unchanged, err)
	}
	r, err := c.report(ctx, "76561198000000001", page, func(map[string]any) {})
	if err != nil || len(r.History) != 2 || len(r.Licenses) != 2 || r.Nickname != "Sample owner" || first.Load() != 1 || more.Load() != 1 || licenses.Load() != 2 {
		t.Fatalf("unexpected report %#v calls=%d,%d,%d err=%v", r, first.Load(), more.Load(), licenses.Load(), err)
	}
}

func TestSteamBillsTransportRejectsExpiredAndForeignPagination(t *testing.T) {
	foreign := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Error("foreign host must not receive an authenticated request")
	}))
	defer foreign.Close()
	var expired atomic.Bool
	expired.Store(true)
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case historyPath:
			if expired.Load() {
				http.Redirect(w, r, "/login/?redir=account", http.StatusFound)
				return
			}
			fmt.Fprint(w, firstBill)
		case licensesPath:
			fmt.Fprint(w, firstLicense+`<a class="license_paginator_next" href="`+foreign.URL+licensesPath+`">next</a>`)
		case "/login/":
			t.Error("login redirect should be detected before following")
		default:
			fmt.Fprint(w, `<a id="account_pulldown">Sample owner</a>`)
		}
	}))
	defer server.Close()
	base, _ := url.Parse(server.URL)
	c, err := newBillSteamClient(base, server.Client(), "steamLoginSecure=76561198000000001%7C%7Ctoken", func(context.Context, time.Duration) error { return nil })
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err = c.probe(context.Background(), "2026-01-05"); !errors.Is(err, errStoreExpired) {
		t.Fatalf("login redirect error: %v", err)
	}
	expired.Store(false)
	if _, err = c.report(context.Background(), "76561198000000001", "", func(map[string]any) {}); err == nil {
		t.Fatal("foreign next link accepted")
	}
}

func TestHistoryRejectsMalformedInitialCursor(t *testing.T) {
	client := billSteamClient{}
	_, err := client.history(context.Background(), firstBill+`<script>g_historyCursor = {"txnid":`+`</script>`, func(map[string]any) {})
	if err == nil {
		t.Fatal("truncated cursor was accepted as a complete history")
	}
}

func TestSteamBillsRejectsCookieOwnerChangeOnSameStore(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/account/" {
			t.Error("made a protected follow-up request after owner changed")
		}
		http.SetCookie(w, &http.Cookie{Name: "steamLoginSecure", Value: "76561198000000002%7C%7Cother", Path: "/"})
		fmt.Fprint(w, `<a id="account_pulldown">unexpected owner</a>`)
	}))
	defer server.Close()
	base, _ := url.Parse(server.URL)
	client, err := newBillSteamClient(base, server.Client(), "steamLoginSecure=76561198000000001%7C%7Ctoken", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := client.report(context.Background(), "76561198000000001", "", func(map[string]any) {}); !errors.Is(err, errStoreOwner) {
		t.Fatalf("cross-account cookie was not rejected: %v", err)
	}
}
