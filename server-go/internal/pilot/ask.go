//go:build windows

// ask.go 领航台问答编排（agent 循环内核）+ 降级路径 + 意图路由直通。
// 语义对齐 pilot/service.py 的 ask / ask_stream / _agent_stream。
package pilot

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"holdexar/server-go/internal/agent"
)

const (
	agentMaxSteps      = 8
	thinkBudgetChars   = 32000
	maxCardsPerTurn    = 4
	attachOnlyQuestion = "请查看我附带的附件。"
	inlineHeadChars    = 200
)

// AskRequest 问答请求载荷。
type AskRequest struct {
	Question     string           `json:"question"`
	AppID        *int             `json:"appid"`
	SessionID    string           `json:"session_id"`
	ReferenceSid string           `json:"reference_sid"`
	Attachments  []string         `json:"attachments"`
	Resume       map[string]any   `json:"resume"`
	ViewContext  map[string]any   `json:"view_context"`
	ElementRefs  []map[string]any `json:"element_refs"`
	Quotes       []map[string]any `json:"quotes"`
}

// StreamEvent SSE 事件（type 定界；done 携带终态全量）。
type StreamEvent struct {
	Type       string         `json:"type"`
	Step       int            `json:"step,omitempty"`
	Active     int            `json:"active,omitempty"`
	Kind       string         `json:"kind,omitempty"`
	Delta      string         `json:"delta,omitempty"`
	Name       string         `json:"name,omitempty"`
	Label      string         `json:"label,omitempty"`
	CardKind   string         `json:"card_kind,omitempty"`
	ElapsedMS  int            `json:"elapsed_ms,omitempty"`
	DurationMS int            `json:"duration_ms,omitempty"`
	Data       map[string]any `json:"data,omitempty"`
	Card       map[string]any `json:"card,omitempty"`
	Payload    map[string]any `json:"-"`
	Reason     string         `json:"reason,omitempty"`
}

// AskStream 流式编排入口（在飞登记 + busy 防护 + 受理回执）。
func (s *Service) AskStream(ctx context.Context, req AskRequest) (<-chan StreamEvent, error) {
	if enabled, err := s.moduleEnabled(ctx); err != nil {
		return nil, err
	} else if !enabled {
		return nil, ErrModuleDisabled
	}
	q := strings.TrimSpace(req.Question)
	if q == "" && len(req.Attachments) == 0 {
		return nil, fmt.Errorf("empty question")
	}
	sid := req.SessionID
	if sid != "" && s.SessionBusy(sid) {
		events := make(chan StreamEvent, 1)
		events <- StreamEvent{Type: "busy"}
		close(events)
		return events, nil
	}
	accepted, active := s.acquireTurn(sid)
	if !accepted {
		events := make(chan StreamEvent, 1)
		events <- StreamEvent{Type: "busy"}
		close(events)
		return events, nil
	}
	events := make(chan StreamEvent, 64)
	go func() {
		defer close(events)
		defer s.releaseTurn(sid)
		events <- StreamEvent{Type: "ack", Active: active}
		s.markSessionRunning(sid)
		s.SetInflightQuestion(sid, strings.TrimSpace(req.Question))
		defer func() {
			s.ClearSessionRunning(sid)
			s.ClearInflightQuestion(sid)
		}()
		for event := range s.askStreamBody(ctx, q, req) {
			events <- event
		}
	}()
	return events, nil
}

func (s *Service) markSessionRunning(sid string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.running[sid] = true
}

// askStreamBody 编排本体：导航直通 → 降级判定 → agent 循环 → 落账。
func (s *Service) askStreamBody(ctx context.Context, question string, req AskRequest) <-chan StreamEvent {
	events := make(chan StreamEvent, 256)
	go func() {
		defer close(events)
		state := s.GetStore().LoadOrNew(req.SessionID)
		intent := Route(question)
		// 导航直通：动词+页面别名命中即执行，不过模型。
		if navTarget := NavTarget(question); intent == IntentNavigate && navTarget != "" {
			result := s.navigateDirect(ctx, navTarget)
			turn := s.recordTurn(state, question, result)
			result["turn_id"] = turn
			result["title"] = s.GetStore().TitleOf(state)
			s.logDecision(state, turn, question, intent, "direct", result)
			result["q"] = question
			events <- StreamEvent{Type: "done", Payload: result}
			return
		}
		cfg := s.LoadConfig()
		if !cfg.LlmReady() || s.OverCap() {
			reason := ""
			if s.OverCap() {
				reason = "cap_reached"
			}
			result := s.fallbackDeterministic(ctx, question, req.AppID, state, intent, reason)
			turn := s.recordTurn(state, question, result)
			result["turn_id"] = turn
			result["title"] = s.GetStore().TitleOf(state)
			s.logDecision(state, turn, question, intent, "fallback", result)
			result["q"] = question
			events <- StreamEvent{Type: "done", Payload: result}
			return
		}
		// agent 循环
		for event := range s.agentStream(ctx, state, question, req, cfg, intent) {
			events <- event
		}
	}()
	return events
}

