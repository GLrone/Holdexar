package httpx

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strconv"
	"strings"
)

const IntParseMsg = "Input should be a valid integer, unable to parse string as an integer"

type ValErr struct {
	Type  string         `json:"type"`
	Loc   []any          `json:"loc"`
	Msg   string         `json:"msg"`
	Input any            `json:"input"`
	Ctx   map[string]any `json:"ctx,omitempty"`
}

func (e *ValErr) Error() string { return e.Msg }

func WriteJSON(w http.ResponseWriter, status int, payload any) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(payload); err != nil {
		http.Error(w, "响应序列化失败", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write(bytes.TrimSuffix(buf.Bytes(), []byte{'\n'}))
}

func WriteValidationError(w http.ResponseWriter, errs ...ValErr) {
	if errs == nil {
		errs = []ValErr{}
	}
	WriteJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
}

func DetailNull() map[string]any {
	return map[string]any{"detail": nil}
}

func MethodNotAllowed(allow string) http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Allow", allow)
		WriteJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "Method Not Allowed"})
	}
}

func QueryInt(q url.Values, field string, def int64, ge, le *int64) (int64, bool, *ValErr) {
	vals, ok := q[field]
	if !ok || len(vals) == 0 {
		return def, false, nil
	}
	raw := vals[0]
	n, err := strconv.ParseInt(strings.TrimSpace(raw), 10, 64)
	if err != nil {
		return 0, true, &ValErr{Type: "int_parsing", Loc: []any{"query", field}, Msg: IntParseMsg, Input: raw}
	}
	if ge != nil && n < *ge {
		return 0, true, &ValErr{
			Type: "greater_than_equal", Loc: []any{"query", field},
			Msg: fmt.Sprintf("Input should be greater than or equal to %d", *ge), Input: raw,
		}
	}
	if le != nil && n > *le {
		return 0, true, &ValErr{
			Type: "less_than_equal", Loc: []any{"query", field},
			Msg: fmt.Sprintf("Input should be less than or equal to %d", *le), Input: raw,
		}
	}
	return n, true, nil
}
