package pilot

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

const (
	// charsPerToken 中文场景粗估：~2 字符/token（各提供方自陈偏低，取宽裕口径）。
	charsPerToken         = 2.0
	fallbackHistoryBudget = 30000
	minHistoryTurns       = 2
	pruneTriggerRatio     = 0.85
	idlePruneTriggerRatio = 0.60
	idlePruneMinutes      = 30.0
	compactTriggerRatio   = 0.95
	toolClipHead          = 800
	toolClipTail          = 400
)

func EstimateTokens(text string) int {
	return int(float64(len([]rune(text))) / charsPerToken)
}

func EstimateMessages(messages []map[string]any) int {
	total := 0
	for _, m := range messages {
		if content, ok := m["content"].(string); ok {
			total += EstimateTokens(content)
		}
		if rawCalls := m["tool_calls"]; rawCalls != nil {
			payload, _ := json.Marshal(rawCalls)
			total += EstimateTokens(string(payload))
		}
	}
	return total + 20
}

func UserText(question string, appid *int, viewContext map[string]any,
	elementRefs []map[string]any, quotes []map[string]any) string {
	parts := []string{}
	if question != "" {
		parts = append(parts, question)
	}
	if appid != nil && *appid > 0 {
		parts = append(parts, fmt.Sprintf("（当前选中游戏 appid=%d）", *appid))
	}
	if len(viewContext) > 0 {
		payload, _ := json.Marshal(viewContext)
		parts = append(parts, "（用户当前页面）"+string(payload))
	}
	if len(elementRefs) > 0 {
		payload, _ := json.Marshal(elementRefs)
		parts = append(parts, "（用户选中的页面元素）"+string(payload))
	}
	if len(quotes) > 0 {
		payload, _ := json.Marshal(quotes)
		parts = append(parts, "（编号引用）"+string(payload))
	}
	return strings.Join(parts, "\n")
}

func ToolClip(text string, limit int) string {
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	if limit < toolClipHead+toolClipTail+20 {
		return string(runes[:limit])
	}
	head := string(runes[:toolClipHead])
	tail := string(runes[len(runes)-toolClipTail:])
	return head + "\n…（中段省略，超出保留预算）\n" + tail
}

func SummaryMessage(summary, sid string) map[string]any {
	return map[string]any{"role": "system",
		"content": "（要点存档·以下是早期轮次的蒸馏摘要，后续为近期轮次原文）\n" + summary}
}

func EnvironmentMessage() map[string]any {
	now := time.Now()
	weekdays := []string{"日", "一", "二", "三", "四", "五", "六"}
	return map[string]any{"role": "system",
		"content": fmt.Sprintf("（系统侧同步·环境事实）今天是 %d 年 %d 月 %d 日 星期%s。",
			now.Year(), int(now.Month()), now.Day(), weekdays[now.Weekday()])}
}

func deriveHistoryBudget(contextWindow, overheadTokens int) int {
	return deriveHistoryBudgetWithCorrection(contextWindow, overheadTokens, 1.0)
}

func deriveHistoryBudgetWithCorrection(contextWindow, overheadTokens int, correction float64) int {
	window := contextWindow
	if window <= 0 {
		window = FallbackContextWindow
	}
	budget := window - overheadTokens - maxOutputTokens
	if correction > 1.0 {
		budget = int(float64(budget) / correction)
	}
	if budget < 2000 {
		budget = 2000
	}
	return budget
}

func turnMessages(turn map[string]any) []map[string]any {
	switch raw := turn["messages"].(type) {
	case []map[string]any:
		return raw
	case []any:
		out := make([]map[string]any, 0, len(raw))
		for _, item := range raw {
			if msg, ok := item.(map[string]any); ok {
				out = append(out, msg)
			}
		}
		return out
	}
	return nil
}

func needsCompaction(full []map[string]any, budget int) bool {
	return EstimateMessages(full) > budget
}

func clipTurnToolResults(turn map[string]any, limit int) {
	for _, msg := range turnMessages(turn) {
		if role, _ := msg["role"].(string); role == "tool" {
			if content, ok := msg["content"].(string); ok {
				msg["content"] = ToolClip(content, limit)
			}
		}
	}
}

func pruneOldToolResults(state *SessionState, keepRecent int, toolLimit int) {
	if state == nil {
		return
	}
	cutoff := len(state.Turns) - keepRecent
	if cutoff <= 0 {
		return
	}
	for i := 0; i < cutoff; i++ {
		clipTurnToolResults(state.Turns[i], toolLimit)
	}
}

