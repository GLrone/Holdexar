package proxypool

import (
	"encoding/json"
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"
)

// pyJSONEncode 输出与 Python json.dumps(sort_keys=true, ensure_ascii=False, 紧凑分隔) 同形的字节串；节点主指纹按此比对，勿改实现。
func pyJSONEncode(v any) (string, error) {
	var sb strings.Builder
	if err := pyEncodeValue(&sb, v); err != nil {
		return "", err
	}
	return sb.String(), nil
}

func pyEncodeValue(sb *strings.Builder, v any) error {
	switch x := v.(type) {
	case nil:
		sb.WriteString("null")
	case bool:
		if x {
			sb.WriteString("true")
		} else {
			sb.WriteString("false")
		}
	case string:
		pyEncodeString(sb, x)
	case int:
		sb.WriteString(strconv.Itoa(x))
	case int64:
		sb.WriteString(strconv.FormatInt(x, 10))
	case float64:
		sb.WriteString(formatPyFloat(x))
	case json.Number:
		return pyEncodeNumber(sb, string(x))
	case []any:
		sb.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				sb.WriteByte(',')
			}
			if err := pyEncodeValue(sb, item); err != nil {
				return err
			}
		}
		sb.WriteByte(']')
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		slices.Sort(keys)
		sb.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				sb.WriteByte(',')
			}
			pyEncodeString(sb, k)
			sb.WriteByte(':')
			if err := pyEncodeValue(sb, x[k]); err != nil {
				return err
			}
		}
		sb.WriteByte('}')
	default:
		return fmt.Errorf("指纹序列化不支持值类型 %T", v)
	}
	return nil
}

func pyEncodeNumber(sb *strings.Builder, n string) error {
	if n == "" {
		return fmt.Errorf("指纹序列化遇到空数字字面量")
	}
	if strings.ContainsAny(n, ".eE") {
		f, err := strconv.ParseFloat(n, 64)
		if err != nil {
			return fmt.Errorf("指纹序列化无法解析数字 %q", n)
		}
		sb.WriteString(formatPyFloat(f))
		return nil
	}
	sb.WriteString(n)
	return nil
}

func pyEncodeString(sb *strings.Builder, s string) {
	sb.WriteByte('"')
	for _, r := range s {
		switch r {
		case '\\':
			sb.WriteString(`\\`)
		case '"':
			sb.WriteString(`\"`)
		case '\b':
			sb.WriteString(`\b`)
		case '\f':
			sb.WriteString(`\f`)
		case '\n':
			sb.WriteString(`\n`)
		case '\r':
			sb.WriteString(`\r`)
		case '\t':
			sb.WriteString(`\t`)
		default:
			if r < 0x20 {
				fmt.Fprintf(sb, `\u%04x`, r)
			} else {
				sb.WriteRune(r)
			}
		}
	}
	sb.WriteByte('"')
}

func utf16Pair(r rune) (rune, rune) {
	v := r - 0x10000
	return 0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF)
}

func formatPyFloat(f float64) string {
	if math.IsNaN(f) {
		return "NaN"
	}
	if math.IsInf(f, 1) {
		return "Infinity"
	}
	if math.IsInf(f, -1) {
		return "-Infinity"
	}
	abs := math.Abs(f)
	if abs != 0 && (abs >= 1e16 || abs < 1e-4) {
		return strconv.FormatFloat(f, 'e', -1, 64)
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}
