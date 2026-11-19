package rates

import (
	"database/sql"
	"fmt"
	"math"
	"net/http"
	"strings"
	"time"

	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/timeutil"
)

// allowedCurrencies 参与缺口扫描、历史修复与汇率刷新清理的 39 个白名单币种。
var allowedCurrencies = map[string]bool{
	"AED": true, "ARS": true, "AUD": true, "BRL": true, "CAD": true,
	"CHF": true, "CLP": true, "CNY": true, "COP": true, "CRC": true,
	"EUR": true, "GBP": true, "HKD": true, "IDR": true, "ILS": true,
	"INR": true, "JPY": true, "KRW": true, "KWD": true, "KZT": true,
	"MXN": true, "MYR": true, "NOK": true, "NZD": true, "PEN": true,
	"PHP": true, "PLN": true, "QAR": true, "RUB": true, "SAR": true,
	"SGD": true, "THB": true, "TRY": true, "TWD": true, "UAH": true,
	"USD": true, "UYU": true, "VND": true, "ZAR": true,
}

var rangeMonths = map[string]int{"1mo": 1, "6mo": 6, "1y": 12, "5y": 60, "10y": 120}

var rangeCutoffDays = func() map[string]int {
	m := make(map[string]int, len(rangeMonths))
	for k, months := range rangeMonths {
		m[k] = int(math.Round(float64(months) * 30.44))
	}
	return m
}()

var beijingZone = timeutil.ZoneCST

func beijingToday() time.Time {
	t := time.Now().In(beijingZone)
	y, m, d := t.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

var beijingTodayFn = beijingToday

func Register(mux *http.ServeMux, db *sql.DB, services ...*Service) {
	h := &handler{db: db}
	mux.HandleFunc("GET /api/v1/rates", h.listRates)
	mux.HandleFunc("GET /api/v1/rates/history", h.rateHistory)
	mux.HandleFunc("GET /api/v1/rates/history/gaps", h.historyGaps)
	if len(services) > 0 {
		services[0].registerWrites(mux)
	}
	for _, p := range []string{"/api/v1/rates", "/api/v1/rates/history", "/api/v1/rates/history/gaps"} {
		mux.HandleFunc(p, httpx.MethodNotAllowed("GET"))
	}
}

type handler struct {
	db *sql.DB
}

type rateItem struct {
	Currency  string  `json:"currency"`
	RateToCny float64 `json:"rateToCny"`
	FetchedAt *string `json:"fetchedAt"`
}

type ratesResponse struct {
	Rates []rateItem `json:"rates"`
}

func (h *handler) listRates(w http.ResponseWriter, r *http.Request) {
	rows, err := h.db.QueryContext(r.Context(),
		// CAST 回文本：DATETIME 声明列会被驱动解析成 time.Time
		"SELECT currency_code, rate_to_cny, CAST(fetched_at AS TEXT) FROM fx_rates ORDER BY currency_code")
	if err != nil {
		http.Error(w, fmt.Sprintf("汇率查询失败：%v", err), http.StatusInternalServerError)
		return
	}
	defer rows.Close()
	items := []rateItem{}
	for rows.Next() {
		var it rateItem
		var fetched sql.NullString
		if err := rows.Scan(&it.Currency, &it.RateToCny, &fetched); err != nil {
			http.Error(w, fmt.Sprintf("汇率读取失败：%v", err), http.StatusInternalServerError)
			return
		}
		it.FetchedAt = isoformatPtr(fetched)
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		http.Error(w, fmt.Sprintf("汇率读取失败：%v", err), http.StatusInternalServerError)
		return
	}
	httpx.WriteJSON(w, http.StatusOK, ratesResponse{Rates: items})
}

type historyItem struct {
	Date       string  `json:"date"`
	RateToCny  float64 `json:"rateToCny"`
	Source     *string `json:"source"`
	SourceKind *string `json:"sourceKind"`
	FetchedAt  *string `json:"fetchedAt"`
}

func (h *handler) rateHistory(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	currency := "USD"
	if vals, ok := q["currency"]; ok {
		currency = strings.ToUpper(vals[0])
	}
	limitN, _, limitErr := httpx.QueryInt(q, "limit", 0, nil, nil)
	if limitErr != nil {
		httpx.WriteValidationError(w, *limitErr)
		return
	}
	limit := int(limitN)
	rangeArg := ""
	hasRange := false
	if vals, ok := q["range"]; ok {
		rangeArg, hasRange = vals[0], true
	}

	rows, err := h.db.QueryContext(r.Context(),
		// CAST 回文本：DATE/DATETIME 声明列会被驱动解析成 time.Time
		"SELECT CAST(rate_date AS TEXT), rate_to_cny, source, source_kind, CAST(fetched_at AS TEXT)"+
			" FROM fx_rate_history WHERE currency_code = ? AND rate_date IS NOT NULL ORDER BY rate_date", currency)
	if err != nil {
		http.Error(w, fmt.Sprintf("汇率历史查询失败：%v", err), http.StatusInternalServerError)
		return
	}
	defer rows.Close()
	items := []historyItem{}
	for rows.Next() {
		var it historyItem
		var rateDate string
		var source, kind, fetched sql.NullString
		if err := rows.Scan(&rateDate, &it.RateToCny, &source, &kind, &fetched); err != nil {
			http.Error(w, fmt.Sprintf("汇率历史读取失败：%v", err), http.StatusInternalServerError)
			return
		}
		it.Date = rateDate
		it.Source = nullStrPtr(source)
		it.SourceKind = nullStrPtr(kind)
		it.FetchedAt = isoformatPtr(fetched)
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		http.Error(w, fmt.Sprintf("汇率历史读取失败：%v", err), http.StatusInternalServerError)
		return
	}

	if hasRange {
		key := strings.ToLower(rangeArg)
		days, ok := rangeCutoffDays[key]
		if !ok && key != "all" {
			httpx.WriteJSON(w, http.StatusBadRequest,
				map[string]string{"detail": fmt.Sprintf("无效的时间范围: %s", rangeArg)})
			return
		}
		if ok {
			cutoff := beijingTodayFn().AddDate(0, 0, -days).Format("2006-01-02")
			kept := items[:0]
			for _, it := range items {
				if it.Date >= cutoff {
					kept = append(kept, it)
				}
			}
			items = kept
		}
	}
	if limit > 0 && len(items) > limit {
		items = items[len(items)-limit:]
	}
	httpx.WriteJSON(w, http.StatusOK, items)
}

func nullStrPtr(v sql.NullString) *string {
	if !v.Valid || v.String == "" {
		return nil
	}
	s := v.String
	return &s
}

func isoformatPtr(v sql.NullString) *string {
	if !v.Valid {
		return nil
	}
	if s, ok := pyIsoformat(v.String); ok {
		return &s
	}
	s := v.String
	return &s
}

// pyIsoformat 转为 T 分隔 ISO 文本，微秒全零省略、非零补足 6 位，不可解析原文返回。
func pyIsoformat(s string) (string, bool) {
	if len(s) < 19 {
		return s, false
	}
	t, err := time.Parse("2006-01-02 15:04:05", s[:19])
	if err != nil {
		if t, err = time.Parse("2006-01-02T15:04:05", s[:19]); err != nil {
			return s, false
		}
	}
	out := t.Format("2006-01-02T15:04:05")
	if len(s) > 19 && s[19] == '.' {
		frac := s[20:]
		if len(frac) > 6 {
			frac = frac[:6]
		}
		for len(frac) < 6 {
			frac += "0"
		}
		if frac != "000000" {
			out += "." + frac
		}
	}
	return out, true
}
