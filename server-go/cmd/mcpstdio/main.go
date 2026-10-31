// mcpstdio：MCP stdio 桥——把本机正式实例的 MCP 端点转发到 stdio，
// 供本机开发 Agent 消费。子进程只做协议转发：不初始化数据库 / 调度器 /
// 代理池，业务执行全部在正式实例进程内，不产生第二写者。
//
// 准入令牌从数据目录 mcp-token 读取（数据目录解析与主程序同一规则，
// 尊重 HOLDEXAR_DATA_DIR）。可选环境变量 HOLDEXAR_MCP_URL 覆盖上游端点
// （默认 http://127.0.0.1:28765/api/v1/mcp）。
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"holdexar/server-go/internal/config"
)

const defaultMCPURL = "http://127.0.0.1:28765/api/v1/mcp"

func main() {
	if err := run(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run() error {
	upstream := defaultMCPURL
	if raw, ok := os.LookupEnv("HOLDEXAR_MCP_URL"); ok {
		upstream = strings.TrimRight(raw, "/")
	}
	token, err := loadToken()
	if err != nil {
		return err
	}

	// 出口忽略环境代理（Python trust_env=False 同语义）；重定向默认跟随。
	base := http.DefaultTransport.(*http.Transport).Clone()
	base.Proxy = nil
	httpClient := &http.Client{Transport: &tokenTransport{token: token, base: base}}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	impl := &mcp.Implementation{Name: "holdexar-stdio", Version: config.Version()}
	session, err := mcp.NewClient(impl, nil).Connect(ctx,
		&mcp.StreamableClientTransport{Endpoint: upstream, HTTPClient: httpClient}, nil)
	if err != nil {
		return fmt.Errorf("MCP 主实例连接失败：%v（请确认主实例已启动且启用 MCP）", err)
	}
	defer session.Close()

	listed, err := session.ListTools(ctx, nil)
	if err != nil {
		return fmt.Errorf("MCP 工具清单获取失败：%v", err)
	}

	// 启动时快照上游工具表：清单内工具逐一注册转发；清单外名称经 middleware
	// 同样转发（纯协议桥不自带语义，与现网桥的动态转发同径）。
	forward := mcp.NewServer(&mcp.Implementation{Name: "holdexar-stdio", Version: config.Version()}, nil)
	callUpstream := func(ctx context.Context, name string, args json.RawMessage) (*mcp.CallToolResult, error) {
		return session.CallTool(ctx, &mcp.CallToolParams{Name: name, Arguments: args})
	}
	registered := map[string]bool{}
	for _, tool := range listed.Tools {
		tool := tool
		registered[tool.Name] = true
		forward.AddTool(tool, func(ctx context.Context, req *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			return callUpstream(ctx, tool.Name, req.Params.Arguments)
		})
	}
	forward.AddReceivingMiddleware(func(next mcp.MethodHandler) mcp.MethodHandler {
		return func(ctx context.Context, method string, req mcp.Request) (mcp.Result, error) {
			if method == "tools/call" {
				if cr, ok := req.(*mcp.CallToolRequest); ok && !registered[cr.Params.Name] {
					return callUpstream(ctx, cr.Params.Name, cr.Params.Arguments)
				}
			}
			return next(ctx, method, req)
		}
	})

	fmt.Fprintf(os.Stderr, "holdexar mcp stdio bridge -> %s\n", upstream)
	return forward.Run(ctx, &mcp.StdioTransport{})
}

// loadToken 从数据目录读准入令牌；空令牌按未启用 MCP 报错退出。
func loadToken() (string, error) {
	dataDir, err := config.ResolveDataDir()
	if err != nil {
		return "", fmt.Errorf("数据目录解析失败：%v", err)
	}
	raw, err := os.ReadFile(filepath.Join(dataDir, "mcp-token"))
	if err != nil {
		return "", fmt.Errorf("mcp-token 读取失败：%v（请确认主实例已启用 MCP，HOLDEXAR_MCP=1）", err)
	}
	token := strings.TrimSpace(string(raw))
	if token == "" {
		return "", errors.New("mcp-token 为空：请确认主实例已启用 MCP（HOLDEXAR_MCP=1）")
	}
	return token, nil
}

// tokenTransport 每跳注入准入令牌头。
type tokenTransport struct {
	token string
	base  http.RoundTripper
}

func (t *tokenTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.Header.Set("X-Holdexar-Mcp", t.token)
	return t.base.RoundTrip(req)
}
