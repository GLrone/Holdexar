package proxypool

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"slices"
	"strconv"
	"strings"
	"time"
)

type stringsBuilder = strings.Builder

type sql_nullstring = sql.NullString
type sql_nullint64 = sql.NullInt64

func jsonUnmarshalUseNumber(data []byte, v any) error {
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.UseNumber()
	return dec.Decode(v)
}

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

func sortStrings(s []string) { slices.Sort(s) }

func sha256Hex(b []byte) string {
	sum := sha256.Sum256(b)
	return hex.EncodeToString(sum[:])
}
