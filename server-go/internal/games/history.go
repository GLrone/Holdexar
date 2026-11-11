package games

import (
	"context"
	"database/sql"
	"errors"
	"math"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/rates"
)

// 历史读端点：GET /api/v1/games/{appid}/history。与 Python games/service.get_game_history
// 同一契约——标准版序列、指定 sub、product=unknown 三种切片；未知序列独立呈现，
// 永不并入标准版；时间窗按裸北京时间比对（snapshot_at 写入侧以同一把钟落库）。

// beijingZone 裸北京时间（无时区标注，与库内 snapshot_at 存储口径一致）。
var beijingZone = time.FixedZone("UTC+8", 8*3600)

// standardProductCond 标准版谓词（与 derived/priceevents 同口径，空值兼容旧行）。
const standardProductCond = "(" +
	"(is_gold IS NULL OR is_gold = 0) " +
	"AND (version_suffix IS NULL OR version_suffix = '') " +
	"AND (is_bundle IS NULL OR is_bundle = 0) " +
	"AND (product_unknown IS NULL OR product_unknown = 0))"

type historyPoint struct {
	Timestamp      string   `json:"timestamp"`
	CnyFen         int64    `json:"cnyFen"`
	CnyYuan        *float64 `json:"cnyYuan"`
	OriginalCents  *int64   `json:"originalCents"`
	FormattedPrice string   `json:"formattedPrice"`
	Discount       int64    `json:"discount"`
	Currency       string   `json:"currency"`
}

type historyVersion struct {
	SubID   *int64  `json:"subId"`
	Suffix  *string `json:"suffix"`
	IsGold  bool    `json:"isGold"`
	Product string  `json:"product"`
}

type historyLowest struct {
	CnyFen         int64    `json:"cnyFen"`
	CnyYuan        *float64 `json:"cnyYuan"`
	Timestamp      string   `json:"timestamp"`
	CnyYuanPoint   *float64 `json:"cnyYuanPoint"`
	FormattedPrice string   `json:"formattedPrice"`
}

type historyHighest struct {
	CnyFen  int64    `json:"cnyFen"`
	CnyYuan *float64 `json:"cnyYuan"`
}

type historyBrief struct {
	State     string  `json:"state"`
	LastOkAt  *string `json:"lastOkAt"`
	LastError *string `json:"lastError"`
}

type historyResponse struct {
	Region     string           `json:"region"`
	Points     []historyPoint   `json:"points"`
	Lowest     *historyLowest   `json:"lowest"`
	Highest    *historyHighest  `json:"highest"`
	Count      int              `json:"count"`
	LowestHits int              `json:"lowestHits"`
	Versions   []historyVersion `json:"versions"`
	Backfill   historyBrief     `json:"backfill"`
}

type historyRowRead struct {
	id         int64
	snapshotAt string // 库内原文 YYYY-MM-DD HH:MM:SS[.ffffff]
	price      *int64
	cnyFen     *int64
	discount   *int64
	currency   *string
	subID      *int64
	suffix     *string
	isGold     bool
}

