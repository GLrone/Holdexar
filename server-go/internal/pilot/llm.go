//go:build windows

// llm.go 多协议 LLM 客户端：openai / openai-responses / anthropic / ollama，
// 归一化为统一事件流（thinking/answer/tool_calls/usage/context/cache）。
// 语义对齐 pilot/llm.py。
package pilot

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"time"
)

// Protocols 支持的协议集。
var Protocols = []string{"openai", "openai-responses", "anthropic", "ollama"}

var defaultBaseURL = map[string]string{
	"openai":           "",
	"openai-responses": "",
	"anthropic":        "https://api.anthropic.com",
	"ollama":           "http://127.0.0.1:11434",
}

const (
	llmTimeout         = 180 * time.Second
	llmConnectTimeout  = 15 * time.Second
	maxOutputTokens    = 4096
	anthropicThinking  = 2048
	diagnosticIDLength = 6
)

// PilotLlmError LLM 调用失败（网络/非 2xx/响应形态不符）。
type PilotLlmError struct {
	Message      string
	Code         string
	StatusCode   int
	DiagnosticID string
}

func (e *PilotLlmError) Error() string { return e.Message }

// ToDiagnostic 诊断载荷（前端展示口径）。
func (e *PilotLlmError) ToDiagnostic() map[string]any {
	return map[string]any{
		"id": e.DiagnosticID, "code": e.Code,
		"message": clip(e.Message, 400), "status_code": statusCodeOrNil(e.StatusCode),
	}
}

func newLlmError(message, code string, statusCode int) *PilotLlmError {
	id := make([]byte, diagnosticIDLength)
	_, _ = rand.Read(id)
	return &PilotLlmError{Message: message, Code: code, StatusCode: statusCode,
		DiagnosticID: hex.EncodeToString(id)}
}

func statusCodeOrNil(code int) any {
	if code <= 0 {
		return nil
	}
	return code
}

func clip(text string, limit int) string {
	if len(text) > limit {
		return text[:limit]
	}
	return text
}

// EffectiveBaseURL 协议默认地址兜底。
func EffectiveBaseURL(protocol, baseURL string) string {
	base := trimSpace(baseURL)
	if base != "" {
		return base
	}
	return defaultBaseURL[protocol]
}

// LlmEvent 统一事件（kind ∈ thinking/answer/tool_calls/usage/context/cache）。
type LlmEvent struct {
	Kind     string
	Text     string
	Calls    []map[string]any // tool_calls
	UsageIn  int              // usage
	UsageOut int
	Cache    map[string]any // cache
	Err      *PilotLlmError // error
}

// authHeaders 匿名端点不落鉴权头。
func authHeaders(protocol, apiKey string) map[string]string {
	if protocol == "anthropic" {
		headers := map[string]string{"anthropic-version": "2023-06-01"}
		if apiKey != "" {
			headers["x-api-key"] = apiKey
		}
		return headers
	}
	if protocol == "ollama" || apiKey == "" {
		return map[string]string{}
	}
	return map[string]string{"Authorization": "Bearer " + apiKey}
}

func isLoopback(baseURL string) bool {
	parsed, err := url.Parse(baseURL)
	if err != nil {
		return false
	}
	host := parsed.Hostname()
	return host == "127.0.0.1" || host == "localhost" || host == "::1"
}

// HttpClient 回环地址绕过系统代理（本地网关经代理会 502）。
func HttpClient(baseURL string) *http.Client {
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if isLoopback(baseURL) {
		transport.Proxy = nil
	}
	return &http.Client{Timeout: llmTimeout, Transport: transport}
}

func assembleToolCalls(pending map[int]map[string]string) []map[string]any {
	keys := make([]int, 0, len(pending))
	for idx := range pending {
		keys = append(keys, idx)
	}
	sort.Ints(keys)
	out := make([]map[string]any, 0, len(pending))
	for _, idx := range keys {
		item := pending[idx]
		if item["name"] == "" {
			continue
		}
		args := map[string]any{}
		_ = json.Unmarshal([]byte(item["args"]), &args)
		out = append(out, map[string]any{"id": item["id"], "name": item["name"], "arguments": args})
	}
	return out
}

