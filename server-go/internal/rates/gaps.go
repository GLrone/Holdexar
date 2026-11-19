package rates

import (
	"context"
	"database/sql"
	"net/http"
	"slices"
	"strings"
	"time"

	"holdexar/server-go/internal/httpx"
)

const (
	clusterGapDays = 7
	// fullHistoryStart 全新安装 bootstrap 锚点 = Provider 全历史起点。
	fullHistoryStart = "2012-01-01"
	maxWindowDays    = 365
)

type gapCurrency struct {
	Missing int    `json:"missing"`
	Carried int    `json:"carried"`
	First   string `json:"first"`
	Last    string `json:"last"`
}

type gapWindow struct {
	Start string `json:"start"`
	End   string `json:"end"`
	Days  int    `json:"days"`
}

type gapScan struct {
	Currencies map[string]gapCurrency `json:"currencies"`
	Windows    []gapWindow            `json:"windows"`
	TotalDays  int                    `json:"totalDays"`
	TotalPairs int                    `json:"totalPairs"`
	Horizon    string                 `json:"horizon"`
}

func (h *handler) historyGaps(w http.ResponseWriter, r *http.Request) {
	result, err := scanHistoryGaps(r.Context(), h.db, beijingTodayFn())
	if err != nil {
		http.Error(w, "缺口扫描失败："+err.Error(), http.StatusInternalServerError)
		return
	}
	httpx.WriteJSON(w, http.StatusOK, result)
}

// scanHistoryGaps 扫描各白名单币种最早记录至昨天；无白名单行时从 fullHistoryStart 启动。
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
	slices.Sort(codes)

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

// loadMarks 兼容无 canonical 列旧行（取 fetched_at 日期），同日多行保留语义更强的一条。
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

// resolveKind bing.currencyapi 一律判 carried；缺 source_kind 时 backfill 判 carried，其余 observed。
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

// buildWindows 将间隔 ≤ clusterGapDays 的待修复日并段，并按 maxWindowDays 切分。
func buildWindows(pending map[string]bool) []gapWindow {
	days := make([]string, 0, len(pending))
	for d := range pending {
		days = append(days, d)
	}
	slices.Sort(days)
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

func minDay(days map[string]string) string {
	min := ""
	for d := range days {
		if min == "" || d < min {
			min = d
		}
	}
	return min
}

func diffDays(a, b string) int {
	ta, errA := time.Parse("2006-01-02", a)
	tb, errB := time.Parse("2006-01-02", b)
	if errA != nil || errB != nil {
		return 0
	}
	return int(tb.Sub(ta).Hours() / 24)
}

func addDays(day string, n int) string {
	t, err := time.Parse("2006-01-02", day)
	if err != nil {
		return day
	}
	return t.AddDate(0, 0, n).Format("2006-01-02")
}