func appendTurnToMessages(msgs []map[string]any, turn map[string]any, toolLimit int) []map[string]any {
	if rawMsgs := turnMessages(turn); len(rawMsgs) > 0 {
		for _, msg := range rawMsgs {
			if role, _ := msg["role"].(string); role == "tool" {
				if content, ok := msg["content"].(string); ok {
					cloned := make(map[string]any, len(msg))
					for k, v := range msg {
						cloned[k] = v
					}
					cloned["content"] = ToolClip(content, toolLimit)
					msgs = append(msgs, cloned)
					continue
				}
			}
			msgs = append(msgs, msg)
		}
		return msgs
	}
	if q, ok := turn["q"].(string); ok && q != "" {
		msgs = append(msgs, map[string]any{"role": "user", "content": q})
	}
	if resp, ok := turn["resp"].(map[string]any); ok {
		if answer, ok := resp["answer"].(string); ok && answer != "" {
			msgs = append(msgs, map[string]any{"role": "assistant", "content": answer})
		}
	}
	return msgs
}

func fullView(state *SessionState, question string, appid *int, toolLimit int,
	viewContext map[string]any, elementRefs []map[string]any, quotes []map[string]any) []map[string]any {
	msgs := []map[string]any{}
	if state != nil && state.Summary != "" {
		msgs = append(msgs, SummaryMessage(state.Summary, state.Sid))
	}
	if state != nil {
		for _, turn := range state.Turns[state.SummaryThrough:] {
			msgs = appendTurnToMessages(msgs, turn, toolLimit)
		}
	}
	msgs = append(msgs, map[string]any{"role": "user", "content": UserText(question, appid, viewContext, elementRefs, quotes)})
	return msgs
}

func assemble(state *SessionState, question string, appid *int, budget int, toolLimit int,
	viewContext map[string]any, elementRefs []map[string]any, quotes []map[string]any) []map[string]any {
	msgs := []map[string]any{}
	if state != nil && state.Summary != "" {
		msgs = append(msgs, SummaryMessage(state.Summary, state.Sid))
	}
	start := 0
	if state != nil {
		start = state.SummaryThrough
	}
	turns := []map[string]any{}
	if state != nil {
		turns = state.Turns[start:]
	}
	var included []map[string]any
	currentTokens := 0
	if state != nil && state.Summary != "" {
		currentTokens = EstimateTokens(state.Summary) + 50
	}
	for i := len(turns) - 1; i >= 0; i-- {
		turn := turns[i]
		turnTokens := estimateTurnTokens(turn, toolLimit)
		if currentTokens+turnTokens > budget && len(turns)-i >= minHistoryTurns {
			break
		}
		currentTokens += turnTokens
		included = append([]map[string]any{turn}, included...)
	}
	for _, turn := range included {
		msgs = appendTurnToMessages(msgs, turn, toolLimit)
	}
	msgs = append(msgs, map[string]any{"role": "user",
		"content": UserText(question, appid, viewContext, elementRefs, quotes)})
	return msgs
}

func estimateTurnTokens(turn map[string]any, toolLimit int) int {
	total := 0
	if msgs := turnMessages(turn); len(msgs) > 0 {
		for _, msg := range msgs {
			if content, ok := msg["content"].(string); ok {
				if role, _ := msg["role"].(string); role == "tool" {
					total += EstimateTokens(ToolClip(content, toolLimit))
				} else {
					total += EstimateTokens(content)
				}
			}
			if rawCalls := msg["tool_calls"]; rawCalls != nil {
				payload, _ := json.Marshal(rawCalls)
				total += EstimateTokens(string(payload))
			}
		}
		return total + 20
	}
	if q, ok := turn["q"].(string); ok {
		total += EstimateTokens(q)
	}
	if resp, ok := turn["resp"].(map[string]any); ok {
		if answer, ok := resp["answer"].(string); ok {
			total += EstimateTokens(answer)
		}
	}
	return total + 20
}

func ReferenceDigest(state *SessionState) string {
	if state == nil || len(state.Turns) == 0 {
		return ""
	}
	lines := []string{}
	maxTurns := 3
	start := len(state.Turns) - maxTurns
	if start < 0 {
		start = 0
	}
	for _, turn := range state.Turns[start:] {
		q, _ := turn["q"].(string)
		resp, _ := turn["resp"].(map[string]any)
		answer, _ := resp["answer"].(string)
		if q != "" {
			lines = append(lines, fmt.Sprintf("问：%s", clip(q, 100)))
		}
		if answer != "" {
			lines = append(lines, fmt.Sprintf("答：%s", clip(answer, 200)))
		}
	}
	return strings.Join(lines, "\n")
}

func ReferenceMessage(title, digest string) map[string]any {
	return map[string]any{"role": "system",
		"content": fmt.Sprintf("（系统侧同步·仅供参照）以下是另一段会话「%s」的近期问答摘要：\n%s", title, digest)}
}
