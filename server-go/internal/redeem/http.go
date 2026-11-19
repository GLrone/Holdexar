package redeem

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"

	"holdexar/server-go/internal/httpx"
)

func reply(w http.ResponseWriter, status int, value any) {
	httpx.WriteJSON(w, status, value)
}

func replyValidation(w http.ResponseWriter, issues ...httpx.ValErr) {
	httpx.WriteValidationError(w, issues...)
}

func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/redeem/keys", s.handleActivateKeys)
	mux.HandleFunc("GET /api/v1/redeem/quota", s.handleQuota)
	mux.HandleFunc("GET /api/v1/redeem/activations", s.handleActivations)
}

func (s *Service) handleActivateKeys(w http.ResponseWriter, r *http.Request) {
	keys, ok := parseActivateRequest(w, r)
	if !ok {
		return
	}
	cleaned := make([]string, 0, len(keys))
	for _, key := range keys {
		if trimmed := strings.TrimSpace(key); trimmed != "" {
			cleaned = append(cleaned, trimmed)
		}
	}
	outcome, err := s.ActivateBatch(r.Context(), cleaned)
	if err != nil {
		var input *InputError
		if errors.As(err, &input) {
			reply(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		reply(w, 502, map[string]any{"detail": fmt.Sprintf("激活请求失败: %s", err)})
		return
	}
	reply(w, 200, outcome)
}

func parseActivateRequest(w http.ResponseWriter, r *http.Request) ([]string, bool) {
	var payload map[string]json.RawMessage
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 256<<10))
	if err := decoder.Decode(&payload); err != nil {
		replyValidation(w, httpx.ValErr{Loc: []any{"body"}, Type: "json_invalid",
			Msg: "JSON decode error: " + err.Error(), Input: nil})
		return nil, false
	}
	raw, present := payload["keys"]
	if !present {
		replyValidation(w, httpx.ValErr{Loc: []any{"body", "keys"}, Type: "missing",
			Msg: "Field required", Input: payload})
		return nil, false
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		replyValidation(w, httpx.ValErr{Loc: []any{"body", "keys"}, Type: "json_invalid",
			Msg: "JSON decode error: " + err.Error(), Input: nil})
		return nil, false
	}
	items, ok := value.([]any)
	if !ok {
		replyValidation(w, httpx.ValErr{Loc: []any{"body", "keys"}, Type: "list_type",
			Msg: "Input should be a valid list", Input: value})
		return nil, false
	}
	if len(items) < 1 {
		replyValidation(w, httpx.ValErr{Loc: []any{"body", "keys"}, Type: "too_short",
			Msg: "List should have at least 1 item after validation, not 0", Input: items})
		return nil, false
	}
	if len(items) > 100 {
		replyValidation(w, httpx.ValErr{Loc: []any{"body", "keys"}, Type: "too_long",
			Msg: fmt.Sprintf("List should have at most 100 items after validation, not %d", len(items)), Input: items})
		return nil, false
	}
	keys := make([]string, 0, len(items))
	var issues []httpx.ValErr
	for i, item := range items {
		text, ok := item.(string)
		if !ok {
			issues = append(issues, httpx.ValErr{Loc: []any{"body", "keys", i}, Type: "string_type",
				Msg: "Input should be a valid string", Input: item})
			continue
		}
		keys = append(keys, text)
	}
	if len(issues) > 0 {
		replyValidation(w, issues...)
		return nil, false
	}
	return keys, true
}

func (s *Service) handleQuota(w http.ResponseWriter, r *http.Request) {
	quota, err := s.QuotaStatus(r.Context())
	if err != nil {
		reply(w, 500, map[string]any{"detail": err.Error()})
		return
	}
	reply(w, 200, quota)
}

func (s *Service) handleActivations(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	limit, offset := 50, 0
	var issues []httpx.ValErr
	for _, item := range []struct {
		name   string
		target *int
	}{{"limit", &limit}, {"offset", &offset}} {
		values, present := q[item.name]
		if !present || len(values) == 0 {
			continue
		}
		value, err := strconv.Atoi(values[0])
		if err != nil {
			issues = append(issues, httpx.ValErr{Loc: []any{"query", item.name}, Type: "int_parsing",
				Msg: httpx.IntParseMsg, Input: values[0]})
			continue
		}
		*item.target = value
	}
	if len(issues) > 0 {
		replyValidation(w, issues...)
		return
	}
	page, err := s.SearchActivations(r.Context(), q.Get("status"), q.Get("q"), q.Get("key"), limit, offset)
	if err != nil {
		var input *InputError
		if errors.As(err, &input) {
			reply(w, 400, map[string]any{"detail": err.Error()})
			return
		}
		reply(w, 500, map[string]any{"detail": err.Error()})
		return
	}
	reply(w, 200, page)
}