// ── openai 兼容 ──────────────────────────────────────────────────────

func openaiRequest(baseURL, apiKey, model string, messages []map[string]any, tools []map[string]any) (string, map[string]string, map[string]any) {
	endpoint := strings.TrimRight(baseURL, "/") + "/chat/completions"
	normalized := make([]map[string]any, 0, len(messages))
	for _, m := range messages {
		if role, _ := m["role"].(string); role == "assistant" && m["content"] == nil {
			copied := map[string]any{}
			for k, v := range m {
				copied[k] = v
			}
			copied["content"] = ""
			normalized = append(normalized, copied)
			continue
		}
		normalized = append(normalized, m)
	}
	body := map[string]any{
		"model": model, "messages": normalized, "temperature": 0.4,
		"max_tokens": maxOutputTokens, "stream": true,
		"stream_options": map[string]any{"include_usage": true},
	}
	if len(tools) > 0 {
		body["tools"] = tools
	}
	return endpoint, authHeaders("openai", apiKey), body
}

// openaiAcc openai 流解析的跨帧状态（tool_calls 分片累积）。
type openaiAcc struct {
	pending map[int]map[string]string
}

func newOpenaiAcc() *openaiAcc { return &openaiAcc{pending: map[int]map[string]string{}} }

func (a *openaiAcc) feed(data string, out func(LlmEvent)) {
	var chunk map[string]any
	if json.Unmarshal([]byte(data), &chunk) != nil {
		return
	}
	pending := a.pending
	{
		if usage, ok := chunk["usage"].(map[string]any); ok {
			prompt := jsonInt(usage["prompt_tokens"])
			out(LlmEvent{Kind: "usage", UsageIn: prompt, UsageOut: jsonInt(usage["completion_tokens"])})
			if prompt > 0 {
				out(LlmEvent{Kind: "context", UsageIn: prompt})
			}
			details, _ := usage["prompt_tokens_details"].(map[string]any)
			read := 0
			hasRead := false
			if details != nil {
				read = jsonInt(details["cached_tokens"])
				hasRead = details["cached_tokens"] != nil
			}
			rate := any(nil)
			if hasRead && prompt > 0 {
				v := float64(read) / float64(prompt)
				if v > 1 {
					v = 1
				}
				rate = v
			}
			base := any(nil)
			if prompt > 0 {
				base = prompt
			}
			readAny := any(nil)
			if hasRead {
				readAny = read
			}
			out(LlmEvent{Kind: "cache", Cache: map[string]any{"rate": rate, "read": readAny, "write": nil, "base": base}})
		}
		choices, _ := chunk["choices"].([]any)
		if len(choices) == 0 {
			return
		}
		choice, _ := choices[0].(map[string]any)
		delta, _ := choice["delta"].(map[string]any)
		if think, ok := delta["reasoning_content"].(string); ok && think != "" {
			out(LlmEvent{Kind: "thinking", Text: think})
		}
		if content, ok := delta["content"].(string); ok && content != "" {
			out(LlmEvent{Kind: "answer", Text: content})
		}
		if rawCalls, ok := delta["tool_calls"].([]any); ok {
			for _, raw := range rawCalls {
				tc, _ := raw.(map[string]any)
				idx := jsonInt(tc["index"])
				acc := pending[idx]
				if acc == nil {
					acc = map[string]string{}
					pending[idx] = acc
				}
				if v, ok := tc["id"].(string); ok && v != "" {
					acc["id"] = v
				}
				if fn, ok := tc["function"].(map[string]any); ok {
					if v, ok := fn["name"].(string); ok && v != "" {
						acc["name"] = v
					}
					if v, ok := fn["arguments"].(string); ok && v != "" {
						acc["args"] += v
					}
				}
			}
		}
		finish := choice["finish_reason"]
		if finish == nil {
			finish = chunk["finish_reason"]
		}
		if finish == "tool_calls" {
			if assembled := assembleToolCalls(a.pending); len(assembled) > 0 {
				out(LlmEvent{Kind: "tool_calls", Calls: assembled})
			}
			a.pending = map[int]map[string]string{}
		}
	}
	if len(a.pending) > 0 {
		// 流末兜底在 ChatStream 关流后由调用方 flush——这里不重复发
	}
}

