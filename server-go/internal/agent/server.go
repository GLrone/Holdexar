package agent

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/config"
)

type Options struct {
	DataDir       string
	EnabledByFlag bool
	Log           func(format string, args ...any)
	Deps          *Deps
}

type Surface struct {
	registry *ToolRegistry
	fetcher  muxFetcher
	poster   muxPoster
	db       *sql.DB
	deps     *Deps
	handler  http.Handler

	dataDir      string
	logf         func(format string, args ...any)
	forcedByFlag bool // -mcp 启动旗标：与 env 同级的强制通道，设置关闭不生效

	enabled atomic.Bool
	token   atomic.Value
	lastErr atomic.Value
}

var Default *Surface

func Register(mux *http.ServeMux, db *sql.DB, gate *access.Gate, opts Options) error {
	registry, err := buildRegistry()
	if err != nil {
		return err
	}
	deps := opts.Deps
	if deps == nil {
		deps = &Deps{}
	}
	s := &Surface{
		registry: registry, fetcher: newMuxFetcher(mux), poster: newMuxPoster(mux), db: db, deps: deps,
		dataDir: opts.DataDir, logf: opts.Log, forcedByFlag: opts.EnabledByFlag,
	}
	for name, schemaErr := range registry.schemaFailures() {
		s.log(opts, "MCP 工具 %s 入参 schema 解析失败，回退必需键校验：%v", name, schemaErr)
	}
	Default = s

	impl := &mcp.Implementation{Name: "holdexar", Version: config.Version()}
	server := mcp.NewServer(impl, nil)
	server.AddReceivingMiddleware(s.availabilityGuard)
	for _, spec := range registry.all() {
		server.AddTool(projectTool(spec), s.callHandler(spec))
	}
	s.handler = mcp.NewStreamableHTTPHandler(
		func(*http.Request) *mcp.Server { return server },
		&mcp.StreamableHTTPOptions{Stateless: true, JSONResponse: true},
	)

	// 前缀闸恒注册，未启用时在会话前返回 404。
	gate.RegisterMCPGate(MountPath, s.gateVerdict)
	mux.Handle(MountPath, s)
	mux.Handle(MountPath+"/", s)

	enabled := envEnabled() || opts.EnabledByFlag || settingsEnabled(db)
	if !enabled {
		_ = os.Remove(filepath.Join(opts.DataDir, TokenFilename))
		return nil
	}
	token, err := ensureTokenFile(opts.DataDir)
	if err != nil {
		s.lastErr.Store("MCP 启用失败：" + err.Error())
		s.log(opts, "MCP 启用失败，保持关闭态：%v", err)
		return nil
	}
	s.token.Store(token)
	s.enabled.Store(true)
	return nil
}

func (s *Surface) log(opts Options, format string, args ...any) {
	if opts.Log != nil {
		opts.Log(format, args...)
	}
}

func Reapply() {
	if Default != nil {
		Default.reapply()
	}
}

func (s *Surface) reapply() {
	if !envEnabled() && !s.forcedByFlag && !settingsEnabled(s.db) {
		s.enabled.Store(false)
		s.token.Store("")
		_ = os.Remove(filepath.Join(s.dataDir, TokenFilename))
		return
	}
	token, err := ensureTokenFile(s.dataDir)
	if err != nil {
		s.enabled.Store(false)
		s.lastErr.Store("MCP 启用失败：" + err.Error())
		if s.logf != nil {
			s.logf("MCP 启用失败，保持关闭态：%v", err)
		}
		return
	}
	s.token.Store(token)
	s.enabled.Store(true)
	s.lastErr.Store("")
}

func MCPStatus() (running bool, errText string, forcedByEnv bool) {
	if Default == nil {
		return false, "", envEnabled()
	}
	errText, _ = Default.lastErr.Load().(string)
	return Default.enabled.Load(), errText, envEnabled() || Default.forcedByFlag
}

func envEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("HOLDEXAR_MCP"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

func settingsEnabled(db *sql.DB) bool {
	var raw sql.NullString
	err := db.QueryRow("SELECT value_json FROM app_settings WHERE key = 'mcp.enabled'").Scan(&raw)
	if err != nil || !raw.Valid || raw.String == "" {
		return false
	}
	var v any
	if json.Unmarshal([]byte(raw.String), &v) != nil {
		return false
	}
	return pyBool(v)
}

func ensureTokenFile(dataDir string) (string, error) {
	path := filepath.Join(dataDir, TokenFilename)
	if raw, err := os.ReadFile(path); err == nil {
		if token := strings.TrimSpace(string(raw)); token != "" {
			return token, nil
		}
	}
	buf := make([]byte, 32)
	if _, err := rand.Read(buf); err != nil {
		return "", err
	}
	token := base64.RawURLEncoding.EncodeToString(buf)
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return "", err
	}
	if err := os.WriteFile(path, []byte(token), 0o600); err != nil {
		return "", err
	}
	return token, nil
}

