package rates

import (
	"context"
	"database/sql"
	"net/http"
	"sort"
	"strings"
	"time"
)

// 历史缺口扫描参数（纯本地零网络）。
const (
	// clusterGapDays 相邻待修复日间隔 ≤ 该值并入同一窗口段。
	clusterGapDays = 7
	// fullHistoryStart 全新安装 bootstrap 锚点 = Provider 全历史起点。
	fullHistoryStart = "2012-01-01"
	// maxWindowDays 单窗天数上限。
	maxWindowDays = 365
)

// gapCurrency 单币种缺口概况。
type gapCurrency struct {
	Missing int    `json:"missing"`
	Carried int    `json:"carried"`
	First   string `json:"first"`
	Last    string `json:"last"`
}

// gapWindow 待修复窗口段。
type gapWindow struct {
	Start string `json:"start"`
	End   string `json:"end"`
	Days  int    `json:"days"`
}

// gapScan 缺口扫描结果。
type gapScan struct {
	Currencies map[string]gapCurrency `json:"currencies"`
	Windows    []gapWindow            `json:"windows"`
	TotalDays  int                    `json:"totalDays"`
	TotalPairs int                    `json:"totalPairs"`
	Horizon    string                 `json:"horizon"`
}

// historyGaps 缺口扫描端点：扫 canonical 缺行日 ∪ carried 日。
func (h *handler) historyGaps(w http.ResponseWriter, r *http.Request) {
	result, err := scanHistoryGaps(r.Context(), h.db, beijingTodayFn())
	if err != nil {
		http.Error(w, "缺口扫描失败："+err.Error(), http.StatusInternalServerError)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// scanHistoryGaps 扫描范围 = 各币种最早历史行 → 昨天；全库零白名单历史行时
// 以 fullHistoryStart 为每个白名单币种建窗（bootstrap）。
func scanHistoryGaps(ctx context.Context, db *sql.DB, today time.Time) (*gapScan, error) {
	horizon := today.AddDate(0, 0, -1).Format("2006-01-02")
	marks, err := loadMarks(ctx, db)
	if err != nil {
		return nil, err
	}

	result := &gapScan{
		Currencies: map[string]gapCurrency{},
		Windows:    []gapWindow{},
		Horizon:    horizon,
	}
	pending := map[string]bool{}
	totalPairs := 0

	var codes []string
	if len(marks) == 0 {
		for code := range allowedCurrencies {
			codes = append(codes, code)
		}
	} else {
		for code := range marks {
			codes = append(codes, code)
		}
	}
	sort.Strings(codes)

	for _, code := range codes {
		days := marks[code]
		spanStart := fullHistoryStart
		if len(days) > 0 {
			spanStart = minDay(days)
		}
		if spanStart > horizon {
			continue
		}
		spanDays := diffDays(spanStart, horizon) + 1
		missing, carried, first, last := 0, 0, "", ""
		for i := 0; i < spanDays; i++ {
			day := addDays(spanStart, i)
			kind, recorded := days[day]
			isCarried := recorded && kind == "carried"
			if !recorded || isCarried {
				if isCarried {
					carried++
				} else {
					missing++
				}
				if first == "" {
					first = day
				}
				last = day
				pending[day] = true
			}
		}
		if first == "" {
			continue
		}
		result.Currencies[code] = gapCurrency{
			Missing: missing, Carried: carried, First: first, Last: last,
		}
		totalPairs += missing + carried
	}
	result.TotalPairs = totalPairs
	result.TotalDays = len(pending)
	result.Windows = buildWindows(pending)
	return result, nil
}

// loadMarks 每币种 {day: source_kind}，兼容新旧两代行格式
// （canonical 列缺失时日期取 fetched_at 日期部分、语义按 source 推导）；
// 同 (币种, 日) 多行取语义更强的一条（observed > derived > carried）。
func loadMarks(ctx context.Context, db *sql.DB) (map[string]map[string]string, error) {
	rows, err := db.QueryContext(ctx,
		// CAST 回文本：驱动会按 DATE/DATETIME 声明列解析成 time.Time，读端要的是库内原文
		"SELECT currency_code, CAST(rate_date AS TEXT), CAST(fetched_at AS TEXT), source_kind, source FROM fx_rate_history")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	marks := map[string]map[string]string{}
	for rows.Next() {
		var code string
		var rateDate, fetchedAt, kind, source sql.NullString
		if err := rows.Scan(&code, &rateDate, &fetchedAt, &kind, &source); err != nil {
			return nil, err
		}
		code = strings.ToUpper(code)
		if !allowedCurrencies[code] {
			continue
		}
		day, ok := dayOf(rateDate)
		if !ok {
			day, ok = dayOf(fetchedAt)
		}
		if !ok {
			continue
		}
		resolved := resolveKind(kind, source)
		bucket := marks[code]
		if bucket == nil {
			bucket = map[string]string{}
			marks[code] = bucket
		}
		if cur, seen := bucket[day]; !seen || kindRank(resolved) > kindRank(cur) {
			bucket[day] = resolved
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	return marks, nil
}

// dayOf 取时间文本前 10 位日期部分；空值或不可解析为 false。
func dayOf(v sql.NullString) (string, bool) {
	if !v.Valid || len(v.String) < 10 {
		return "", false
	}
	head := v.String[:10]
	if _, err := time.Parse("2006-01-02", head); err != nil {
		return "", false
	}
	return head, true
}

// resolveKind 行语义解析：Bing 源一律 carried（纳入待修复）；有标记用标记；
// 缺失时 backfill / bing 延续值 → carried，其余 → observed。
func resolveKind(kind, source sql.NullString) string {
	if strings.EqualFold(source.String, "bing.currencyapi") {
		return "carried"
	}
	if kind.Valid && kind.String != "" {
		return strings.ToLower(kind.String)
	}
	switch strings.ToLower(source.String) {
	case "backfill", "bing.currencyapi":
		return "carried"
	}
	return "observed"
}

// kindRank 语义可信度：observed > derived > carried，未知为 0。
func kindRank(kind string) int {
	switch kind {
	case "observed":
		return 3
	case "derived":
		return 2
	case "carried":
		return 1
	}
	return 0
}

// buildWindows 待修复日并集聚类（相邻间隔 ≤ clusterGapDays 并段）后
// 拆成 ≤ maxWindowDays 天的窗口。
func buildWindows(pending map[string]bool) []gapWindow {
	days := make([]string, 0, len(pending))
	for d := range pending {
		days = append(days, d)
	}
	sort.Strings(days)
	windows := []gapWindow{}
	for i := 0; i < len(days); {
		j := i
		for j+1 < len(days) && diffDays(days[j], days[j+1]) <= clusterGapDays {
			j++
		}
		for cursor := days[i]; cursor <= days[j]; {
			end := addDays(cursor, maxWindowDays-1)
			if end > days[j] {
				end = days[j]
			}
			windows = append(windows, gapWindow{
				Start: cursor, End: end, Days: diffDays(cursor, end) + 1,
			})
			cursor = addDays(end, 1)
		}
		i = j + 1
	}
	return windows
}

// minDay 字典序最小日期（ISO 文本字典序即时间序）。
func minDay(days map[string]string) string {
	min := ""
	for d := range days {
		if min == "" || d < min {
			min = d
		}
	}
	return min
}

// diffDays b − a 的整天数（均为 YYYY-MM-DD，UTC 无夏令时）。
func diffDays(a, b string) int {
	ta, errA := time.Parse("2006-01-02", a)
	tb, errB := time.Parse("2006-01-02", b)
	if errA != nil || errB != nil {
		return 0
	}
	return int(tb.Sub(ta).Hours() / 24)
}

// addDays 日期加 n 天。
func addDays(day string, n int) string {
	t, err := time.Parse("2006-01-02", day)
	if err != nil {
		return day
	}
	return t.AddDate(0, 0, n).Format("2006-01-02")
}
