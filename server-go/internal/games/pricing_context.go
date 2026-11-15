package games

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// 价格上下文（账单许可证命中条）：截至 date 的当时价 + 历史最低。与历史
// 走势同一条标准版序列；窗口按裸北京时间比对（snapshot_at 写入侧同一把钟）；
// date 非法（非 %Y-%m-%d）不报错，回 null 对——调用方静默省略价格段。

type priceContextValue struct {
	CnyFen     int64  `json:"cnyFen"`
	Discount   int64  `json:"discount"`
	SnapshotAt string `json:"snapshotAt"`
}

type priceContextItem struct {
	AppID  int64              `json:"appid"`
	Region string             `json:"region"`
	Date   string             `json:"date"`
	At     *priceContextValue `json:"at"`
	Lowest *priceContextValue `json:"lowest"`
}

func (h *handler) handlePriceContext(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	q := r.URL.Query()
	var errs []valErr
	if !q.Has("date") {
		errs = append(errs, valErr{Type: "missing", Loc: []any{"query", "date"}, Msg: "Field required", Input: nil})
	} else if n := utf8.RuneCountInString(q.Get("date")); n < 10 {
		errs = append(errs, valErr{Type: "string_too_short", Loc: []any{"query", "date"},
			Msg: "String should have at least 10 characters", Input: q.Get("date"),
			Ctx: map[string]any{"min_length": 10}})
	} else if n > 10 {
		errs = append(errs, valErr{Type: "string_too_long", Loc: []any{"query", "date"},
			Msg: "String should have at most 10 characters", Input: q.Get("date"),
			Ctx: map[string]any{"max_length": 10}})
	}
	if q.Has("region") && utf8.RuneCountInString(q.Get("region")) > 5 {
		errs = append(errs, valErr{Type: "string_too_long", Loc: []any{"query", "region"},
			Msg: "String should have at most 5 characters", Input: q.Get("region"),
			Ctx: map[string]any{"max_length": 5}})
	}
	if len(errs) > 0 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
		return
	}
	region := strings.ToLower(q.Get("region"))
	if !q.Has("region") {
		region = "cn"
	}
	resp, err := h.priceContext(r.Context(), appid, q.Get("date"), region)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, resp)
}

// priceContext 单条上下文：窗口 = 标准版 ∪ 在售 sub（与历史走势同口径；
// 并集右支不携带 product_unknown 守卫——与 Python get_price_context 逐字
// 一致，两侧各自对齐自己的真值源），snapshot_at ≤ date 当日 23:59:59。
func (h *handler) priceContext(ctx context.Context, appid int64, date, region string) (priceContextItem, error) {
	empty := priceContextItem{AppID: appid, Region: region, Date: date}
	if _, err := time.Parse("2006-01-02", date); err != nil {
		return empty, nil
	}
	regionUpper := strings.ToUpper(region)

	var saleSub sql.NullInt64
	e := h.db.QueryRowContext(ctx,
		`SELECT sub_id FROM game_current_prices
		 WHERE appid=? AND region_code=? AND price_status='ok' AND sub_id IS NOT NULL AND sub_id > 0
		 ORDER BY updated_at DESC LIMIT 1`, appid, regionUpper).Scan(&saleSub)
	if e != nil && !errors.Is(e, sql.ErrNoRows) {
		return empty, e
	}
	versionCond := standardProductCond
	if saleSub.Valid && saleSub.Int64 > 0 {
		versionCond = "(" + standardProductCond + " OR sub_id = " + strconv.FormatInt(saleSub.Int64, 10) + ")"
	}

	rows, e := h.db.QueryContext(ctx,
		`SELECT id, snapshot_at, price, cny_fen, discount_percent, currency, sub_id, version_suffix, is_gold
		 FROM game_price_history
		 WHERE appid=? AND region_code=? AND price_status='ok' AND `+versionCond+
			` AND snapshot_at IS NOT NULL AND snapshot_at <= ?
		 ORDER BY snapshot_at ASC`,
		appid, regionUpper, date+" 23:59:59")
	if e != nil {
		return empty, e
	}
	defer rows.Close()
	var read []historyRowRead
	for rows.Next() {
		var it historyRowRead
		var gold sql.NullBool
		if e = rows.Scan(&it.id, &it.snapshotAt, &it.price, &it.cnyFen, &it.discount, &it.currency, &it.subID, &it.suffix, &gold); e != nil {
			return empty, e
		}
		it.isGold = gold.Bool
		read = append(read, it)
	}
	if e = rows.Err(); e != nil {
		return empty, e
	}

	filled, e := h.fillCnyFen(ctx, regionUpper, read)
	if e != nil {
		return empty, e
	}
	var points []priceContextValue
	for _, it := range read {
		var cnyFen int64
		hasCny := false
		if it.cnyFen != nil {
			cnyFen, hasCny = *it.cnyFen, true
		} else if v, ok := filled[it.id]; ok {
			cnyFen, hasCny = v, true
		}
		if !hasCny || cnyFen <= 0 {
			continue
		}
		discount := int64(0)
		if it.discount != nil {
			discount = *it.discount
		}
		points = append(points, priceContextValue{
			CnyFen: cnyFen, Discount: discount, SnapshotAt: isoNaive(it.snapshotAt),
		})
	}
	if len(points) == 0 {
		return empty, nil
	}
	lowest := points[0]
	for _, p := range points[1:] {
		if p.CnyFen < lowest.CnyFen {
			lowest = p
		}
	}
	return priceContextItem{AppID: appid, Region: region, Date: date,
		At: &points[len(points)-1], Lowest: &lowest}, nil
}

