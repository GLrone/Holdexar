package pilot

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/httpx"
)

func Register(mux *http.ServeMux, service *Service) {
	writeJSON := httpx.WriteJSON
	writeErr := func(w http.ResponseWriter, status int, detail string) {
		writeJSON(w, status, map[string]string{"detail": detail})
	}
	decode := func(w http.ResponseWriter, r *http.Request, target any) bool {
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 4<<20)).Decode(target); err != nil {
			writeErr(w, http.StatusBadRequest, "请求体解析失败")
			return false
		}
		return true
	}

	mux.HandleFunc("GET /api/v1/pilot/providers", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"items": service.LoadProviders()})
	})
	mux.HandleFunc("POST /api/v1/pilot/providers", func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		if !decode(w, r, &payload) {
			return
		}
		created, err := service.CreateProvider(r.Context(), payload)
		if err != nil {
			writeErr(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, created)
	})
	mux.HandleFunc("PUT /api/v1/pilot/providers/{pid}", func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		if !decode(w, r, &payload) {
			return
		}
		updated, err := service.UpdateProvider(r.Context(), r.PathValue("pid"), payload)
		if err != nil {
			writeErr(w, http.StatusNotFound, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, updated)
	})
	mux.HandleFunc("DELETE /api/v1/pilot/providers/{pid}", func(w http.ResponseWriter, r *http.Request) {
		if err := service.DeleteProvider(r.Context(), r.PathValue("pid")); err != nil {
			writeErr(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	})
	mux.HandleFunc("GET /api/v1/pilot/config", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, service.LoadConfig())
	})
	mux.HandleFunc("PUT /api/v1/pilot/config", func(w http.ResponseWriter, r *http.Request) {
		var payload map[string]any
		if !decode(w, r, &payload) {
			return
		}
		if err := service.SaveConfig(r.Context(), payload); err != nil {
			writeErr(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, service.LoadConfig())
	})
	mux.HandleFunc("POST /api/v1/pilot/detect", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			BaseURL    string `json:"base_url"`
			APIKey     string `json:"api_key"`
			Protocol   string `json:"protocol"`
			ProviderID string `json:"provider_id"`
		}
		if !decode(w, r, &req) {
			return
		}
		apiKey := req.APIKey
		if apiKey == "" {
			ownerBase := ""
			storedKey := ""
			if req.ProviderID != "" {
				for _, p := range service.LoadProviders() {
					if p.ID == req.ProviderID {
						ownerBase = p.BaseURL
						break
					}
				}
				storedKey = service.ProviderKey(req.ProviderID)
			} else {
				cfg := service.LoadConfig()
				ownerBase = cfg.BaseURL
				storedKey = service.ProviderKey(cfg.Active)
			}
			// 已存密钥同源闸：实际检测地址与配置地址同源才携带（防跨源泄露 API Key）
			if storedKey != "" {
				if strings.TrimSpace(req.BaseURL) == "" || !sameOrigin(req.BaseURL, ownerBase) {
					writeErr(w, http.StatusUnprocessableEntity, keyScopeHint)
					return
				}
				apiKey = storedKey
			}
		}
		writeJSON(w, http.StatusOK, DetectProvider(req.BaseURL, apiKey, req.Protocol))
	})
	mux.HandleFunc("POST /api/v1/pilot/test", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Protocol *string `json:"protocol"`
			BaseURL  *string `json:"base_url"`
			APIKey   *string `json:"api_key"`
			Model    *string `json:"model"`
		}
		if !decode(w, r, &req) {
			return
		}
		cfg := service.LoadConfig()
		protocol := cfg.Protocol
		baseURL := cfg.BaseURL
		model := cfg.Model
		apiKey := ""
		if req.Protocol != nil {
			protocol = *req.Protocol
		}
		if req.BaseURL != nil {
			baseURL = *req.BaseURL
		}
		if req.Model != nil {
			model = *req.Model
		}
		if req.APIKey != nil {
			apiKey = *req.APIKey
		} else {
			storedKey := service.ProviderKey(cfg.Active)
			// 已存密钥同源闸：实际请求地址与配置地址同源才携带（密钥只去配置过的源）
			if storedKey != "" {
				if !sameOrigin(EffectiveBaseURL(protocol, baseURL), EffectiveBaseURL(protocol, cfg.BaseURL)) {
					writeErr(w, http.StatusUnprocessableEntity, keyScopeHint)
					return
				}
				apiKey = storedKey
			}
		}
		if baseURL == "" || model == "" {
			writeJSON(w, http.StatusOK, map[string]any{"ok": false, "latency_ms": 0,
				"model": model, "reason": "missing_fields"})
			return
		}
		messages := []map[string]any{
			{"role": "system", "content": "连通性测试：收到任何内容都只回复 pong。"},
			{"role": "user", "content": "ping"},
		}
		started := time.Now()
		events, err := service.ChatStream(r.Context(), protocol, EffectiveBaseURL(protocol, baseURL),
			apiKey, model, messages, nil, 16, nil)
		if err != nil {
			writeJSON(w, http.StatusOK, map[string]any{"ok": false, "latency_ms": 0,
				"model": model, "reason": "unreachable", "detail": err.Error()})
			return
		}
		var reply strings.Builder
		var usageIn, usageOut int
		for event := range events {
			switch event.Kind {
			case "answer":
				reply.WriteString(event.Text)
			case "usage":
				usageIn += event.UsageIn
				usageOut += event.UsageOut
			case "error":
				detail := event.Text
				lowered := strings.ToLower(detail)
				reason := "server_error"
				switch {
				case strings.Contains(detail, "401") || strings.Contains(detail, "403") || strings.Contains(lowered, "unauthorized"):
					reason = "key_invalid"
				case strings.Contains(lowered, "connect") || strings.Contains(lowered, "timeout") || strings.Contains(lowered, "unreachable"):
					reason = "unreachable"
				}
				writeJSON(w, http.StatusOK, map[string]any{"ok": false,
					"latency_ms": int(time.Since(started).Milliseconds()),
					"model":      model, "reason": reason, "detail": clip(detail, 200)})
				return
			}
		}
		if usageIn != 0 || usageOut != 0 {
			service.AddUsage(r.Context(), usageIn, usageOut)
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true,
			"latency_ms": int(time.Since(started).Milliseconds()),
			"model":      model, "reply": reply.String()})
	})
	mux.HandleFunc("GET /api/v1/pilot/sessions", func(w http.ResponseWriter, r *http.Request) {
		limit := 50
		if v, err := strconv.Atoi(r.URL.Query().Get("limit")); err == nil && v > 0 && v <= 200 {
			limit = v
		}
		items := service.GetStore().ListRecent(limit, r.URL.Query().Get("q"))
		for _, item := range items {
			sid, _ := item["sid"].(string)
			item["running"] = service.SessionRunning(sid)
		}
		writeJSON(w, http.StatusOK, map[string]any{"items": items})
	})
	mux.HandleFunc("GET /api/v1/pilot/sessions/{session_id}", func(w http.ResponseWriter, r *http.Request) {
		sid := r.PathValue("session_id")
		state := service.GetStore().Load(sid)
		if state == nil {
			writeErr(w, http.StatusNotFound, "session not found")
			return
		}
		feedbackByTid := map[string]string{}
		for _, record := range service.GetStore().RawRecords(sid) {
			if t, _ := record["t"].(string); t == "feedback" {
				tid, _ := record["tid"].(string)
				label, _ := record["label"].(string)
				if label == "accepted" || label == "rejected" {
					feedbackByTid[tid] = label
				}
			}
		}
		turns := []map[string]any{}
		for _, turn := range state.Turns {
			tid, _ := turn["tid"].(string)
			resp, _ := turn["resp"].(map[string]any)
			ts, _ := turn["ts"].(string)
			turns = append(turns, map[string]any{
				"q": stringOr(turn["q"], ""), "resp": resp, "ts": ts, "tid": tid,
				"feedback": feedbackByTid[tid],
			})
		}
		proposals := []map[string]any{}
		for pid, info := range state.Proposals {
			proposals = append(proposals, map[string]any{
				"pid": pid, "state": state.ProposalStateOf(pid),
				"done": info["done"], "failedCount": info["failedCount"],
			})
		}
		stats := sessionStats(state)
		inflight := service.InflightQuestion(sid)
		writeJSON(w, http.StatusOK, map[string]any{
			"session_id": sid, "turns": turns, "updated_at": state.UpdatedAt,
			"turn_total": len(state.Turns), "summary_through": state.SummaryThrough,
			"markers": state.Markers, "title": service.GetStore().TitleOf(state),
			"proposals": proposals, "stats": stats,
			"pinned": state.Pinned, "archived": state.Archived,
			"forked_from": forkedFromOrEmpty(state), "inflight_q": inflight,
		})
	})
	mux.HandleFunc("PUT /api/v1/pilot/sessions/{session_id}/title", func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			Text string `json:"text"`
		}
		if !decode(w, r, &payload) {
			return
		}
		sid := r.PathValue("session_id")
		if service.GetStore().Load(sid) == nil {
			writeErr(w, http.StatusNotFound, "session not found")
			return
		}
		if !service.GetStore().SetTitle(sid, payload.Text) {
			writeErr(w, http.StatusBadGateway, "rename_failed")
			return
		}
		writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
	})
	flagHandler := func(field string) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			var payload struct {
				On bool `json:"on"`
			}
			if !decode(w, r, &payload) {
				return
			}
			sid := r.PathValue("session_id")
			if service.GetStore().Load(sid) == nil {
				writeJSON(w, http.StatusOK, map[string]any{"ok": false, "reason": "no_session"})
				return
			}
			ok := false
			if field == "pin" {
				ok = service.GetStore().SetPinned(sid, payload.On)
			} else {
				ok = service.GetStore().SetArchived(sid, payload.On)
			}
			if !ok {
				writeJSON(w, http.StatusOK, map[string]any{"ok": false, "reason": "write_failed"})
				return
			}
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "reason": nil})
		}
	}
	mux.HandleFunc("PUT /api/v1/pilot/sessions/{session_id}/pin", flagHandler("pin"))
	mux.HandleFunc("PUT /api/v1/pilot/sessions/{session_id}/archive", flagHandler("archive"))
	mux.HandleFunc("POST /api/v1/pilot/sessions/{session_id}/fork", func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			KeepTurns int `json:"keep_turns"`
		}
		if !decode(w, r, &payload) {
			return
		}
		sid := r.PathValue("session_id")
		if service.SessionBusy(sid) {
			writeJSON(w, http.StatusOK, map[string]any{"ok": false, "reason": "busy",
				"sid": "", "turn_total": 0})
			return
		}
		newSid := newProviderID()
		turnTotal := 0
		state := service.GetStore().Load(sid)
		if state != nil {
			turnTotal = len(state.Turns)
		}
		if !service.GetStore().Fork(sid, newSid, payload.KeepTurns) {
			writeJSON(w, http.StatusOK, map[string]any{"ok": false, "reason": "write_failed",
				"sid": "", "turn_total": turnTotal})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "reason": nil,
			"sid": newSid, "turn_total": minInt(payload.KeepTurns, turnTotal)})
	})
	mux.HandleFunc("DELETE /api/v1/pilot/sessions/{session_id}", func(w http.ResponseWriter, r *http.Request) {
		result := service.GetStore().DeleteWithReason(r.PathValue("session_id"))
		switch result {
		case "no_session":
			writeErr(w, http.StatusNotFound, "session not found")
		case "deleted":
			writeJSON(w, http.StatusOK, map[string]bool{"ok": true})
		default:
			writeErr(w, http.StatusBadGateway, result)
		}
	})
	mux.HandleFunc("POST /api/v1/pilot/attachments", func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			SessionID string `json:"session_id"`
			Text      string `json:"text"`
			Name      string `json:"name"`
		}
		if !decode(w, r, &payload) {
			return
		}
		ref, ok := SaveAttachment(service.deps.DataDir, payload.SessionID, payload.Text, payload.Name)
		if !ok {
			writeErr(w, http.StatusUnprocessableEntity, "attachment_rejected")
			return
		}
		writeJSON(w, http.StatusOK, ref)
	})
	mux.HandleFunc("GET /api/v1/pilot/attachments", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{
			"items": ListAttachments(service.deps.DataDir, r.URL.Query().Get("session_id"))})
	})
	mux.HandleFunc("GET /api/v1/pilot/attachments/{aid}", func(w http.ResponseWriter, r *http.Request) {
		sid := r.URL.Query().Get("session_id")
		startLine, _ := strconv.Atoi(r.URL.Query().Get("start_line"))
		if startLine < 1 {
			startLine = 1
		}
		endLine, _ := strconv.Atoi(r.URL.Query().Get("end_line"))
		got := ReadRange(service.deps.DataDir, sid, r.PathValue("aid"), startLine, endLine)
		if got == nil {
			writeErr(w, http.StatusNotFound, "attachment not found")
			return
		}
		writeJSON(w, http.StatusOK, got)
	})
	mux.HandleFunc("PUT /api/v1/pilot/sessions/{session_id}/turns/{turn_id}/feedback", func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			Feedback string `json:"feedback"`
		}
		if !decode(w, r, &payload) {
			return
		}
		reason := service.GetStore().SetTurnFeedback(r.PathValue("session_id"), r.PathValue("turn_id"), payload.Feedback)
		switch reason {
		case "":
			writeJSON(w, http.StatusOK, map[string]any{"ok": true, "feedback": payload.Feedback})
		case "no_session", "turn_gone":
			writeErr(w, http.StatusNotFound, reason)
		case "bad_request":
			writeErr(w, http.StatusUnprocessableEntity, reason)
		default:
			writeErr(w, http.StatusBadGateway, reason)
		}
	})
	mux.HandleFunc("GET /api/v1/pilot/flywheel", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, FlywheelStats(service.GetStore().Base(), service.GetStore().RawRecords))
	})
	mux.HandleFunc("POST /api/v1/pilot/flywheel/export", func(w http.ResponseWriter, r *http.Request) {
		base := service.GetStore().Base()
		stats := FlywheelStats(base, service.GetStore().RawRecords)
		count, prefPath, ok := ExportPreferencePairs(base, service.GetStore().RawRecords)
		writeJSON(w, http.StatusOK, map[string]any{
			"ok": ok, "count": stats["total"],
			"preference_count": count,
			"path":             datasetPath(base),
			"preference_path":  prefPath,
			"stats":            stats,
		})
	})
	mux.HandleFunc("POST /api/v1/pilot/proposal/confirm", func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			SessionID string `json:"session_id"`
			PID       string `json:"pid"`
			Approve   bool   `json:"approve"`
		}
		if !decode(w, r, &payload) {
			return
		}
		writeJSON(w, http.StatusOK, service.ConfirmProposal(r.Context(), payload.SessionID, payload.PID, payload.Approve))
	})
	mux.HandleFunc("POST /api/v1/pilot/tool", func(w http.ResponseWriter, r *http.Request) {
		var payload struct {
			Name      string         `json:"name"`
			Label     string         `json:"label"`
			SessionID string         `json:"session_id"`
			Args      map[string]any `json:"args"`
		}
		if !decode(w, r, &payload) {
			return
		}
		result := service.ToolExecute(r.Context(), payload.Name, payload.Args, false, payload.SessionID)
		step := toolStep(payload.Name, result)
		var cards []any
		if card := cardOf(payload.Name, result); card != nil {
			cards = []any{card}
		} else {
			cards = []any{}
		}
		writeJSON(w, http.StatusOK, map[string]any{"step": step, "cards": cards})
	})
	mux.HandleFunc("POST /api/v1/pilot/ask", func(w http.ResponseWriter, r *http.Request) {
		var req AskRequest
		if !decode(w, r, &req) {
			return
		}
		events, err := service.AskStream(r.Context(), req)
		if err != nil {
			if errors.Is(err, ErrModuleDisabled) {
				writeJSON(w, http.StatusConflict, map[string]any{"detail": err.Error()})
				return
			}
			writeErr(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
		var result map[string]any
		for event := range events {
			if event.Type == "done" {
				result = event.Payload
			} else if event.Type == "busy" {
				writeJSON(w, http.StatusOK, map[string]any{"answer": "", "source": "none",
					"reason": "session_busy"})
				return
			} else if event.Type == "error" {
				writeJSON(w, http.StatusOK, map[string]any{"answer": "", "source": "none",
					"reason": "llm_failed"})
				return
			}
		}
		if result == nil {
			writeJSON(w, http.StatusOK, map[string]any{"answer": "", "source": "none",
				"reason": "llm_failed"})
			return
		}
		result["cached"] = false
		writeJSON(w, http.StatusOK, result)
	})
	mux.HandleFunc("POST /api/v1/pilot/ask/stream", func(w http.ResponseWriter, r *http.Request) {
		var req AskRequest
		if !decode(w, r, &req) {
			return
		}
		events, err := service.AskStream(r.Context(), req)
		if err != nil {
			if errors.Is(err, ErrModuleDisabled) {
				writeJSON(w, http.StatusConflict, map[string]any{"detail": err.Error()})
				return
			}
			writeErr(w, http.StatusUnprocessableEntity, err.Error())
			return
		}
		w.Header().Set("Content-Type", "text/event-stream")
		w.Header().Set("Cache-Control", "no-cache")
		w.Header().Set("X-Accel-Buffering", "no")
		w.WriteHeader(http.StatusOK)
		flusher, hasFlusher := w.(http.Flusher)
		for event := range events {
			payload := map[string]any{"type": event.Type}
			for key, value := range eventMap(event) {
				payload[key] = value
			}
			data, _ := json.Marshal(payload)
			fmt.Fprintf(w, "data: %s\n\n", data)
			if hasFlusher {
				flusher.Flush()
			}
		}
		fmt.Fprint(w, "data: [DONE]\n\n")
		if hasFlusher {
			flusher.Flush()
		}
	})
	mux.HandleFunc("POST /api/v1/pilot/sessions/{session_id}/compact", func(w http.ResponseWriter, r *http.Request) {
		sid := r.PathValue("session_id")
		if service.SessionBusy(sid) {
			writeJSON(w, http.StatusConflict, map[string]any{"ok": false, "reason": "busy", "detail": "session is busy"})
			return
		}
		state := service.GetStore().Load(sid)
		if state == nil {
			writeErr(w, http.StatusNotFound, "session not found")
			return
		}
		cfg := service.LoadConfig()
		if !cfg.LlmReady() {
			writeJSON(w, http.StatusOK, map[string]any{"ok": false, "reason": "llm_off"})
			return
		}
		if service.OverCap() {
			writeJSON(w, http.StatusOK, map[string]any{"ok": false, "reason": "cap_reached"})
			return
		}
		writeJSON(w, http.StatusOK, service.CompactNow(r.Context(), state, cfg))
	})
	mux.HandleFunc("POST /api/v1/pilot/sessions/{session_id}/rewind", func(w http.ResponseWriter, r *http.Request) {
		keepTurns, verr := parseKeepTurns(w, r)
		if verr != nil {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []valErr{*verr}})
			return
		}
		sid := r.PathValue("session_id")
		type rewindOut struct {
			OK        bool    `json:"ok"`
			TurnTotal int     `json:"turn_total"`
			Reason    *string `json:"reason"`
		}
		busyReason := "busy"
		noSessionReason := "no_session"
		writeFailedReason := "write_failed"
		if service.SessionBusy(sid) {
			writeJSON(w, http.StatusOK, rewindOut{OK: false, Reason: &busyReason})
			return
		}
		state := service.GetStore().Load(sid)
		if state == nil {
			writeJSON(w, http.StatusOK, rewindOut{OK: false, Reason: &noSessionReason})
			return
		}
		keep := minInt(keepTurns, len(state.Turns))
		if !service.GetStore().Truncate(sid, keep) {
			writeJSON(w, http.StatusOK, rewindOut{OK: false, TurnTotal: len(state.Turns), Reason: &writeFailedReason})
			return
		}
		writeJSON(w, http.StatusOK, rewindOut{OK: true, TurnTotal: keep})
	})
}

