package bills

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"regexp"
	"strconv"
	"strings"

	"holdexar/server-go/internal/store"
)

const billViewPrefix = "/api/v1/bills"

// RegisterViews serves persisted bill views and manual edits.
func RegisterViews(mux *http.ServeMux, db *sql.DB, gate *store.WriteGate) {
	mux.HandleFunc("GET "+billViewPrefix+"/imports", func(w http.ResponseWriter, r *http.Request) {
		value, err := billImportViews(r.Context(), db)
		writeBillView(w, value, err, "")
	})
	mux.HandleFunc("GET "+billViewPrefix+"/imports/{import_id}/overview", billViewByID("import_id", "账单", func(r *http.Request, id int64) (any, error) {
		return billOverview(r.Context(), db, id)
	}))
	mux.HandleFunc("GET "+billViewPrefix+"/imports/{import_id}/game-txs", billViewByID("import_id", "", func(r *http.Request, id int64) (any, error) {
		limit, offset, err := billPagination(r, 200)
		if err != nil {
			return nil, err
		}
		const columns = `id, date, tx_type, items_json, currency, amount, sign, cny_fen, fx_rate, fx_note, payment,
discount_pct, orig_price, NULLIF(wallet_balance, ''), NULLIF(base_price, ''), payment_parts_json, gift_to_json,
is_gift, is_refund, orig_is_gift`
		from, args := "FROM bill_game_txs WHERE import_id = ?", []any{id}
		query := r.URL.Query()
		for _, key := range []string{"year", "month"} {
			if v := billQueryValue(query[key]); v != "" {
				from += " AND date LIKE (? || '%')"
				args = append(args, v)
			}
		}
		if v := billQueryValue(query["txType"]); v != "" {
			from += " AND tx_type = ?"
			args = append(args, v)
		}
		if v := billQueryValue(query["search"]); v != "" {
			from += " AND items_json LIKE ('%' || ? || '%')"
			args = append(args, v)
		}
		return billPage(r.Context(), db, columns, from, args, limit, offset, scanBillGame)
	}))
	mux.HandleFunc("GET "+billViewPrefix+"/imports/{import_id}/topup-txs", billViewByID("import_id", "", func(r *http.Request, id int64) (any, error) {
		limit, offset, err := billPagination(r, 200)
		if err != nil {
			return nil, err
		}
		const columns = `id, date, "desc", currency, amount, sign, cny_fen, fx_rate, payment, tx_type, is_refund`
		return billPage(r.Context(), db, columns, "FROM bill_topup_txs WHERE import_id = ?", []any{id}, limit, offset, scanBillTopup)
	}))
	mux.HandleFunc("GET "+billViewPrefix+"/imports/{import_id}/cdk", billViewByID("import_id", "", func(r *http.Request, id int64) (any, error) {
		limit, offset, err := billPagination(r, 500)
		if err != nil {
			return nil, err
		}
		const columns = `id, name, appid, date, acq, acq_label, manual_fen`
		from, args := "FROM bill_cdk_games WHERE import_id = ?", []any{id}
		query := r.URL.Query()
		if v := billQueryValue(query["acq"]); v != "" {
			from += " AND acq = ?"
			args = append(args, v)
		}
		if v := billQueryValue(query["search"]); v != "" {
			from += " AND name LIKE ('%' || ? || '%')"
			args = append(args, v)
		}
		return billPage(r.Context(), db, columns, from, args, limit, offset, scanBillCdk)
	}))
	mux.HandleFunc("DELETE "+billViewPrefix+"/imports/{import_id}", billViewByID("import_id", "", func(r *http.Request, id int64) (any, error) {
		return deleteBillImport(r.Context(), gate, id)
	}))
	mux.HandleFunc("PUT "+billViewPrefix+"/cdk/{cdk_id}/price", func(w http.ResponseWriter, r *http.Request) {
		id, err := parseBillInt(r.PathValue("cdk_id"), "path", "cdk_id")
		if err != nil {
			writeBillView(w, nil, err, "")
			return
		}
		fen, err := readBillPrice(w, r)
		if err != nil {
			writeBillView(w, nil, err, "")
			return
		}
		value, err := updateBillCdkPrice(r.Context(), gate, id, fen)
		writeBillView(w, value, err, fmt.Sprintf("'CDK 条目 %d 不存在'", id))
	})
}