// navigateDirect 导航直通终态。
func (s *Service) navigateDirect(ctx context.Context, target string) map[string]any {
	result := s.ToolExecute(ctx, "navigate", map[string]any{"target": target}, false, "")
	step := toolStep("navigate", result)
	facts := map[string]any{"kind": "navigate", "target": target, "path": stringOf(result["path"])}
	return map[string]any{
		"answer": "", "thinking": nil, "think_ms": nil, "source": "facts",
		"reason": nil, "facts": facts, "cards": []any{facts},
		"steps": []any{step}, "tools": []any{toolSummary("navigate", result)},
	}
}

// fallbackDeterministic 无 LLM 时的确定性回退。
func (s *Service) fallbackDeterministic(ctx context.Context, question string, appID *int, state *SessionState, intent, reason string) map[string]any {
	empty := func(source, reason string) map[string]any {
		return map[string]any{"answer": "", "thinking": nil, "source": source, "reason": reason, "facts": nil}
	}
	guarded := IsGuarded(question)
	switch intent {
	case IntentHowTo:
		return empty("guide", "")
	case IntentCreateAlert:
		matches := s.searchGamesFor(ctx, question)
		if len(matches) == 1 {
			first, _ := matches[0].(map[string]any)
			targetType, value := extractAlert(question)
			if targetType == "price" && value == 0 {
				return empty("guide", "")
			}
			args := map[string]any{"appid": first["appid"], "target_type": targetType}
			if value > 0 {
				args["target_value_yuan"] = float64(value) / 100
			}
			result := s.ToolExecute(ctx, "create_price_alert", args, guarded, sidOf(state))
			if stringOf(result["kind"]) == "denied" {
				return empty("guide", "")
			}
			action := map[string]any{"kind": "action"}
			for key, value := range result {
				action[key] = value
			}
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": "", "facts": action}
		}
		if len(matches) > 0 {
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": "need_target", "facts": map[string]any{"kind": "games", "items": matches}}
		}
		return empty("none", "no_data")
	case IntentAddMonitor:
		matches := s.searchGamesFor(ctx, question)
		if len(matches) == 1 {
			first, _ := matches[0].(map[string]any)
			result := s.ToolExecute(ctx, "add_follow", map[string]any{"appid": first["appid"]}, guarded, sidOf(state))
			if stringOf(result["kind"]) == "denied" {
				return empty("guide", "")
			}
			action := map[string]any{"kind": "action"}
			for key, value := range result {
				action[key] = value
			}
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": "", "facts": action}
		}
		if len(matches) > 0 {
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": "need_target", "facts": map[string]any{"kind": "games", "items": matches}}
		}
		return empty("none", "no_data")
	case IntentPriceAnalysis:
		var target int
		if appID != nil {
			target = *appID
		}
		if target == 0 {
			matches := s.searchGamesFor(ctx, question)
			if len(matches) == 1 {
				first, _ := matches[0].(map[string]any)
				target = jsonInt(first["appid"])
			}
		}
		if target > 0 {
			facts := s.ToolExecute(ctx, "get_price_briefing", map[string]any{"appid": target}, false, sidOf(state))
			if stringOf(facts["kind"]) != "failed" && stringOf(facts["kind"]) != "empty" {
				return map[string]any{"answer": "", "thinking": nil, "source": "facts",
					"reason": "", "facts": facts}
			}
		}
		items := s.ToolExecute(ctx, "recommend_games", map[string]any{}, false, sidOf(state))
		if list := anySlice(items["items"]); len(list) > 0 {
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": "need_target", "facts": items}
		}
		return empty("none", "no_data")
	case IntentFindGames:
		items := s.ToolExecute(ctx, "recommend_games", map[string]any{}, false, sidOf(state))
		if list := anySlice(items["items"]); len(list) > 0 {
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": "", "facts": items}
		}
		return empty("none", "no_data")
	}
	return empty("none", "llm_off")
}

