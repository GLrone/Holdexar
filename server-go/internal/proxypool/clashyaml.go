package proxypool

// Clash 订阅 YAML 子集解析器（无第三方依赖）。对齐 PyYAML safe_load 在订阅
// 文件真实形态上的行为：块式/流式映射与序列、YAML 1.1 标量 resolver
//（yes/on 八态布尔、0 前缀八进制、指数必须带符号的浮点、.inf/.nan）。
// 不支持的形态（锚点别名、多行标量、merge 键、多文档）一律显式报错——
// fail-closed，不做「可能解析错」的静默近似。

import (
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
)

// decodeUTF8Strict 严格 UTF-8 解码：非法字节一律报错，不提供任何降级通道。
func decodeUTF8Strict(raw []byte) (string, error) {
	if !utf8Valid(raw) {
		return "", &InvalidEncodingError{}
	}
	return string(raw), nil
}

func utf8Valid(b []byte) bool {
	for i := 0; i < len(b); {
		if b[i] < 0x80 {
			i++
			continue
		}
		r, size := decodeRune(b[i:])
		if r == 0xFFFD && size <= 1 {
			return false
		}
		i += size
	}
	return true
}

// decodeRune 手写 UTF-8 解码（避免 unicode/utf8 的整体扫描开销与导入面）。
func decodeRune(b []byte) (rune, int) {
	c := b[0]
	switch {
	case c&0xE0 == 0xC0 && len(b) >= 2:
		r := rune(c&0x1F)<<6 | rune(b[1]&0x3F)
		if b[1]&0xC0 == 0x80 && r >= 0x80 {
			return r, 2
		}
	case c&0xF0 == 0xE0 && len(b) >= 3:
		r := rune(c&0x0F)<<12 | rune(b[1]&0x3F)<<6 | rune(b[2]&0x3F)
		if b[1]&0xC0 == 0x80 && b[2]&0xC0 == 0x80 && r >= 0x800 {
			return r, 3
		}
	case c&0xF8 == 0xF0 && len(b) >= 4:
		r := rune(c&0x07)<<18 | rune(b[1]&0x3F)<<12 | rune(b[2]&0x3F)<<6 | rune(b[3]&0x3F)
		if b[1]&0xC0 == 0x80 && b[2]&0xC0 == 0x80 && b[3]&0xC0 == 0x80 && r >= 0x10000 && r <= 0x10FFFF {
			return r, 4
		}
	}
	return 0xFFFD, 1
}

// ClashYAMLError 订阅 YAML 解析失败（对齐 Python yaml.YAMLError 捕获面）。
type ClashYAMLError struct{ Detail string }

func (e *ClashYAMLError) Error() string {
	return "订阅 YAML 解析失败：" + e.Detail
}

type yamlLine struct {
	indent  int
	content string
}

type blockParser struct {
	lines []yamlLine
	pos   int
}

// clashYAMLLoad 文档 → 顶层映射（对齐 safe_load 的映射约束）。
func clashYAMLLoad(text string) (map[string]any, error) {
	lines, err := scanYAMLLines(text)
	if err != nil {
		return nil, err
	}
	p := &blockParser{lines: lines}
	if p.pos < len(p.lines) {
		if v, err := p.parseValue(p.lines[p.pos].indent); err != nil {
			return nil, err
		} else {
			if m, ok := v.(map[string]any); ok && p.pos >= len(p.lines) {
				return m, nil
			}
			return nil, &ClashYAMLError{Detail: "文档顶层不是单一映射"}
		}
	}
	return nil, &ClashYAMLError{Detail: "文档为空"}
}

// scanYAMLLines 行预处理：去注释、记缩进、跳过空行与文档标记。
func scanYAMLLines(text string) ([]yamlLine, error) {
	var out []yamlLine
	sawDocStart := false
	for _, raw := range pySplitLines(text) {
		trimmed := pyTrimSpaceTabs(raw)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		if trimmed == "---" {
			if sawDocStart || len(out) > 0 {
				return nil, &ClashYAMLError{Detail: "不支持多文档订阅"}
			}
			sawDocStart = true
			continue
		}
		if trimmed == "..." {
			break
		}
		indent := 0
		for indent < len(raw) && raw[indent] == ' ' {
			indent++
		}
		if indent < len(raw) && raw[indent] == '\t' {
			return nil, &ClashYAMLError{Detail: "缩进不允许制表符"}
		}
		body, err := stripYAMLComment(raw[indent:])
		if err != nil {
			return nil, err
		}
		body = strings.TrimRight(body, " ")
		if body == "" {
			continue
		}
		out = append(out, yamlLine{indent: indent, content: body})
	}
	return out, nil
}

