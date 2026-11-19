package events

// 线格式 JSON 编码（ensure_ascii=False、", "/": " 分隔、浮点兼容 repr），与存储序列化独立维护。
// SSE 帧字节格式为跨端对齐面，与 Python 实现逐字节对照。

import (
	"fmt"
	"math"
	"slices"
	"strconv"
	"strings"
)

func pyObject(b *strings.Builder, pairs []KV) {
	b.WriteByte('{')
	for i, kv := range pairs {
		if i > 0 {
			b.WriteString(", ")
		}
		pyString(b, kv.Key)
		b.WriteString(": ")
		pyValue(b, kv.Val)
	}
	b.WriteByte('}')
}

func pyValue(b *strings.Builder, v any) {
	switch x := v.(type) {
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case string:
		pyString(b, x)
	case int:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int8:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int16:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int32:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case uint:
		b.WriteString(strconv.FormatUint(uint64(x), 10))
	case uint32:
		b.WriteString(strconv.FormatUint(uint64(x), 10))
	case uint64:
		b.WriteString(strconv.FormatUint(x, 10))
	case float32:
		pyFloat(b, float64(x))
	case float64:
		pyFloat(b, x)
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(", ")
			}
			pyValue(b, e)
		}
		b.WriteByte(']')
	case []KV:
		pyObject(b, x)
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		slices.Sort(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteString(", ")
			}
			pyString(b, k)
			b.WriteString(": ")
			pyValue(b, x[k])
		}
		b.WriteByte('}')
	default:
		pyString(b, fmt.Sprint(x))
	}
}

func pyString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString("\\\"")
		case '\\':
			b.WriteString("\\\\")
		case '\n':
			b.WriteString("\\n")
		case '\r':
			b.WriteString("\\r")
		case '\t':
			b.WriteString("\\t")
		case '\b':
			b.WriteString("\\b")
		case '\f':
			b.WriteString("\\f")
		default:
			if r < 0x20 {
				const hex = "0123456789abcdef"
				b.WriteString("\\u00")
				b.WriteByte(hex[(r>>4)&0xf])
				b.WriteByte(hex[r&0xf])
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

func pyFloat(b *strings.Builder, f float64) {
	if math.IsNaN(f) {
		b.WriteString("NaN")
		return
	}
	if math.IsInf(f, 1) {
		b.WriteString("Infinity")
		return
	}
	if math.IsInf(f, -1) {
		b.WriteString("-Infinity")
		return
	}
	s := strconv.FormatFloat(f, 'e', -1, 64)
	if s[0] == '-' {
		b.WriteByte('-')
		s = s[1:]
	}
	e := strings.IndexByte(s, 'e')
	mant, expStr := s[:e], s[e+1:]
	exp, _ := strconv.Atoi(expStr)
	digits := strings.Replace(mant, ".", "", 1)
	decpt := exp + 1
	switch {
	case decpt <= -4 || decpt > 16:
		b.WriteByte(digits[0])
		if len(digits) > 1 {
			b.WriteByte('.')
			b.WriteString(digits[1:])
		}
		b.WriteByte('e')
		if exp >= 0 {
			b.WriteByte('+')
		} else {
			b.WriteByte('-')
			exp = -exp
		}
		es := strconv.Itoa(exp)
		if len(es) < 2 {
			b.WriteByte('0')
		}
		b.WriteString(es)
	case decpt <= 0:
		b.WriteString("0.")
		for i := 0; i < -decpt; i++ {
			b.WriteByte('0')
		}
		b.WriteString(digits)
	case decpt >= len(digits):
		b.WriteString(digits)
		for i := len(digits); i < decpt; i++ {
			b.WriteByte('0')
		}
		b.WriteString(".0")
	default:
		b.WriteString(digits[:decpt])
		b.WriteByte('.')
		b.WriteString(digits[decpt:])
	}
}
