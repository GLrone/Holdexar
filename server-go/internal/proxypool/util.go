package proxypool

// 共享小工具：字符串/数值/时间的 Python 语义对齐辅助。

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"sort"
	"strconv"
	"strings"
	"time"
)

// stringsBuilder strings.Builder 的短别名（文件内多处使用）。
type stringsBuilder = strings.Builder

type sql_nullstring = sql.NullString
type sql_nullint64 = sql.NullInt64

// jsonUnmarshalUseNumber 解析 JSON 保数字字面量（json.Number），防 float64
// 改写整数与浮点形态。
func jsonUnmarshalUseNumber(data []byte, v any) error {
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.UseNumber()
	return dec.Decode(v)
}

// pyNumberToInt64 数值 → int64（缺失/非数返回 false）。
func pyNumberToInt64(v any) (int64, bool) {
	switch x := v.(type) {
	case int:
		return int64(x), true
	case int64:
		return x, true
	case float64:
		return int64(x), true
	case json.Number:
		if n, err := strconv.ParseInt(string(x), 10, 64); err == nil {
			return n, true
		}
		if f, err := strconv.ParseFloat(string(x), 64); err == nil {
			return int64(f), true
		}
	}
	return 0, false
}

// parsePyDatetime 库内时间文本（空格分隔 naive）→ time.Time（本地挂钟语义）。
func parsePyDatetime(s string) (time.Time, error) {
	for _, layout := range []string{
		"2006-01-02 15:04:05.000000",
		"2006-01-02 15:04:05",
		"2006-01-02T15:04:05.000000",
		"2006-01-02T15:04:05",
	} {
		if t, err := time.ParseInLocation(layout, s, time.Local); err == nil {
			return t, nil
		}
	}
	return time.Time{}, strconv.ErrSyntax
}

func stringOrNull(v sql_nullstring) string {
	if v.Valid {
		return v.String
	}
	return ""
}

func nullIntOr(v sql_nullint64, def int) int {
	if v.Valid {
		return int(v.Int64)
	}
	return def
}

func sortStrings(s []string) { sort.Strings(s) }

// sha256Hex 字节内容的十六进制摘要（快照内容寻址）。
func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