func sidOf(state *SessionState) string {
	if state == nil {
		return ""
	}
	return state.Sid
}

// extractAlert 解析提醒条件：史低类无值；价格类「低于 N」取 N×100 分。
func extractAlert(question string) (string, int) {
	if strings.Contains(question, "史低") {
		return "historic_low", 0
	}
	// 阈值词表对齐 _THRESH_RE：低于/跌到/降价到 + 数字
	words := []string{"低于", "跌到", "跌至", "降至", "以下"}
	for _, word := range words {
		if idx := strings.Index(question, word); idx >= 0 {
			rest := question[idx+len(word):]
			if value := parseLeadingNumber(rest); value > 0 {
				return "price", int(value * 100)
			}
		}
	}
	return "price", 0
}

func parseLeadingNumber(text string) float64 {
	runes := []rune(strings.TrimSpace(text))
	digits := []rune{}
	for _, r := range runes {
		if (r >= '0' && r <= '9') || r == '.' {
			digits = append(digits, r)
		} else if len(digits) > 0 {
			break
		}
	}
	if len(digits) == 0 {
		return 0
	}
	var value float64
	fmt.Sscanf(string(digits), "%f", &value)
	return value
}

// recordTurn 落一轮问答并返回账本 id。
func (s *Service) recordTurn(state *SessionState, question string, result map[string]any) string {
	if state == nil {
		return ""
	}
	tid := newProviderID()
	record := map[string]any{"tid": tid, "q": question, "resp": result,
		"last_game": state.LastGame}
	if !s.GetStore().AppendTurn(state.Sid, record) {
		return ""
	}
	return tid
}

// logDecision 罗盘数据飞轮追加（shadow，失败静默）。
func (s *Service) logDecision(state *SessionState, tid, question, intent, mode string, result map[string]any) {
	entry := map[string]any{
		"ts": nowISO(), "question": question, "intent": intent, "mode": mode,
		"source": stringOf(result["source"]), "reason": stringOr(result["reason"], ""),
	}
	if tools, ok := result["tools"].([]any); ok {
		entry["tools"] = tools
	}
	if state != nil {
		entry["sid"] = state.Sid
		if tid != "" {
			entry["tid"] = tid
		}
	}
	AppendDecision(filepath.Join(s.deps.DataDir, "pilot", "sessions"), entry)
}

// toolSummary 工具事件的上屏摘要。
func toolSummary(name string, result map[string]any) string {
	kind := stringOf(result["kind"])
	switch {
	case kind == "denied":
		return name + ": denied"
	case kind == "timeout" || kind == "failed":
		return name + ": " + kind
	case kind == "rows":
		rows := anySlice(result["rows"])
		return fmt.Sprintf("%s: %d rows", name, len(rows))
	case name == "search_games" || name == "recommend_games":
		items := anySlice(result["items"])
		return fmt.Sprintf("%s: %d hits", name, len(items))
	case name == "get_price_briefing":
		return fmt.Sprintf("get_price_briefing: %s", stringOr(result["name"], "no_data"))
	case name == "navigate":
		return fmt.Sprintf("navigate: %s", stringOr(result["target"], "none"))
	}
	return name
}

// toolStep 工具时间线步骤（label/status/data 词条化）。
func toolStep(name string, result map[string]any) map[string]any {
	kind := stringOf(result["kind"])
	status := "done"
	switch kind {
	case "denied":
		status = "denied"
	case "timeout":
		status = "timeout"
	case "failed":
		status = "failed"
	case "empty":
		status = "empty"
	}
	return map[string]any{"name": name, "status": status, "data": result}
}

// searchGamesFor 降级路径的游戏检索。
func (s *Service) searchGamesFor(ctx context.Context, question string) []any {
	result := s.ToolExecute(ctx, "search_games", map[string]any{"q": question}, false, "")
	items := anySlice(result["items"])
	return items
}

