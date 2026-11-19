package notifications

import (
	"bytes"
	"database/sql"
	"encoding/json"
	"strings"
	"time"
)

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

func nullTextPtr(v sql.NullString) *string {
	if !v.Valid {
		return nil
	}
	s := v.String
	return &s
}

func nullIntPtr(v sql.NullInt64) *int64 {
	if !v.Valid {
		return nil
	}
	n := v.Int64
	return &n
}

func attemptsOrZero(v sql.NullInt64) int64 {
	if !v.Valid {
		return 0
	}
	return v.Int64
}

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
