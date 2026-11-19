package pilot

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/agent"
)

const (
	agentMaxSteps      = 8
	thinkBudgetChars   = 32000
	maxCardsPerTurn    = 4
	attachOnlyQuestion = "请查看我附带的附件。"
	inlineHeadChars    = 200

	toolProgressTick = 1 * time.Second
	stuckRepeatLimit = 2
	emptyStreakLimit = 3

	correctionMinSample = 400
	correctionMax       = 1.6

	nudgeEmptyPrompt = "（系统提示）你刚才没有输出任何内容也未调用工具。请立即调用合适的工具获取真实数据再回答；若确实无需工具，直接输出最终结论。"
	nudgeAnnounced   = "（系统提示）你刚才宣告了要查询但没有实际调用工具。请立即调用所需的工具获取真实数据；若确实无需工具，直接输出最终结论。"
	nudgeTruncated   = "（系统提示）你刚才的回答看起来没写完就停了。请接着把结论补完整：该给的数据/清单/依据要交代清楚，不要停在一句话中间。"
	nudgeNarration   = "（系统提示）你刚才只是描述了你准备做什么，没有给出用户要的答案。请立刻把查到的事实写成结论交给用户：先给结论，再列依据；如果确实查不到，就明确说「没有查到相关数据」并说明限制，不要再描述你的计划。"
	fallbackSummary  = "（系统提示）本轮工具调用已达上限。请立刻基于上面已经拿到的全部工具结果，直接输出给用户的最终回答：先给结论，再列依据；没有查到的部分如实说明「未查到」，不得编造数据。不要再调用任何工具。"
)

var (
	dataIntents = map[string]bool{
		IntentPriceAnalysis: true, IntentFindGames: true,
		IntentAddMonitor: true, IntentCreateAlert: true,
	}
	announceWords = []string{"我来", "让我来", "我这就", "先查", "查一下", "查查", "排查", "分析一下", "帮你查", "帮您查"}
)

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
		s.SetInflightQuestion(sid, q)
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

func (s *Service) askStreamBody(ctx context.Context, question string, req AskRequest) <-chan StreamEvent {
	events := make(chan StreamEvent, 256)
	go func() {
		defer close(events)
		var state *SessionState
		if req.SessionID != "" {
			state = s.GetStore().LoadOrNew(req.SessionID)
		}
		var attRefs []map[string]any
		var attMsg map[string]any
		if sid := sidOf(state); sid != "" && s.deps.DataDir != "" && (len(req.Attachments) > 0 || (question != "" && !ShouldInline(question))) {
			resolved := ResolveRefsWithText(s.deps.DataDir, sid, req.Attachments, question)
			if len(resolved) > 0 {
				if question != "" && !ShouldInline(question) {
					r := []rune(question)
					if len(r) > inlineHeadChars {
						r = r[:inlineHeadChars]
					}
					question = string(r) + "……\n（提问正文过长，已整体存为附件，见下方附件清单）"
				}
				attRefs = AttachmentPublicRefs(resolved)
				attMsg = AttachmentReferenceMessage(resolved)
			}
		}
		if question == "" {
			question = attachOnlyQuestion
		}
		intent := Route(question)
		if navTarget := NavTarget(question); intent == IntentNavigate && navTarget != "" {
			result := s.navigateDirect(ctx, navTarget)
			if steps, _ := result["steps"].([]any); len(steps) > 0 {
				if step, ok := steps[0].(map[string]any); ok {
					events <- StreamEvent{Type: "tool_start", Name: "navigate", Label: stringOf(step["label"])}
					events <- StreamEvent{Type: "tool", Name: "navigate", Data: step}
				}
			}
			turn := s.recordTurn(state, question, result, nil)
			result["turn_id"] = turn
			result["title"] = titleOrNil(s, state)
			s.logDecision(state, turn, question, intent, "direct", result)
			result["q"] = question
			events <- StreamEvent{Type: "done", Payload: result}
			return
		}
		cfg := s.LoadConfig()
		overCap := s.OverCap()
		if !cfg.LlmReady() || overCap {
			reason := ""
			if overCap {
				reason = "cap_reached"
			}
			result := s.fallbackDeterministic(ctx, question, req.AppID, state, intent, reason)
			if result["facts"] != nil {
				result["cards"] = []any{result["facts"]}
			} else {
				result["cards"] = []any{}
			}
			if len(attRefs) > 0 {
				result["attachments"] = attRefs
			}
			if len(req.Quotes) > 0 {
				result["quotes"] = req.Quotes
			}
			turn := s.recordTurn(state, question, result, nil)
			result["turn_id"] = turn
			result["title"] = titleOrNil(s, state)
			s.logDecision(state, turn, question, intent, "fallback", result)
			result["q"] = question
			events <- StreamEvent{Type: "done", Payload: result}
			return
		}

		for event := range s.agentStream(ctx, state, question, req, cfg, intent, attRefs, attMsg) {
			events <- event
		}
	}()
	return events
}

func titleOrNil(s *Service, state *SessionState) any {
	if state == nil {
		return nil
	}
	return s.GetStore().TitleOf(state)
}

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

