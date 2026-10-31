package proxypool

// Python json.dumps 序列化原语（节点指纹的字节级对齐层）。
// 语义来源：Python json 编码器 ensure_ascii 两种模式 + float.__repr__。
// 本包与 rates/pyfloat.go 同源、包间禁 import，各自持有副本。

import (
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// pyJSONEncode 值树 → Python json.dumps(sort_keys=True, separators=(",", ":"),
// ensure_ascii=False) 的字节形态。支持 nil/bool/int64/float64/string/[]any/
// map[string]any/json.Number；其他类型报错。
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
		sort.Strings(keys)
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

// json.Number 承载从 JSON 文本往返的数字原样字面量（int/float 形态在
// 解析侧即已区分，见 pyEncodeNumber）。

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
	// 整数字面量原样透传（合法 JSON 整数与 Python int 渲染一致）
	sb.WriteString(n)
	return nil
}

// pyEncodeString Python json.dumps(ensure_ascii=False) 的字符串转义：
// 仅引号、反斜杠与控制字符转义，其余（含 <、>、&、U+2028/2029）原样输出。
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

// utf16Pair 码点 → UTF-16 代理对。
func utf16Pair(r rune) (rune, rune) {
	v := r - 0x10000
	return 0xD800 + (v >> 10), 0xDC00 + (v & 0x3FF)
}

// formatPyFloat Python repr 浮点形态：|x| ∈ [1e-4, 1e16) 定点（整值补 .0），
// 越界 e 记法（Go 'e' 格式指数两位带符号，与 Python 一致）。
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