// ── 批量端点 ─────────────────────────────────────────────────────────────

// bodyErr 单条 body 校验错误（loc 含 body 段与列表下标；形状对齐 pydantic
// venv 实测锚点，含错误累积与宽松整数强转规则）。
type bodyErr struct {
	Type  string         `json:"type"`
	Loc   []any          `json:"loc"`
	Msg   string         `json:"msg"`
	Input any            `json:"input"`
	Ctx   map[string]any `json:"ctx,omitempty"`
}

const (
	bodyMsgIntParsing   = "Input should be a valid integer, unable to parse string as an integer"
	bodyMsgIntType      = "Input should be a valid integer"
	bodyMsgIntFromFloat = "Input should be a valid integer, got a number with a fractional part"
	bodyMsgModelType    = "Input should be a valid dictionary or object to extract fields from"
)

func (h *handler) handlePriceContextBatch(w http.ResponseWriter, r *http.Request) {
	body, ok := decodeJSONBody(w, r)
	if !ok {
		return
	}
	rawItems, present := body["items"]
	if !present {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []any{
			bodyErr{Type: "missing", Loc: []any{"body", "items"}, Msg: "Field required", Input: body}}})
		return
	}
	items, ok := rawItems.([]any)
	if !ok {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []any{
			bodyErr{Type: "list_type", Loc: []any{"body", "items"}, Msg: "Input should be a valid list", Input: rawItems}}})
		return
	}
	var errs []bodyErr
	type pair struct {
		appid int64
		date  string
	}
	pairs := make([]pair, 0, len(items))
	for i, raw := range items {
		item, ok := raw.(map[string]any)
		if !ok {
			errs = append(errs, bodyErr{Type: "model_attributes_type", Loc: []any{"body", "items", i},
				Msg: bodyMsgModelType, Input: raw})
			continue
		}
		// 校验不短路：appid 与 date 独立校验，全部错误按序累积（pydantic 同径）。
		valid := true
		var appid int64
		if _, present := item["appid"]; !present {
			errs = append(errs, bodyErr{Type: "missing", Loc: []any{"body", "items", i, "appid"},
				Msg: "Field required", Input: item})
			valid = false
		} else if v, ok, errType, msg := pyIntFromBody(item["appid"]); !ok {
			errs = append(errs, bodyErr{Type: errType, Loc: []any{"body", "items", i, "appid"}, Msg: msg, Input: item["appid"]})
			valid = false
		} else {
			appid = v
		}
		var date string
		if _, present := item["date"]; !present {
			errs = append(errs, bodyErr{Type: "missing", Loc: []any{"body", "items", i, "date"},
				Msg: "Field required", Input: item})
			valid = false
		} else if s, ok := item["date"].(string); !ok {
			errs = append(errs, bodyErr{Type: "string_type", Loc: []any{"body", "items", i, "date"},
				Msg: "Input should be a valid string", Input: item["date"]})
			valid = false
		} else {
			date = s
		}
		if valid {
			pairs = append(pairs, pair{appid: appid, date: date})
		}
	}
	if len(errs) > 0 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
		return
	}

	// ≤200 对截断；appid 非法 / 日期为空的项直接回 null 对（无 region 键，
	// 与 Python 批量旁路字典一致），不抛错——调用方按缺价格段渲染。
	if len(pairs) > 200 {
		pairs = pairs[:200]
	}
	results := make([]map[string]any, 0, len(pairs))
	for _, p := range pairs {
		if p.appid <= 0 || p.date == "" {
			results = append(results, map[string]any{
				"appid": p.appid, "date": p.date, "at": nil, "lowest": nil})
			continue
		}
		item, err := h.priceContext(r.Context(), p.appid, p.date, "cn")
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, detailNull())
			return
		}
		results = append(results, map[string]any{
			"appid": item.AppID, "region": item.Region, "date": item.Date,
			"at": item.At, "lowest": item.Lowest})
	}
	writeJSON(w, http.StatusOK, map[string]any{"results": results})
}

