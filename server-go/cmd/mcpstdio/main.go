// mcpstdio：把本机正式实例的 MCP HTTP 端点转发到 stdio（无状态协议桥，不持第二写者）。
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

type tokenTransport struct {
	token string
	base  http.RoundTripper
}

func (t *tokenTransport) RoundTrip(req *http.Request) (*http.Response, error) {
	req = req.Clone(req.Context())
	req.Header.Set("X-Holdexar-Mcp", t.token)
	return t.base.RoundTrip(req)
}
