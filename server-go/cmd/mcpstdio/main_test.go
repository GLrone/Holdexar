package main

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

func TestLoadTokenStates(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("HOLDEXAR_DATA_DIR", dir)

	if _, err := loadToken(); err == nil {
		t.Fatal("缺少 mcp-token 文件时应返回错误")
	}

	tokenPath := filepath.Join(dir, "mcp-token")
	if err := os.WriteFile(tokenPath, []byte("   \n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := loadToken(); err == nil {
		t.Fatal("空 mcp-token 文件时应返回错误")
	}

	if err := os.WriteFile(tokenPath, []byte("secret-token-123\r\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	got, err := loadToken()
	if err != nil {
		t.Fatalf("读取有效 mcp-token 失败: %v", err)
	}
	if got != "secret-token-123" {
		t.Fatalf("token=%q want secret-token-123", got)
	}
}

func TestTokenTransportInjectsHeaderWithoutMutatingOriginal(t *testing.T) {
	var seenHeader string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenHeader = r.Header.Get("X-Holdexar-Mcp")
		w.WriteHeader(http.StatusOK)
	}))
	defer srv.Close()

	rt := &tokenTransport{token: "tok-xyz", base: srv.Client().Transport}
	req, err := http.NewRequest(http.MethodGet, srv.URL, nil)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := (&http.Client{Transport: rt}).Do(req)
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()

	if seenHeader != "tok-xyz" {
		t.Fatalf("上游未收到 X-Holdexar-Mcp 头: %q", seenHeader)
	}
	if req.Header.Get("X-Holdexar-Mcp") != "" {
		t.Fatal("不应原地修改调用方 Request.Header")
	}
}

func TestMCPStreamableRoundTripWithTokenTransport(t *testing.T) {
	upstream := mcp.NewServer(&mcp.Implementation{Name: "upstream-test", Version: "1.0.0"}, nil)
	upstream.AddTool(&mcp.Tool{
		Name:        "ping_tool",
		Description: "test ping",
		InputSchema: map[string]any{"type": "object"},
	}, func(_ context.Context, _ *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
		return &mcp.CallToolResult{
			Content: []mcp.Content{&mcp.TextContent{Text: `{"ok":true}`}},
		}, nil
	})

	var sawAuth bool
	handler := mcp.NewStreamableHTTPHandler(func(r *http.Request) *mcp.Server {
		if r.Header.Get("X-Holdexar-Mcp") == "bridge-secret" {
			sawAuth = true
		}
		return upstream
	}, &mcp.StreamableHTTPOptions{Stateless: true, JSONResponse: true})

	srv := httptest.NewServer(handler)
	defer srv.Close()

	clientHTTP := &http.Client{
		Transport: &tokenTransport{token: "bridge-secret", base: srv.Client().Transport},
	}
	ctx := context.Background()
	session, err := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "1.0"}, nil).Connect(
		ctx,
		&mcp.StreamableClientTransport{Endpoint: srv.URL, HTTPClient: clientHTTP},
		nil,
	)
	if err != nil {
		t.Fatalf("连接 MCP 桩服务失败: %v", err)
	}
	defer session.Close()

	listed, err := session.ListTools(ctx, nil)
	if err != nil || len(listed.Tools) != 1 || listed.Tools[0].Name != "ping_tool" {
		t.Fatalf("工具列表异常: err=%v tools=%+v", err, listed)
	}
	res, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "ping_tool"})
	if err != nil || res.IsError || len(res.Content) == 0 {
		t.Fatalf("调用转发失败: err=%v res=%+v", err, res)
	}
	if !sawAuth {
		t.Fatal("请求未携带令牌头到达上游")
	}
}
