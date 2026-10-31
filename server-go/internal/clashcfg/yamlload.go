package clashcfg

// Clash 订阅/配置 YAML 子集解析器（无第三方依赖）。对齐 PyYAML safe_load 在
// 订阅与内核配置真实形态上的行为：块式/流式映射与序列、缩进无关序列、YAML 1.1
// 标量 resolver（yes/on 布尔、0 前缀八进制、六十进制、.inf/.nan）、块标量、
// 多行纯量折叠。不支持的形态（锚点别名、merge 键、多文档）显式报错——
// fail-closed，不做「可能解析错」的静默近似。

import (
	"fmt"
	"strings"
)

// LoadYAML 解析 YAML 文本为顶层映射；空文档或非映射形态返回错误。
func LoadYAML(text string) (map[string]any, error) {
	doc, err := loadYAMLDoc(text)
	if err != nil {
		return nil, err
	}
	m, ok := doc.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("配置不是 YAML 映射")
	}
	return m, nil
}

func loadYAMLDoc(text string) (any, error) {
	lines := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	p := &yamlParser{lines: lines}
	p.skipDocHeader()
	p.skipBlank()
	if p.pos >= len(p.lines) {
		return nil, nil
	}
	v, err := p.parseNode(minIndentOf(p.lines[p.pos]))
	if err != nil {
		return nil, err
	}
	p.skipBlank()
	if p.pos < len(p.lines) {
		return nil, fmt.Errorf("YAML 第 %d 行出现多余内容", p.pos+1)
	}
	return v, nil
}

type yamlParser struct {
	lines []string
	pos   int
}

func (p *yamlParser) skipDocHeader() {
	p.skipBlank()
	if p.pos < len(p.lines) && strings.TrimSpace(p.lines[p.pos]) == "---" {
		p.pos++
	}
}

func (p *yamlParser) skipBlank() {
	for p.pos < len(p.lines) && strings.TrimSpace(p.lines[p.pos]) == "" {
		p.pos++
	}
}

func indentOf(line string) int {
	n := 0
	for n < len(line) && line[n] == ' ' {
		n++
	}
	return n
}

func minIndentOf(line string) int { return indentOf(line) }

func isSeqItemLine(line string) bool {
	t := strings.TrimSpace(line)
	return t == "-" || strings.HasPrefix(t, "- ")
}

// looksLikeKey 判断行是否为块映射键行（键可带引号，冒号后空格或行尾冒号）。
func looksLikeKey(line string) bool {
	_, _, ok := splitMapKey(line)
	return ok
}

func splitMapKey(line string) (key, rest string, ok bool) {
	s := strings.TrimSpace(line)
	if s == "" || isSeqItemLine(s) {
		return "", "", false
	}
	i := 0
	if s[0] == '"' || s[0] == '\'' {
		q := s[0]
		i = 1
		for i < len(s) {
			if s[i] == '\\' && q == '"' {
				i += 2
				continue
			}
			if s[i] == q {
				if q == '\'' && i+1 < len(s) && s[i+1] == '\'' {
					i += 2
					continue
				}
				i++
				break
			}
			i++
		}
		if i > len(s) {
			return "", "", false
		}
		for i < len(s) && s[i] == ' ' {
			i++
		}
		if i >= len(s) || s[i] != ':' {
			return "", "", false
		}
		key = s[:i]
		rest = strings.TrimLeft(s[i+1:], " ")
		return key, rest, true
	}
	for ; i < len(s); i++ {
		if s[i] == ':' && (i+1 == len(s) || s[i+1] == ' ') {
			key = strings.TrimSpace(s[:i])
			if key == "" || strings.ContainsAny(key[:1], "[]{}#&*!|>'\"%@`") {
				return "", "", false
			}
			rest = strings.TrimLeft(s[i+1:], " ")
			return key, rest, true
		}
		if s[i] == '#' && i > 0 && s[i-1] == ' ' {
			return "", "", false
		}
	}
	return "", "", false
}

