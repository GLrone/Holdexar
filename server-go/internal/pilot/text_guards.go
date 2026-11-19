package pilot

import (
	"regexp"
	"strings"
	"unicode"
)

const (
	truncatedMinChars     = 40
	thinkLeadLen          = 120
	narrationTailMaxChars = 200
)

var (
	leakMarkers = []string{
		"（系统侧同步", "系统侧同步", "弹性 SVG", "视觉降级触发契约", "视觉增强的触发条件",
		"Per the instructions", "According to my rules", "Let me think",
		"I need to check", "让我看看有哪些工具", "我的规则", "按照规则",
		"<｜DSML｜", "emit_card",
	}
	leakLineHints = []string{
		"我需要", "我应该", "让我", "我手头", "我注意到", "我看一下", "我先确认",
		"I should", "I need to", "Let me", "I'll check",
	}
	toolEnvelopeRe    = regexp.MustCompile(`(?s)<｜DSML｜tool_calls>.*?</｜DSML｜tool_calls>|<｜DSML｜parameter.*?</｜DSML｜parameter>`)
	toolEnvelopeTagRe = regexp.MustCompile(`</?｜DSML｜(?:invoke|tool_calls|parameter)[^>]*>`)
	blankLinesRe      = regexp.MustCompile(`\n{3,}`)
	paragraphSplitRe  = regexp.MustCompile(`\n\s*\n`)
	sentSplitRe       = regexp.MustCompile(`[。！？\n]`)

	narrationLead = []string{
		"我来", "让我", "我这就", "我先", "我先去", "我去", "我再", "我重新", "我接着",
		"我马上", "我看看", "我看一下", "我查一下", "我确认", "我核实", "我试着", "我准备",
		"我需要", "我要", "我想", "我得", "我把它", "我可以帮你", "我帮你", "我给您",
		"我注意", "我发现", "我这边",
	}
	narrationMeta = []string{
		"用户问的是", "用户想知道", "用户可能", "这是一个关于", "这个问题属于",
		"这个问题得", "这个问题需要", "最合理的做法", "我的规则", "按照规则", "根据规则",
		"我没有那个上下文", "先说明一个情况", "先说明一个关键事实",
	}
	narrationEN = []string{
		"let me", "i'll", "i need to", "i should", "let's", "actually,", "i want to", "i will",
	}
	narrationPayload = []string{"¥", "元", "%", "|", "## 结论", "**结论", "结论："}
)

func endsMidClause(body string) bool {
	if body == "" {
		return false
	}
	runes := []rune(body)
	tail := runes[len(runes)-1]
	return tail == '，' || tail == ',' || tail == '、' || unicode.IsDigit(tail)
}

func looksTruncated(text string, toolsRan int, cards []any) bool {
	body := strings.TrimSpace(text)
	if body == "" || (toolsRan == 0 && len(cards) == 0) {
		return false
	}
	return len([]rune(body)) < truncatedMinChars && endsMidClause(body)
}

func looksLikeThinking(part string) bool {
	lead := part
	if r := []rune(part); len(r) > thinkLeadLen {
		lead = string(r[:thinkLeadLen])
	}
	for _, marker := range leakMarkers {
		if strings.Contains(lead, marker) {
			return true
		}
	}
	lines := []string{}
	for _, ln := range strings.Split(part, "\n") {
		if t := strings.TrimSpace(ln); t != "" {
			lines = append(lines, t)
		}
	}
	if len(lines) == 0 {
		return false
	}
	hits := 0
	for _, ln := range lines {
		for _, hint := range leakLineHints {
			if strings.Contains(ln, hint) {
				hits++
				break
			}
		}
	}
	return hits >= 2 || (hits == 1 && len(lines) <= 3)
}

func sentIsNarration(s string) bool {
	head := strings.ToLower(s)
	if r := []rune(head); len(r) > 26 {
		head = string(r[:26])
	}
	if looksLikeThinking(s) || containsAny(s, narrationLead) || containsAny(s, narrationMeta) {
		return true
	}
	return containsAny(head, narrationEN)
}

func isSelfNarration(text string) bool {
	body := strings.TrimSpace(text)
	if body == "" {
		return true
	}
	if containsAny(body, narrationPayload) {
		return false
	}
	if toolEnvelopeRe.MatchString(body) {
		return true
	}
	parts := sentSplitRe.Split(body, -1)
	sents := []string{}
	for _, p := range parts {
		if t := strings.TrimSpace(p); t != "" {
			sents = append(sents, t)
		}
	}
	if len(sents) == 0 {
		return true
	}
	allNarration := true
	for _, s := range sents {
		if !sentIsNarration(s) {
			allNarration = false
			break
		}
	}
	if allNarration {
		return true
	}
	return sentIsNarration(sents[len(sents)-1]) && len([]rune(body)) < narrationTailMaxChars
}

// stripLeakedThinking 剔除误入正文通道的推理碎片与 DSML 信封，全空时回退原文防空白。
func stripLeakedThinking(text string) string {
	if text == "" {
		return text
	}
	cleaned := toolEnvelopeRe.ReplaceAllString(text, "")
	cleaned = toolEnvelopeTagRe.ReplaceAllString(cleaned, "")
	trimmed := strings.TrimSpace(cleaned)
	var kept string
	for _, rawParts := range [][]string{
		paragraphSplitRe.Split(trimmed, -1),
		strings.Split(trimmed, "\n"),
	} {
		parts := make([]string, 0, len(rawParts))
		for _, p := range rawParts {
			if strings.TrimSpace(p) != "" {
				parts = append(parts, p)
			}
		}
		if len(parts) == 0 {
			continue
		}
		head := 0
		for head < len(parts) && looksLikeThinking(parts[head]) {
			head++
		}
		tail := len(parts)
		for tail > head && looksLikeThinking(parts[tail-1]) {
			tail--
		}
		if candidate := strings.TrimSpace(strings.Join(parts[head:tail], "\n")); candidate != "" {
			kept = candidate
			break
		}
	}
	kept = toolEnvelopeRe.ReplaceAllString(kept, "")
	kept = toolEnvelopeTagRe.ReplaceAllString(kept, "")
	kept = blankLinesRe.ReplaceAllString(kept, "\n\n")
	kept = strings.TrimSpace(kept)
	if kept != "" {
		return kept
	}
	return strings.TrimSpace(text)
}
