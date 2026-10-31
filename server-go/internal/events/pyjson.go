package events

// Python json.dumps 缺省参数的线格式渲染（ensure_ascii=False、分隔符 ", "/": "、
// 浮点 repr）。本包专用副本：与 rates/dbwriter 的存储格式 pyformat 同源不同
// profile（这里是线格式，非 ASCII 原样 UTF-8），包间禁 import 各自持有。

import (
	"fmt"
	"math"
	"sort"
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
		// 兜底：Go map 无序，按键排序保证输出确定；需保持键序的调用方用 []KV
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
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

// pyString ensure_ascii=False 转义：双引号/反斜杠/五类控制短写，其余控制位
// \uXXXX 小写 hex；非 ASCII（含增补平面）原样 UTF-8。
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

// pyFloat 对齐 Python repr(float)：最短往返十进制，小数点指数位 -4 < decpt <= 16
// 用定点（整值补 .0），否则科学计数（指数带符号、最少两位）。
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
