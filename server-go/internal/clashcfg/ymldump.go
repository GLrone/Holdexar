package clashcfg

// Clash 运行配置 YAML 发射器（块式、缩进无关序列，PyYAML safe_dump 同形布局）。
// 键序语义无关（YAML 映射无序），序列保序；普通 map 发射时 name 键置首——
// 轻量节点名扫描只认 `- name:` 首键形态。

import (
	"fmt"
	"math"
	"sort"
	"strconv"
	"strings"
)

// Doc 保序映射：运行配置骨架用它构造，产出键序稳定。
type Doc struct {
	keys []string
	m    map[string]any
}

func NewDoc() *Doc { return &Doc{m: map[string]any{}} }

func (d *Doc) Set(key string, val any) {
	if _, exists := d.m[key]; !exists {
		d.keys = append(d.keys, key)
	}
	d.m[key] = val
}

func (d *Doc) Get(key string) (any, bool) {
	v, ok := d.m[key]
	return v, ok
}

func (d *Doc) Delete(key string) {
	if _, exists := d.m[key]; !exists {
		return
	}
	delete(d.m, key)
	for i, k := range d.keys {
		if k == key {
			d.keys = append(d.keys[:i], d.keys[i+1:]...)
			break
		}
	}
}

func (d *Doc) Keys() []string { return append([]string(nil), d.keys...) }

func (d *Doc) Len() int { return len(d.keys) }

// DumpYAML 任意值 → 块式 YAML 文本（尾部带换行）。
func DumpYAML(v any) string {
	var b strings.Builder
	dumpNode(&b, v, 0, true)
	return b.String()
}

func dumpNode(b *strings.Builder, v any, indent int, atRoot bool) {
	pad := strings.Repeat(" ", indent)
	switch val := v.(type) {
	case *Doc:
		dumpMapping(b, val.keys, val.m, indent, atRoot)
	case map[string]any:
		keys := sortedMapKeys(val)
		dumpMapping(b, keys, val, indent, atRoot)
	case []any:
		if len(val) == 0 {
			b.WriteString(pad + "[]\n")
			return
		}
		dumpSequence(b, val, indent)
	default:
		b.WriteString(pad + formatScalar(v) + "\n")
	}
}

// dumpMapping 键序由调用方给定；普通 map 已排序且 name 置首。
func dumpMapping(b *strings.Builder, keys []string, m map[string]any, indent int, atRoot bool) {
	pad := strings.Repeat(" ", indent)
	if len(keys) == 0 {
		b.WriteString(pad + "{}\n")
		return
	}
	for _, key := range keys {
		b.WriteString(pad)
		emitKeyValue(b, key, m[key], indent)
	}
	_ = atRoot
}

// emitKeyValue 单个键值对（不含行首缩进）：标量同行，集合换行成子块。
// 键行缩进为 keyIndent，序列项与键同列（PyYAML 缩进无关序列形态）。
func emitKeyValue(b *strings.Builder, key string, val any, keyIndent int) {
	keyText := formatKey(key)
	switch v := val.(type) {
	case *Doc:
		if v.Len() == 0 {
			b.WriteString(keyText + ": {}\n")
			return
		}
		b.WriteString(keyText + ":\n")
		dumpMapping(b, v.keys, v.m, keyIndent+2, false)
	case map[string]any:
		if len(v) == 0 {
			b.WriteString(keyText + ": {}\n")
			return
		}
		b.WriteString(keyText + ":\n")
		dumpMapping(b, sortedMapKeys(v), v, keyIndent+2, false)
	case []any:
		if len(v) == 0 {
			b.WriteString(keyText + ": []\n")
			return
		}
		b.WriteString(keyText + ":\n")
		dumpSequence(b, v, keyIndent)
	default:
		b.WriteString(keyText + ": " + formatScalar(val) + "\n")
	}
}

