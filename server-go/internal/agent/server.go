package agent

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/config"
)

// Options 装配入参：数据目录（令牌文件落点）与启动日志出口。
type Options struct {
	DataDir string
	// EnabledByFlag 启动旗标（-mcp）：与 env HOLDEXAR_MCP 等价的启用通道。
	EnabledByFlag bool
	// Log 非致命事件出口（MCP 启用失败不拖垮主程序，与现网同语义）；可空。
	Log func(format string, args ...any)
}

// Surface MCP 面运行态：协议服务器、令牌与启用态的唯一持有者。
type Surface struct {
	registry *ToolRegistry
	fetcher  muxFetcher
	db       *sql.DB
	handler  http.Handler

	enabled atomic.Bool
	token   atomic.Value // string
	lastErr atomic.Value // string
}

// Register 装配 MCP 面：注册表构建（重名即错，启动即失败）→ 协议服务器 →
// 挂载路径与前缀闸 → 启用判定（env HOLDEXAR_MCP 或设置 mcp.enabled）→
// 令牌落盘。未启用时令牌文件清除、前缀一律 404。
func Register(mux *http.ServeMux, db *sql.DB, gate *access.Gate, opts Options) error {
	registry, err := buildRegistry()
	if err != nil {
		return err
	}
	s := &Surface{registry: registry, fetcher: newMuxFetcher(mux), db: db}

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

	// 前缀闸恒注册：未启用时 Host/Origin 之后、会话之前回 404（与现网一致）。
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
		// 启用失败不抛：保持关闭态，原因经 lastError 留痕。
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

// envEnabled 环境开关：HOLDEXAR_MCP ∈ {1,true,yes,on}（大小写不敏感）。
func envEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv("HOLDEXAR_MCP"))) {
	case "1", "true", "yes", "on":
		return true
	}
	return false
}

// settingsEnabled 设置开关：app_settings 的 mcp.enabled（JSON 布尔，缺行/空为关）。
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

// ensureTokenFile 令牌持久化（一次生成跨重启稳定），0600 权限。
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

// gateVerdict MCP 前缀闸三态：0 放行 / 401 令牌不符 / 404 未启用。
// 令牌头恒时比较；未启用（或令牌未就绪）一律 404。
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

// ServeHTTP 挂载壳：未启用时 404（与前缀闸双保险），启用后交协议处理器。
func (s *Surface) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if !s.enabled.Load() {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = w.Write([]byte(`{"detail":"not found"}`))
		return
	}
	s.handler.ServeHTTP(w, r)
}

// projectTool 注册表条目 → MCP 工具投影。写组与领航员会话 / UI 绑定工具
// 不进本注册表，协议面天然不可达（fail-closed）。
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

// availabilityGuard 不可用工具拦截：注册表外的名称一律回 isError 结果
// （文案与现网执行边界同文；协议错误按工具结果回灌，与 Python 一致）。
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

// callHandler 工具调用 → 公共执行边界，结果 JSON 文本回传
// （payload = {"status": 归一态, **data}）。
func (s *Surface) callHandler(spec *ToolSpec) mcp.ToolHandler {
	return func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		args := map[string]any{}
		if len(req.Params.Arguments) > 0 {
			if err := jsonDecoder(req.Params.Arguments).Decode(&args); err != nil {
				return nil, err
			}
		}
		result := Execute(&CallContext{Ctx: ctx, Args: args, fetch: s.fetcher, db: s.db}, spec, false)
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