// agentStream agent 循环（流式）：模型是驾驶员，工具由服务层执行后回灌，
// 循环直到终答或步数上限；每步一段 phase（思考/前言/工具）。
func (s *Service) agentStream(ctx context.Context, state *SessionState, question string,
	req AskRequest, cfg ConfigPayload, intent string) <-chan StreamEvent {
	events := make(chan StreamEvent, 256)
	go func() {
		defer close(events)
		baseURL := EffectiveBaseURL(cfg.Protocol, cfg.BaseURL)
		client := HttpClient(baseURL)
		defer client.CloseIdleConnections()
		messages := s.buildBaseMessages(state, question, req, cfg)
		tools := s.toolSpecs()
		cards := []any{}
		steps := []any{}
		toolLog := []any{}
		turnStarted := time.Now()
		var usageIn, usageOut int
		var cacheRead, cacheWrite, cacheBase int
		cacheSeen, cacheReadSeen := false, false
		var answer string
		reason := ""
		exhausted := true
		overflowRetried := false
		step := 0
		for step < agentMaxSteps {
			step++
			events <- StreamEvent{Type: "step_start", Step: step}
			var roundAnswer strings.Builder
			var toolCalls []map[string]any
			reqCtx, cancel := context.WithTimeout(ctx, llmTimeout)
			eventCh, err := s.ChatStream(reqCtx, cfg.Protocol, baseURL, s.activeAPIKey(cfg),
				cfg.Model, messages, tools, 0, client)
			if err != nil {
				cancel()
				s.emitLlmFailure(events, state, question, req, intent, err)
				return
			}
			var llmErr *PilotLlmError
			for event := range eventCh {
				if event.Kind == "error" {
					if event.Err != nil {
						llmErr = event.Err
					} else {
						llmErr = newLlmError(event.Text, "provider_stream_error", 0)
					}
					continue
				}
				switch event.Kind {
				case "thinking", "answer":
					if event.Kind == "answer" {
						roundAnswer.WriteString(event.Text)
					}
					events <- StreamEvent{Type: event.Kind, Delta: event.Text}
				case "tool_calls":
					toolCalls = event.Calls
				case "usage":
					usageIn += event.UsageIn
					usageOut += event.UsageOut
				case "cache":
					// 本轮全部请求聚合（单次请求的比率不代表整轮）；provider 未报则不出
					if event.Cache != nil {
						cacheSeen = true
						if v, ok := event.Cache["read"]; ok && v != nil {
							cacheReadSeen = true
							cacheRead += jsonInt(v)
						}
						if v, ok := event.Cache["write"]; ok && v != nil {
							cacheWrite += jsonInt(v)
						}
						cacheBase += jsonInt(event.Cache["base"])
					}
				}
			}
			cancel()
			if llmErr != nil {
				// 溢出回收：首轮且尚无流出时丢弃历史（仅存档 + 当前问题）重试一次
				if step == 1 && roundAnswer.Len() == 0 && !overflowRetried &&
					state != nil && (len(state.Turns) > 0 || state.Summary != "") &&
					LooksLikeOverflow(llmErr.Message) {
					overflowRetried = true
					messages = s.buildMinimalMessages(state, question)
					continue
				}
				s.emitLlmFailure(events, state, question, req, intent, llmErr)
				return
			}
			if len(toolCalls) == 0 {
				text := roundAnswer.String()
				if text == "" && step < agentMaxSteps && !s.nudgedEmpty(messages) {
					s.nudge(messages, "(系统提示)你刚才没有输出任何内容也未调用工具。请立即调用合适的工具获取真实数据再回答；若确实无需工具，直接输出最终结论。")
					continue
				}
				exhausted = false
				answer = text
				break
			}
			assistant := map[string]any{"role": "assistant", "content": roundAnswer.String()}
			callList := []any{}
			for _, call := range toolCalls {
				argsJSON, _ := json.Marshal(call["arguments"])
				callList = append(callList, map[string]any{
					"id": call["id"], "type": "function",
					"function": map[string]any{"name": call["name"], "arguments": string(argsJSON)},
				})
			}
			assistant["tool_calls"] = callList
			messages = append(messages, assistant)
			for _, call := range toolCalls {
				name, _ := call["name"].(string)
				args, _ := call["arguments"].(map[string]any)
				events <- StreamEvent{Type: "tool_start", Name: name, Label: s.ToolNameLabel(name)}
				started := time.Now()
				result := s.ToolExecute(ctx, name, args, IsGuarded(question), sidOf(state))
				duration := int(time.Since(started).Milliseconds())
				stepData := toolStep(name, result)
				steps = append(steps, stepData)
				toolLog = append(toolLog, toolSummary(name, result))
				events <- StreamEvent{Type: "tool", Name: name, DurationMS: duration, Data: stepData}
				// 决策代理裁决卡片准入（含意图门禁/跨轮去重/模型显式控制）
				baseCard := cardOf(name, result)
				dec := s.DecideCard(name, result, args, question, toMapSlice(cards), baseCard, recentCardsDigest(state))
				if dec.ShouldEmit && dec.Card != nil {
					if len(cards) < maxCardsPerTurn {
						cards = append(cards, dec.Card)
					}
					events <- StreamEvent{Type: "card", Name: name, Card: dec.Card}
				} else {
					events <- StreamEvent{Type: "card", Name: name}
				}
				// 模型视图增强：回灌时注入卡片呈现状态（双系统认知打通）
				modelView, _ := json.Marshal(EnhanceModelView(result, dec))
				messages = append(messages, map[string]any{
					"role": "tool", "tool_call_id": stringOf(call["id"]),
					"content": string(modelView),
				})
			}
		}
		if exhausted && len(steps) > 0 {
			reason = "max_steps"
		}
		s.addUsage(cfg, usageIn, usageOut)
		turn := s.recordTurn(state, question, map[string]any{
			"answer": answer, "source": "llm", "reason": reason,
			"cards": cards, "steps": steps, "tools": toolLog,
			"usage_in": usageIn, "usage_out": usageOut,
		})
		var lastGame map[string]any
		if state != nil {
			lastGame = state.LastGame
		}
		followups := SuggestFollowups(question, intent, toMapSlice(cards), lastGame, len(steps))
		result := map[string]any{
			"answer": answer, "thinking": nil, "source": "llm", "reason": reason,
			"facts": nil, "cards": cards, "steps": steps, "tools": toolLog,
			"usage_in": usageIn, "usage_out": usageOut,
			"turn_id": turn, "title": s.GetStore().TitleOf(state),
			"elapsed_ms": int(time.Since(turnStarted).Milliseconds()),
		}
		if cacheSeen {
			var hitRate any
			if cacheReadSeen && cacheBase > 0 {
				rate := float64(cacheRead) / float64(cacheBase)
				if rate > 1 {
					rate = 1
				}
				hitRate = rate
			}
			result["cache_hit_rate"] = hitRate
			result["cache_read_tokens"] = any(nil)
			if cacheReadSeen {
				result["cache_read_tokens"] = cacheRead
			}
			result["cache_write_tokens"] = any(nil)
			if cacheWrite > 0 || cacheReadSeen {
				result["cache_write_tokens"] = cacheWrite
			}
			if cacheBase > 0 {
				result["cache_base_tokens"] = cacheBase
			}
		}
		if state != nil {
			result["archived_through"] = state.SummaryThrough
		}
		if len(followups) > 0 {
			result["followups"] = followups
		}
		s.logDecision(state, turn, question, intent, "agent", result)
		result["q"] = question
		events <- StreamEvent{Type: "done", Payload: result}
	}()
	return events
}