func pyTrimSpaceTabs(s string) string {
	return strings.Trim(s, " \t\r")
}

// pySplitLines Python str.splitlines 语义（\v \f \x1c-\x1e \x85 \u2028
// \u2029 也算行界）。
func pySplitLines(s string) []string {
	return strings.FieldsFunc(s, func(r rune) bool {
		switch r {
		case '\n', '\r', '\v', '\f', 0x1c, 0x1d, 0x1e, 0x85, 0x2028, 0x2029:
			return true
		}
		return false
	}) // FieldsFunc 会丢空行，但空行在后续被跳过，语义等价
}

// stripYAMLComment 引号与流深度感知的去注释（# 前必须是空白才开启注释）。
func stripYAMLComment(s string) (string, error) {
	depth := 0
	inSingle, inDouble := false, false
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case inSingle:
			if c == '\'' {
				if i+1 < len(s) && s[i+1] == '\'' {
					i++
					continue
				}
				inSingle = false
			}
		case inDouble:
			if c == '\\' {
				i++
			} else if c == '"' {
				inDouble = false
			}
		case c == '\'':
			inSingle = true
		case c == '"':
			inDouble = true
		case c == '{' || c == '[':
			depth++
		case c == '}' || c == ']':
			depth--
		case c == '#' && (i == 0 || s[i-1] == ' ' || s[i-1] == '\t'):
			return s[:i], nil
		}
	}
	_ = depth
	return s, nil
}

// parseValue 当前位置的块值（映射 / 序列 / 标量）。
func (p *blockParser) parseValue(indent int) (any, error) {
	if p.pos >= len(p.lines) {
		return nil, nil
	}
	line := p.lines[p.pos]
	if isSequenceItem(line.content) {
		return p.parseBlockSequence(indent)
	}
	return p.parseBlockMapping(indent)
}

func isSequenceItem(content string) bool {
	return content == "-" || strings.HasPrefix(content, "- ")
}

