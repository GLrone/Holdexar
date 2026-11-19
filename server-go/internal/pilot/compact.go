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

func (s *Service) compactFailCount(sid string) int {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.compactFail[sid]
}

func (s *Service) incCompactFail(sid string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.compactFail == nil {
		s.compactFail = map[string]int{}
	}
	if len(s.compactFail) > 256 {
		s.compactFail = map[string]int{}
	}
	s.compactFail[sid]++
}

func (s *Service) resetCompactFail(sid string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	delete(s.compactFail, sid)
}

type CompactPlan struct {
	Through int
	Prompt  string
}

func compactPlan(state *SessionState) *CompactPlan {
	if state == nil || len(state.Turns) == 0 {
		return nil
	}
	start := state.SummaryThrough
	end := len(state.Turns) - minHistoryTurns
	if end <= start {
		return nil
	}
	through := end
	return &CompactPlan{Through: through}
}

func (s *Service) compact(ctx context.Context, state *SessionState, cfg ConfigPayload,
	protocol, baseURL, apiKey, model string, sysPrompt string) (string, int, [2]int, error) {
	plan := compactPlan(state)
	if plan == nil {
		return "", 0, [2]int{}, fmt.Errorf("无可蒸馏轮次")
	}
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
		for _, msg := range turnMessages(turn) {
			if role, _ := msg["role"].(string); role == "tool" {
				content := ToolClip(stringOf(msg["content"]), 200)
				transcript.WriteString(fmt.Sprintf("工具：%s\n", content))
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

func (s *Service) assembleForModel(ctx context.Context, state *SessionState, question string,
	req AskRequest, cfg ConfigPayload, sysPrompt string, expanded map[string]bool) ([]map[string]any, bool) {
	if state == nil {
		return []map[string]any{{"role": "user", "content": UserText(question, req.AppID, req.ViewContext, req.ElementRefs, req.Quotes)}}, false
	}
	budget := s.historyBudgetWith(cfg, state, sysPrompt, expanded)
	msgs := assemble(state, question, req.AppID, budget, cfg.ToolContentLimit,
		req.ViewContext, req.ElementRefs, req.Quotes)
	full := fullView(state, question, req.AppID, cfg.ToolContentLimit,
		req.ViewContext, req.ElementRefs, req.Quotes)
	if !needsCompaction(full, budget) {
		return msgs, false
	}
	pruneOldToolResults(state, 6, cfg.ToolContentLimit)
	full2 := fullView(state, question, req.AppID, cfg.ToolContentLimit,
		req.ViewContext, req.ElementRefs, req.Quotes)
	if !needsCompaction(full2, budget) {
		return assemble(state, question, req.AppID, budget, cfg.ToolContentLimit,
			req.ViewContext, req.ElementRefs, req.Quotes), false
	}
	if s.compactFailCount(state.Sid) >= compactFailLimit {
		return msgs, false
	}
	baseURL := EffectiveBaseURL(cfg.Protocol, cfg.BaseURL)
	apiKey := s.activeAPIKey(cfg)
	text, through, usage, err := s.compact(ctx, state, cfg, cfg.Protocol, baseURL, apiKey, cfg.Model, sysPrompt)
	if err != nil {
		s.incCompactFail(state.Sid)
		return msgs, false
	}
	preTokens := EstimateMessages(full2)
	prevSummary, prevThrough := state.Summary, state.SummaryThrough
	state.Summary, state.SummaryThrough = text, through
	postTokens := EstimateMessages(fullView(state, question, req.AppID, cfg.ToolContentLimit,
		req.ViewContext, req.ElementRefs, req.Quotes))
	postMsgs := assemble(state, question, req.AppID, budget, cfg.ToolContentLimit,
		req.ViewContext, req.ElementRefs, req.Quotes)
	state.Summary, state.SummaryThrough = prevSummary, prevThrough
	if postTokens >= preTokens {
		s.log("压缩存档未缩小源内容（%d→%d），已丢弃", preTokens, postTokens)
		return msgs, false
	}
	if usage[0] != 0 || usage[1] != 0 {
		s.AddUsage(ctx, usage[0], usage[1])
	}
	if s.GetStore().AppendSummary(state.Sid, text, through, "auto", preTokens, postTokens) {
		state.Summary, state.SummaryThrough = text, through
		s.resetCompactFail(state.Sid)
		if s.deps.DB != nil {
			exCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			agent.ExtractPreferencesFromText(exCtx, s.deps.DB, s.gate, text, fmt.Sprintf("compact:%s", state.Sid))
		}
		return postMsgs, true
	}
	return msgs, false
}

// CompactNow 手动触发会话压缩（跳过预算闸，成功重置连败熔断）。
func (s *Service) CompactNow(ctx context.Context, state *SessionState, cfg ConfigPayload) map[string]any {
	plan := compactPlan(state)
	if plan == nil {
		return map[string]any{"ok": false, "reason": "nothing_to_distill"}
	}
	preTokens := EstimateMessages(fullView(state, "", reqAppID(&cfg), cfg.ToolContentLimit, nil, nil, nil))
	baseURL := EffectiveBaseURL(cfg.Protocol, cfg.BaseURL)
	apiKey := s.activeAPIKey(cfg)
	text, through, usage, err := s.compact(ctx, state, cfg, cfg.Protocol, baseURL, apiKey, cfg.Model, SystemPrompt)
	if err != nil {
		return map[string]any{"ok": false, "reason": "compact_failed"}
	}
	state.Summary = text
	state.SummaryThrough = through
	postTokens := EstimateMessages(fullView(state, "", reqAppID(&cfg), cfg.ToolContentLimit, nil, nil, nil))
	state.Summary = ""
	state.SummaryThrough = 0
	if postTokens >= preTokens {
		return map[string]any{"ok": false, "reason": "not_smaller"}
	}
	if usage[0] != 0 || usage[1] != 0 {
		s.AddUsage(ctx, usage[0], usage[1])
	}
	if s.GetStore().AppendSummary(state.Sid, text, through, "manual", preTokens, postTokens) {
		state.Summary = text
		state.SummaryThrough = through
		s.resetCompactFail(state.Sid)
		return map[string]any{"ok": true, "summary_through": through, "turn_total": len(state.Turns),
			"pre_tokens": preTokens, "post_tokens": postTokens}
	}
	return map[string]any{"ok": false, "reason": "compact_failed"}
}

func reqAppID(cfg *ConfigPayload) *int {
	return nil // 压缩装配不带 appid 上下文
}

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