// ── anthropic（Messages API）────────────────────────────────────────

func anthropicConvert(messages []map[string]any) (string, []map[string]any) {
	systems := []string{}
	for _, m := range messages {
		if role, _ := m["role"].(string); role == "system" {
			if content, ok := m["content"].(string); ok {
				systems = append(systems, content)
			}
		}
	}
	system := strings.Join(systems, "\n")
	out := make([]map[string]any, 0, len(messages))
	for _, m := range messages {
		role, _ := m["role"].(string)
		content, _ := m["content"].(string)
		switch role {
		case "system":
			continue
		case "user":
			blocks := []map[string]any{{"type": "text", "text": content}}
			out = appendBlocks(out, "user", blocks)
		case "assistant":
			blocks := []map[string]any{}
			if content != "" {
				blocks = append(blocks, map[string]any{"type": "text", "text": content})
			}
			if rawCalls, ok := m["tool_calls"].([]any); ok {
				for _, raw := range rawCalls {
					tc, _ := raw.(map[string]any)
					fn, _ := tc["function"].(map[string]any)
					args := map[string]any{}
					_ = json.Unmarshal([]byte(stringOf(fn["arguments"])), &args)
					blocks = append(blocks, map[string]any{
						"type": "tool_use", "id": tc["id"],
						"name": fn["name"], "input": args,
					})
				}
			}
			out = appendBlocks(out, "assistant", blocks)
		case "tool":
			blocks := []map[string]any{{
				"type": "tool_result", "tool_use_id": stringOf(m["tool_call_id"]),
				"content": content,
			}}
			out = appendBlocks(out, "user", blocks)
		}
	}
	return system, out
}

// appendBlocks 追加同角色连续合并块；返回新切片（append 可能重定位底层数组）。
func appendBlocks(out []map[string]any, role string, blocks []map[string]any) []map[string]any {
	if len(out) > 0 {
		if last, ok := out[len(out)-1]["role"].(string); ok && last == role {
			existing, _ := out[len(out)-1]["content"].([]map[string]any)
			out[len(out)-1]["content"] = append(existing, blocks...)
			return out
		}
	}
	return append(out, map[string]any{"role": role, "content": blocks})
}

func anthropicRequest(baseURL, apiKey, model string, messages []map[string]any, tools []map[string]any) (string, map[string]string, map[string]any) {
	base := strings.TrimRight(baseURL, "/")
	base = strings.TrimSuffix(base, "/v1")
	endpoint := base + "/v1/messages"
	system, converted := anthropicConvert(messages)
	body := map[string]any{
		"model": model, "max_tokens": anthropicThinking + maxOutputTokens,
		"messages": converted, "stream": true,
		"thinking": map[string]any{"type": "enabled", "budget_tokens": anthropicThinking},
	}
	if system != "" {
		body["system"] = system
	}
	if len(tools) > 0 {
		converted := make([]map[string]any, 0, len(tools))
		for _, t := range tools {
			fn, _ := t["function"].(map[string]any)
			params, _ := fn["parameters"].(map[string]any)
			if params == nil {
				params = map[string]any{"type": "object"}
			}
			desc, _ := fn["description"].(string)
			converted = append(converted, map[string]any{
				"name": fn["name"], "description": desc, "input_schema": params,
			})
		}
		body["tools"] = converted
	}
	return endpoint, authHeaders("anthropic", apiKey), body
}

