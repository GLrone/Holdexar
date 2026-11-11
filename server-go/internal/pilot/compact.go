//go:build windows

// compact.go LLM 驱动的上下文自动压缩：蒸早期轮次为要点存档。
// 语义对齐 pilot/context.py 的 compact_plan / compact。
package pilot

import (
	"context"
	"encoding/json"
	"fmt"
	"strings"
	"time"

	"holdexar/server-go/internal/agent"
)

const compactFailLimit = 2

var compactFailures = map[string]int{}

// CompactPlan 压缩计划：蒸 [0, through) 的轮次为摘要。
type CompactPlan struct {
	Through int // 蒸馏边界（前 N 轮）
	Prompt  string
}

// compactPlan 决定压缩区间：无可蒸馏轮次或区间太小返回 nil。
func compactPlan(state *SessionState) *CompactPlan {
	if state == nil || len(state.Turns) == 0 {
		return nil
	}
	// 已有摘要时从 summaryThrough 开始，否则从头开始
	start := state.SummaryThrough
	// 保留最近 minHistoryTurns 轮不压缩
	end := len(state.Turns) - minHistoryTurns
	if end <= start {
		return nil // 区间太小或无可蒸馏
	}
	through := end
	return &CompactPlan{Through: through}
}

// compact 调 LLM 蒸馏早期轮次为要点存档。
// 返回 (摘要文本, 蒸馏边界, 输入/输出用量) 或 nil（失败）。
func (s *Service) compact(ctx context.Context, state *SessionState, cfg ConfigPayload,
	protocol, baseURL, apiKey, model string, sysPrompt string) (string, int, [2]int, error) {
	plan := compactPlan(state)
	if plan == nil {
		return "", 0, [2]int{}, fmt.Errorf("无可蒸馏轮次")
	}
	// 装配蒸馏请求的消息序列
	turns := state.Turns[:plan.Through]
	var transcript strings.Builder
	for _, turn := range turns {
		q, _ := turn["q"].(string)
		resp, _ := turn["resp"].(map[string]any)
		answer, _ := resp["answer"].(string)
		if q != "" {
			transcript.WriteString(fmt.Sprintf("用户：%s\n", clip(q, 200)))
		}
		if answer != "" {
			transcript.WriteString(fmt.Sprintf("助手：%s\n", clip(answer, 300)))
		}
		// 工具结果摘要
		if msgs, ok := turn["messages"].([]map[string]any); ok {
			for _, msg := range msgs {
				if role, _ := msg["role"].(string); role == "tool" {
					content := ToolClip(stringOf(msg["content"]), 200)
					transcript.WriteString(fmt.Sprintf("工具：%s\n", content))
				}
			}
		}
	}

	compactPrompt := map[string]any{"role": "user", "content": fmt.Sprintf(
		"以下是之前对话的前 %d 轮问答记录。请将其蒸馏为一段简洁的要点存档（500 字以内），"+
			"保留：涉及的游戏名称与 appid、用户表达过的偏好、重要结论、待办事项。"+
			"丢弃闲聊与重复内容。直接输出存档正文，不要加标题或解释。\n\n%s",
		plan.Through, transcript.String())}
	messages := []map[string]any{
		{"role": "system", "content": "你是摘要引擎。把对话历史蒸馏为要点存档。"},
		compactPrompt,
	}

	// 调 LLM（非流式：等完整响应）
	events, err := s.ChatStream(ctx, protocol, baseURL, apiKey, model, messages, nil, 512, nil)
	if err != nil {
		return "", 0, [2]int{}, err
	}
	var summary strings.Builder
	var usageIn, usageOut int
	for event := range events {
		if event.Err != nil {
			return "", 0, [2]int{}, event.Err
		}
		switch event.Kind {
		case "answer":
			summary.WriteString(event.Text)
		case "usage":
			usageIn += event.UsageIn
			usageOut += event.UsageOut
		}
	}
	text := strings.TrimSpace(summary.String())
	if text == "" {
		return "", 0, [2]int{}, fmt.Errorf("压缩摘要为空")
	}
	return text, plan.Through, [2]int{usageIn, usageOut}, nil
}

// compactIfNeeded 检查全量视图是否需要压缩，需要则执行。
// 返回 (是否发生了压缩, 错误)。
func (s *Service) compactIfNeeded(ctx context.Context, state *SessionState, cfg ConfigPayload,
	question string) (bool, error) {
	if state == nil || len(state.Turns) == 0 {
		return false, nil
	}
	// 连败熔断
	if compactFailures[state.Sid] >= compactFailLimit {
		return false, nil
	}
	budget := s.historyBudget(cfg, state)
	full := fullView(state, question, reqAppID(&cfg), cfg.ToolContentLimit, nil, nil, nil)
	if !needsCompaction(full, budget) {
		return false, nil
	}
	// 免费剪枝已够则不花模型调用
	pruneOldToolResults(state, 6, cfg.ToolContentLimit)
	full2 := fullView(state, question, reqAppID(&cfg), cfg.ToolContentLimit, nil, nil, nil)
	if !needsCompaction(full2, budget) {
		return false, nil
	}
	// 调 LLM 蒸馏
	sysPrompt := SystemPrompt
	baseURL := EffectiveBaseURL(cfg.Protocol, cfg.BaseURL)
	apiKey := s.activeAPIKey(cfg)
	text, through, usage, err := s.compact(ctx, state, cfg, cfg.Protocol, baseURL, apiKey, cfg.Model, sysPrompt)
	if err != nil {
		if len(compactFailures) > 256 {
			compactFailures = map[string]int{}
		}
		compactFailures[state.Sid]++
		return false, err
	}
	// 先估后账：压缩后体量必须比压缩前小
	preTokens := EstimateMessages(full2)
	state.Summary = text
	state.SummaryThrough = through
	postMsgs := fullView(state, question, reqAppID(&cfg), cfg.ToolContentLimit, nil, nil, nil)
	postTokens := EstimateMessages(postMsgs)
	state.Summary = ""
	state.SummaryThrough = 0
	if postTokens >= preTokens {
		// 摘要没缩小源内容：拒绝，不计熔断
		s.log("压缩存档未缩小源内容（%d→%d），已丢弃", preTokens, postTokens)
		return false, nil
	}
	if usage[0] != 0 || usage[1] != 0 {
		s.AddUsage(ctx, usage[0], usage[1])
	}
	if s.GetStore().AppendSummary(state.Sid, text, through, "auto", preTokens, postTokens) {
		state.Summary = text
		state.SummaryThrough = through
		delete(compactFailures, state.Sid)
		// 存档蒸馏出用户显式偏好 → 记忆账本（失败静默，不拖垮压缩）
		if s.deps.DB != nil {
			exCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			agent.ExtractPreferencesFromText(exCtx, s.deps.DB, text, fmt.Sprintf("compact:%s", state.Sid))
		}
		return true, nil
	}
	return false, nil
}

// reqAppID 从配置取当前 appid（压缩时通常无上下文 appid）。
func reqAppID(cfg *ConfigPayload) *int {
	return nil // 压缩装配不带 appid 上下文
}

// LooksLikeOverflow 检测上下文溢出错误（导出到 ask.go 使用）。
func LooksLikeOverflow(errText string) bool {
	lower := strings.ToLower(errText)
	for _, pattern := range []string{
		"maximum context length", "context_length_exceeded",
		"too many tokens", "context window", "input too long",
		"prompt is too long", "reduce the length",
	} {
		if strings.Contains(lower, pattern) {
			return true
		}
	}
	return false
}

var _ = json.Marshal
var _ = time.Now
