// MCP 工具注册表与执行边界（写组与领航员会话/UI 工具不进 MCP；未启用回 404）。
package agent

import (
	"context"
	"database/sql"
	"strings"

	"holdexar/server-go/internal/eventstream"
	"holdexar/server-go/internal/store"
)

const (
	MountPath     = "/api/v1/mcp"
	TokenHeader   = "X-Holdexar-Mcp"
	TokenFilename = "mcp-token"
)

type (
	Group  string
	Risk   string
	Status string
)

const (
	GroupRead  Group = "read"
	GroupWrite Group = "write"
)

const (
	StatusOK      Status = "ok"
	StatusEmpty   Status = "empty"
	StatusDenied  Status = "denied"
	StatusError   Status = "error"
	StatusTimeout Status = "timeout"
)

type ToolSpec struct {
	Name           string
	Group          Group
	Risk           Risk
	Description    string
	Parameters     string
	Handler        HandlerFunc
	TimeoutS       float64
	Idempotent     bool
	required       []string
	requiredParsed bool
}

// HandlerFunc 返回 error 时由执行器归一化为 failed 结果，不中断整次调用。
type HandlerFunc func(c *CallContext) (*ToolResult, error)

// Deps 共享依赖面；零值字段由 handler 走未装配降级或直连语义，不得 panic。
type Deps struct {
	LogRing        *eventstream.Ring
	WriteGate      *store.WriteGate
	ResolveProxy   func(ctx context.Context) (proxyURL string, ok bool)
	ImportAppids   func(ctx context.Context, appids []any) (map[string]any, error)
	AppIDFreshness func(ctx context.Context, appids []int64) ([]map[string]any, error)
}

type CallContext struct {
	Ctx   context.Context
	Args  map[string]any
	fetch func(ctx context.Context, path string) (int, []byte, error)
	post  func(ctx context.Context, path string, body []byte) (int, []byte, error)
	db    *sql.DB
	deps  *Deps
}

type ToolResult struct {
	Status  Status
	Data    *Obj
	ErrCode string
}

func deriveStatus(data *Obj) Status {
	switch data.get("kind") {
	case "denied":
		return StatusDenied
	case "empty", "disabled":
		return StatusEmpty
	case "failed":
		return StatusError
	case "timeout":
		return StatusTimeout
	}
	return StatusOK
}

var guardWords = []string{"清空", "删除", "移除", "移出", "停用", "停止", "批量", "全部", "所有"}

func isGuarded(text string) bool {
	for _, w := range guardWords {
		if strings.Contains(text, w) {
			return true
		}
	}
	return false
}

func decide(spec *ToolSpec, guarded bool) string {
	if spec.Group == GroupWrite && guarded {
		return "deny"
	}
	return "allow"
}

func denyResult() *Obj {
	obj := newObj()
	obj.set("kind", "denied")
	obj.set("note", "bulk_guard")
	obj.set("via", "propose_bulk")
	return obj
}

const readRowsLimit = 8