func (s *Service) fallbackDeterministic(ctx context.Context, question string, appID *int, state *SessionState, intent, reason string) map[string]any {
	empty := func(source, fallbackReason string) map[string]any {
		r := fallbackReason
		if r == "" {
			r = reason
		}
		var reasonVal any = r
		if r == "" {
			reasonVal = nil
		}
		return map[string]any{"answer": "", "thinking": nil, "source": source, "reason": reasonVal, "facts": nil}
	}
	guarded := IsGuarded(question)
	switch intent {
	case IntentHowTo:
		return empty("guide", "")
	case IntentCreateAlert:
		query := extractTitle(question)
		if query == "" {
			query = question
		}
		matches := s.searchGamesFor(ctx, query)
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
			for key, val := range result {
				action[key] = val
			}
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": reasonOrNil(reason), "facts": action}
		}
		if len(matches) > 0 {
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": "need_target", "facts": map[string]any{"kind": "games", "items": matches}}
		}
		return empty("none", "no_data")
	case IntentAddMonitor:
		query := extractTitle(question)
		if query == "" {
			query = question
		}
		matches := s.searchGamesFor(ctx, query)
		if len(matches) == 1 {
			first, _ := matches[0].(map[string]any)
			result := s.ToolExecute(ctx, "add_follow", map[string]any{"appid": first["appid"]}, guarded, sidOf(state))
			if stringOf(result["kind"]) == "denied" {
				return empty("guide", "")
			}
			action := map[string]any{"kind": "action"}
			for key, val := range result {
				action[key] = val
			}
			return map[string]any{"answer": "", "thinking": nil, "source": "facts",
				"reason": reasonOrNil(reason), "facts": action}
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
			} else if len(matches) == 0 && state != nil && state.LastGame != nil {
				if containsAny(question, []string{"它", "这游戏", "该游戏", "这款"}) {
					target = jsonInt(state.LastGame["appid"])
				}
			}
		}
		if target > 0 {
			facts := s.ToolExecute(ctx, "get_price_briefing", map[string]any{"appid": target}, false, sidOf(state))
			if stringOf(facts["kind"]) != "failed" && stringOf(facts["kind"]) != "empty" {
				if state != nil {
					state.LastGame = map[string]any{"appid": target, "name": facts["name"]}
				}
				return map[string]any{"answer": "", "thinking": nil, "source": "facts",
					"reason": reasonOrNil(reason), "facts": facts}
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
				"reason": reasonOrNil(reason), "facts": items}
		}
		return empty("none", "no_data")
	}
	if reason != "" {
		return empty("none", reason)
	}
	return empty("none", "llm_off")
}

func reasonOrNil(reason string) any {
	if reason == "" {
		return nil
	}
	return reason
}

func sidOf(state *SessionState) string {
	if state == nil {
		return ""
	}
	return state.Sid
}

func extractAlert(question string) (string, int) {
	if strings.Contains(question, "史低") {
		return "historic_low", 0
	}
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

func (s *Service) recordTurn(state *SessionState, question string, result map[string]any, turnMessages []map[string]any) string {
	if state == nil {
		return ""
	}
	tid := newProviderID()
	storedMsgs := turnMessages
	if storedMsgs == nil {
		storedMsgs = []map[string]any{}
	}
	record := map[string]any{
		"tid":       tid,
		"q":         question,
		"resp":      result,
		"messages":  storedMsgs,
		"last_game": state.LastGame,
	}
	if !s.GetStore().AppendTurn(state.Sid, record) {
		return ""
	}
	return tid
}

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
	case name == "add_follow" || name == "create_price_alert":
		if label := stringOf(result["name"]); label != "" {
			return fmt.Sprintf("%s: %s", name, label)
		}
		return fmt.Sprintf("%s: %v", name, result["appid"])
	case name == "propose_bulk":
		return fmt.Sprintf("propose_bulk: %d pending", len(anySlice(result["items"])))
	}
	return name
}

func (s *Service) searchGamesFor(ctx context.Context, question string) []any {
	result := s.ToolExecute(ctx, "search_games", map[string]any{"q": question}, false, "")
	return anySlice(result["items"])
}

func (s *Service) referenceMessage(refSid string) map[string]any {
	if strings.TrimSpace(refSid) == "" {
		return nil
	}
	ref := s.GetStore().Load(refSid)
	if ref == nil {
		return nil
	}
	digest := ReferenceDigest(ref)
	if digest == "" {
		return nil
	}
	return ReferenceMessage(s.GetStore().TitleOf(ref), digest)
}

type phaseRec struct {
	Step      int
	Thinking  strings.Builder
	Text      strings.Builder
	Steps     []map[string]any
	Cards     []any
	ThinkMS   float64
	Truncated bool
	Terminal  bool
	Discarded bool
}

func (p *phaseRec) toMap(narration bool) map[string]any {
	text := p.Text.String()
	var thinkMS any
	if int(p.ThinkMS) > 0 {
		thinkMS = int(p.ThinkMS)
	}
	stepsAny := make([]any, len(p.Steps))
	for i, s := range p.Steps {
		stepsAny[i] = s
	}
	m := map[string]any{
		"step":      p.Step,
		"thinking":  p.Thinking.String(),
		"text":      text,
		"steps":     stepsAny,
		"cards":     p.Cards,
		"think_ms":  thinkMS,
		"truncated": p.Truncated,
		"terminal":  p.Terminal,
	}
	if narration && strings.TrimSpace(text) != "" {
		m["narration"] = p.Discarded || isSelfNarration(text)
	}
	return m
}