// decodeJSONBody 解析 JSON body；不可解码回 json_invalid、非对象回
// model_attributes_type（loc ["body"]，形状对齐 FastAPI）。
func decodeJSONBody(w http.ResponseWriter, r *http.Request) (map[string]any, bool) {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20))
	dec.UseNumber()
	var raw any
	if err := dec.Decode(&raw); err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []any{
			bodyErr{Type: "json_invalid", Loc: []any{"body", 0}, Msg: "JSON decode error",
				Input: map[string]any{}, Ctx: map[string]any{"error": err.Error()}}}})
		return nil, false
	}
	body, ok := raw.(map[string]any)
	if !ok {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []any{
			bodyErr{Type: "model_attributes_type", Loc: []any{"body"}, Msg: bodyMsgModelType, Input: raw}}})
		return nil, false
	}
	return body, true
}

// pyIntFromBody JSON 值 → 整数（pydantic lax 同径：bool 0/1、整值浮点收整、
// 数值字符串收整；null/容器 → int_type，带小数 → int_from_float，坏形状
// 字符串 → int_parsing）。返回（值, 合法, 错误类型, 错误文案）。
func pyIntFromBody(v any) (int64, bool, string, string) {
	switch t := v.(type) {
	case nil:
		return 0, false, "int_type", bodyMsgIntType
	case bool:
		if t {
			return 1, true, "", ""
		}
		return 0, true, "", ""
	case float64:
		if t == float64(int64(t)) {
			return int64(t), true, "", ""
		}
		return 0, false, "int_from_float", bodyMsgIntFromFloat
	case json.Number:
		if i, err := t.Int64(); err == nil {
			return i, true, "", ""
		}
		if f, err := t.Float64(); err == nil {
			if f == float64(int64(f)) {
				return int64(f), true, "", ""
			}
			return 0, false, "int_from_float", bodyMsgIntFromFloat
		}
		return 0, false, "int_parsing", bodyMsgIntParsing
	case string:
		if n, ok := pyInt(t); ok {
			return int64(n), true, "", ""
		}
		return 0, false, "int_parsing", bodyMsgIntParsing
	}
	return 0, false, "int_type", bodyMsgIntType
}