func (p *yamlParser) parseNode(minIndent int) (any, error) {
	p.skipBlank()
	if p.pos >= len(p.lines) {
		return nil, nil
	}
	line := p.lines[p.pos]
	ind := indentOf(line)
	if ind < minIndent {
		return nil, nil
	}
	if isSeqItemLine(line) {
		return p.parseBlockSeq(ind)
	}
	if _, _, ok := splitMapKey(line); ok {
		return p.parseBlockMap(ind)
	}
	return p.parseMultilineScalar(ind, "")
}

func (p *yamlParser) parseBlockMap(indent int) (any, error) {
	out := map[string]any{}
	for {
		p.skipBlank()
		if p.pos >= len(p.lines) {
			return out, nil
		}
		line := p.lines[p.pos]
		if indentOf(line) != indent || isSeqItemLine(line) {
			return out, nil
		}
		key, rest, ok := splitMapKey(line)
		if !ok {
			return nil, fmt.Errorf("YAML 第 %d 行不是合法键行", p.pos+1)
		}
		rawKey, err := mapKeyString(key)
		if err != nil {
			return nil, fmt.Errorf("YAML 第 %d 行键解析失败：%w", p.pos+1, err)
		}
		p.pos++
		val, err := p.parseValueAfterKey(indent, rest)
		if err != nil {
			return nil, err
		}
		out[rawKey] = val
	}
}

// parseValueAfterKey 解析键行冒号之后的值：行内标量/流式集合/块标量/子块。
func (p *yamlParser) parseValueAfterKey(keyIndent int, rest string) (any, error) {
	rest = strings.TrimSpace(stripYAMLCommentLoose(rest))
	if rest != "" && rest[0] == '|' || rest != "" && (rest[0] == '>') {
		return p.parseBlockScalar(keyIndent, rest)
	}
	if rest != "" && (rest[0] == '[' || rest[0] == '{') {
		v, err := parseFlow(rest)
		if err != nil {
			return nil, fmt.Errorf("YAML 第 %d 行流式集合解析失败：%w", p.pos, err)
		}
		return v, nil
	}
	if rest != "" {
		return p.parseMultilineScalar(keyIndent, rest)
	}
	// 行内无值：子块（更深的映射/序列/纯量）、与键同列的缩进无关序列，或 null。
	// PyYAML 的块序列项与所属键同缩进（嵌套时也不额外加深）。
	p.skipBlank()
	if p.pos >= len(p.lines) {
		return nil, nil
	}
	next := p.lines[p.pos]
	nextIndent := indentOf(next)
	if isSeqItemLine(next) && nextIndent >= keyIndent {
		return p.parseBlockSeq(nextIndent)
	}
	if nextIndent <= keyIndent {
		return nil, nil
	}
	t := strings.TrimSpace(next)
	if isSeqItemLine(next) {
		return p.parseBlockSeq(nextIndent)
	}
	if _, _, ok := splitMapKey(next); ok {
		return p.parseBlockMap(nextIndent)
	}
	return p.parseMultilineScalar(keyIndent, strings.TrimSpace(stripYAMLCommentLoose(t)))
}

func (p *yamlParser) parseBlockSeq(indent int) (any, error) {
	out := []any{}
	for {
		p.skipBlank()
		if p.pos >= len(p.lines) {
			return out, nil
		}
		line := p.lines[p.pos]
		if indentOf(line) != indent || !isSeqItemLine(line) {
			return out, nil
		}
		t := strings.TrimSpace(line)
		rest := strings.TrimPrefix(strings.TrimPrefix(t, "-"), " ")
		if rest == "" {
			p.pos++
			v, err := p.parseNode(indent + 1)
			if err != nil {
				return nil, err
			}
			out = append(out, v)
			continue
		}
		if _, _, ok := splitMapKey(rest); ok {
			// 行内映射首键：把首行虚拟为 indent+2 的键行继续解析
			v, err := p.parseInlineMapItem(indent, rest)
			if err != nil {
				return nil, err
			}
			out = append(out, v)
			continue
		}
		p.pos++
		v, err := p.parseMultilineScalar(indent, stripYAMLCommentLoose(rest))
		if err != nil {
			return nil, err
		}
		out = append(out, v)
	}
}