func (s *Service) agentStream(ctx context.Context, state *SessionState, question string,
	req AskRequest, cfg ConfigPayload, intent string,
	attRefs []map[string]any, attMsg map[string]any) <-chan StreamEvent {
	events := make(chan StreamEvent, 256)
	go func() {
		defer close(events)
		expanded := sessionExpandedTools(state)
		unlocked := cloneStringBoolMap(expanded)
		allSpecs := s.allToolSpecs()
		sysPrompt := s.systemPrompt()

		base, compacted := s.assembleForModel(ctx, state, question, req, cfg, sysPrompt, expanded)
		pre := []map[string]any{}
		if refMsg := s.referenceMessage(req.ReferenceSid); refMsg != nil {
			pre = append(pre, refMsg)
		}
		if attMsg != nil {
			pre = append(pre, attMsg)
		}
		if len(pre) > 0 {
			combined := make([]map[string]any, 0, len(pre)+len(base))
			combined = append(combined, pre...)
			combined = append(combined, base...)
			base = combined
		}

		messages := make([]map[string]any, 0, len(base)+8)
		messages = append(messages, map[string]any{"role": "system", "content": sysPrompt})
		messages = append(messages, EnvironmentMessage())
		messages = append(messages, base...)

		newMsgs := []map[string]any{base[len(base)-1]}
		appendSys := func(msg map[string]any) {
			if msg == nil {
				return
			}
			messages = append(messages, msg)
			newMsgs = append(newMsgs, msg)
		}
		appendSys(resumeNote(req.Resume))
		appendSys(deterministicNote(question, intent))
		recentCards := recentCardsDigest(state)
		appendSys(cardLedgerNote(recentCards))

		var prefetch *prefetchResult
		if req.Resume == nil {
			prefetch = s.prefetchFacts(ctx, question, req.AppID, state, intent, recentCards)
		}
		if prefetch != nil {
			appendSys(prefetch.Note)
		}
		prefetchEmitted := false

		ctxTokens := EstimateMessages(messages)
		ctxBudget := s.historyBudgetWith(cfg, state, sysPrompt, unlocked)

		baseURL := EffectiveBaseURL(cfg.Protocol, cfg.BaseURL)
		client := HttpClient(baseURL)
		defer client.CloseIdleConnections()

		turnStarted := time.Now()
		phaseList := []*phaseRec{}
		announcedStep := 0
		cards := []any{}
		toolLog := []any{}
		var lastGame map[string]any
		if state != nil {
			lastGame = state.LastGame
		}

		var usageIn, usageOut int
		var firstIn *int
		var cacheRead, cacheWrite, cacheBase int
		cacheSeen, cacheReadSeen := false, false
		var ttftMS, decodeMS time.Duration
		var ttftN, decodeOut int
		var thinkMS float64
		var thinkOpenAt *time.Time
		thinkClose := func() {
			if thinkOpenAt != nil {
				elapsed := float64(time.Since(*thinkOpenAt).Milliseconds())
				thinkMS += elapsed
				if len(phaseList) > 0 {
					phaseList[len(phaseList)-1].ThinkMS += elapsed
				}
				thinkOpenAt = nil
			}
		}

		var answer string
		reason := ""
		exhausted := true
		overflowRetried := false
		nudged := false
		truncatedPrefix := ""
		stuckNudged := false
		stuckRepeats := 0
		lastCallSig := ""
		emptyStreak := 0
		emptyStreakNudged := false
		toolsRan := 0
		step := 0
		baseGuarded := IsGuarded(question)

		settleAllRunning := func() {
			for _, p := range phaseList {
				for _, st := range p.Steps {
					if stringOf(st["status"]) == "running" {
						st["status"] = "stopped"
						st["data"] = map[string]any{}
					}
				}
			}
		}

		emitResult := func(phase *phaseRec, name, callID string, args map[string]any, result map[string]any, durationMS int) {
			stepData := toolStep(name, result)
			settled := false
			for _, st := range phase.Steps {
				if stringOf(st["status"]) == "running" {
					for k := range st {
						delete(st, k)
					}
					for k, v := range stepData {
						st[k] = v
					}
					settled = true
					break
				}
			}
			if !settled {
				phase.Steps = append(phase.Steps, stepData)
			}
			toolLog = append(toolLog, toolSummary(name, result))
			if isEmptyToolResult(result) {
				emptyStreak++
				if !emptyStreakNudged && emptyStreak >= emptyStreakLimit && step < agentMaxSteps {
					emptyStreakNudged = true
					messages = append(messages, map[string]any{
						"role":    "system",
						"content": fmt.Sprintf("（系统提示）你已经连续 %d 次调用的工具都没有返回有效数据。继续换词重试大概率还是空——请立刻停止检索，基于目前已有的信息给出结论；确实查不到的部分，如实告诉用户「没有查到相关数据」，不要编造。", emptyStreak),
					})
				}
			} else {
				emptyStreak = 0
			}
			toolEvt := map[string]any{}
			for k, v := range stepData {
				toolEvt[k] = v
			}
			events <- StreamEvent{Type: "tool", Name: name, DurationMS: durationMS, Data: toolEvt}

			dec := s.DecideCard(name, result, args, question, toMapSlice(cards), cardOf(name, result), recentCards)
			if dec.ShouldEmit && dec.Card != nil {
				if len(cards) < maxCardsPerTurn {
					cards = append(cards, dec.Card)
				}
				phase.Cards = append(phase.Cards, dec.Card)
				events <- StreamEvent{Type: "card", Name: name, Card: dec.Card}
			} else {
				events <- StreamEvent{Type: "card", Name: name, Card: nil}
			}
			if rawProp, ok := result["proposal"].(map[string]any); ok && rawProp != nil {
				pDec := s.DecideCard("propose_bulk", rawProp, args, question, toMapSlice(cards), cardOf("propose_bulk", rawProp), recentCards)
				if pDec.ShouldEmit && pDec.Card != nil {
					if len(cards) < maxCardsPerTurn {
						cards = append(cards, pDec.Card)
					}
					phase.Cards = append(phase.Cards, pDec.Card)
					events <- StreamEvent{Type: "card", Name: "propose_bulk", Card: pDec.Card}
				}
			}
			if (name == "add_follow" || name == "create_price_alert") && jsonInt(result["appid"]) > 0 {
				lastGame = map[string]any{"appid": jsonInt(result["appid"]), "name": result["name"]}
			}
			if lastGame == nil || strings.TrimSpace(stringOf(lastGame["name"])) == "" {
				if n := strings.TrimSpace(stringOf(result["name"])); n != "" && jsonInt(result["appid"]) > 0 {
					lastGame = map[string]any{"appid": jsonInt(result["appid"]), "name": n}
				}
			}
			if callID == "" {
				callID = fmt.Sprintf("%s-%d", name, len(messages))
			}
			modelView, _ := json.Marshal(EnhanceModelView(result, dec))
			toolMsg := map[string]any{"role": "tool", "tool_call_id": callID, "content": string(modelView)}
			messages = append(messages, toolMsg)
			newMsgs = append(newMsgs, toolMsg)
		}

		for step < agentMaxSteps {
			phase := &phaseRec{Step: step + 1, Cards: []any{}}
			phaseList = append(phaseList, phase)
			if phase.Step != announcedStep {
				announcedStep = phase.Step
				events <- StreamEvent{Type: "step_start", Step: phase.Step}
			}
			if phase.Step == 1 && prefetch != nil {
				for _, item := range prefetch.Items {
					phase.Steps = append(phase.Steps, item.Step)
					if item.Card != nil {
						phase.Cards = append(phase.Cards, item.Card)
					}
				}
				if !prefetchEmitted {
					prefetchEmitted = true
					cards = append(cards, prefetch.Cards...)
					for _, item := range prefetch.Items {
						toolLog = append(toolLog, item.Summary)
						events <- StreamEvent{Type: "tool_start", Name: item.Name, Label: item.Label, CardKind: item.CardKind}
						events <- StreamEvent{Type: "tool", Name: item.Name, DurationMS: item.DurationMS, Data: item.Step}
						events <- StreamEvent{Type: "card", Name: item.Name, Card: item.Card}
					}
					toolsRan += len(prefetch.Items)
				}
			}

			var stepTools []map[string]any
			if !(step == 0 && prefetch != nil && prefetch.Force) {
				stepTools = s.visibleToolSpecs(allSpecs, unlocked)
			}
			phaseThinkChars := 0
			var roundAnswer strings.Builder
			var toolCalls []map[string]any
			emitted := false
			reqStarted := time.Now()
			var firstDeltaAt *time.Time
			reqOut := -1

			reqCtx, cancel := context.WithTimeout(ctx, llmTimeout)
			eventCh, err := s.ChatStream(reqCtx, cfg.Protocol, baseURL, s.activeAPIKey(cfg),
				cfg.Model, messages, stepTools, 0, client)
			if err != nil {
				cancel()
				settleAllRunning()
				s.emitLlmFailure(events, state, question, req, intent, attRefs, err)
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
				if (event.Kind == "thinking" || event.Kind == "answer") && firstDeltaAt == nil {
					now := time.Now()
					firstDeltaAt = &now
				}
				switch event.Kind {
				case "thinking":
					if thinkOpenAt == nil {
						now := time.Now()
						thinkOpenAt = &now
					}
					runesDelta := len([]rune(event.Text))
					if phaseThinkChars+runesDelta > thinkBudgetChars {
						phase.Truncated = true
						continue
					}
					phaseThinkChars += runesDelta
					phase.Thinking.WriteString(event.Text)
					emitted = true
					events <- StreamEvent{Type: "thinking", Delta: event.Text}
				case "answer":
					thinkClose()
					phase.Text.WriteString(event.Text)
					roundAnswer.WriteString(event.Text)
					emitted = true
					events <- StreamEvent{Type: "answer", Delta: event.Text}
				case "tool_calls":
					thinkClose()
					toolCalls = event.Calls
				case "usage":
					usageIn += event.UsageIn
					usageOut += event.UsageOut
					reqOut = event.UsageOut
				case "context":
					if firstIn == nil && event.UsageIn > 0 {
						v := event.UsageIn
						firstIn = &v
					}
				case "cache":
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
			reqEnded := time.Now()
			cancel()
			if firstDeltaAt != nil {
				ttftMS += firstDeltaAt.Sub(reqStarted)
				ttftN++
				if reqOut >= 0 {
					decodeMS += reqEnded.Sub(*firstDeltaAt)
					decodeOut += reqOut
				}
			}
			if llmErr != nil {
				if step == 0 && !emitted && !overflowRetried &&
					state != nil && (len(state.Turns) > 0 || state.Summary != "") &&
					LooksLikeOverflow(llmErr.Message) {
					overflowRetried = true
					minimal := s.buildMinimalMessages(state, question, req)
					messages = append([]map[string]any{{"role": "system", "content": sysPrompt}}, minimal...)
					newMsgs = []map[string]any{minimal[len(minimal)-1]}
					ctxTokens = EstimateMessages(messages)
					phaseList = phaseList[:len(phaseList)-1]
					continue
				}
				settleAllRunning()
				s.emitLlmFailure(events, state, question, req, intent, attRefs, llmErr)
				return
			}

			step++
			if len(toolCalls) == 0 {
				text := roundAnswer.String()
				if !nudged && step < agentMaxSteps {
					var nudgeMsg string
					discardText := false
					truncatedNudge := false
					switch {
					case strings.TrimSpace(text) == "":
						nudgeMsg = nudgeEmptyPrompt
					case toolsRan == 0 && dataIntents[intent] && containsAny(text, announceWords):
						nudgeMsg = nudgeAnnounced
						discardText = true
					case isSelfNarration(text):
						nudgeMsg = nudgeNarration
						discardText = true
					case looksTruncated(text, toolsRan, cards):
						nudgeMsg = nudgeTruncated
						truncatedNudge = true
					}
					if nudgeMsg != "" {
						nudged = true
						if discardText {
							phase.Discarded = true
						}
						if truncatedNudge {
							messages = append(messages,
								map[string]any{"role": "assistant", "content": text},
								map[string]any{"role": "user", "content": nudgeMsg},
							)
							truncatedPrefix = text
						} else {
							messages = append(messages, map[string]any{"role": "system", "content": nudgeMsg})
						}
						continue
					}
				}
				phase.Terminal = true
				messages = append(messages, map[string]any{"role": "assistant", "content": emptyToNil(text)})
				newMsgs = append(newMsgs, map[string]any{"role": "assistant", "content": emptyToNil(truncatedPrefix + text)})
				exhausted = false
				break
			}

			truncatedPrefix = ""
			assistant := map[string]any{"role": "assistant", "content": emptyToNil(roundAnswer.String())}
			callList := make([]any, 0, len(toolCalls))
			for _, call := range toolCalls {
				argsJSON, _ := json.Marshal(call["arguments"])
				callList = append(callList, map[string]any{
					"id": call["id"], "type": "function",
					"function": map[string]any{"name": call["name"], "arguments": string(argsJSON)},
				})
			}
			assistant["tool_calls"] = callList
			messages = append(messages, assistant)
			newMsgs = append(newMsgs, assistant)

			for _, call := range toolCalls {
				if stringOf(call["name"]) == "list_tools" {
					for _, expName := range s.expandableToolNames(allSpecs) {
						unlocked[expName] = true
					}
					break
				}
			}

			if !stuckNudged {
				sigList := make([]map[string]any, 0, len(toolCalls))
				for _, call := range toolCalls {
					a, _ := call["arguments"].(map[string]any)
					if a == nil {
						a = map[string]any{}
					}
					sigList = append(sigList, map[string]any{"name": call["name"], "args": a})
				}
				sig := string(mustJSON(sigList))
				if sig == lastCallSig {
					stuckRepeats++
					if stuckRepeats >= stuckRepeatLimit {
						stuckNudged = true
						messages = append(messages, map[string]any{
							"role":    "system",
							"content": fmt.Sprintf("（系统提示）你已经连续用同样的参数调用同一个工具 %d 次，上一次的结果在前面，先读它。重复同样的调用拿不到新东西：请换参数、换另一个工具、或者证据已经够了就直接给结论（没有证据的部分如实说没查到）。", stuckRepeats+1),
						})
					}
				} else {
					stuckRepeats = 0
					lastCallSig = sig
				}
			}

			allParallelRead := len(toolCalls) > 1
			for _, call := range toolCalls {
				if !agent.IsReadTool(stringOf(call["name"])) {
					allParallelRead = false
					break
				}
			}

			if allParallelRead {
				type batchItem struct {
					name       string
					label      string
					callID     string
					args       map[string]any
					startedAt  time.Time
					durationMS int
					result     map[string]any
					done       bool
				}
				batch := make([]*batchItem, len(toolCalls))
				var mu sync.Mutex
				var wg sync.WaitGroup
				for i, call := range toolCalls {
					toolsRan++
					name, _ := call["name"].(string)
					args, _ := call["arguments"].(map[string]any)
					label := s.ToolNameLabel(name)
					phase.Steps = append(phase.Steps, map[string]any{"label": label, "status": "running", "data": map[string]any{}})
					events <- StreamEvent{
						Type:     "tool_start",
						Name:     name,
						Label:    label,
						CardKind: s.StreamCardHint(name, args, question, toMapSlice(cards), cardKindHint(name), recentCards),
					}
					item := &batchItem{
						name:      name,
						label:     label,
						callID:    stringOf(call["id"]),
						args:      args,
						startedAt: time.Now(),
					}
					batch[i] = item
					wg.Go(func() {
						res := s.executeToolWithBudget(ctx, item.name, item.args, baseGuarded, sidOf(state))
						mu.Lock()
						item.result = res
						item.durationMS = int(time.Since(item.startedAt).Milliseconds())
						item.done = true
						mu.Unlock()
					})
				}
				waitCh := make(chan struct{})
				go func() {
					wg.Wait()
					close(waitCh)
				}()
				ticker := time.NewTicker(toolProgressTick)
			batchLoop:
				for {
					select {
					case <-waitCh:
						break batchLoop
					case <-ticker.C:
						mu.Lock()
						for _, it := range batch {
							if !it.done {
								events <- StreamEvent{
									Type:      "tool_progress",
									Name:      it.name,
									Label:     it.label,
									ElapsedMS: int(time.Since(it.startedAt).Milliseconds()),
								}
							}
						}
						mu.Unlock()
					}
				}
				ticker.Stop()
				for _, it := range batch {
					emitResult(phase, it.name, it.callID, it.args, it.result, it.durationMS)
				}
				continue
			}

			writeSeen := 0
			for _, call := range toolCalls {
				toolsRan++
				name, _ := call["name"].(string)
				args, _ := call["arguments"].(map[string]any)
				label := s.ToolNameLabel(name)
				phase.Steps = append(phase.Steps, map[string]any{"label": label, "status": "running", "data": map[string]any{}})
				events <- StreamEvent{
					Type:     "tool_start",
					Name:     name,
					Label:    label,
					CardKind: s.StreamCardHint(name, args, question, toMapSlice(cards), cardKindHint(name), recentCards),
				}
				guarded := baseGuarded
				if agent.IsWriteTool(name) {
					writeSeen++
					guarded = guarded || writeSeen > 2
				}
				startedAt := time.Now()
				doneCh := make(chan map[string]any, 1)
				go func() {
					doneCh <- s.executeToolWithBudget(ctx, name, args, guarded, sidOf(state))
				}()
				ticker := time.NewTicker(toolProgressTick)
				var result map[string]any
			seqWait:
				for {
					select {
					case result = <-doneCh:
						break seqWait
					case <-ticker.C:
						events <- StreamEvent{
							Type:      "tool_progress",
							Name:      name,
							Label:     label,
							ElapsedMS: int(time.Since(startedAt).Milliseconds()),
						}
					}
				}
				ticker.Stop()
				emitResult(phase, name, stringOf(call["id"]), args, result, int(time.Since(startedAt).Milliseconds()))
			}
		}

		var summaryDiagnostic map[string]any
		if exhausted && toolsRan > 0 {
			for _, attempt := range []string{fallbackSummary, nudgeNarration} {
				phase := &phaseRec{Step: step + 1, Cards: []any{}, Terminal: true}
				phaseList = append(phaseList, phase)
				events <- StreamEvent{Type: "step_start", Step: phase.Step}
				messages = append(messages, map[string]any{"role": "system", "content": attempt})
				reqCtx, cancel := context.WithTimeout(ctx, llmTimeout)
				eventCh, err := s.ChatStream(reqCtx, cfg.Protocol, baseURL, s.activeAPIKey(cfg),
					cfg.Model, messages, nil, 0, client)
				if err != nil {
					cancel()
					if llmErr, ok := err.(*PilotLlmError); ok {
						summaryDiagnostic = llmErr.ToDiagnostic()
					}
					continue
				}
				var streamErr *PilotLlmError
				for ev := range eventCh {
					switch ev.Kind {
					case "thinking":
						phase.Thinking.WriteString(ev.Text)
						events <- StreamEvent{Type: "thinking", Delta: ev.Text}
					case "answer":
						phase.Text.WriteString(ev.Text)
						events <- StreamEvent{Type: "answer", Delta: ev.Text}
					case "usage":
						usageIn += ev.UsageIn
						usageOut += ev.UsageOut
					case "error":
						if ev.Err != nil {
							streamErr = ev.Err
						} else {
							streamErr = newLlmError(ev.Text, "provider_stream_error", 0)
						}
					}
				}
				cancel()
				if streamErr != nil {
					summaryDiagnostic = streamErr.ToDiagnostic()
				}
			}
		}

		settleAllRunning()
		thinkClose()
		if exhausted && len(phaseList) > 0 && strings.TrimSpace(phaseList[len(phaseList)-1].Text.String()) == "" {
			reason = "max_steps"
		}

		phasesOut := make([]any, 0, len(phaseList))
		stepsFlat := []any{}
		var thinkingJoined strings.Builder
		answerTexts := make([]string, 0, len(phaseList))
		for _, p := range phaseList {
			pm := p.toMap(true)
			phasesOut = append(phasesOut, pm)
			for _, st := range p.Steps {
				stepsFlat = append(stepsFlat, st)
			}
			thinkingJoined.WriteString(p.Thinking.String())
			if !p.Discarded {
				answerTexts = append(answerTexts, p.Text.String())
			}
		}
		for len(answerTexts) > 1 && isSelfNarration(answerTexts[len(answerTexts)-1]) {
			answerTexts = answerTexts[:len(answerTexts)-1]
		}
		answer = stripLeakedThinking(strings.Join(answerTexts, ""))

		var thinkingVal any
		if tStr := thinkingJoined.String(); tStr != "" {
			thinkingVal = tStr
		}
		var thinkMSVal any
		if int(thinkMS) > 0 {
			thinkMSVal = int(thinkMS)
		}
		if usageIn != 0 || usageOut != 0 {
			s.addUsage(cfg, usageIn, usageOut)
		}
		if state != nil && lastGame != nil {
			state.LastGame = lastGame
		}
		newlyUnlocked := []string{}
		for name := range unlocked {
			if !expanded[name] {
				newlyUnlocked = append(newlyUnlocked, name)
			}
		}
		slices.Sort(newlyUnlocked)

		followups := SuggestFollowups(question, intent, toMapSlice(cards), lastGame, toolsRan)
		cappedCards := cards
		if len(cappedCards) > maxCardsPerTurn {
			cappedCards = cappedCards[:maxCardsPerTurn]
		}
		result := map[string]any{
			"answer": answer, "thinking": thinkingVal, "think_ms": thinkMSVal,
			"source": "llm", "reason": reasonOrNil(reason),
			"facts": nil, "cards": cappedCards, "steps": stepsFlat,
			"phases": phasesOut, "tools": toolLog,
			"ctx_tokens": ctxTokens, "ctx_budget": ctxBudget,
			"ctx_breakdown": s.ctxBreakdown(base, state, question, req, sysPrompt, unlocked),
			"usage_in":      usageIn, "usage_out": usageOut,
			"first_in":   nil,
			"decode_ms":  int(decodeMS.Milliseconds()),
			"decode_out": decodeOut,
			"ttft_ms":    int(ttftMS.Milliseconds()),
			"ttft_n":     ttftN,
			"elapsed_ms": int(time.Since(turnStarted).Milliseconds()),
		}
		if firstIn != nil {
			result["first_in"] = *firstIn
		}
		if len(newlyUnlocked) > 0 {
			result["tool_catalog"] = newlyUnlocked
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
		if len(attRefs) > 0 {
			result["attachments"] = attRefs
		}
		if len(req.Quotes) > 0 {
			result["quotes"] = req.Quotes
		}
		if compacted {
			result["compacted"] = true
		}
		if summaryDiagnostic != nil {
			result["diagnostic"] = summaryDiagnostic
		}

		storedMsgs := newMsgs
		if len(pre) > 0 {
			storedMsgs = append(append([]map[string]any{}, pre...), newMsgs...)
		}
		turn := s.recordTurn(state, question, result, storedMsgs)
		result["turn_id"] = turn
		result["title"] = titleOrNil(s, state)
		s.logDecision(state, turn, question, intent, "agent", result)
		result["q"] = question
		events <- StreamEvent{Type: "done", Payload: result}
	}()
	return events
}

func emptyToNil(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func (s *Service) emitLlmFailure(events chan<- StreamEvent, state *SessionState, question string,
	req AskRequest, intent string, attRefs []map[string]any, err error) {
	llmErr, ok := err.(*PilotLlmError)
	if !ok {
		llmErr = newLlmError(err.Error(), "provider_error", 0)
	}
	result := s.fallbackDeterministic(context.Background(), question, req.AppID, state, intent, "llm_failed")
	if stringOf(result["source"]) != "guide" {
		result["reason"] = "llm_failed"
	}
	result["diagnostic"] = llmErr.ToDiagnostic()
	if result["facts"] != nil {
		result["cards"] = []any{result["facts"]}
	} else {
		result["cards"] = []any{}
	}
	if len(attRefs) > 0 {
		result["attachments"] = attRefs
	}
	if len(req.Quotes) > 0 {
		result["quotes"] = req.Quotes
	}
	turn := s.recordTurn(state, question, result, nil)
	result["turn_id"] = turn
	result["title"] = titleOrNil(s, state)
	s.logDecision(state, turn, question, intent, "fallback", result)
	result["q"] = question
	events <- StreamEvent{Type: "done", Payload: result}
}

func (s *Service) executeToolWithBudget(ctx context.Context, name string, args map[string]any, guarded bool, sid string) map[string]any {
	budget := agent.BudgetFor(name)
	toolCtx, cancel := context.WithTimeout(ctx, time.Duration(budget*float64(time.Second)))
	defer cancel()
	done := make(chan map[string]any, 1)
	go func() {
		defer func() {
			if r := recover(); r != nil {
				done <- map[string]any{"kind": "failed", "tool": name, "note": fmt.Sprintf("panic: %v", r)}
			}
		}()
		done <- s.ToolExecute(toolCtx, name, args, guarded, sid)
	}()
	select {
	case res := <-done:
		return res
	case <-toolCtx.Done():
		if ctx.Err() != nil {
			return map[string]any{"kind": "failed", "tool": name, "note": "context canceled"}
		}
		return map[string]any{"kind": "timeout", "tool": name, "budget_s": int(budget)}
	}
}

func (s *Service) historyBudget(cfg ConfigPayload, state *SessionState) int {
	return s.historyBudgetWith(cfg, state, SystemPrompt, nil)
}

func (s *Service) historyBudgetWith(cfg ConfigPayload, state *SessionState, sysPrompt string, expanded map[string]bool) int {
	window := 0
	if cfg.ContextWindow != nil {
		window = *cfg.ContextWindow
	}
	toolsTokens := EstimateTokens(string(mustJSON(s.visibleToolSpecs(s.allToolSpecs(), expanded))))
	overhead := EstimateTokens(sysPrompt) + toolsTokens
	correction := measureCorrection(state, toolsTokens)
	return deriveHistoryBudgetWithCorrection(window, overhead, correction)
}

func measureCorrection(state *SessionState, toolsTokens int) float64 {
	if state == nil {
		return 1.0
	}
	for i := len(state.Turns) - 1; i >= 0; i-- {
		resp, _ := state.Turns[i]["resp"].(map[string]any)
		realIn := jsonInt(resp["first_in"])
		est := jsonInt(resp["ctx_tokens"])
		if realIn <= 0 || est < correctionMinSample {
			continue
		}
		denom := est + toolsTokens
		if denom < 1 {
			denom = 1
		}
		factor := float64(realIn) / float64(denom)
		if factor < 0.5 || factor > 4.0 {
			return 1.0
		}
		if factor < 1.0 {
			return 1.0
		}
		if factor > correctionMax {
			return correctionMax
		}
		return factor
	}
	return 1.0
}

func (s *Service) ctxBreakdown(base []map[string]any, state *SessionState, question string, req AskRequest,
	sysPrompt string, expanded map[string]bool) []map[string]any {
	summaryChars := 0
	if state != nil {
		summaryChars = len([]rune(state.Summary))
	}
	baseChars := 0
	for _, m := range base {
		if c, ok := m["content"].(string); ok {
			baseChars += len([]rune(c))
		}
	}
	currentChars := len([]rune(UserText(question, req.AppID, req.ViewContext, req.ElementRefs, req.Quotes)))
	promptChars := len([]rune(sysPrompt))
	toolsChars := len([]rune(string(mustJSON(s.visibleToolSpecs(s.allToolSpecs(), expanded)))))
	histChars := baseChars - summaryChars - currentChars
	if histChars < 0 {
		histChars = 0
	}
	return []map[string]any{
		{"source": "system", "chars": promptChars},
		{"source": "tools", "chars": toolsChars},
		{"source": "summary", "chars": summaryChars},
		{"source": "history", "chars": histChars},
		{"source": "current", "chars": currentChars},
	}
}

func recentCardsDigest(state *SessionState) [][2]string {
	out := [][2]string{}
	if state == nil {
		return out
	}
	start := len(state.Turns) - recentCardTurns
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
	return s.ProviderKey(cfg.Active)
}

func (s *Service) addUsage(cfg ConfigPayload, in, out int) {
	s.AddUsage(context.Background(), in, out)
}

func (s *Service) allToolSpecs() []map[string]any {
	if s.deps.ToolSpecs == nil {
		return nil
	}
	return s.deps.ToolSpecs()
}

func specName(spec map[string]any) string {
	fn, _ := spec["function"].(map[string]any)
	if fn == nil {
		return ""
	}
	name, _ := fn["name"].(string)
	return name
}

func (s *Service) visibleToolSpecs(all []map[string]any, expanded map[string]bool) []map[string]any {
	if len(all) == 0 {
		return nil
	}
	hasCatalog := false
	for _, sp := range all {
		if specName(sp) == "list_tools" {
			hasCatalog = true
			break
		}
	}
	if !hasCatalog {
		return all
	}
	core := agent.CoreToolNames()
	out := make([]map[string]any, 0, len(core)+len(expanded))
	for _, sp := range all {
		name := specName(sp)
		if core[name] || expanded[name] {
			out = append(out, sp)
		}
	}
	return out
}

func (s *Service) expandableToolNames(all []map[string]any) []string {
	core := agent.CoreToolNames()
	out := []string{}
	for _, sp := range all {
		name := specName(sp)
		if name != "" && !core[name] && name != "list_tools" {
			out = append(out, name)
		}
	}
	return out
}

func toMapSlice(list []any) []map[string]any {
	out := make([]map[string]any, 0, len(list))
	for _, item := range list {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

func (s *Service) buildMinimalMessages(state *SessionState, question string, req AskRequest) []map[string]any {
	msgs := []map[string]any{}
	if state != nil && state.Summary != "" {
		msgs = append(msgs, SummaryMessage(state.Summary, state.Sid))
	}
	msgs = append(msgs, map[string]any{"role": "user", "content": UserText(question, req.AppID, req.ViewContext, req.ElementRefs, req.Quotes)})
	return msgs
}

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