// gateVerdict 返回 0 放行 / 401 令牌不符 / 404 未启用（令牌恒时比较，未就绪回 404）。
func (s *Surface) gateVerdict(r *http.Request) int {
	if !s.enabled.Load() {
		return http.StatusNotFound
	}
	token, _ := s.token.Load().(string)
	if token == "" {
		return http.StatusNotFound
	}
	provided := r.Header.Get(TokenHeader)
	if provided == "" {
		return http.StatusUnauthorized
	}
	if subtle.ConstantTimeCompare([]byte(provided), []byte(token)) != 1 {
		return http.StatusUnauthorized
	}
	return 0
}

func (s *Surface) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !s.enabled.Load() {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"not found"}`))
		return
	}
	s.handler.ServeHTTP(w, r)
}

func projectTool(spec *ToolSpec) *mcp.Tool {
	no := false
	return &mcp.Tool{
		Name:        spec.Name,
		Description: spec.Description,
		InputSchema: json.RawMessage(spec.Parameters),
		Annotations: &mcp.ToolAnnotations{
			ReadOnlyHint:    true,
			DestructiveHint: &no,
			IdempotentHint:  spec.Idempotent,
		},
	}
}

func (s *Surface) availabilityGuard(next mcp.MethodHandler) mcp.MethodHandler {
	return func(ctx context.Context, method string, req mcp.Request) (mcp.Result, error) {
		if method == "tools/call" {
			if cr, ok := req.(*mcp.CallToolRequest); ok && s.registry.get(cr.Params.Name) == nil {
				return &mcp.CallToolResult{
					IsError: true,
					Content: []mcp.Content{&mcp.TextContent{
						Text: "tool not available over MCP: " + cr.Params.Name,
					}},
				}, nil
			}
		}
		return next(ctx, method, req)
	}
}

// validateArgs 全量校验工具入参（type/enum/minimum/required 等），未声明额外键不拦截；
// schema 解析失败时回退必需键检查。
func validateArgs(reg *ToolRegistry, spec *ToolSpec, args map[string]any) error {
	if res := reg.resolvedSchema(spec.Name); res != nil {
		if err := res.Validate(schemaView(args)); err != nil {
			return fmt.Errorf("%s 参数校验失败：%v", spec.Name, err)
		}
		return nil
	}
	required := spec.required
	if !spec.requiredParsed {
		required = parseRequiredParams(spec.Parameters)
	}
	for _, k := range required {
		if _, ok := args[k]; !ok {
			return fmt.Errorf("%s 缺少必需参数 %q", spec.Name, k)
		}
	}
	return nil
}

// schemaView 复制入参并把 json.Number 归一为 float64，使 schema 数值类型可识别。
func schemaView(v any) any {
	switch x := v.(type) {
	case json.Number:
		if f, err := x.Float64(); err == nil {
			return f
		}
		return x.String()
	case map[string]any:
		out := make(map[string]any, len(x))
		for k, item := range x {
			out[k] = schemaView(item)
		}
		return out
	case []any:
		out := make([]any, len(x))
		for i, item := range x {
			out[i] = schemaView(item)
		}
		return out
	default:
		return v
	}
}

func (s *Surface) callHandler(spec *ToolSpec) mcp.ToolHandler {
	return func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args := map[string]any{}
		if len(req.Params.Arguments) > 0 {
			if err := jsonDecoder(req.Params.Arguments).Decode(&args); err != nil {
				return nil, err
			}
		}
		if err := validateArgs(s.registry, spec, args); err != nil {
			return &mcp.CallToolResult{
				IsError: true,
				Content: []mcp.Content{&mcp.TextContent{Text: err.Error()}},
			}, nil
		}
		result := Execute(&CallContext{Ctx: ctx, Args: args, fetch: s.fetcher, post: s.poster, db: s.db, deps: s.deps}, spec, false)
		payload := newObj()
		payload.set("status", string(result.Status))
		for i, k := range result.Data.keys {
			payload.set(k, result.Data.vals[i])
		}
		return &mcp.CallToolResult{
			Content: []mcp.Content{&mcp.TextContent{Text: encodeJSON(payload)}},
		}, nil
	}
}