// nudgedEmpty 空转追问只给一次（回看最近 3 条）。
func (s *Service) nudgedEmpty(messages []map[string]any) bool {
	for i := len(messages) - 1; i >= 0 && len(messages)-i <= 3; i-- {
		if role, _ := messages[i]["role"].(string); role == "system" {
			if content, _ := messages[i]["content"].(string); strings.Contains(content, "系统提示") {
				return true
			}
		}
	}
	return false
}

func (s *Service) nudge(messages []map[string]any, text string) {
	messages = append(messages, map[string]any{"role": "system", "content": text})
}

// emitLlmFailure LLM 失败帧（降级为确定性事实）。
func (s *Service) emitLlmFailure(events chan<- StreamEvent, state *SessionState, question string, req AskRequest, intent string, err error) {
	llmErr, ok := err.(*PilotLlmError)
	if !ok {
		llmErr = newLlmError(err.Error(), "provider_error", 0)
	}
	result := s.fallbackDeterministic(context.Background(), question, req.AppID, state, intent, "llm_failed")
	result["reason"] = "llm_failed"
	result["diagnostic"] = llmErr.ToDiagnostic()
	turn := s.recordTurn(state, question, result)
	result["turn_id"] = turn
	result["title"] = s.GetStore().TitleOf(state)
	s.logDecision(state, turn, question, intent, "fallback", result)
	result["q"] = question
	events <- StreamEvent{Type: "done", Payload: result}
}

