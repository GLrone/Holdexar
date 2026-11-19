package dbwriter

// 存储格式统一：DATETIME 写裸北京时间文本，JSON 写 ASCII 转义并保留 ", "/":" 分隔符。

import (
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"

	"holdexar/server-go/internal/timeutil"
)

var BeijingLoc = timeutil.ZoneUTC8

func FormatNaive(t time.Time) string {
	return t.Format("2006-01-02 15:04:05.000000")
}

func PyDumps(v any) string {
	var b strings.Builder
	pyWrite(&b, v)
	return b.String()
}

func pyWrite(b *strings.Builder, v any) {
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
		pyStr(b, x)
	case int:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int32:
		b.WriteString(strconv.FormatInt(int64(x), 10))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case float64:
		// shortcut: 'g' 形态与 Python repr 在极端指数上不等价；列表实装只装字符串
		b.WriteString(strconv.FormatFloat(x, 'g', -1, 64))
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(", ")
			}
			pyWrite(b, e)
		}
		b.WriteByte(']')
	default:
		pyStr(b, fmt.Sprint(x))
	}
}

func pyStr(b *strings.Builder, s string) {
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
			switch {
			case r < 0x20, r == 0x7f:
				fmt.Fprintf(b, "\\u%04x", r)
			case r <= 0x7e:
				b.WriteRune(r)
			case r <= 0xffff:
				fmt.Fprintf(b, "\\u%04x", r)
			default:
				hi, lo := utf16.EncodeRune(r)
				fmt.Fprintf(b, "\\u%04x\\u%04x", hi, lo)
			}
		}
	}
	b.WriteByte('"')
}