type valErr = httpx.ValErr

func parseKeepTurns(w http.ResponseWriter, r *http.Request) (int, *valErr) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		return 0, &valErr{Type: "missing", Loc: []any{"body"}, Msg: "Field required"}
	}
	trimmed := strings.TrimSpace(string(body))
	if trimmed == "" {
		return 0, &valErr{Type: "missing", Loc: []any{"body"}, Msg: "Field required"}
	}
	dec := json.NewDecoder(strings.NewReader(trimmed))
	dec.UseNumber()
	var top any
	if err := dec.Decode(&top); err != nil {
		return 0, &valErr{Type: "json_invalid", Loc: []any{"body"}, Msg: "JSON decode error", Input: map[string]any{}}
	}
	fields, ok := top.(map[string]any)
	if !ok {
		return 0, &valErr{Type: "model_attributes_type", Loc: []any{"body"},
			Msg: "Input should be a valid dictionary or object to extract fields from", Input: top}
	}
	raw, present := fields["keep_turns"]
	if !present || raw == nil {
		return 0, &valErr{Type: "missing", Loc: []any{"body", "keep_turns"}, Msg: "Field required", Input: fields}
	}
	var n int64
	switch v := raw.(type) {
	case json.Number:
		if i, e := strconv.ParseInt(v.String(), 10, 64); e == nil {
			n = i
			break
		}
		if f, e := strconv.ParseFloat(v.String(), 64); e == nil && f == float64(int64(f)) {
			n = int64(f)
			break
		}
		return 0, &valErr{Type: "int_from_float", Loc: []any{"body", "keep_turns"},
			Msg: "Input should be a valid integer, got a number with a fractional part", Input: raw}
	case string:
		i, e := strconv.ParseInt(strings.TrimSpace(v), 10, 64)
		if e != nil {
			return 0, &valErr{Type: "int_parsing", Loc: []any{"body", "keep_turns"},
				Msg: "Input should be a valid integer, unable to parse string as an integer", Input: raw}
		}
		n = i
	case float64:
		if v != float64(int64(v)) {
			return 0, &valErr{Type: "int_from_float", Loc: []any{"body", "keep_turns"},
				Msg: "Input should be a valid integer, got a number with a fractional part", Input: raw}
		}
		n = int64(v)
	case bool:
		if v {
			n = 1
		}
	default:
		return 0, &valErr{Type: "int_parsing", Loc: []any{"body", "keep_turns"},
			Msg: "Input should be a valid integer, unable to parse string as an integer", Input: raw}
	}
	if n < 0 {
		return 0, &valErr{Type: "greater_than_equal", Loc: []any{"body", "keep_turns"},
			Msg: "Input should be greater than or equal to 0", Input: n, Ctx: map[string]any{"ge": 0}}
	}
	if n > 999 {
		return 0, &valErr{Type: "less_than_equal", Loc: []any{"body", "keep_turns"},
			Msg: "Input should be less than or equal to 999", Input: n, Ctx: map[string]any{"le": 999}}
	}
	return int(n), nil
}