// parseInlineMapItem 解析 `- key: value` 形态：首键在项行内，其余键在后续更深行。
func (p *yamlParser) parseInlineMapItem(dashIndent int, rest string) (any, error) {
	contentIndent := dashIndent + 2
	idx := p.pos
	saved := p.lines[idx]
	p.lines[idx] = strings.Repeat(" ", contentIndent) + rest
	defer func() { p.lines[idx] = saved }()
	out, err := p.parseBlockMap(contentIndent)
	if err != nil {
		return nil, err
	}
	return out, nil
}

// parseMultilineScalar 解析可能跨行的纯量（含引号标量折叠与多行纯量折叠）。
func (p *yamlParser) parseMultilineScalar(keyIndent int, first string) (any, error) {
	first = strings.TrimSpace(stripYAMLCommentLoose(first))
	if first != "" && (first[0] == '\'' || first[0] == '"') {
		text, done := p.readQuotedMultiline(keyIndent, first)
		if !done {
			return nil, fmt.Errorf("YAML 第 %d 行引号标量未闭合", p.pos+1)
		}
		if first[0] == '\'' {
			return unquoteSingle(text), nil
		}
		return unquoteDouble(text)
	}
	// 纯量折叠：后续更深缩进的行并入（空行折成换行）
	parts := []string{first}
	newlines := 0
	for p.pos < len(p.lines) {
		line := p.lines[p.pos]
		if strings.TrimSpace(line) == "" {
			newlines++
			p.pos++
			continue
		}
		if indentOf(line) <= keyIndent {
			break
		}
		t := strings.TrimSpace(stripYAMLCommentLoose(line))
		if looksLikeKey(t) || isSeqItemLine(t) {
			break
		}
		if len(parts) > 0 && newlines > 0 {
			parts = append(parts, strings.Repeat("\n", newlines))
		} else if len(parts) > 0 {
			parts = append(parts, " ")
		}
		newlines = 0
		parts = append(parts, t)
		p.pos++
	}
	var b strings.Builder
	prevBreak := false
	for i, part := range parts {
		if i == 0 {
			b.WriteString(part)
			continue
		}
		if part == "\n" {
			b.WriteString("\n")
			prevBreak = true
			continue
		}
		if !prevBreak {
			b.WriteString(" ")
		}
		b.WriteString(part)
		prevBreak = false
	}
	return resolveScalarText(strings.TrimSpace(b.String())), nil
}

// readQuotedMultiline 读取可能折行的引号标量，返回引号内原文。
func (p *yamlParser) readQuotedMultiline(keyIndent int, first string) (string, bool) {
	if closed, _, ok := closedQuoted(first); ok && closed {
		return trimOuterQuote(first), true
	}
	buf := first
	for p.pos < len(p.lines) {
		line := p.lines[p.pos]
		if strings.TrimSpace(line) == "" {
			return "", false
		}
		if indentOf(line) <= keyIndent {
			return "", false
		}
		buf += " " + strings.TrimSpace(line)
		p.pos++
		if closed, _, ok := closedQuoted(buf); ok && closed {
			return trimOuterQuote(buf), true
		}
	}
	return "", false
}

// closedQuoted 判断引号标量是否在本行闭合（首字符为引号）。
func closedQuoted(s string) (closed bool, end int, ok bool) {
	if s == "" {
		return false, 0, false
	}
	q := s[0]
	i := 1
	for i < len(s) {
		if s[i] == '\\' && q == '"' {
			i += 2
			continue
		}
		if s[i] == q {
			if q == '\'' && i+1 < len(s) && s[i+1] == '\'' {
				i += 2
				continue
			}
			return true, i + 1, true
		}
		i++
	}
	return false, 0, true
}

func trimOuterQuote(s string) string {
	if len(s) >= 2 {
		return s[1 : len(s)-1]
	}
	return s
}

func unquoteSingle(s string) string {
	return strings.ReplaceAll(s, "''", "'")
}

