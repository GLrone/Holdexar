package rates

import (
	"context"
	"net/http"
	"testing"
	"time"
)

func TestHistoryGapsMixed(t *testing.T) {
	db, mux := newMux(t)
	overrideToday(t, "2026-10-31")
	seedHistory(t, db, []historyRow{
		{Code: "USD", FetchedAt: "2026-10-01 01:00:00.000000", Source: "backfill"},
		{Code: "USD", RateDate: "2026-10-01", Kind: "observed", Source: "frankfurter"},
		{Code: "USD", RateDate: "2026-10-03", Kind: "observed", Source: "augmentedsteam"},
		{Code: "USD", RateDate: "2026-10-02", Kind: "carried", Source: "backfill"},
		{Code: "USD", FetchedAt: "2026-10-20 04:00:00.000000", Kind: "observed", Source: "augmentedsteam"},
		{Code: "XXX", RateDate: "2026-10-01", Kind: "observed", Source: "x"},
		{Code: "EUR", RateDate: "2026-10-05", Source: "bing.currencyapi"},
		{Code: "EUR", FetchedAt: "2026-10-06 02:00:00.000000", Kind: "observed", Source: "augmentedsteam"},
	})

	rec := do(t, mux, http.MethodGet, "/api/v1/rates/history/gaps")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `{"currencies":{` +
		`"EUR":{"missing":24,"carried":1,"first":"2026-10-05","last":"2026-10-30"},` +
		`"USD":{"missing":26,"carried":1,"first":"2026-10-02","last":"2026-10-30"}},` +
		`"windows":[{"start":"2026-10-02","end":"2026-10-30","days":29}],` +
		`"totalDays":28,"totalPairs":52,"horizon":"2026-10-30"}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("响应体不一致\n got: %s\nwant: %s", got, want)
	}
}

func TestScanGapsBootstrap(t *testing.T) {
	cases := []struct {
		name  string
		seedX bool
	}{
		{"空库", false},
		{"仅白名单外行", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			db, _ := newMux(t)
			if tc.seedX {
				seedHistory(t, db, []historyRow{
					{Code: "XXX", RateDate: "2026-10-01", Kind: "observed", Source: "x"},
				})
			}
			today, _ := time.Parse("2006-01-02", "2026-10-31")
			result, err := scanHistoryGaps(context.Background(), db, today)
			if err != nil {
				t.Fatalf("扫描失败：%v", err)
			}
			spanDays := diffDays(fullHistoryStart, "2026-10-30") + 1
			if len(result.Currencies) != len(allowedCurrencies) {
				t.Fatalf("币种数 = %d, want %d", len(result.Currencies), len(allowedCurrencies))
			}
			if len(allowedCurrencies) != 39 {
				t.Fatalf("白名单应为 39 币, got %d", len(allowedCurrencies))
			}
			usd := result.Currencies["USD"]
			if usd.First != fullHistoryStart || usd.Last != "2026-10-30" {
				t.Fatalf("USD 首末日 = %s..%s", usd.First, usd.Last)
			}
			if usd.Missing != spanDays || usd.Carried != 0 {
				t.Fatalf("USD 缺行/延续 = %d/%d, want %d/0", usd.Missing, usd.Carried, spanDays)
			}
			if result.TotalDays != spanDays || result.TotalPairs != spanDays*len(allowedCurrencies) {
				t.Fatalf("totalDays/totalPairs = %d/%d, want %d/%d",
					result.TotalDays, result.TotalPairs, spanDays, spanDays*len(allowedCurrencies))
			}
			if result.Horizon != "2026-10-30" {
				t.Fatalf("horizon = %s", result.Horizon)
			}
			wantWindows := (spanDays + maxWindowDays - 1) / maxWindowDays
			if len(result.Windows) != wantWindows {
				t.Fatalf("窗口数 = %d, want %d", len(result.Windows), wantWindows)
			}
			if result.Windows[0].Start != fullHistoryStart {
				t.Fatalf("首窗起点 = %s", result.Windows[0].Start)
			}
			last := result.Windows[len(result.Windows)-1]
			if last.End != "2026-10-30" {
				t.Fatalf("末窗终点 = %s", last.End)
			}
			covered := 0
			for i, win := range result.Windows {
				if win.Days > maxWindowDays {
					t.Fatalf("窗口 %d 超上限：%d 天", i, win.Days)
				}
				if win.Days != diffDays(win.Start, win.End)+1 {
					t.Fatalf("窗口 %d 天数与起止不符", i)
				}
				if i > 0 && result.Windows[i-1].End != addDays(win.Start, -1) {
					t.Fatalf("窗口 %d 与前窗不衔接", i)
				}
				covered += win.Days
			}
			if covered != spanDays {
				t.Fatalf("窗口覆盖 %d 天, want %d", covered, spanDays)
			}
		})
	}
}

func TestScanGapsNoPending(t *testing.T) {
	db, mux := newMux(t)
	overrideToday(t, "2026-10-31")
	rows := make([]historyRow, 0, 30)
	for d := 1; d <= 30; d++ {
		rows = append(rows, historyRow{
			Code: "USD", RateDate: day202610(d), Kind: "observed", Source: "augmentedsteam",
		})
	}
	seedHistory(t, db, rows)
	rec := do(t, mux, http.MethodGet, "/api/v1/rates/history/gaps")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `{"currencies":{},"windows":[],"totalDays":0,"totalPairs":0,"horizon":"2026-10-30"}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("响应体 = %s", got)
	}
}

func day202610(d int) string {
	if d < 10 {
		return "2026-10-0" + string(rune('0'+d))
	}
	return "2026-10-" + string(rune('0'+d/10)) + string(rune('0'+d%10))
}

func TestScanGapsFutureOnlyRowSkipped(t *testing.T) {
	db, mux := newMux(t)
	overrideToday(t, "2026-10-31")
	seedHistory(t, db, []historyRow{
		{Code: "USD", RateDate: "2026-10-31", Kind: "observed", Source: "augmentedsteam"},
	})
	rec := do(t, mux, http.MethodGet, "/api/v1/rates/history/gaps")
	if rec.Code != http.StatusOK {
		t.Fatalf("状态码 = %d, want 200", rec.Code)
	}
	want := `{"currencies":{},"windows":[],"totalDays":0,"totalPairs":0,"horizon":"2026-10-30"}`
	if got := rec.Body.String(); got != want {
		t.Fatalf("响应体 = %s", got)
	}
}

func TestHistoryGapsMethodFallback(t *testing.T) {
	_, mux := newMux(t)
	rec := do(t, mux, http.MethodPost, "/api/v1/rates/history/gaps")
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("状态码 = %d, want 405", rec.Code)
	}
}
