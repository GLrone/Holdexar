package agent

import (
	"bytes"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
)

// muxFetcher 经主 mux 进程内转发请求（MCP 鉴权后绕过外部访问闸）。
type muxFetcher func(ctx context.Context, path string) (int, []byte, error)

func newMuxFetcher(mux *http.ServeMux) muxFetcher {
	return func(ctx context.Context, path string) (int, []byte, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://agent.internal"+path, nil)
		if err != nil {
			return 0, nil, err
		}
		req.RemoteAddr = "127.0.0.1:0"
		rec := &recorder{header: http.Header{}}
		mux.ServeHTTP(rec, req)
		if rec.status == 0 {
			rec.status = http.StatusOK
		}
		return rec.status, rec.body.Bytes(), nil
	}
}

type muxPoster func(ctx context.Context, path string, body []byte) (int, []byte, error)

func newMuxPoster(mux *http.ServeMux) muxPoster {
	return func(ctx context.Context, path string, body []byte) (int, []byte, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, "http://agent.internal"+path, bytes.NewReader(body))
		if err != nil {
			return 0, nil, err
		}
		req.RemoteAddr = "127.0.0.1:0"
		req.Header.Set("Content-Type", "application/json")
		rec := &recorder{header: http.Header{}}
		mux.ServeHTTP(rec, req)
		if rec.status == 0 {
			rec.status = http.StatusOK
		}
		return rec.status, rec.body.Bytes(), nil
	}
}

type recorder struct {
	header http.Header
	body   bytes.Buffer
	status int
}

func (r *recorder) Header() http.Header { return r.header }

func (r *recorder) Write(p []byte) (int, error) {
	if r.status == 0 {
		r.status = http.StatusOK
	}
	return r.body.Write(p)
}

func (c *recorder) WriteHeader(code int) {
	if c.status == 0 {
		c.status = code
	}
}

func (c *CallContext) getJSON(path string, out any) error {
	status, body, err := c.fetch(c.Ctx, path)
	if err != nil {
		return err
	}
	if status != http.StatusOK {
		return fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	if err := dec.Decode(out); err != nil {
		return err
	}
	return nil
}

func (c *CallContext) postJSON(path string, body any, out any) error {
	if c.post == nil {
		return errors.New("执行环境未注入 POST 通道")
	}
	encoded, err := json.Marshal(body)
	if err != nil {
		return err
	}
	status, respBody, err := c.post(c.Ctx, path, encoded)
	if err != nil {
		return err
	}
	if status != http.StatusOK {
		return fmt.Errorf("内部请求失败（%d）：%s", status, strings.TrimSpace(string(respBody)))
	}
	if out == nil {
		return nil
	}
	dec := json.NewDecoder(bytes.NewReader(respBody))
	dec.UseNumber()
	return dec.Decode(out)
}

// pyBool 按动态真值规则求值（字符串 "false" 亦为真）。
func pyBool(v any) bool {
	switch x := v.(type) {
	case nil:
		return false
	case bool:
		return x
	case string:
		return x != ""
	case json.Number:
		return x.String() != "" && x.String() != "0"
	case float64:
		return x != 0
	case int, int64:
		return fmt.Sprintf("%v", x) != "0"
	case *Obj:
		return x != nil && len(x.keys) > 0
	case []any:
		return len(x) > 0
	case []string:
		return len(x) > 0
	}
	return true
}

func pyIntOr(v any) (int64, error) {
	if v == nil {
		return 0, nil
	}
	switch x := v.(type) {
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case json.Number:
		return pyParseInt(x.String())
	case string:
		s := strings.TrimSpace(x)
		if s == "" {
			return 0, nil
		}
		return pyParseInt(s)
	case float64:
		return int64(x), nil
	case int:
		return int64(x), nil
	case int64:
		return x, nil
	}
	return 0, fmt.Errorf("invalid literal for int(): %v", v)
}

func pyParseInt(s string) (int64, error) {
	if strings.ContainsAny(s, ".eE") {
		return 0, fmt.Errorf("invalid literal for int() with base 10: %q", s)
	}
	n, err := strconv.ParseInt(strings.TrimSpace(s), 10, 64)
	if err != nil {
		return 0, fmt.Errorf("invalid literal for int() with base 10: %q", s)
	}
	return n, nil
}

func pyFloatOr(v any) (float64, error) {
	switch x := v.(type) {
	case nil:
		return 0, nil
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case json.Number:
		return strconv.ParseFloat(x.String(), 64)
	case string:
		return strconv.ParseFloat(strings.TrimSpace(x), 64)
	case float64:
		return x, nil
	case int:
		return float64(x), nil
	case int64:
		return float64(x), nil
	}
	return 0, fmt.Errorf("could not convert string to float: %v", v)
}

func clampInt(v any, def int64, lo, hi int64) int64 {
	if v == nil {
		return def
	}
	n, err := pyIntOr(v)
	if err != nil {
		return def
	}
	if n < lo {
		return lo
	}
	if n > hi {
		return hi
	}
	return n
}

func maskAddr(addr string) string {
	head, domain, ok := strings.Cut(addr, "@")
	if !ok {
		return ""
	}
	if head == "" {
		return "***@" + domain
	}
	return head[:1] + "***@" + domain
}

func disabledResult(tool, note string) *ToolResult {
	obj := newObj()
	obj.set("kind", "disabled")
	obj.set("tool", tool)
	if note != "" {
		obj.set("note", note)
	}
	return &ToolResult{Status: StatusEmpty, Data: obj}
}

func conflictDetail(body []byte, fallback string) string {
	var obj map[string]any
	if len(body) > 0 && json.Unmarshal(body, &obj) == nil {
		if d := strings.TrimSpace(pyStrOr(obj["detail"])); d != "" {
			return d
		}
	}
	return fallback
}

func isModuleDisabled(ctx context.Context, db *sql.DB, key string) bool {
	if db == nil || key == "" {
		return false
	}
	var raw sql.NullString
	if err := db.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, key).Scan(&raw); err != nil {
		return false
	}
	return strings.TrimSpace(raw.String) == "false"
}

func (c *CallContext) getJSONOrDisabled(tool, path, moduleKey, fallbackNote string, out any) (*ToolResult, error) {
	if isModuleDisabled(c.Ctx, c.db, moduleKey) {
		return disabledResult(tool, fallbackNote), nil
	}
	status, body, err := c.fetch(c.Ctx, path)
	if err != nil {
		return nil, err
	}
	if status == http.StatusConflict {
		return disabledResult(tool, conflictDetail(body, fallbackNote)), nil
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("内部读取失败（%d）：%s", status, strings.TrimSpace(string(body)))
	}
	if err := jsonDecoder(body).Decode(out); err != nil {
		return nil, err
	}
	return nil, nil
}

func jsonDecoder(body []byte) *json.Decoder {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	return dec
}