// buildBaseMessages 装配基线消息（系统提示 + 环境事实 + 预算内历史 + 当前问题）。
// 预算由模型窗口派生，超预算时先让位（保留最少 2 轮），再压缩。
func (s *Service) buildBaseMessages(state *SessionState, question string, req AskRequest, cfg ConfigPayload) []map[string]any {
	budget := s.historyBudget(cfg, state)
	var appid *int
	if req.AppID != nil {
		appid = req.AppID
	}
	messages := []map[string]any{{"role": "system", "content": s.systemPrompt()}}
	// 环境事实：每轮现取（今天日期），不入账本
	messages = append(messages, EnvironmentMessage())
	// 预算内装配历史轮次
	history := assemble(state, question, appid, budget, cfg.ToolContentLimit,
		req.ViewContext, req.ElementRefs, req.Quotes)
	messages = append(messages, history...)
	return messages
}

// historyBudget 由模型窗口派生历史预算。
func (s *Service) historyBudget(cfg ConfigPayload, state *SessionState) int {
	window := 0
	if cfg.ContextWindow != nil {
		window = *cfg.ContextWindow
	}
	overhead := EstimateTokens(SystemPrompt) + EstimateTokens(string(mustJSON(s.toolSpecs())))
	return deriveHistoryBudget(window, overhead)
}

// recentCardsDigest 近几轮已向用户呈现的卡片 (kind, identity) 清单。
func recentCardsDigest(state *SessionState) [][2]string {
	out := [][2]string{}
	if state == nil {
		return out
	}
	start := len(state.Turns) - 6
	if start < 0 {
		start = 0
	}
	for _, turn := range state.Turns[start:] {
		resp, _ := turn["resp"].(map[string]any)
		rawCards := anySlice(resp["cards"])
		for _, raw := range rawCards {
			card, ok := raw.(map[string]any)
			if !ok {
				continue
			}
			kind := stringOf(card["kind"])
			if kind == "" {
				continue
			}
			identity := CardIdentity(kind, card)
			if identity != "" {
				out = append(out, [2]string{kind, identity})
			}
		}
	}
	return out
}

func (s *Service) activeAPIKey(cfg ConfigPayload) string {
	if !cfg.HasAPIKey {
		return ""
	}
	key, _ := s.kvGetSecret(providerKeyName(cfg.Active))
	return key
}

func (s *Service) addUsage(cfg ConfigPayload, in, out int) {
	s.AddUsage(context.Background(), in, out)
}

// toolSpecs 工具表（桥接层注入，缺省空=纯对话轮）。
func (s *Service) toolSpecs() []map[string]any {
	if s.deps.ToolSpecs == nil {
		return nil
	}
	return s.deps.ToolSpecs()
}

// cardOf 工具结果 → 前端可渲染卡片（紧凑投影）。
func cardOf(name string, result map[string]any) map[string]any {
	kind := stringOf(result["kind"])
	switch kind {
	case "rows", "games", "compare", "family", "achievements", "price", "action",
		"epic", "navigate", "regions":
		card := map[string]any{"kind": kind}
		for _, key := range []string{"items", "rows", "titleKey", "appid", "name",
			"total", "target", "path", "offers", "mobile", "count", "fetchedAt"} {
			if v, ok := result[key]; ok {
				card[key] = v
			}
		}
		return card
	}
	if stringOf(result["componentId"]) != "" || stringOf(result["component_id"]) != "" {
		return result
	}
	return nil
}

// toMapSlice []any → []map[string]any。
func toMapSlice(list []any) []map[string]any {
	out := make([]map[string]any, 0, len(list))
	for _, item := range list {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

// buildMinimalMessages 溢出回收装配：仅要点存档 + 当前问题。
func (s *Service) buildMinimalMessages(state *SessionState, question string) []map[string]any {
	msgs := []map[string]any{{"role": "system", "content": s.systemPrompt()}}
	if state != nil && state.Summary != "" {
		msgs = append(msgs, map[string]any{"role": "system", "content": state.Summary})
	}
	msgs = append(msgs, map[string]any{"role": "user", "content": question})
	return msgs
}

// systemPrompt 基础规则 + 用户长期画像偏好（画像失败不影响本轮）。
func (s *Service) systemPrompt() string {
	if s.deps.DB == nil {
		return SystemPrompt
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	profile := agent.BuildProfileContext(ctx, s.deps.DB)
	if profile != "" {
		return SystemPrompt + "\n\n" + profile
	}
	return SystemPrompt
}