func (p *blockParser) parseBlockSequence(indent int) (any, error) {
	out := []any{}
	for p.pos < len(p.lines) {
		line := p.lines[p.pos]
		if line.indent < indent {
			break
		}
		if line.indent > indent || !isSequenceItem(line.content) {
			return nil, &ClashYAMLError{Detail: "列表项缩进不一致"}
		}
		rest := strings.TrimPrefix(strings.TrimPrefix(line.content, "-"), " ")
		p.pos++
		if rest == "" {
			v, err := p.parseChildBlock(indent)
			if err != nil {
				return nil, err
			}
			out = append(out, v)
			continue
		}
		v, err := p.parseInlineItem(rest, indent)
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
	return out, nil
}

// parseInlineItem 序列项的内联值：flow 容器、`- key: v` 映射首键、标量。
func (p *blockParser) parseInlineItem(rest string, dashIndent int) (any, error) {
	if strings.HasPrefix(rest, "{") || strings.HasPrefix(rest, "[") {
		v, consumed, err := parseFlow(rest, 0)
		if err != nil {
			return nil, err
		}
		if consumed != len(rest) {
			return nil, &ClashYAMLError{Detail: "流式值后存在多余内容"}
		}
		return v, nil
	}
	if key, val, hasVal, ok := splitMapEntry(rest); ok {
		// `- key: v`：该项是块映射，后续键与首键同缩进（dash 缩进 + 2）
		itemIndent := dashIndent + 2
		virtual := append([]yamlLine{{indent: itemIndent, content: inlineMapRest(key, val, hasVal)}}, p.remainingDeeper(itemIndent)...)
		sub := &blockParser{lines: virtual}
		return sub.parseBlockMapping(itemIndent)
	}
	return resolveScalarErr(rest)
}

func inlineMapRest(key, val string, hasVal bool) string {
	if hasVal {
		return key + ": " + val
	}
	return key + ":"
}

// remainingDeeper 取缩进 >= minIndent 的后续行（属于该项映射的其余键）。
func (p *blockParser) remainingDeeper(minIndent int) []yamlLine {
	var out []yamlLine
	for p.pos < len(p.lines) && p.lines[p.pos].indent >= minIndent {
		out = append(out, p.lines[p.pos])
		p.pos++
	}
	return out
}

func (p *blockParser) parseChildBlock(parentIndent int) (any, error) {
	if p.pos >= len(p.lines) {
		return nil, nil
	}
	next := p.lines[p.pos]
	if next.indent <= parentIndent {
		return nil, nil
	}
	return p.parseValue(next.indent)
}

func (p *blockParser) parseBlockMapping(indent int) (any, error) {
	out := map[string]any{}
	for p.pos < len(p.lines) {
		line := p.lines[p.pos]
		if line.indent < indent {
			break
		}
		if line.indent > indent {
			return nil, &ClashYAMLError{Detail: "映射键缩进不一致"}
		}
		if isSequenceItem(line.content) {
			break
		}
		key, val, hasVal, ok := splitMapEntry(line.content)
		if !ok {
			return nil, &ClashYAMLError{Detail: "映射行缺少「键: 值」结构"}
		}
		p.pos++
		keyStr, err := mapKeyString(key)
		if err != nil {
			return nil, err
		}
		if !hasVal {
			// PyYAML 语义：块序列允许与父键同缩进（`key:` 后顶格 `-`）——
			// yaml.safe_dump 的列表值与多数订阅原文都是这一形态。
			if p.pos < len(p.lines) && p.lines[p.pos].indent == indent &&
				isSequenceItem(p.lines[p.pos].content) {
				v, err := p.parseBlockSequence(indent)
				if err != nil {
					return nil, err
				}
				out[keyStr] = v
				continue
			}
			v, err := p.parseChildBlock(indent)
			if err != nil {
				return nil, err
			}
			out[keyStr] = v
			continue
		}
		v, err := p.parseBlockScalarValue(val)
		if err != nil {
			return nil, err
		}
		out[keyStr] = v
	}
	return out, nil
}

// parseBlockScalarValue 块上下文的值：flow 容器 / 引号标量 / plain 标量。
func (p *blockParser) parseBlockScalarValue(val string) (any, error) {
	if strings.HasPrefix(val, "{") || strings.HasPrefix(val, "[") {
		v, consumed, err := parseFlow(val, 0)
		if err != nil {
			return nil, err
		}
		if consumed != len(val) {
			return nil, &ClashYAMLError{Detail: "流式值后存在多余内容"}
		}
		return v, nil
	}
	if len(val) >= 2 && (val[0] == '"' || val[0] == '\'') {
		return quotedScalar(val)
	}
	if val == "|" || val == ">" || strings.HasPrefix(val, "|") || strings.HasPrefix(val, ">") {
		return nil, &ClashYAMLError{Detail: "不支持多行字面量标量"}
	}
	if val[0] == '&' || val[0] == '*' {
		return nil, &ClashYAMLError{Detail: "不支持锚点/别名"}
	}
	return resolveScalarErr(val)
}

// resolveScalarErr 带错误位的 plain 标量解析（plain resolver 不产错）。
func resolveScalarErr(s string) (any, error) {
	return resolveScalarText(s), nil
}

// splitMapEntry 拆「键: 值」：键可为引号或 plain；`:` 后必须是空白或行尾。
func splitMapEntry(content string) (key, val string, hasVal, ok bool) {
	inSingle, inDouble := false, false
	depth := 0
	for i := 0; i < len(content); i++ {
		c := content[i]
		switch {
		case inSingle:
			if c == '\'' {
				if i+1 < len(content) && content[i+1] == '\'' {
					i++
					continue
				}
				inSingle = false
			}
		case inDouble:
			if c == '\\' {
				i++
			} else if c == '"' {
				inDouble = false
			}
		case c == '\'':
			inSingle = true
		case c == '"':
			inDouble = true
		case c == '{' || c == '[':
			depth++
		case c == '}' || c == ']':
			depth--
		case c == ':' && depth == 0:
			if i+1 == len(content) || content[i+1] == ' ' {
				key = strings.TrimRight(content[:i], " ")
				rest := strings.TrimLeft(content[i+1:], " ")
				return key, rest, rest != "", true
			}
		}
	}
	return "", "", false, false
}

// mapKeyString 键标量 → 字符串（plain 键同样过 resolver，int 键转十进制
// 形态，对齐 json.dumps 的键序列化）。
func mapKeyString(key string) (string, error) {
	if key == "<<" {
		return "", &ClashYAMLError{Detail: "不支持 merge 键"}
	}
	if len(key) >= 2 && (key[0] == '"' || key[0] == '\'') {
		v, err := quotedScalar(key)
		if err != nil {
			return "", err
		}
		s, ok := v.(string)
		if !ok {
			return "", &ClashYAMLError{Detail: "映射键不是字符串"}
		}
		return s, nil
	}
	v := resolveScalarText(key)
	if s, ok := v.(string); ok {
		return s, nil
	}
	switch n := v.(type) {
	case int64:
		return strconv.FormatInt(n, 10), nil
	case float64:
		return formatPyFloat(n), nil
	case bool:
		if n {
			return "True", nil
		}
		return "False", nil
	case nil:
		return "None", nil
	}
	return "", &ClashYAMLError{Detail: "不支持的映射键形态"}
}

// ── 标量 resolver（PyYAML YAML 1.1 隐式解析对齐）───────────────────

var (
	reYAMLBool    = regexp.MustCompile(`^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$`)
	reYAMLNull    = regexp.MustCompile(`^(?:~|null|Null|NULL|)$`)
	reYAMLInt     = regexp.MustCompile(`^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(:[0-5]?[0-9])+)$`)
	reYAMLFloat   = regexp.MustCompile(`^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9_]+(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$`)
	reSexagesimal = regexp.MustCompile(`^[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+(\.[0-9_]*)?$`)
)

// resolveScalarText plain 标量 → Python 值（resolver 命中顺序 bool → null →
// int → float → 字符串）。
func resolveScalarText(s string) any {
	if reYAMLNull.MatchString(s) {
		return nil
	}
	if reYAMLBool.MatchString(s) {
		switch strings.ToLower(s) {
		case "yes", "true", "on":
			return true
		}
		return false
	}
	if reYAMLInt.MatchString(s) {
		if v, ok := parseYAMLInt(s); ok {
			return v
		}
		return s
	}
	if reYAMLFloat.MatchString(s) {
		if v, ok := parseYAMLFloat(s); ok {
			return v
		}
		return s
	}
	return s
}

func parseYAMLInt(s string) (int64, bool) {
	neg := false
	body := s
	if strings.HasPrefix(body, "-") {
		neg = true
		body = body[1:]
	} else if strings.HasPrefix(body, "+") {
		body = body[1:]
	}
	clean := strings.ReplaceAll(body, "_", "")
	var v int64
	switch {
	case strings.HasPrefix(clean, "0b"):
		n, err := strconv.ParseInt(clean[2:], 2, 64)
		if err != nil {
			return 0, false
		}
		v = n
	case strings.HasPrefix(clean, "0x"):
		n, err := strconv.ParseInt(clean[2:], 16, 64)
		if err != nil {
			return 0, false
		}
		v = n
	case len(clean) > 1 && clean[0] == '0':
		n, err := strconv.ParseInt(clean[1:], 8, 64)
		if err != nil {
			return 0, false
		}
		v = n
	case strings.Contains(clean, ":"):
		n, ok := parseSexagesimalInt(clean)
		if !ok {
			return 0, false
		}
		v = n
	default:
		n, err := strconv.ParseInt(clean, 10, 64)
		if err != nil {
			return 0, false
		}
		v = n
	}
	if neg {
		v = -v
	}
	return v, true
}

// parseSexagesimalInt YAML 1.1 性六十进制（1:30 → 90）。
func parseSexagesimalInt(s string) (int64, bool) {
	parts := strings.Split(s, ":")
	total, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return 0, false
	}
	for _, p := range parts[1:] {
		n, err := strconv.ParseInt(p, 10, 64)
		if err != nil {
			return 0, false
		}
		total = total*60 + n
	}
	return total, true
}