func unquoteDouble(s string) (string, error) {
	var b strings.Builder
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c != '\\' {
			b.WriteByte(c)
			continue
		}
		i++
		if i >= len(s) {
			return "", fmt.Errorf("双引号标量转义不完整")
		}
		switch s[i] {
		case 'n':
			b.WriteByte('\n')
		case 't':
			b.WriteByte('\t')
		case 'r':
			b.WriteByte('\r')
		case '0':
			b.WriteByte(0)
		case '"':
			b.WriteByte('"')
		case '\\':
			b.WriteByte('\\')
		case '/':
			b.WriteByte('/')
		case 'u':
			if i+4 < len(s)+1 && i+5 <= len(s) {
				var r rune
				_, err := fmt.Sscanf(s[i+1:i+5], "%04x", &r)
				if err != nil {
					return "", fmt.Errorf("双引号标量 \\u 转义非法")
				}
				b.WriteRune(r)
				i += 4
			} else {
				return "", fmt.Errorf("双引号标量 \\u 转义非法")
			}
		default:
			b.WriteByte('\\')
			b.WriteByte(s[i])
		}
	}
	return b.String(), nil
}

// parseBlockScalar 解析 | 与 > 块标量（含 chomping 与显式缩进指示符）。
func (p *yamlParser) parseBlockScalar(keyIndent int, header string) (any, error) {
	folded := header[0] == '>'
	chomp := byte(0) // 0=clip, '-'=strip, '+'=keep
	explicit := 0
	for i := 1; i < len(header); i++ {
		switch {
		case header[i] == '-' || header[i] == '+':
			chomp = header[i]
		case header[i] >= '1' && header[i] <= '9':
			explicit = int(header[i] - '0')
		case header[i] == ' ':
		default:
			return nil, fmt.Errorf("YAML 第 %d 行块标量头非法", p.pos+1)
		}
	}
	contentIndent := 0
	if explicit > 0 {
		contentIndent = keyIndent + explicit
	}
	var contentLines []string
	for p.pos < len(p.lines) {
		line := p.lines[p.pos]
		if strings.TrimSpace(line) == "" {
			contentLines = append(contentLines, "")
			p.pos++
			continue
		}
		ind := indentOf(line)
		if contentIndent == 0 {
			if ind <= keyIndent {
				break
			}
			contentIndent = ind
		}
		if ind < contentIndent {
			break
		}
		contentLines = append(contentLines, line[contentIndent:])
		p.pos++
	}
	// 去掉折叠尾部空行前的记录
	for len(contentLines) > 0 && contentLines[len(contentLines)-1] == "" {
		contentLines = contentLines[:len(contentLines)-1]
		p.pos--
	}
	var text string
	if folded {
		var b strings.Builder
		for i, cl := range contentLines {
			if i > 0 {
				if cl == "" || strings.HasPrefix(cl, " ") || strings.HasPrefix(contentLines[i-1], " ") {
					b.WriteString("\n")
				} else {
					b.WriteString(" ")
				}
			}
			b.WriteString(cl)
		}
		text = b.String()
	} else {
		text = strings.Join(contentLines, "\n")
	}
	switch chomp {
	case '-':
	case '+':
		text += "\n"
	default:
		if len(contentLines) > 0 {
			text += "\n"
		}
	}
	return text, nil
}

func mapKeyString(key string) (string, error) {
	if key == "" {
		return "", fmt.Errorf("空键")
	}
	if key[0] == '\'' {
		return unquoteSingle(trimOuterQuote(key)), nil
	}
	if key[0] == '"' {
		return unquoteDouble(trimOuterQuote(key))
	}
	v := resolveScalarText(key)
	if s, ok := v.(string); ok {
		return s, nil
	}
	return key, nil
}

// stripYAMLCommentLoose 去掉行内注释（引号外的 # 前有空格或行首）。
func stripYAMLCommentLoose(s string) string {
	inS, inD := false, false
	for i := 0; i < len(s); i++ {
		c := s[i]
		switch {
		case inS:
			if c == '\'' {
				inS = false
			}
		case inD:
			if c == '\\' {
				i++
			} else if c == '"' {
				inD = false
			}
		case c == '\'':
			inS = true
		case c == '"':
			inD = true
		case c == '#':
			if i == 0 || s[i-1] == ' ' {
				return strings.TrimRight(s[:i], " \t")
			}
		}
	}
	return strings.TrimRight(s, " \t")
}