func (h *handler) handleHistory(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	q := r.URL.Query()
	region := strings.ToLower(strings.TrimSpace(q.Get("region")))
	if region == "" {
		region = "cn"
	}
	if len(region) > 5 {
		writeJSON(w, http.StatusUnprocessableEntity, queryError("region", "string_too_long", "String should have at most 5 characters", q.Get("region")))
		return
	}
	days := 0
	if raw := q.Get("days"); raw != "" {
		var e error
		if days, e = strconv.Atoi(raw); e != nil || days < 0 || days > 3650 {
			writeJSON(w, http.StatusUnprocessableEntity, queryError("days", "int_parsing", "Value should be within 0 and 3650", raw))
			return
		}
	}
	var subID *int64
	if raw := q.Get("subId"); raw != "" {
		v, e := strconv.ParseInt(raw, 10, 64)
		if e != nil || v < 1 {
			writeJSON(w, http.StatusUnprocessableEntity, queryError("subId", "int_parsing", "Input should be a valid integer, greater than or equal to 1", raw))
			return
		}
		subID = &v
	}
	product := q.Get("product")
	if product != "" && product != "unknown" {
		writeJSON(w, http.StatusUnprocessableEntity, queryError("product", "string_pattern_mismatch", "String should match pattern '^(unknown)$'", product))
		return
	}
	resp, err := h.gameHistory(r.Context(), int64(appid), region, days, subID, product)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// queryError 对齐 FastAPI 校验错误形状。
func queryError(field, errType, msg, input string) map[string]any {
	return map[string]any{"detail": []any{map[string]any{
		"type": errType, "loc": []any{"query", field}, "msg": msg, "input": input,
	}}}
}

func (h *handler) gameHistory(ctx context.Context, appid int64, region string, days int, subID *int64, product string) (*historyResponse, error) {
	regionUpper := strings.ToUpper(region)

	// 在售 sub：当前价 ok 行的最新 sub_id；缺失或 0（db_writer 对 None 的归一值）
	// 都视同无在售 sub，不进并集。
	var saleSub sql.NullInt64
	e := h.db.QueryRowContext(ctx,
		`SELECT sub_id FROM game_current_prices
		 WHERE appid=? AND region_code=? AND price_status='ok' AND sub_id IS NOT NULL
		 ORDER BY updated_at DESC LIMIT 1`, appid, regionUpper).Scan(&saleSub)
	if e != nil && !errors.Is(e, sql.ErrNoRows) {
		return nil, e
	}

	versionCond := standardProductCond
	switch {
	case product == "unknown":
		// 商品身份未知序列：独立呈现，永不与标准版/具体版本混合。
		versionCond = "product_unknown = 1"
	case subID != nil:
		versionCond = "sub_id = " + strconv.FormatInt(*subID, 10)
	case saleSub.Valid && saleSub.Int64 > 0:
		// 并集右支只认已识别行：未知态不得借道在售 sub 混进标准序列。
		versionCond = "(" + standardProductCond + " OR (sub_id = " +
			strconv.FormatInt(saleSub.Int64, 10) +
			" AND (product_unknown IS NULL OR product_unknown = 0)))"
	}

	query := `SELECT id, snapshot_at, price, cny_fen, discount_percent, currency, sub_id, version_suffix, is_gold
		FROM game_price_history
		WHERE appid=? AND region_code=? AND price_status='ok' AND ` + versionCond
	args := []any{appid, regionUpper}
	if days > 0 {
		query += " AND snapshot_at >= ?"
		args = append(args, time.Now().In(beijingZone).AddDate(0, 0, -days).Format("2006-01-02 15:04:05"))
	}
	query += " ORDER BY snapshot_at ASC"

	rows, e := h.db.QueryContext(ctx, query, args...)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	var read []historyRowRead
	for rows.Next() {
		var it historyRowRead
		var gold sql.NullBool
		if e = rows.Scan(&it.id, &it.snapshotAt, &it.price, &it.cnyFen, &it.discount, &it.currency, &it.subID, &it.suffix, &gold); e != nil {
			return nil, e
		}
		it.isGold = gold.Bool
		read = append(read, it)
	}
	if e = rows.Err(); e != nil {
		return nil, e
	}

	filled, e := h.fillCnyFen(ctx, regionUpper, read)
	if e != nil {
		return nil, e
	}

	regionCurrency := regionToCurrency[regionUpper]
	points := make([]historyPoint, 0, len(read))
	for _, it := range read {
		var cnyFen int64
		hasCny := false
		if it.cnyFen != nil {
			cnyFen, hasCny = *it.cnyFen, true
		} else if v, ok := filled[it.id]; ok {
			cnyFen, hasCny = v, true
		}
		var cnyYuan *float64
		if hasCny {
			v := math.Round(float64(cnyFen)) / 100
			cnyYuan = &v
		}
		discount := int64(0)
		if it.discount != nil {
			discount = *it.discount
		}
		currency := regionCurrency
		if it.currency != nil && *it.currency != "" {
			currency = *it.currency
		}
		formatted := ""
		if it.price != nil && regionCurrency != "" {
			formatted = FormatMinorUnits(*it.price, regionCurrency)
		}
		points = append(points, historyPoint{
			Timestamp:      isoNaive(it.snapshotAt),
			CnyFen:         cnyFen,
			CnyYuan:        cnyYuan,
			OriginalCents:  it.price,
			FormattedPrice: formatted,
			Discount:       discount,
			Currency:       currency,
		})
	}

	versions, e := h.historyVersions(ctx, appid, regionUpper)
	if e != nil {
		return nil, e
	}

	resp := &historyResponse{
		Region: region, Points: points, Versions: versions,
		Backfill: h.brief(ctx, appid, regionUpper), Count: len(points),
	}
	var valid []int64
	for _, p := range points {
		if p.CnyFen > 0 {
			valid = append(valid, p.CnyFen)
		}
	}
	if len(valid) == 0 {
		return resp, nil
	}
	lowest, highest := valid[0], valid[0]
	for _, v := range valid {
		if v < lowest {
			lowest = v
		}
		if v > highest {
			highest = v
		}
	}
	var lowestPoint historyPoint
	for _, p := range points {
		if p.CnyFen == lowest {
			lowestPoint = p
			break
		}
	}
	lowYuan := math.Round(float64(lowest)) / 100
	highYuan := math.Round(float64(highest)) / 100
	resp.Lowest = &historyLowest{
		CnyFen: lowest, CnyYuan: &lowYuan,
		Timestamp: lowestPoint.Timestamp, CnyYuanPoint: lowestPoint.CnyYuan,
		FormattedPrice: lowestPoint.FormattedPrice,
	}
	resp.Highest = &historyHighest{CnyFen: highest, CnyYuan: &highYuan}

	// 史低次数：追平/跌破历史最低的事件数；同一促销期连续同价只计一次，
	// 回升后再次到达 = 新的一次。
	runningLow := lowest
	inStreak := false
	for _, p := range points {
		if p.CnyFen <= 0 {
			continue
		}
		if p.CnyFen <= runningLow {
			runningLow = p.CnyFen
			if !inStreak {
				resp.LowestHits++
				inStreak = true
			}
		} else {
			inStreak = false
		}
	}
	return resp, nil
}

func (h *handler) historyVersions(ctx context.Context, appid int64, regionUpper string) ([]historyVersion, error) {
	rows, e := h.db.QueryContext(ctx,
		`SELECT DISTINCT sub_id, version_suffix, is_gold FROM game_price_history
		 WHERE appid=? AND region_code=? AND price_status='ok' AND product_unknown = 0`,
		appid, regionUpper)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	type versionRow struct {
		subID  *int64
		suffix *string
		isGold bool
	}
	var list []versionRow
	for rows.Next() {
		var it versionRow
		var gold sql.NullBool
		if e = rows.Scan(&it.subID, &it.suffix, &gold); e != nil {
			return nil, e
		}
		it.isGold = gold.Bool
		list = append(list, it)
	}
	if e = rows.Err(); e != nil {
		return nil, e
	}
	sort.Slice(list, func(i, j int) bool {
		ki := versionSortKey(list[i].suffix, list[i].isGold)
		kj := versionSortKey(list[j].suffix, list[j].isGold)
		for c := 0; c < 3; c++ {
			if ki[c] != kj[c] {
				return ki[c] < kj[c]
			}
		}
		return false
	})
	versions := make([]historyVersion, 0, len(list)+1)
	for _, it := range list {
		product := "sub"
		if (it.suffix == nil || *it.suffix == "") && !it.isGold {
			product = "standard"
		}
		versions = append(versions, historyVersion{SubID: it.subID, Suffix: it.suffix, IsGold: it.isGold, Product: product})
	}
	var probe int
	if e = h.db.QueryRowContext(ctx,
		`SELECT 1 FROM game_price_history
		 WHERE appid=? AND region_code=? AND price_status='ok' AND product_unknown = 1 LIMIT 1`,
		appid, regionUpper).Scan(&probe); e == nil {
		versions = append(versions, historyVersion{SubID: nil, Suffix: nil, IsGold: false, Product: "unknown"})
	} else if !errors.Is(e, sql.ErrNoRows) {
		return nil, e
	}
	return versions, nil
}

func versionSortKey(suffix *string, isGold bool) [3]string {
	s := ""
	if suffix != nil {
		s = *suffix
	}
	first, gold := "1", "0"
	if s == "" && !isGold {
		first = "0"
	}
	if isGold {
		gold = "1"
	}
	return [3]string{first, s, gold}
}

// fillCnyFen 对 cny_fen 缺失的行按 snapshot_at 历史汇率补算 CNY 分（最多回溯
// 15 天）；历史汇率缺失的行不补——绝不用当前汇率折算历史价格。行级一次批量
// 取数（按币种聚合窗口），不做逐点查询。
func (h *handler) fillCnyFen(ctx context.Context, regionUpper string, read []historyRowRead) (map[int64]int64, error) {
	type needKey struct {
		currency, day string
	}
	needs := map[needKey]struct{}{}
	fallback := regionToCurrency[regionUpper]
	for _, it := range read {
		if it.cnyFen != nil || it.price == nil || *it.price <= 0 || it.snapshotAt == "" {
			continue
		}
		currency := fallback
		if it.currency != nil && *it.currency != "" {
			currency = strings.ToUpper(*it.currency)
		}
		if currency == "" {
			continue
		}
		day, ok := snapshotDay(it.snapshotAt)
		if !ok {
			continue
		}
		needs[needKey{currency, day}] = struct{}{}
	}
	if len(needs) == 0 {
		return map[int64]int64{}, nil
	}
	currencySet := map[string]struct{}{}
	minDay, maxDay := "", ""
	for k := range needs {
		currencySet[k.currency] = struct{}{}
		if minDay == "" || k.day < minDay {
			minDay = k.day
		}
		if k.day > maxDay {
			maxDay = k.day
		}
	}
	currencies := make([]string, 0, len(currencySet))
	for c := range currencySet {
		currencies = append(currencies, c)
	}
	start := shiftDay(minDay, -15)
	series, e := rates.LoadHistoricalRates(ctx, h.db, currencies, start, maxDay)
	if e != nil {
		return nil, e
	}
	filled := map[int64]int64{}
	for _, it := range read {
		if it.cnyFen != nil || it.price == nil || *it.price <= 0 || it.snapshotAt == "" {
			continue
		}
		currency := fallback
		if it.currency != nil && *it.currency != "" {
			currency = strings.ToUpper(*it.currency)
		}
		if currency == "" {
			continue
		}
		if currency == "CNY" {
			filled[it.id] = *it.price
			continue
		}
		day, ok := snapshotDay(it.snapshotAt)
		if !ok {
			continue
		}
		// 回溯上界由加载窗口保证：序列只含 [day-15, maxDay] 的行，
		// 取「≤day 最近一条」即不会越过 15 天上界。
		rate := rates.PickHistoricalRate(series[currency], currency, day)
		if rate == nil {
			continue
		}
		filled[it.id] = int64(math.Round(float64(*it.price) * *rate))
	}
	return filled, nil
}

func snapshotDay(snapshotAt string) (string, bool) {
	if len(snapshotAt) < 10 {
		return "", false
	}
	return snapshotAt[:10], true
}

func shiftDay(day string, delta int) string {
	t, e := time.Parse("2006-01-02", day)
	if e != nil {
		return day
	}
	return t.AddDate(0, 0, delta).Format("2006-01-02")
}

// isoNaive 裸北京时间 → Python datetime.isoformat 同形（非零微秒保留）。
func isoNaive(snapshotAt string) string {
	if len(snapshotAt) < 19 {
		return snapshotAt
	}
	base := snapshotAt[:10] + "T" + snapshotAt[11:19]
	if len(snapshotAt) > 19 && snapshotAt[19] == '.' {
		if frac := strings.TrimRight(snapshotAt[20:], "0"); frac != "" {
			return base + "." + frac
		}
	}
	return base
}
