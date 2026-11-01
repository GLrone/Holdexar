package agent

import (
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"strconv"
	"strings"
)

// Obj 保序对象：工具结果载荷以 Python dict 字面量的插入序生成（json.Marshal
// 对 map 的键排序会打乱现网顺序）。
type Obj struct {
	keys []string
	vals []any
}

func newObj() *Obj { return &Obj{} }

func (o *Obj) set(key string, val any) {
	o.keys = append(o.keys, key)
	o.vals = append(o.vals, val)
}

// get 按键取值（缺键回 nil）。
func (o *Obj) get(key string) any {
	for i, k := range o.keys {
		if k == key {
			return o.vals[i]
		}
	}
	return nil
}

// encodeJSON 以 Python json.dumps(ensure_ascii=False) 语义编码：
// 键序 = 插入序；数字字面量（json.Number）原样；无 HTML 转义；非 ASCII 原样。
func encodeJSON(v any) string {
	var b strings.Builder
	encodeValue(&b, v)
	return b.String()
}

func encodeValue(b *strings.Builder, v any) {
	// 实现 json.Marshaler 的类型（如 games 包的 pyFloat 浮点形态）优先走
	// 自述序列化；产出必为合法 JSON 字面量，直接并入。
	if m, ok := v.(json.Marshaler); ok {
		if raw, err := m.MarshalJSON(); err == nil {
			b.Write(raw)
			return
		}
	}
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
		encodeString(b, x)
	case int:
		b.WriteString(strconv.Itoa(x))
	case int64:
		b.WriteString(strconv.FormatInt(x, 10))
	case json.Number:
		b.WriteString(x.String())
	case float64:
		b.WriteString(pyFloat(x))
	case *Obj:
		if x == nil {
			b.WriteString("null")
			return
		}
		b.WriteByte('{')
		for i, k := range x.keys {
			if i > 0 {
				b.WriteString(", ")
			}
			encodeString(b, k)
			b.WriteString(": ")
			encodeValue(b, x.vals[i])
		}
		b.WriteByte('}')
	case []any:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(", ")
			}
			encodeValue(b, e)
		}
		b.WriteByte(']')
	case []string:
		b.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				b.WriteString(", ")
			}
			encodeString(b, e)
		}
		b.WriteByte(']')
	default:
		// 指针解引用（清单行 / 详情行的可空列）；其余未登记类型按 null 输出
		// （调用方契约上不应触达）。
		if rv := reflect.ValueOf(v); rv.Kind() == reflect.Pointer {
			if rv.IsNil() {
				b.WriteString("null")
			} else {
				encodeValue(b, rv.Elem().Interface())
			}
			return
		}
		b.WriteString("null")
	}
}

// encodeString 字符串转义：引号/反斜杠短写、五类控制短写、其余控制位小写
// 十六进制 \u 形式；非 ASCII 原样 UTF-8。
func encodeString(b *strings.Builder, s string) {
	b.WriteByte('"')
	for _, r := range s {
		switch r {
		case '"':
			b.WriteString(`\"`)
		case '\\':
			b.WriteString(`\\`)
		case '\n':
			b.WriteString(`\n`)
		case '\r':
			b.WriteString(`\r`)
		case '\t':
			b.WriteString(`\t`)
		case '\b':
			b.WriteString(`\b`)
		case '\f':
			b.WriteString(`\f`)
		default:
			if r < 0x20 {
				fmt.Fprintf(b, `\u%04x`, r)
			} else {
				b.WriteRune(r)
			}
		}
	}
	b.WriteByte('"')
}

// pyFloat Python repr 浮点形态：最短往返；1e-4 ≤ |x| < 1e16 定点（整值补 .0），
// 越界科学计数（指数两位带符号）；NaN/Infinity 按允许输出。
func pyFloat(f float64) string {
	if math.IsNaN(f) {
		return "NaN"
	}
	if math.IsInf(f, 0) {
		if f > 0 {
			return "Infinity"
		}
		return "-Infinity"
	}
	if f == 0 {
		return "0.0"
	}
	abs := math.Abs(f)
	if abs >= 1e-4 && abs < 1e16 {
		s := strconv.FormatFloat(f, 'f', -1, 64)
		if !strings.ContainsAny(s, ".") {
			s += ".0"
		}
		return s
	}
	// 科学计数：Python e 形态指数最少两位带符号。
	mantissa, exp := strconv.FormatFloat(f, 'e', -1, 64), 0
	ei := strings.IndexByte(mantissa, 'e')
	sign := "+"
	expStr := mantissa[ei+1:]
	if expStr[0] == '-' || expStr[0] == '+' {
		sign = string(expStr[0])
		expStr = expStr[1:]
	}
	exp, _ = strconv.Atoi(expStr)
	m := mantissa[:ei]
	if strings.IndexByte(m, '.') < 0 {
		m += ".0"
	}
	return fmt.Sprintf("%se%s%02d", m, sign, exp)
}
