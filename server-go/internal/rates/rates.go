// Package rates：汇率只读查询（实时快照 / 日线历史 / 缺口扫描）。
// 刷新与历史修复（POST，触网+写库）未迁移，见 deferred 清单。
package rates

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// allowedCurrencies 币种白名单：41 区清单币种去重（37 币，源清单
// server/app/crawler/config.py CC_LIST）加永久预留 TRY/ARS；
// 缺口扫描与历史查询过滤只认这批，区服清单增减时须同步。
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

// rangeMonths 时间范围档位 → 月数。
var rangeMonths = map[string]int{"1mo": 1, "6mo": 6, "1y": 12, "5y": 60, "10y": 120}

// rangeCutoffDays 月数折算天数：round(月数 × 30.44)，窗口过滤无需日历级月末精确。
var rangeCutoffDays = func() map[string]int {
	m := make(map[string]int, len(rangeMonths))
	for k, months := range rangeMonths {
		m[k] = int(math.Round(float64(months) * 30.44))
	}
	return m
}()

// beijingZone 北京时区现行无夏令时，固定 +8 与 Asia/Shanghai 等价。
var beijingZone = time.FixedZone("CST", 8*3600)

// beijingToday 北京日历日（UTC 零点归一，便于日差运算）。
func beijingToday() time.Time {
	t := time.Now().In(beijingZone)
	y, m, d := t.Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

// beijingTodayFn 时钟入口，测试可替换。
var beijingTodayFn = beijingToday

// Register 挂载 rates 只读路由；POST refresh / history/repair 未迁移。
func Register(mux *http.ServeMux, db *sql.DB) {
	h := &handler{db: db}
	mux.HandleFunc("GET /api/v1/rates", h.listRates)
	mux.HandleFunc("GET /api/v1/rates/history", h.rateHistory)
	mux.HandleFunc("GET /api/v1/rates/history/gaps", h.historyGaps)
	for _, p := range []string{"/api/v1/rates", "/api/v1/rates/history", "/api/v1/rates/history/gaps"} {
		mux.HandleFunc(p, methodNotAllowed("GET"))
	}
}

type handler struct {
	db *sql.DB
}

// rateItem 汇率快照行。
type rateItem struct {
	Currency  string  `json:"currency"`
	RateToCny pyFloat `json:"rateToCny"`
	FetchedAt *string `json:"fetchedAt"`
}

type ratesResponse struct {
	Rates []rateItem `json:"rates"`
}

// listRates 全部币种快照，按币种代码排序。
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
	writeJSON(w, http.StatusOK, ratesResponse{Rates: items})
}

// historyItem 日线历史行。
type historyItem struct {
	Date       string  `json:"date"`
	RateToCny  pyFloat `json:"rateToCny"`
	Source     *string `json:"source"`
	SourceKind *string `json:"sourceKind"`
	FetchedAt  *string `json:"fetchedAt"`
}

// rateHistory 单币种日线序列，按 rate_date 升序；range 六档窗口过滤，
// limit > 0 再取尾段 limit 天。
func (h *handler) rateHistory(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	currency := "USD"
	if vals, ok := q["currency"]; ok {
		currency = strings.ToUpper(vals[0])
	}
	limit := 0
	if vals, ok := q["limit"]; ok {
		n, err := strconv.Atoi(vals[0])
		if err != nil {
			writeJSON(w, http.StatusUnprocessableEntity, intParseError("limit", vals[0]))
			return
		}
		limit = n
	}
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
			writeJSON(w, http.StatusBadRequest,
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
	writeJSON(w, http.StatusOK, items)
}

// validationError 查询参数校验失败明细（对齐 pydantic v2 形态）。
type validationError struct {
	Type  string   `json:"type"`
	Loc   []string `json:"loc"`
	Msg   string   `json:"msg"`
	Input string   `json:"input"`
}

// intParseError 整数解析失败的 422 响应体。
func intParseError(field, input string) map[string]any {
	return map[string]any{
		"detail": []validationError{{
			Type:  "int_parsing",
			Loc:   []string{"query", field},
			Msg:   "Input should be a valid integer, unable to parse string as an integer",
			Input: input,
		}},
	}
}

// nullStrPtr 文本列 NULL → nil，空串也视为缺失。
func nullStrPtr(v sql.NullString) *string {
	if !v.Valid || v.String == "" {
		return nil
	}
	s := v.String
	return &s
}

// isoformatPtr SQLite 时间文本 → Python naive isoformat 形态；NULL → nil。
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

// pyIsoformat 「YYYY-MM-DD[ T]HH:MM:SS[.frac]」→ T 分隔 ISO 文本：
// 微秒整零省略，非整零恒 6 位（Python isoformat 语义）。
// 不可解析的文本原样返回。
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

// methodNotAllowed 非声明方法的回退响应（与 regions 包各自持有同款工具，包间不互引）。
func methodNotAllowed(allow string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Allow", allow)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "Method Not Allowed"})
	}
}

// writeJSON JSON 响应体不带尾随换行。
func writeJSON(w http.ResponseWriter, status int, payload any) {
	body, err := json.Marshal(payload)
	if err != nil {
		http.Error(w, "响应序列化失败", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(body)
}
