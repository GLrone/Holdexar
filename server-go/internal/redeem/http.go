// http.go redeem 域三条端点（对齐 Python domains/redeem/router.py）：
// POST /api/v1/redeem/keys · GET /api/v1/redeem/quota · GET /api/v1/redeem/activations。
// POST 校验 422 形状逐字段复刻 FastAPI（loc/type/msg/input），错误信封统一
// {"detail": ...}；ValueError 类输入 → 400，其余异常 → 502 兜底。
package redeem

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
)

func reply(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

type validationIssue struct {
	Loc   []any  `json:"loc"`
	Msg   string `json:"msg"`
	Type  string `json:"type"`
	Input any    `json:"input"`
}

func replyValidation(w http.ResponseWriter, issues []validationIssue) {
	reply(w, 422, map[string]any{"detail": issues})
}

func (s *Service) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/redeem/keys", s.handleActivateKeys)
	mux.HandleFunc("GET /api/v1/redeem/quota", s.handleQuota)
	mux.HandleFunc("GET /api/v1/redeem/activations", s.handleActivations)
}

// handleActivateKeys 批量激活：逐项 strip 并丢弃空串；过滤后为空 → 400
// 「激活码列表为空」；其余异常 → 502 兜底信封。
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

// parseActivateRequest 入参校验（FastAPI keys: list[str] min1/max100 复刻）：
// 非对象体/非法 JSON → json_invalid；缺 keys → missing；非列表 → list_type；
// 长度越界 → too_short/too_long；元素非字符串 → string_type（带下标 loc）。
func parseActivateRequest(w http.ResponseWriter, r *http.Request) ([]string, bool) {
	var payload map[string]json.RawMessage
	decoder := json.NewDecoder(http.MaxBytesReader(w, r.Body, 256<<10))
	if err := decoder.Decode(&payload); err != nil {
		replyValidation(w, []validationIssue{{Loc: []any{"body"}, Type: "json_invalid",
			Msg: "JSON decode error: " + err.Error(), Input: nil}})
		return nil, false
	}
	raw, present := payload["keys"]
	if !present {
		replyValidation(w, []validationIssue{{Loc: []any{"body", "keys"}, Type: "missing",
			Msg: "Field required", Input: payload}})
		return nil, false
	}
	var value any
	if err := json.Unmarshal(raw, &value); err != nil {
		replyValidation(w, []validationIssue{{Loc: []any{"body", "keys"}, Type: "json_invalid",
			Msg: "JSON decode error: " + err.Error(), Input: nil}})
		return nil, false
	}
	items, ok := value.([]any)
	if !ok {
		replyValidation(w, []validationIssue{{Loc: []any{"body", "keys"}, Type: "list_type",
			Msg: "Input should be a valid list", Input: value}})
		return nil, false
	}
	if len(items) < 1 {
		replyValidation(w, []validationIssue{{Loc: []any{"body", "keys"}, Type: "too_short",
			Msg: "List should have at least 1 item after validation, not 0", Input: items}})
		return nil, false
	}
	if len(items) > 100 {
		replyValidation(w, []validationIssue{{Loc: []any{"body", "keys"}, Type: "too_long",
			Msg: fmt.Sprintf("List should have at most 100 items after validation, not %d", len(items)), Input: items}})
		return nil, false
	}
	keys := make([]string, 0, len(items))
	var issues []validationIssue
	for i, item := range items {
		text, ok := item.(string)
		if !ok {
			issues = append(issues, validationIssue{Loc: []any{"body", "keys", i}, Type: "string_type",
				Msg: "Input should be a valid string", Input: item})
			continue
		}
		keys = append(keys, text)
	}
	if len(issues) > 0 {
		replyValidation(w, issues)
		return nil, false
	}
	return keys, true
}

// handleQuota 激活前提状态（Cookie 可用性）——前端激活前的引导提示；无 4xx 分支。
func (s *Service) handleQuota(w http.ResponseWriter, r *http.Request) {
	quota, err := s.QuotaStatus(r.Context())
	if err != nil {
		reply(w, 500, map[string]any{"detail": err.Error()})
		return
	}
	reply(w, 200, quota)
}

// handleActivations 激活历史：limit/offset 仅 int 校验（422 int_parsing 形状），
// 夹取与兜底在服务层。
func (s *Service) handleActivations(w http.ResponseWriter, r *http.Request) {
	const msgIntParsing = "Input should be a valid integer, unable to parse string as an integer"
	q := r.URL.Query()
	limit, offset := 50, 0
	var issues []validationIssue
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
			issues = append(issues, validationIssue{Loc: []any{"query", item.name}, Type: "int_parsing",
				Msg: msgIntParsing, Input: values[0]})
			continue
		}
		*item.target = value
	}
	if len(issues) > 0 {
		replyValidation(w, issues)
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
