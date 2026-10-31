package dbwriter

// Python 侧存储格式对齐工具：DATETIME 列统一 naive 北京挂钟文本（对齐 _naive
// 剥时区语义），JSON 列统一 json.dumps 缺省参数文本（ensure_ascii、分隔符
// ", "/": "）。格式分叉会让 ORDER BY 与逐值对照失真。

import (
	"fmt"
	"strconv"
	"strings"
	"time"
	"unicode/utf16"
)

// BeijingLoc 北京挂钟时区：UTC+8 固定偏移（现行无夏令时）。
var BeijingLoc = time.FixedZone("UTC+8", 8*3600)

// FormatNaive 时间列存储文本：直接按入参时钟值渲染，不做时区换算
// （对齐 Python _naive 的 replace(tzinfo=None)：调用方给什么墙钟就存什么）。
func FormatNaive(t time.Time) string {
	return t.Format("2006-01-02 15:04:05.000000")
}

// PyDumps 对齐 Python json.dumps 缺省参数的 JSON 文本。
// 支持 nil/bool/string/整数/float64/[]any；其余类型按字符串兜底。
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

// pyStr 字符串转义：双引号/反斜杠/五类控制缩写，其余控制位与全部非 ASCII
// 按 \uXXXX（小写 hex，增补平面拆代理对）——对齐 ensure_ascii 词表。
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