func billViewByID(param, resource string, run func(*http.Request, int64) (any, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		id, err := parseBillInt(r.PathValue(param), "path", param)
		if err != nil {
			writeBillView(w, nil, err, "")
			return
		}
		value, err := run(r, id)
		writeBillView(w, value, err, fmt.Sprintf("'%s %d 不存在'", resource, id))
	}
}

type billViewInputError struct {
	Type  string   `json:"type"`
	Loc   []string `json:"loc"`
	Msg   string   `json:"msg"`
	Input any      `json:"input"`
}

func (e *billViewInputError) Error() string { return e.Msg }

func writeBillView(w http.ResponseWriter, value any, err error, missing string) {
	status := http.StatusOK
	if err != nil {
		status = http.StatusInternalServerError
		var detail any = "账单操作失败"
		var input *billViewInputError
		switch {
		case errors.As(err, &input):
			status, detail = http.StatusUnprocessableEntity, []any{input}
		case missing != "" && errors.Is(err, sql.ErrNoRows):
			status, detail = http.StatusNotFound, missing
		}
		value = map[string]any{"detail": detail}
	}
	writeBillJSON(w, status, value)
}

func writeBillJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func billQueryValue(values []string) string {
	if len(values) == 0 {
		return ""
	}
	return values[len(values)-1]
}

var billIntString = regexp.MustCompile(`^[+-]?[0-9]+(?:_[0-9]+)*(?:\.0+)?$`)

func parseBillInt(value, scope, name string) (int64, error) {
	trimmed := strings.TrimSpace(value)
	if billIntString.MatchString(trimmed) {
		integer, _, _ := strings.Cut(trimmed, ".")
		if number, err := strconv.ParseInt(strings.ReplaceAll(integer, "_", ""), 10, 64); err == nil {
			return number, nil
		}
	}
	return 0, &billViewInputError{"int_parsing", []string{scope, name},
		"Input should be a valid integer, unable to parse string as an integer", value}
}

func billPagination(r *http.Request, defaultLimit int64) (int64, int64, error) {
	limit, offset := defaultLimit, int64(0)
	query := r.URL.Query()
	if values, exists := query["limit"]; exists {
		n, err := parseBillInt(billQueryValue(values), "query", "limit")
		if err != nil {
			return 0, 0, err
		}
		limit = n
	}
	if values, exists := query["offset"]; exists {
		n, err := parseBillInt(billQueryValue(values), "query", "offset")
		if err != nil {
			return 0, 0, err
		}
		offset = n
	}
	return limit, offset, nil
}

func readBillPrice(w http.ResponseWriter, r *http.Request) (*int64, error) {
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	decoder.UseNumber()
	var body, extra any
	err := decoder.Decode(&body)
	if err == io.EOF || (err == nil && body == nil) {
		return nil, &billViewInputError{"missing", []string{"body"}, "Field required", nil}
	}
	data, isObject := body.(map[string]any)
	if err != nil || !isObject || decoder.Decode(&extra) != io.EOF {
		return nil, &billViewInputError{"model_attributes_type", []string{"body"}, "Input should be a valid dictionary or object to extract fields from", body}
	}
	key := "manualFen"
	value, exists := data[key]
	if !exists {
		key = "manual_fen"
		value = data[key]
	}
	loc := []string{"body", key}
	var fen int64
	switch v := value.(type) {
	case nil:
		return nil, nil
	case string:
		parsed, err := parseBillInt(v, "body", key)
		return &parsed, err
	case json.Number:
		if exact, err := v.Int64(); err == nil {
			return &exact, nil
		}
		if strings.ContainsAny(string(v), ".eE") {
			number, err := v.Float64()
			if err == nil && number >= -0x1p63 && number < 0x1p63 && math.Trunc(number) == number {
				fen = int64(number)
				return &fen, nil
			}
		}
		return nil, &billViewInputError{"int_from_float", loc, "Input should be a valid integer, got a number with a fractional part", value}
	case bool:
		if v {
			fen = 1
		}
	default:
		return nil, &billViewInputError{"int_type", loc, "Input should be a valid integer", value}
	}
	return &fen, nil
}