func parseYAMLFloat(s string) (float64, bool) {
	lower := strings.ToLower(s)
	if strings.Contains(lower, "inf") {
		if strings.HasPrefix(s, "-") {
			return math.Inf(-1), true
		}
		return math.Inf(1), true
	}
	if strings.Contains(lower, "nan") {
		return math.NaN(), true
	}
	if strings.Contains(s, ":") {
		if !reSexagesimal.MatchString(s) {
			return 0, false
		}
		neg := strings.HasPrefix(s, "-")
		body := strings.TrimLeft(s, "+-")
		intPart := body
		frac := ""
		if i := strings.Index(body, "."); i >= 0 {
			intPart, frac = body[:i], body[i+1:]
		}
		total, ok := parseSexagesimalInt(intPart)
		if !ok {
			return 0, false
		}
		f := float64(total)
		if frac != "" {
			fv, err := strconv.ParseFloat("0."+strings.ReplaceAll(frac, "_", ""), 64)
			if err != nil {
				return 0, false
			}
			f += fv
		}
		if neg {
			f = -f
		}
		return f, true
	}
	f, err := strconv.ParseFloat(strings.ReplaceAll(s, "_", ""), 64)
	if err != nil {
		return 0, false
	}
	return f, true
}

// ── 引号标量 ──────────────────────────────────────────────────────