func eventMap(event StreamEvent) map[string]any {
	out := map[string]any{}
	if event.Step > 0 {
		out["step"] = event.Step
	}
	if event.Active > 0 {
		out["active"] = event.Active
	}
	if event.Delta != "" {
		out["delta"] = event.Delta
	}
	if event.Name != "" {
		out["name"] = event.Name
	}
	if event.Label != "" {
		out["label"] = event.Label
	}
	if event.CardKind != "" {
		out["card_kind"] = event.CardKind
	}
	if event.ElapsedMS > 0 {
		out["elapsed_ms"] = event.ElapsedMS
	}
	if event.DurationMS > 0 {
		out["duration_ms"] = event.DurationMS
	}
	if event.Data != nil {
		for key, value := range event.Data {
			out[key] = value
		}
	}
	if event.Type == "card" || event.Card != nil {
		out["card"] = event.Card
	}
	if event.Payload != nil {
		for key, value := range event.Payload {
			out[key] = value
		}
	}
	if event.Reason != "" {
		out["reason"] = event.Reason
	}
	return out
}

func sessionStats(state *SessionState) map[string]any {
	steps, elapsedMS, ttftMS, ttftN, decodeMS, decodeOut, usageIn, usageOut := 0, 0, 0, 0, 0, 0, 0, 0
	var cacheRead, cacheWrite, cacheBase *int
	for _, turn := range state.Turns {
		resp, _ := turn["resp"].(map[string]any)
		steps += len(anySlice(resp["steps"]))
		elapsedMS += jsonInt(resp["elapsed_ms"])
		ttftMS += jsonInt(resp["ttft_ms"])
		ttftN += jsonInt(resp["ttft_n"])
		decodeMS += jsonInt(resp["decode_ms"])
		decodeOut += jsonInt(resp["decode_out"])
		usageIn += jsonInt(resp["usage_in"])
		usageOut += jsonInt(resp["usage_out"])
		accumulateCache(&cacheRead, resp["cache_read_tokens"])
		accumulateCache(&cacheWrite, resp["cache_write_tokens"])
		accumulateCache(&cacheBase, resp["cache_base_tokens"])
	}
	return map[string]any{
		"turns": len(state.Turns), "steps": steps, "elapsed_ms": elapsedMS,
		"ttft_ms": ttftMS, "ttft_n": ttftN, "decode_ms": decodeMS, "decode_out": decodeOut,
		"usage_in": usageIn, "usage_out": usageOut,
		"cache_read_tokens": cacheRead, "cache_write_tokens": cacheWrite,
		"cache_base_tokens": cacheBase,
	}
}