// OllamaParse 供测试复用的 ndjson 解析。
func OllamaParse(out func(LlmEvent), line string) {
	var ev map[string]any
	if json.Unmarshal([]byte(line), &ev) != nil {
		return
	}
	msg, _ := ev["message"].(map[string]any)
	if think, ok := msg["thinking"].(string); ok && think != "" {
		out(LlmEvent{Kind: "thinking", Text: think})
	}
	if content, ok := msg["content"].(string); ok && content != "" {
		out(LlmEvent{Kind: "answer", Text: content})
	}
	if rawCalls, ok := msg["tool_calls"].([]any); ok {
		calls := make([]map[string]any, 0, len(rawCalls))
		for _, raw := range rawCalls {
			tc, _ := raw.(map[string]any)
			fn, _ := tc["function"].(map[string]any)
			name := stringOf(fn["name"])
			if name == "" {
				continue
			}
			args := map[string]any{}
			switch v := fn["arguments"].(type) {
			case string:
				_ = json.Unmarshal([]byte(v), &args)
			case map[string]any:
				args = v
			}
			calls = append(calls, map[string]any{
				"id": fmt.Sprintf("ol-%s-%d", name, len(calls)), "name": name, "arguments": args,
			})
		}
		if len(calls) > 0 {
			out(LlmEvent{Kind: "tool_calls", Calls: calls})
		}
	}
	if ev["done"] == true {
		prompt := jsonInt(ev["prompt_eval_count"])
		out(LlmEvent{Kind: "usage", UsageIn: prompt, UsageOut: jsonInt(ev["eval_count"])})
		if prompt > 0 {
			out(LlmEvent{Kind: "context", UsageIn: prompt})
		}
	}
}

// ── openai-responses（Responses API）────────────────────────────────

func responsesInput(messages []map[string]any) (string, []map[string]any) {
	systems := []string{}
	out := []map[string]any{}
	for _, m := range messages {
		role, _ := m["role"].(string)
		content, _ := m["content"].(string)
		switch role {
		case "system":
			if content != "" {
				systems = append(systems, content)
			}
		case "user":
			out = append(out, map[string]any{"role": "user",
				"content": []map[string]any{{"type": "input_text", "text": content}}})
		case "assistant":
			if content != "" {
				out = append(out, map[string]any{"role": "assistant",
					"content": []map[string]any{{"type": "output_text", "text": content}}})
			}
			if rawCalls, ok := m["tool_calls"].([]any); ok {
				for _, raw := range rawCalls {
					tc, _ := raw.(map[string]any)
					fn, _ := tc["function"].(map[string]any)
					args := stringOf(fn["arguments"])
					if args == "" {
						args = "{}"
					}
					out = append(out, map[string]any{"type": "function_call",
						"call_id": tc["id"], "name": fn["name"], "arguments": args})
				}
			}
		case "tool":
			out = append(out, map[string]any{"type": "function_call_output",
				"call_id": stringOf(m["tool_call_id"]), "output": content})
		}
	}
	return strings.Join(systems, "\n"), out
}

func responsesRequest(baseURL, apiKey, model string, messages []map[string]any, tools []map[string]any) (string, map[string]string, map[string]any) {
	endpoint := strings.TrimRight(baseURL, "/") + "/responses"
	instructions, items := responsesInput(messages)
	body := map[string]any{"model": model, "input": items, "stream": true,
		"max_output_tokens": maxOutputTokens, "temperature": 0.4}
	if instructions != "" {
		body["instructions"] = instructions
	}
	if len(tools) > 0 {
		converted := make([]map[string]any, 0, len(tools))
		for _, t := range tools {
			fn, _ := t["function"].(map[string]any)
			params, _ := fn["parameters"].(map[string]any)
			if params == nil {
				params = map[string]any{"type": "object"}
			}
			desc, _ := fn["description"].(string)
			converted = append(converted, map[string]any{
				"type": "function", "name": fn["name"], "description": desc, "parameters": params,
			})
		}
		body["tools"] = converted
	}
	return endpoint, authHeaders("openai-responses", apiKey), body
}

// ── ollama 请求 ─────────────────────────────────────────────────────

func ollamaRequest(baseURL, apiKey, model string, messages []map[string]any, tools []map[string]any) (string, map[string]string, map[string]any) {
	endpoint := strings.TrimRight(baseURL, "/") + "/api/chat"
	converted := make([]map[string]any, 0, len(messages))
	for _, m := range messages {
		item := map[string]any{"role": m["role"], "content": stringOf(m["content"])}
		if rawCalls, ok := m["tool_calls"].([]any); ok {
			calls := make([]map[string]any, 0, len(rawCalls))
			for _, raw := range rawCalls {
				tc, _ := raw.(map[string]any)
				fn, _ := tc["function"].(map[string]any)
				args := map[string]any{}
				_ = json.Unmarshal([]byte(stringOf(fn["arguments"])), &args)
				calls = append(calls, map[string]any{"function": map[string]any{"name": fn["name"], "arguments": args}})
			}
			item["tool_calls"] = calls
		}
		converted = append(converted, item)
	}
	body := map[string]any{"model": model, "messages": converted, "stream": true}
	if len(tools) > 0 {
		body["tools"] = tools
	}
	return endpoint, authHeaders("ollama", apiKey), body
}

