//go:build windows

// context.go 模型上下文装配：预算派生、历史裁剪、工具结果裁剪、自动压缩、
// 溢出回收。语义对齐 pilot/context.py 的核心行为面。
package pilot

import (
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

const (
	// charsPerToken 中文场景粗估：~2 字符/token（各提供方自陈偏低，取宽裕口径）。
	charsPerToken = 2.0
	// fallbackHistoryBudget 窗口未知时的固定历史预算（token）。
	fallbackHistoryBudget = 30000
	// minHistoryTurns 装配结果最少保留的近期轮数（不让位到零）。
	minHistoryTurns = 2
	// pruneTriggerRatio 全量视图超出预算的触发比（超过即先尝试剪枝）。
	pruneTriggerRatio = 0.85
	// idlePruneTriggerRatio 闲置会话的更早触发比（闲置 30 分钟后阈值降到 0.6）。
	idlePruneTriggerRatio = 0.60
	// idlePruneMinutes 闲置判定阈值（分钟）。
	idlePruneMinutes = 30.0
	// compactTriggerRatio 剪枝后仍超预算的压缩触发比。
	compactTriggerRatio = 0.95
	// toolClipHead/Tail 超长工具结果的头尾保留（字符）。
	toolClipHead = 800
	toolClipTail = 400
)

// EstimateTokens 字符 → token 粗估（中文 ~2 字符/token）。
func EstimateTokens(text string) int {
	return int(float64(len([]rune(text))) / charsPerToken)
}

// EstimateMessages 消息序列 token 估算。
func EstimateMessages(messages []map[string]any) int {
	total := 0
	for _, m := range messages {
		if content, ok := m["content"].(string); ok {
			total += EstimateTokens(content)
		}
		// tool_calls 的 arguments 也计入
		if rawCalls, ok := m["tool_calls"].([]any); ok {
			payload, _ := json.Marshal(rawCalls)
			total += EstimateTokens(string(payload))
		}
	}
	return total + 20 // 每条消息的格式开销
}

// UserText 构造用户消息文本（附加 appid / 页面上下文 / 元素引用 / 编号引用）。
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

// ToolClip 超长工具结果裁剪（头尾保留、中段省略）。
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

// SummaryMessage 要点存档消息。
func SummaryMessage(summary, sid string) map[string]any {
	return map[string]any{"role": "system",
		"content": "（要点存档·以下是早期轮次的蒸馏摘要，后续为近期轮次原文）\n" + summary}
}

// EnvironmentMessage 环境事实（每轮现取：今天日期/星期几）。
func EnvironmentMessage() map[string]any {
	now := time.Now()
	weekdays := []string{"日", "一", "二", "三", "四", "五", "六"}
	return map[string]any{"role": "system",
		"content": fmt.Sprintf("（系统侧同步·环境事实）今天是 %d 年 %d 月 %d 日 星期%s。",
			now.Year(), int(now.Month()), now.Day(), weekdays[now.Weekday()])}
}

// deriveHistoryBudget 从模型窗口派生历史预算（token）。
func deriveHistoryBudget(contextWindow, overheadTokens int) int {
	window := contextWindow
	if window <= 0 {
		window = FallbackContextWindow
	}
	// 预算 = 窗口 - 系统提示与工具表开销 - 输出预留（maxOutputTokens）
	budget := window - overheadTokens - maxOutputTokens
	if budget < 2000 {
		budget = 2000
	}
	return budget
}

// needsCompaction 全量视图是否需要压缩。
func needsCompaction(full []map[string]any, budget int) bool {
	return EstimateMessages(full) > budget
}

// clipTurnToolResults 裁剪单轮中的超长工具结果。
func clipTurnToolResults(turn map[string]any, limit int) {
	if messages, ok := turn["messages"].([]map[string]any); ok {
		for _, msg := range messages {
			if role, _ := msg["role"].(string); role == "tool" {
				if content, ok := msg["content"].(string); ok {
					msg["content"] = ToolClip(content, limit)
				}
			}
		}
	}
}

// pruneOldToolResults 剪枝：对早期轮次的工具结果做裁剪（免费清理，不调模型）。
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

// fullView 全量视图（不裁剪的全部未存档轮次 + 当前问题）。
func fullView(state *SessionState, question string, appid *int, toolLimit int,
	viewContext map[string]any, elementRefs []map[string]any, quotes []map[string]any) []map[string]any {
	msgs := []map[string]any{}
	if state != nil && state.Summary != "" {
		msgs = append(msgs, SummaryMessage(state.Summary, state.Sid))
	}
	if state != nil {
		for _, turn := range state.Turns[state.SummaryThrough:] {
			if msgsRaw, ok := turn["messages"].([]map[string]any); ok && len(msgsRaw) > 0 {
				for _, msg := range msgsRaw {
					if role, _ := msg["role"].(string); role == "tool" {
						if content, ok := msg["content"].(string); ok {
							cloned := map[string]any{"role": role, "content": ToolClip(content, toolLimit)}
							if tc, ok := msg["tool_call_id"]; ok {
								cloned["tool_call_id"] = tc
							}
							msgs = append(msgs, cloned)
							continue
						}
					}
					msgs = append(msgs, msg)
				}
				continue
			}
			if q, ok := turn["q"].(string); ok && q != "" {
				msgs = append(msgs, map[string]any{"role": "user", "content": q})
			}
			if resp, ok := turn["resp"].(map[string]any); ok {
				if answer, ok := resp["answer"].(string); ok && answer != "" {
					msgs = append(msgs, map[string]any{"role": "assistant", "content": answer})
				}
			}
		}
	}
	msgs = append(msgs, map[string]any{"role": "user", "content": UserText(question, appid, viewContext, elementRefs, quotes)})
	return msgs
}

// assemble 在预算内装配消息序列（要点存档 + 近期轮次 + 当前问题）。
// 超预算时从旧到新让位（保留最少 minHistoryTurns 轮）。
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
	// 从旧到新试装配，超预算即截断（从最新往回保留）
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
		if msgsRaw, ok := turn["messages"].([]map[string]any); ok && len(msgsRaw) > 0 {
			for _, msg := range msgsRaw {
				if role, _ := msg["role"].(string); role == "tool" {
					if content, ok := msg["content"].(string); ok {
						cloned := map[string]any{"role": role, "content": ToolClip(content, toolLimit)}
						if tc, ok := msg["tool_call_id"]; ok {
							cloned["tool_call_id"] = tc
						}
						msgs = append(msgs, cloned)
						continue
					}
				}
				msgs = append(msgs, msg)
			}
			continue
		}
		if q, ok := turn["q"].(string); ok && q != "" {
			msgs = append(msgs, map[string]any{"role": "user", "content": q})
		}
		if resp, ok := turn["resp"].(map[string]any); ok {
			if answer, ok := resp["answer"].(string); ok && answer != "" {
				msgs = append(msgs, map[string]any{"role": "assistant", "content": answer})
			}
		}
	}
	msgs = append(msgs, map[string]any{"role": "user",
		"content": UserText(question, appid, viewContext, elementRefs, quotes)})
	return msgs
}

// estimateTurnTokens 单轮的 token 估算。
func estimateTurnTokens(turn map[string]any, toolLimit int) int {
	total := 0
	if q, ok := turn["q"].(string); ok {
		total += EstimateTokens(q)
	}
	if resp, ok := turn["resp"].(map[string]any); ok {
		if answer, ok := resp["answer"].(string); ok {
			total += EstimateTokens(answer)
		}
	}
	if msgs, ok := turn["messages"].([]map[string]any); ok {
		for _, msg := range msgs {
			if content, ok := msg["content"].(string); ok {
				total += EstimateTokens(ToolClip(content, toolLimit))
			}
		}
	}
	return total + 20
}

// ReferenceDigest 跨会话引用摘要（最近几轮的问答对精简）。
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

// ReferenceMessage 跨会话引用 → system 消息。
func ReferenceMessage(title, digest string) map[string]any {
	return map[string]any{"role": "system",
		"content": fmt.Sprintf("（系统侧同步·仅供参照）以下是另一段会话「%s」的近期问答摘要：\n%s", title, digest)}
}
