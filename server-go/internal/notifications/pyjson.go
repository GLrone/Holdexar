package notifications

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"
)

// 本文件工具与 internal/rates、internal/regions 包内同款同源：包间禁止互相
// import（store 除外），各包自持副本，改动须三处同步。

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

// methodNotAllowed 非声明方法的回退响应。
func methodNotAllowed(allow string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Allow", allow)
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"detail": "Method Not Allowed"})
	}
}

// validationError 查询参数校验失败明细（对齐 pydantic v2 形态）。
type validationError struct {
	Type  string   `json:"type"`
	Loc   []string `json:"loc"`
	Msg   string   `json:"msg"`
	Input string   `json:"input"`
}

// queryError 查询参数校验失败的 422 响应体。
func queryError(field, errType, msg, input string) map[string]any {
	return map[string]any{
		"detail": []validationError{{
			Type: errType, Loc: []string{"query", field}, Msg: msg, Input: input,
		}},
	}
}

const (
	intParseMsg = "Input should be a valid integer, unable to parse string as an integer"
	geMsg       = "Input should be greater than or equal to %d"
	leMsg       = "Input should be less than or equal to %d"
)

// queryInt 解析单个整型查询参数：缺席回 (def, nil)；非法/越界回 422 明细。
func queryInt(q map[string][]string, field string, def, ge, le int64) (int64, map[string]any) {
	vals, ok := q[field]
	if !ok || len(vals) == 0 {
		return def, nil
	}
	n, err := strconvParseInt(vals[0])
	if err != nil {
		return 0, queryError(field, "int_parsing", intParseMsg, vals[0])
	}
	if n < ge {
		return 0, queryError(field, "greater_than_equal", fmt.Sprintf(geMsg, ge), vals[0])
	}
	if n > le {
		return 0, queryError(field, "less_than_equal", fmt.Sprintf(leMsg, le), vals[0])
	}
	return n, nil
}

// queryIntUnbounded 无上限整型查询参数（afterId 用）。
func queryIntUnbounded(q map[string][]string, field string, ge int64) (int64, bool, map[string]any) {
	vals, ok := q[field]
	if !ok || len(vals) == 0 {
		return 0, false, nil
	}
	n, err := strconvParseInt(vals[0])
	if err != nil {
		return 0, false, queryError(field, "int_parsing", intParseMsg, vals[0])
	}
	if n < ge {
		return 0, false, queryError(field, "greater_than_equal", fmt.Sprintf(geMsg, ge), vals[0])
	}
	return n, true, nil
}

// strconvParseInt pydantic 风格整型读取：容忍首尾空白与符号，其余拒绝。
func strconvParseInt(s string) (int64, error) {
	return strconv.ParseInt(strings.TrimSpace(s), 10, 64)
}

// orderedObj 保序 JSON 对象：对齐 Python dict 的插入序序列化。
type orderedObj struct {
	keys []string
	vals map[string]any
}

func newOrderedObj() *orderedObj {
	return &orderedObj{vals: map[string]any{}}
}

func (o *orderedObj) set(key string, value any) {
	if _, exists := o.vals[key]; !exists {
		o.keys = append(o.keys, key)
	}
	o.vals[key] = value
}

func (o *orderedObj) MarshalJSON() ([]byte, error) {
	var buf bytes.Buffer
	buf.WriteByte('{')
	for i, key := range o.keys {
		if i > 0 {
			buf.WriteByte(',')
		}
		keyJSON, err := json.Marshal(key)
		if err != nil {
			return nil, err
		}
		buf.Write(keyJSON)
		buf.WriteByte(':')
		valJSON, err := json.Marshal(o.vals[key])
		if err != nil {
			return nil, err
		}
		buf.Write(valJSON)
	}
	buf.WriteByte('}')
	return buf.Bytes(), nil
}

// kvObj 按成对参数构造保序对象；参数个数必须为偶数。
func kvObj(pairs ...any) *orderedObj {
	v := newOrderedObj()
	for i := 0; i+1 < len(pairs); i += 2 {
		v.set(pairs[i].(string), pairs[i+1])
	}
	return v
}

// truthy Python 真值语义：None/false/0/空串/空数组/空对象为假。
func truthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case json.Number:
		f, err := x.Float64()
		return err == nil && f != 0
	case string:
		return x != ""
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

// parseJSONNumber 库内 JSON 文本 → 保留数值字面量的值树；空/非法文本回 nil。
func parseJSONNumber(text string) any {
	if strings.TrimSpace(text) == "" {
		return nil
	}
	dec := json.NewDecoder(strings.NewReader(text))
	dec.UseNumber()
	var value any
	if err := dec.Decode(&value); err != nil {
		return nil
	}
	return value
}

// jsonFieldOr Python 的 `a or b` 链：首个真值；全假回末值（Python or 语义）。
func jsonFieldOr(values ...any) any {
	for _, v := range values {
		if truthy(v) {
			return v
		}
	}
	if len(values) > 0 {
		return values[len(values)-1]
	}
	return nil
}

// rawOrFallback 库内 JSON 列文本 → 紧凑 RawMessage：值假（null/空容器）或缺席
// 回 fallback；正常值去空白、保键序、数值字面量原样（对齐 FastAPI 紧凑重序列化）。
func rawOrFallback(text string, fallback string) json.RawMessage {
	if strings.TrimSpace(text) == "" {
		return json.RawMessage(fallback)
	}
	value := parseJSONNumber(text)
	if !truthy(value) {
		return json.RawMessage(fallback)
	}
	var buf bytes.Buffer
	if err := json.Compact(&buf, []byte(text)); err != nil {
		return json.RawMessage(fallback)
	}
	return buf.Bytes()
}

// nullTextPtr 文本列 NULL → nil；空串按 Python 语义原样输出（不折叠）。
func nullTextPtr(v sql.NullString) *string {
	if !v.Valid {
		return nil
	}
	s := v.String
	return &s
}

// nullIntPtr 整数列 NULL → nil（SQLAlchemy 对 NULL 整数列同样返回 None）。
func nullIntPtr(v sql.NullInt64) *int64 {
	if !v.Valid {
		return nil
	}
	n := v.Int64
	return &n
}

// attemptsOrZero 尝试次数列 NULL 按 0 计（Python int(attempts or 0) 语义）。
func attemptsOrZero(v sql.NullInt64) int64 {
	if !v.Valid {
		return 0
	}
	return v.Int64
}

// pyIsoformat 「YYYY-MM-DD[ T]HH:MM:SS[.frac]」→ T 分隔 ISO 文本：
// 微秒整零省略，非整零恒 6 位（Python isoformat 语义）。不可解析文本原样返回。
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

// isoformatPtr 时间列文本 → isoformat 指针；NULL → nil。
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