// ChatStream 按协议构造请求并流式解析，产出统一事件；error 事件携带
// PilotLlmError（消费方转诊断帧）。
func (s *Service) ChatStream(ctx context.Context, protocol, baseURL, apiKey, model string,
	messages []map[string]any, tools []map[string]any, maxTokens int, client *http.Client) (<-chan LlmEvent, error) {
	var endpoint string
	var headers map[string]string
	var body map[string]any
	switch protocol {
	case "openai":
		endpoint, headers, body = openaiRequest(baseURL, apiKey, model, messages, tools)
	case "openai-responses":
		endpoint, headers, body = responsesRequest(baseURL, apiKey, model, messages, tools)
	case "anthropic":
		endpoint, headers, body = anthropicRequest(baseURL, apiKey, model, messages, tools)
	case "ollama":
		endpoint, headers, body = ollamaRequest(baseURL, apiKey, model, messages, tools)
	default:
		return nil, newLlmError(fmt.Sprintf("未知协议: %s", protocol), "unsupported_protocol", 0)
	}
	if maxTokens > 0 {
		if protocol == "openai-responses" {
			body["max_output_tokens"] = maxTokens
		} else {
			body["max_tokens"] = maxTokens
		}
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if !isLoopback(baseURL) {
		transport.Proxy = http.ProxyFromEnvironment
	} else {
		transport.Proxy = nil
	}
	nc := &http.Client{Timeout: llmTimeout, Transport: transport}
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, newLlmError(err.Error(), "provider_response_error", 0)
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(payload))
	if err != nil {
		return nil, newLlmError(err.Error(), "provider_network_error", 0)
	}
	for key, value := range headers {
		request.Header.Set(key, value)
	}
	request.Header.Set("Content-Type", "application/json")
	events := make(chan LlmEvent, 64)
	go func() {
		defer close(events)
		resp, err := nc.Do(request)
		if err != nil {
			code := "provider_network_error"
			if strings.Contains(err.Error(), "timeout") || strings.Contains(err.Error(), "Client.Timeout") {
				code = "provider_timeout"
			}
			events <- LlmEvent{Kind: "error", Err: newLlmError(err.Error(), code, 0)}
			return
		}
		defer resp.Body.Close()
		if resp.StatusCode >= 400 {
			raw, _ := io.ReadAll(io.LimitReader(resp.Body, 4<<10))
			events <- LlmEvent{Kind: "error", Err: newLlmError(
				fmt.Sprintf("HTTP %d: %s", resp.StatusCode, clip(string(raw), 300)), "provider_http_error", resp.StatusCode)}
			return
		}
		switch protocol {
		case "openai":
			acc := newOpenaiAcc()
			sseLines(resp, func(data string) {
				acc.feed(data, func(ev LlmEvent) { events <- ev })
			})
			if assembled := assembleToolCalls(acc.pending); len(assembled) > 0 {
				// 流末兜底：部分实现不发 finish_reason
				events <- LlmEvent{Kind: "tool_calls", Calls: assembled}
			}
		case "openai-responses":
			responsesSSE(resp, events)
		case "anthropic":
			toolsAcc := map[int]map[string]string{}
			var usage [2]int
			ctxInput := 0
			stop := false
			sseLines(resp, func(data string) {
				if stop {
					return
				}
				var ev map[string]any
				if json.Unmarshal([]byte(data), &ev) != nil {
					return
				}
				switch etype := stringOf(ev["type"]); etype {
				case "message_start":
					message, _ := ev["message"].(map[string]any)
					u, _ := message["usage"].(map[string]any)
					usage[0] = jsonInt(u["input_tokens"])
					read := jsonInt(u["cache_read_input_tokens"])
					write := jsonInt(u["cache_creation_input_tokens"])
					inputSide := usage[0] + read + write
					ctxInput = inputSide
					events <- LlmEvent{Kind: "cache", Cache: cacheBuckets(inputSide, read, write)}
				case "content_block_delta":
					d, _ := ev["delta"].(map[string]any)
					switch stringOf(d["type"]) {
					case "thinking_delta":
						if think, ok := d["thinking"].(string); ok && think != "" {
							events <- LlmEvent{Kind: "thinking", Text: think}
						}
					case "text_delta":
						if text, ok := d["text"].(string); ok && text != "" {
							events <- LlmEvent{Kind: "answer", Text: text}
						}
					case "input_json_delta":
						idx := jsonInt(ev["index"])
						acc := toolsAcc[idx]
						if acc == nil {
							acc = map[string]string{}
							toolsAcc[idx] = acc
						}
						acc["args"] += stringOf(d["partial_json"])
					}
				case "content_block_start":
					block, _ := ev["content_block"].(map[string]any)
					if stringOf(block["type"]) == "tool_use" {
						idx := jsonInt(ev["index"])
						acc := toolsAcc[idx]
						if acc == nil {
							acc = map[string]string{}
							toolsAcc[idx] = acc
						}
						acc["id"] = stringOf(block["id"])
						acc["name"] = stringOf(block["name"])
					}
				case "message_delta":
					d, _ := ev["delta"].(map[string]any)
					u, _ := ev["usage"].(map[string]any)
					usage[1] += jsonInt(u["output_tokens"])
					if stringOf(d["stop_reason"]) == "tool_use" {
						if assembled := assembleToolCalls(toolsAcc); len(assembled) > 0 {
							events <- LlmEvent{Kind: "tool_calls", Calls: assembled}
						}
						toolsAcc = map[int]map[string]string{}
					}
				case "message_stop":
					stop = true
				}
			})
			events <- LlmEvent{Kind: "usage", UsageIn: usage[0], UsageOut: usage[1]}
			if ctxInput > 0 {
				events <- LlmEvent{Kind: "context", UsageIn: ctxInput}
			}
		case "ollama":
			ndjsonLines(resp, func(line string) { OllamaParse(func(ev LlmEvent) { events <- ev }, line) })
		}
	}()
	return events, nil
}