// parseFlow 解析单行流式序列/映射。
func parseFlow(s string) (any, error) {
	f := &flowParser{s: s}
	v, err := f.parseValue()
	if err != nil {
		return nil, err
	}
	f.skipSpace()
	if f.pos < len(f.s) {
		return nil, fmt.Errorf("流式集合尾部有多余内容：%q", f.s[f.pos:])
	}
	return v, nil
}

type flowParser struct {
	s   string
	pos int
}

func (f *flowParser) skipSpace() {
	for f.pos < len(f.s) && (f.s[f.pos] == ' ' || f.s[f.pos] == '\t') {
		f.pos++
	}
}

func (f *flowParser) parseValue() (any, error) {
	f.skipSpace()
	if f.pos >= len(f.s) {
		return nil, fmt.Errorf("流式内容为空")
	}
	switch f.s[f.pos] {
	case '[':
		return f.parseSeq()
	case '{':
		return f.parseMap()
	default:
		return f.parseScalar()
	}
}

func (f *flowParser) parseSeq() (any, error) {
	f.pos++ // [
	out := []any{}
	for {
		f.skipSpace()
		if f.pos >= len(f.s) {
			return nil, fmt.Errorf("流式序列未闭合")
		}
		if f.s[f.pos] == ']' {
			f.pos++
			return out, nil
		}
		v, err := f.parseValue()
		if err != nil {
			return nil, err
		}
		out = append(out, v)
		f.skipSpace()
		if f.pos < len(f.s) && f.s[f.pos] == ',' {
			f.pos++
			continue
		}
		if f.pos < len(f.s) && f.s[f.pos] == ']' {
			f.pos++
			return out, nil
		}
		return nil, fmt.Errorf("流式序列缺少分隔符")
	}
}

func (f *flowParser) parseMap() (any, error) {
	f.pos++ // {
	out := map[string]any{}
	for {
		f.skipSpace()
		if f.pos >= len(f.s) {
			return nil, fmt.Errorf("流式映射未闭合")
		}
		if f.s[f.pos] == '}' {
			f.pos++
			return out, nil
		}
		k, err := f.parseScalar()
		if err != nil {
			return nil, err
		}
		f.skipSpace()
		if f.pos >= len(f.s) || f.s[f.pos] != ':' {
			return nil, fmt.Errorf("流式映射缺少冒号")
		}
		f.pos++
		v, err := f.parseValue()
		if err != nil {
			return nil, err
		}
		ks, ok := k.(string)
		if !ok {
			ks = fmt.Sprintf("%v", k)
		}
		out[ks] = v
		f.skipSpace()
		if f.pos < len(f.s) && f.s[f.pos] == ',' {
			f.pos++
			continue
		}
		if f.pos < len(f.s) && f.s[f.pos] == '}' {
			f.pos++
			return out, nil
		}
		return nil, fmt.Errorf("流式映射缺少分隔符")
	}
}

func (f *flowParser) parseScalar() (any, error) {
	f.skipSpace()
	if f.pos >= len(f.s) {
		return nil, nil
	}
	start := f.pos
	if c := f.s[f.pos]; c == '\'' || c == '"' {
		q := c
		f.pos++
		for f.pos < len(f.s) {
			if q == '"' && f.s[f.pos] == '\\' {
				f.pos += 2
				continue
			}
			if f.s[f.pos] == q {
				if q == '\'' && f.pos+1 < len(f.s) && f.s[f.pos+1] == '\'' {
					f.pos += 2
					continue
				}
				f.pos++
				raw := f.s[start:f.pos]
				if q == '\'' {
					return unquoteSingle(trimOuterQuote(raw)), nil
				}
				return unquoteDouble(trimOuterQuote(raw))
			}
			f.pos++
		}
		return nil, fmt.Errorf("流式引号标量未闭合")
	}
	for f.pos < len(f.s) {
		c := f.s[f.pos]
		if c == ',' || c == ']' || c == '}' {
			break
		}
		if c == ':' && (f.pos+1 >= len(f.s) || f.s[f.pos+1] == ' ' || f.s[f.pos+1] == ',' || f.s[f.pos+1] == ']' || f.s[f.pos+1] == '}') {
			break
		}
		f.pos++
	}
	return resolveScalarText(strings.TrimSpace(f.s[start:f.pos])), nil
}