func anySlice(value any) []any {
	switch list := value.(type) {
	case []any:
		return list
	case []map[string]any:
		out := make([]any, len(list))
		for i, item := range list {
			out[i] = item
		}
		return out
	}
	return nil
}

func accumulateCache(target **int, value any) {
	if value == nil {
		return
	}
	n := jsonInt(value)
	if *target == nil {
		*target = intPtr(n)
		return
	}
	**target += n
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func DetectProvider(baseURL, apiKey, protocolHint string) map[string]any {
	knownHosts := []struct {
		hostKey  string
		protocol string
		vendor   string
		models   []string
	}{
		{"api.deepseek.com", "openai", "DeepSeek", []string{"deepseek-chat", "deepseek-reasoner"}},
		{"api.moonshot.cn", "openai", "Kimi", nil},
		{"api.moonshot.ai", "openai", "Kimi", nil},
		{"dashscope.aliyuncs.com", "openai", "通义千问", nil},
		{"open.bigmodel.cn", "openai", "智谱 GLM", nil},
		{"api.siliconflow.cn", "openai", "硅基流动", nil},
		{"openrouter.ai", "openai", "OpenRouter", nil},
		{"api.groq.com", "openai", "Groq", nil},
		{"api.x.ai", "openai", "Grok", nil},
		{"api.openai.com", "openai", "OpenAI", nil},
		{"api.anthropic.com", "anthropic", "Anthropic", nil},
		{":11434", "ollama", "Ollama 本地", nil},
	}
	protocol, vendor := "openai", ""
	suggested := []string{}
	lower := strings.ToLower(baseURL)
	if lower != "" {
		for _, host := range knownHosts {
			if strings.Contains(lower, host.hostKey) {
				protocol, vendor, suggested = host.protocol, host.vendor, host.models
				break
			}
		}
	}
	if protocolHint != "" {
		protocol = protocolHint
	}
	base := EffectiveBaseURL(protocol, baseURL)
	models, reason, keyValid := suggested, "", any(nil)
	listURL := strings.TrimRight(base, "/") + "/models"
	switch protocol {
	case "anthropic":
		listURL = strings.TrimRight(base, "/") + "/v1/models"
	case "ollama":
		listURL = strings.TrimRight(base, "/") + "/api/tags"
	}
	client := HttpClient(base)
	defer client.CloseIdleConnections()
	request, err := http.NewRequest(http.MethodGet, listURL, nil)
	if err != nil {
		return map[string]any{"protocol": protocol, "vendor": vendor, "models": suggested,
			"suggested": suggested, "key_valid": nil, "reason": "unreachable"}
	}
	for key, value := range authHeaders(protocol, apiKey) {
		request.Header.Set(key, value)
	}
	resp, err := client.Do(request)
	if err != nil {
		return map[string]any{"protocol": protocol, "vendor": vendor, "models": models,
			"suggested": suggested, "key_valid": nil, "reason": "unreachable"}
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == 401 || resp.StatusCode == 403:
		return map[string]any{"protocol": protocol, "vendor": vendor, "models": models,
			"suggested": suggested, "key_valid": false, "reason": "key_invalid"}
	case resp.StatusCode == 404:
		return map[string]any{"protocol": protocol, "vendor": vendor, "models": models,
			"suggested": suggested, "key_valid": nil, "reason": "not_found"}
	case resp.StatusCode >= 400:
		return map[string]any{"protocol": protocol, "vendor": vendor, "models": models,
			"suggested": suggested, "key_valid": nil, "reason": "upstream"}
	}
	var payload map[string]any
	if json.NewDecoder(resp.Body).Decode(&payload) == nil {
		models = []string{}
		source := anySlice(payload["data"])
		nameKey := "id"
		if protocol == "ollama" {
			source = anySlice(payload["models"])
			nameKey = "name"
		}
		for _, item := range source {
			entry, _ := item.(map[string]any)
			if name := stringOf(entry[nameKey]); name != "" {
				models = append(models, name)
			}
		}
		sortStrings(models)
		keyValid = true
		reason = ""
	}
	return map[string]any{"protocol": protocol, "vendor": vendor, "models": models,
		"suggested": suggested, "key_valid": keyValid, "reason": reason}
}

func sortStrings(list []string) {
	for i := 1; i < len(list); i++ {
		for j := i; j > 0 && list[j] < list[j-1]; j-- {
			list[j], list[j-1] = list[j-1], list[j]
		}
	}
}

const keyScopeHint = "该地址与已配置服务不一致，请填写该服务的 API 密钥后再试"
