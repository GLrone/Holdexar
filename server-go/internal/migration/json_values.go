package migration

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"strconv"
	"strings"
	"unicode"
)

type migrationJSONField struct {
	key   string
	value any
}
type migrationJSONObject []migrationJSONField

func migrationDecodeJSON(raw string) (any, error) {
	decoder := json.NewDecoder(strings.NewReader(raw))
	decoder.UseNumber()
	var read func() (any, error)
	read = func() (any, error) {
		token, err := decoder.Token()
		if err != nil {
			return nil, err
		}
		switch token {
		case json.Delim('['):
			items := []any{}
			for decoder.More() {
				value, err := read()
				if err != nil {
					return nil, err
				}
				items = append(items, value)
			}
			_, err = decoder.Token()
			return items, err
		case json.Delim('{'):
			fields := migrationJSONObject{}
			for decoder.More() {
				key, err := decoder.Token()
				if err != nil {
					return nil, err
				}
				value, err := read()
				if err != nil {
					return nil, err
				}
				fields = append(fields, migrationJSONField{key: key.(string), value: value})
			}
			_, err = decoder.Token()
			return fields, err
		default:
			return token, nil
		}
	}
	value, err := read()
	if err != nil {
		return nil, err
	}
	if _, err = decoder.Token(); err != io.EOF {
		if err == nil {
			err = fmt.Errorf("配置含多段JSON值")
		}
		return nil, err
	}
	return value, nil
}

func migrationTruthy(value any) bool {
	switch value := value.(type) {
	case nil:
		return false
	case bool:
		return value
	case string:
		return value != ""
	case []any:
		return len(value) > 0
	case migrationJSONObject:
		return len(value) > 0
	case json.Number:
		number, err := strconv.ParseFloat(value.String(), 64)
		return err != nil || number != 0
	default:
		return true
	}
}

func migrationString(value any) string {
	var repr func(any, bool) string
	quote := func(value string) string {
		mark := rune('\'')
		if strings.ContainsRune(value, '\'') && !strings.ContainsRune(value, '"') {
			mark = '"'
		}
		var out strings.Builder
		out.WriteRune(mark)
		for _, r := range value {
			switch {
			case r == mark || r == '\\':
				out.WriteRune('\\')
				out.WriteRune(r)
			case r == '\n':
				out.WriteString(`\n`)
			case r == '\r':
				out.WriteString(`\r`)
			case r == '\t':
				out.WriteString(`\t`)
			case unicode.IsPrint(r):
				out.WriteRune(r)
			case r <= 0xff:
				fmt.Fprintf(&out, `\x%02x`, r)
			case r <= 0xffff:
				fmt.Fprintf(&out, `\u%04x`, r)
			default:
				fmt.Fprintf(&out, `\U%08x`, r)
			}
		}
		out.WriteRune(mark)
		return out.String()
	}
	repr = func(value any, nested bool) string {
		switch value := value.(type) {
		case nil:
			return "None"
		case string:
			if nested {
				return quote(value)
			}
			return value
		case bool:
			if value {
				return "True"
			}
			return "False"
		case json.Number:
			raw := value.String()
			if !strings.ContainsAny(raw, ".eE") {
				return raw
			}
			f, err := strconv.ParseFloat(raw, 64)
			if err != nil && !math.IsInf(f, 0) {
				return raw
			}
			if math.IsInf(f, 1) {
				return "inf"
			}
			if math.IsInf(f, -1) {
				return "-inf"
			}
			format := byte('g')
			if f == 0 || (math.Abs(f) >= 1e-4 && math.Abs(f) < 1e16) {
				format = 'f'
			}
			out := strconv.FormatFloat(f, format, -1, 64)
			if !strings.ContainsAny(out, ".eE") {
				out += ".0"
			}
			return out
		case []any:
			items := make([]string, len(value))
			for i, item := range value {
				items[i] = repr(item, true)
			}
			return "[" + strings.Join(items, ", ") + "]"
		case migrationJSONObject:
			items := make([]string, len(value))
			for i, field := range value {
				items[i] = quote(field.key) + ": " + repr(field.value, true)
			}
			return "{" + strings.Join(items, ", ") + "}"
		default:
			return fmt.Sprint(value)
		}
	}
	return repr(value, false)
}