// cacheBuckets 缓存计量三桶（命中率分母含缓存读，避免部分命中高估到 100%）。
func cacheBuckets(inputSide, read, write int) map[string]any {
	rate := any(nil)
	if inputSide > 0 {
		v := float64(read) / float64(inputSide)
		if v > 1 {
			v = 1
		}
		rate = v
	}
	readAny := any(nil)
	if read > 0 {
		readAny = read
	}
	writeAny := any(nil)
	if write > 0 {
		writeAny = write
	}
	base := any(nil)
	if inputSide > 0 {
		base = inputSide
	}
	return map[string]any{"rate": rate, "read": readAny, "write": writeAny, "base": base}
}

func sseLines(resp *http.Response, handle func(string)) {
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 1<<20), 8<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if !strings.HasPrefix(line, "data:") {
			continue
		}
		data := strings.TrimSpace(line[len("data:"):])
		if data == "[DONE]" {
			return // 流终止信号：立即停止读取
		}
		if data == "" {
			continue
		}
		handle(data)
	}
}

func ndjsonLines(resp *http.Response, handle func(string)) {
	scanner := bufio.NewScanner(resp.Body)
	scanner.Buffer(make([]byte, 0, 1<<20), 8<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line != "" {
			handle(line)
		}
	}
}

func responsesSSE(resp *http.Response, events chan<- LlmEvent) {
	pending := map[string]map[string]string{}
	order := []string{}
	flush := func() []map[string]any {
		if len(order) == 0 {
			return nil
		}
		calls := []map[string]any{}
		for _, itemID := range order {
			acc, ok := pending[itemID]
			if !ok || acc["name"] == "" {
				continue
			}
			args := map[string]any{}
			_ = json.Unmarshal([]byte(acc["args"]), &args)
			calls = append(calls, map[string]any{"id": acc["id"], "name": acc["name"], "arguments": args})
			delete(pending, itemID)
		}
		order = nil
		if len(calls) == 0 {
			return nil
		}
		return calls
	}
	sseLines(resp, func(data string) {
		var ev map[string]any
		if json.Unmarshal([]byte(data), &ev) != nil {
			return
		}
		switch etype := stringOf(ev["type"]); etype {
		case "response.output_text.delta":
			if delta, ok := ev["delta"].(string); ok && delta != "" {
				events <- LlmEvent{Kind: "answer", Text: delta}
			}
		case "response.reasoning_summary_text.delta":
			if delta, ok := ev["delta"].(string); ok && delta != "" {
				events <- LlmEvent{Kind: "thinking", Text: delta}
			}
		case "response.output_item.added":
			item, _ := ev["item"].(map[string]any)
			if stringOf(item["type"]) == "function_call" && stringOf(item["item_id"]) != "" {
				itemID := stringOf(item["item_id"])
				order = append(order, itemID)
				pending[itemID] = map[string]string{
					"id":   firstNonEmptyStr(stringOf(item["call_id"]), itemID),
					"name": stringOf(item["name"]), "args": stringOf(item["arguments"]),
				}
			}
		case "response.function_call_arguments.delta":
			itemID := stringOf(ev["item_id"])
			if acc, ok := pending[itemID]; ok {
				if delta, ok := ev["delta"].(string); ok && delta != "" {
					acc["args"] += delta
				}
			}
		case "response.output_item.done":
			item, _ := ev["item"].(map[string]any)
			itemID := firstNonEmptyStr(stringOf(item["item_id"]), stringOf(item["id"]))
			if stringOf(item["type"]) == "function_call" {
				if acc, ok := pending[itemID]; ok {
					if v := stringOf(item["name"]); v != "" {
						acc["name"] = v
					}
					if v := stringOf(item["call_id"]); v != "" {
						acc["id"] = v
					}
					if v := stringOf(item["arguments"]); v != "" {
						acc["args"] = v
					}
				}
			}
		case "response.completed":
			if calls := flush(); calls != nil {
				events <- LlmEvent{Kind: "tool_calls", Calls: calls}
			}
			response, _ := ev["response"].(map[string]any)
			usage, _ := response["usage"].(map[string]any)
			inp := jsonInt(usage["input_tokens"])
			events <- LlmEvent{Kind: "usage", UsageIn: inp, UsageOut: jsonInt(usage["output_tokens"])}
			if inp > 0 {
				events <- LlmEvent{Kind: "context", UsageIn: inp}
			}
			details, _ := usage["input_tokens_details"].(map[string]any)
			read := 0
			hasRead := false
			if details != nil {
				read = jsonInt(details["cached_tokens"])
				hasRead = details["cached_tokens"] != nil
			}
			rate := any(nil)
			if hasRead && inp > 0 {
				v := float64(read) / float64(inp)
				if v > 1 {
					v = 1
				}
				rate = v
			}
			readAny := any(nil)
			if hasRead {
				readAny = read
			}
			base := any(nil)
			if inp > 0 {
				base = inp
			}
			events <- LlmEvent{Kind: "cache", Cache: map[string]any{"rate": rate, "read": readAny, "write": nil, "base": base}}
		}
	})
	if calls := flush(); calls != nil {
		events <- LlmEvent{Kind: "tool_calls", Calls: calls}
	}
}

func jsonInt(value any) int {
	switch v := value.(type) {
	case float64:
		return int(v)
	case json.Number:
		n, _ := v.Int64()
		return int(n)
	case int:
		return v
	case string:
		n, _ := strconvParseInt(v)
		return n
	}
	return 0
}

func stringOf(value any) string {
	text, _ := value.(string)
	return text
}

func firstNonEmptyStr(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

func strconvParseInt(s string) (int, error) {
	n := 0
	for _, r := range s {
		if r < '0' || r > '9' {
			return n, fmt.Errorf("not a number")
		}
		n = n*10 + int(r-'0')
	}
	return n, nil
}
