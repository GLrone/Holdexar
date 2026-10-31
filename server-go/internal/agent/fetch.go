package agent

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
)

// muxFetcher 经主 mux 进程内调用已迁移只读端点：与真实 HTTP 同一处理器、
// 同一 SQL，不产生第二套口径。绕过访问闸（工具调用已在 MCP 闸之后，
// 进程内转发属可信路径）。
type muxFetcher func(ctx context.Context, path string) (int, []byte, error)

func newMuxFetcher(mux *http.ServeMux) muxFetcher {
	return func(ctx context.Context, path string) (int, []byte, error) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, "http://agent.internal"+path, nil)
		if err != nil {
			return 0, nil, err
		}
		rec := &recorder{header: http.Header{}}
		mux.ServeHTTP(rec, req)
		return rec.status, rec.body.Bytes(), nil
	}
}

// recorder 最小响应收集器（进程内转发专用，不引 httptest）。
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

// getJSON 进程内 GET 并解析；状态码非 200 按带状态码的错误返回。
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

// pyBool Python bool() 语义：None/False/0/空串/空容器为假，其余为真
// （字符串 "false" 是真值——KV 历史行为的同源副本）。
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

// pyIntOr int(value or 0)：数值/字符串按 Python int() 解析；解析失败返回错误
// （执行器归一化为 failed，与 Python int() 抛 ValueError 同径）。
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

// pyParseInt Python int(str) 语义：十进制整数，允许前后空白与符号；小数字符串
// 拒绝（Python int("1.5") 抛 ValueError）。
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

// pyFloatOr Python float(value)：数值/字符串解析；失败返回错误。
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

// clampInt 与 Python _shared._clamp 同语义：int(value) 失败（含缺席 None）
// 回缺省，数值后夹界。
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

// maskAddr 收件邮箱脱敏：保留首字符与域名（与 Python _mask_addr 一致）。
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

// queryBuilder 保序查询串（推荐链路固定参数序，可读性优先）。
type queryBuilder struct {
	pairs []string
}

func urlValues() *queryBuilder { return &queryBuilder{} }

func (q *queryBuilder) set(key, val string) {
	q.pairs = append(q.pairs, key+"="+val)
}

func (q *queryBuilder) encode() string {
	return strings.Join(q.pairs, "&")
}

// jsonDecoder UseNumber 解码器（数字字面量保留，键值查表不丢精度）。
func jsonDecoder(body []byte) *json.Decoder {
	dec := json.NewDecoder(bytes.NewReader(body))
	dec.UseNumber()
	return dec
}