// quotedScalar 解析整段引号标量（必须整段恰好是一个引号单元）。
func quotedScalar(s string) (any, error) {
	if strings.HasPrefix(s, "'") {
		end := 1
		var sb strings.Builder
		for end < len(s) {
			if s[end] == '\'' {
				if end+1 < len(s) && s[end+1] == '\'' {
					sb.WriteByte('\'')
					end += 2
					continue
				}
				end++
				if strings.TrimSpace(s[end:]) != "" {
					return nil, &ClashYAMLError{Detail: "单引号标量后存在多余内容"}
				}
				return sb.String(), nil
			}
			sb.WriteByte(s[end])
			end++
		}
		return nil, &ClashYAMLError{Detail: "单引号未闭合"}
	}
	if strings.HasPrefix(s, "\"") {
		v, end, err := parseDoubleQuoted(s)
		if err != nil {
			return nil, err
		}
		if strings.TrimSpace(s[end:]) != "" {
			return nil, &ClashYAMLError{Detail: "双引号标量后存在多余内容"}
		}
		return v, nil
	}
	return nil, &ClashYAMLError{Detail: "不是引号标量"}
}

func parseDoubleQuoted(s string) (string, int, error) {
	var sb strings.Builder
	i := 1
	for i < len(s) {
		c := s[i]
		if c == '"' {
			return sb.String(), i + 1, nil
		}
		if c == '\\' {
			i++
			if i >= len(s) {
				return "", 0, &ClashYAMLError{Detail: "双引号转义未闭合"}
			}
			e := s[i]
			switch e {
			case '0':
				sb.WriteByte(0)
			case 'a':
				sb.WriteByte(7)
			case 'b':
				sb.WriteByte(8)
			case 't', '\t':
				sb.WriteByte('\t')
			case 'n':
				sb.WriteByte('\n')
			case 'v':
				sb.WriteByte(11)
			case 'f':
				sb.WriteByte(12)
			case 'r':
				sb.WriteByte('\r')
			case 'e':
				sb.WriteByte(27)
			case ' ':
				sb.WriteByte(' ')
			case '"':
				sb.WriteByte('"')
			case '/':
				sb.WriteByte('/')
			case '\\':
				sb.WriteByte('\\')
			case 'N':
				sb.WriteRune(0x85)
			case '_':
				sb.WriteRune(0xA0)
			case 'L':
				sb.WriteRune(0x2028)
			case 'P':
				sb.WriteRune(0x2029)
			case 'x', 'u', 'U':
				width := map[byte]int{'x': 2, 'u': 4, 'U': 8}[e]
				if i+width >= len(s) {
					return "", 0, &ClashYAMLError{Detail: "十六进制转义不完整"}
				}
				n, err := strconv.ParseUint(s[i+1:i+1+width], 16, 32)
				if err != nil {
					return "", 0, &ClashYAMLError{Detail: "十六进制转义非法"}
				}
				sb.WriteRune(rune(n))
				i += width
			default:
				return "", 0, &ClashYAMLError{Detail: fmt.Sprintf("未知转义 \\%c", e)}
			}
			i++
			continue
		}
		sb.WriteByte(c)
		i++
	}
	return "", 0, &ClashYAMLError{Detail: "双引号未闭合"}
}

// ── 流式集合 ──────────────────────────────────────────────────────

// parseFlow 流式映射/序列递归下降，返回值与消费长度。
func parseFlow(s string, pos int) (any, int, error) {
	pos = skipFlowSpace(s, pos)
	if pos >= len(s) {
		return nil, pos, &ClashYAMLError{Detail: "流式值意外结束"}
	}
	switch s[pos] {
	case '{':
		return parseFlowMapping(s, pos)
	case '[':
		return parseFlowSequence(s, pos)
	}
	return nil, pos, &ClashYAMLError{Detail: "流式值不以 { 或 [ 开头"}
}

func skipFlowSpace(s string, pos int) int {
	for pos < len(s) && (s[pos] == ' ' || s[pos] == '\t') {
		pos++
	}
	return pos
}

