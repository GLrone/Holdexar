package proxypool

// Python 标量语义辅助（strip 空白集 / truthy / str 形态），身份与解析面共用。

import (
	"encoding/json"
	"strconv"
	"strings"
)

// pythonStripRunes Python str.strip() 的空白集合。
var pythonStripRunes = func(r rune) bool {
	switch r {
	case ' ', '\t', '\n', '\v', '\f', '\r', 0x1c, 0x1d, 0x1e, 0x1f, 0x85, 0xa0,
		0x1680, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000:
		return true
	}
	return r >= 0x2000 && r <= 0x200a
}

func pyTrimSpace(s string) string {
	return strings.TrimFunc(s, pythonStripRunes)
}

// pyTruthy Python 真值判定（name: 0 / 空串 / 空容器都按假过滤）。
func pyTruthy(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case int:
		return x != 0
	case int64:
		return x != 0
	case float64:
		return x != 0
	case json.Number:
		f, err := strconv.ParseFloat(string(x), 64)
		return err == nil && f != 0
	case []any:
		return len(x) > 0
	case map[string]any:
		return len(x) > 0
	}
	return true
}

// pyStr Python str() 对标量的形态（is_info_placeholder 等消费点用）。
func pyStr(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case bool:
		if x {
			return "True"
		}
		return "False"
	case string:
		return x
	case int:
		return strconv.Itoa(x)
	case int64:
		return strconv.FormatInt(x, 10)
	case float64:
		return formatPyFloat(x)
	case json.Number:
		if strings.ContainsAny(string(x), ".eE") {
			f, err := strconv.ParseFloat(string(x), 64)
			if err == nil {
				return formatPyFloat(f)
			}
		}
		return string(x)
	}
	return ""
}