func dumpSequence(b *strings.Builder, items []any, indent int) {
	pad := strings.Repeat(" ", indent)
	for _, item := range items {
		switch v := item.(type) {
		case *Doc:
			if v.Len() == 0 {
				b.WriteString(pad + "- {}\n")
				continue
			}
			for i, key := range v.keys {
				if i == 0 {
					b.WriteString(pad + "- ")
				} else {
					b.WriteString(pad + "  ")
				}
				emitKeyValue(b, key, v.m[key], indent+2)
			}
		case map[string]any:
			if len(v) == 0 {
				b.WriteString(pad + "- {}\n")
				continue
			}
			for i, key := range sortedMapKeys(v) {
				if i == 0 {
					b.WriteString(pad + "- ")
				} else {
					b.WriteString(pad + "  ")
				}
				emitKeyValue(b, key, v[key], indent+2)
			}
		case []any:
			if len(v) == 0 {
				b.WriteString(pad + "- []\n")
				continue
			}
			b.WriteString(pad + "-\n")
			dumpSequence(b, v, indent+2)
		default:
			b.WriteString(pad + "- " + formatScalar(item) + "\n")
		}
	}
}

// sortedMapKeys 普通 map 的确定性键序：name 置首，其余字典序。
func sortedMapKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Slice(keys, func(i, j int) bool {
		pi, pj := keys[i] == "name", keys[j] == "name"
		if pi != pj {
			return pi
		}
		return keys[i] < keys[j]
	})
	return keys
}

func formatKey(key string) string {
	return formatScalarString(key, true)
}

// formatScalar 标量 → YAML 文本片段。
func formatScalar(v any) string {
	switch val := v.(type) {
	case nil:
		return "null"
	case bool:
		if val {
			return "true"
		}
		return "false"
	case int:
		return strconv.Itoa(val)
	case int64:
		return strconv.FormatInt(val, 10)
	case float64:
		return formatYAMLFloat(val)
	case string:
		return formatScalarString(val, false)
	default:
		return formatScalarString(fmt.Sprintf("%v", val), false)
	}
}

func formatYAMLFloat(f float64) string {
	if math.IsNaN(f) {
		return ".nan"
	}
	if math.IsInf(f, 1) {
		return ".inf"
	}
	if math.IsInf(f, -1) {
		return "-.inf"
	}
	s := strconv.FormatFloat(f, 'g', -1, 64)
	if !strings.ContainsAny(s, ".eE") && !strings.Contains(s, "inf") && !strings.Contains(s, "nan") {
		s += ".0"
	}
	return s
}

// formatScalarString 文本 → YAML 标量：安全即纯、可单引即单引、其余双引转义。
func formatScalarString(s string, isKey bool) string {
	if s == "" {
		return "''"
	}
	if plainSafe(s, isKey) {
		return s
	}
	if strings.ContainsFunc(s, func(r rune) bool {
		return r == '\n' || r == '\r' || r == '\t' || r < 0x20
	}) {
		return doubleQuoteYAML(s)
	}
	return "'" + strings.ReplaceAll(s, "'", "''") + "'"
}

func plainSafe(s string, isKey bool) bool {
	if s == "" {
		return false
	}
	if s[0] == ' ' || s[len(s)-1] == ' ' {
		return false
	}
	if strings.ContainsAny(s, "\n\r\t") {
		return false
	}
	if isKey {
		if strings.ContainsAny(s, ":#{}[],&*!|>'\"%@`") {
			return false
		}
	} else {
		if strings.ContainsAny(s, ",[]{}#&*!|>'\"%@`") {
			return false
		}
		if strings.Contains(s, ": ") || strings.HasSuffix(s, ":") || strings.Contains(s, " #") {
			return false
		}
		if s[0] == '-' && (len(s) == 1 || s[1] == ' ') {
			return false
		}
		if s[0] == '?' && (len(s) == 1 || s[1] == ' ') {
			return false
		}
	}
	if resolveScalarText(s) != any(s) {
		return false
	}
	return true
}

func doubleQuoteYAML(s string) string {
	var b strings.Builder
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
		default:
			if r < 0x20 {
				b.WriteString(fmt.Sprintf("\\x%02x", r))
				continue
			}
			b.WriteRune(r)
		}
	}
	b.WriteByte('"')
	return b.String()
}