func parseFlowMapping(s string, pos int) (any, int, error) {
	out := map[string]any{}
	pos++ // consume {
	for {
		pos = skipFlowSpace(s, pos)
		if pos >= len(s) {
			return nil, pos, &ClashYAMLError{Detail: "流式映射未闭合"}
		}
		if s[pos] == '}' {
			return out, pos + 1, nil
		}
		// 键
		var keyStr string
		var err error
		if s[pos] == '"' || s[pos] == '\'' {
			quoted, end, qerr := quotedUnit(s, pos)
			if qerr != nil {
				return nil, pos, qerr
			}
			k, ok := quoted.(string)
			if !ok {
				return nil, pos, &ClashYAMLError{Detail: "流式键不是字符串"}
			}
			keyStr = k
			pos = end
		} else {
			start := pos
			for pos < len(s) && s[pos] != ':' && s[pos] != ',' && s[pos] != '}' {
				pos++
			}
			keyStr, err = mapKeyString(strings.TrimSpace(s[start:pos]))
			if err != nil {
				return nil, pos, err
			}
		}
		pos = skipFlowSpace(s, pos)
		var val any = nil
		if pos < len(s) && s[pos] == ':' {
			pos++
			pos = skipFlowSpace(s, pos)
			if pos < len(s) && (s[pos] == '{' || s[pos] == '[') {
				val, pos, err = parseFlow(s, pos)
				if err != nil {
					return nil, pos, err
				}
			} else if pos < len(s) && (s[pos] == '"' || s[pos] == '\'') {
				val, pos, err = quotedUnit(s, pos)
				if err != nil {
					return nil, pos, err
				}
			} else {
				start := pos
				for pos < len(s) && s[pos] != ',' && s[pos] != '}' {
					pos++
				}
				text := strings.TrimSpace(s[start:pos])
				if strings.HasPrefix(text, "*") || strings.HasPrefix(text, "&") {
					return nil, pos, &ClashYAMLError{Detail: "不支持锚点/别名"}
				}
				val = resolveScalarText(text)
			}
		}
		out[keyStr] = val
		pos = skipFlowSpace(s, pos)
		if pos < len(s) && s[pos] == ',' {
			pos++
			continue
		}
		if pos < len(s) && s[pos] == '}' {
			return out, pos + 1, nil
		}
		return nil, pos, &ClashYAMLError{Detail: "流式映射缺少分隔符"}
	}
}

func parseFlowSequence(s string, pos int) (any, int, error) {
	out := []any{}
	pos++ // consume [
	for {
		pos = skipFlowSpace(s, pos)
		if pos >= len(s) {
			return nil, pos, &ClashYAMLError{Detail: "流式序列未闭合"}
		}
		if s[pos] == ']' {
			return out, pos + 1, nil
		}
		var val any
		var err error
		if s[pos] == '{' || s[pos] == '[' {
			val, pos, err = parseFlow(s, pos)
			if err != nil {
				return nil, pos, err
			}
		} else if s[pos] == '"' || s[pos] == '\'' {
			val, pos, err = quotedUnit(s, pos)
			if err != nil {
				return nil, pos, err
			}
		} else {
			start := pos
			for pos < len(s) && s[pos] != ',' && s[pos] != ']' {
				pos++
			}
			text := strings.TrimSpace(s[start:pos])
			if strings.HasPrefix(text, "*") || strings.HasPrefix(text, "&") {
				return nil, pos, &ClashYAMLError{Detail: "不支持锚点/别名"}
			}
			val = resolveScalarText(text)
		}
		out = append(out, val)
		pos = skipFlowSpace(s, pos)
		if pos < len(s) && s[pos] == ',' {
			pos++
			continue
		}
		if pos < len(s) && s[pos] == ']' {
			return out, pos + 1, nil
		}
		return nil, pos, &ClashYAMLError{Detail: "流式序列缺少分隔符"}
	}
}

// quotedUnit 流上下文的引号标量（返回值与结束位置）。
func quotedUnit(s string, pos int) (any, int, error) {
	if s[pos] == '\'' {
		var sb strings.Builder
		i := pos + 1
		for i < len(s) {
			if s[i] == '\'' {
				if i+1 < len(s) && s[i+1] == '\'' {
					sb.WriteByte('\'')
					i += 2
					continue
				}
				return sb.String(), i + 1, nil
			}
			sb.WriteByte(s[i])
			i++
		}
		return nil, pos, &ClashYAMLError{Detail: "单引号未闭合"}
	}
	v, end, err := parseDoubleQuoted(s[pos:])
	if err != nil {
		return nil, pos, err
	}
	return v, pos + end, nil
}
