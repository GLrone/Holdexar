// Package agent：MCP 面——工具注册表投影到 MCP 协议（Streamable HTTP + stdio 桥）。
//
// 单一事实源是本包工具注册表：工具的名称/描述/参数 Schema 只在这里声明，
// 外部 MCP 客户端经同一执行边界消费已迁移的业务读路径。fail-closed 铁律：
// 只注册 Go 侧有真实实现的只读工具；绑定领航员会话 / UI / 写组的工具不进
// MCP 面；未启用时该前缀一律 404。
package agent

import (
	"context"
	"database/sql"
	"strings"
)

// MCP 挂载路径与令牌头；与现网 Python 侧逐字对齐。
const (
	MountPath     = "/api/v1/mcp"
	TokenHeader   = "X-Holdexar-Mcp"
	TokenFilename = "mcp-token"
)

// Group 工具风险分组；Risk 风险级；Status 归一化结果状态。
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

// ToolSpec 工具契约：一处声明携带协议视图（description/parameters）与风险面
// （group/risk）。handler 经 CallContext 访问已迁移读路径。
type ToolSpec struct {
	Name        string
	Group       Group
	Risk        Risk
	Description string
	// Parameters 是 MCP inputSchema 的原样 JSON 文本（与 Python 声明逐字段一致）。
	Parameters string
	Handler    HandlerFunc
	// TimeoutS 执行预算（秒），超时归一化为 timeout 结果。
	TimeoutS float64
	// Idempotent 幂等提示；只影响协议投影注解。
	Idempotent bool
}

// HandlerFunc 工具执行体：返回 error 即「工具失败」，由执行器归一化为结果
// 回灌，绝不终结调用。
type HandlerFunc func(c *CallContext) (*ToolResult, error)

// CallContext 单次工具调用的执行环境。
type CallContext struct {
	Ctx context.Context
	// Args MCP 客户端原始入参（UseNumber 解码，键值查表用）。
	Args map[string]any
	// fetch 经主 mux 进程内调用已迁移只读端点（GET，返回状态码与响应体）。
	fetch func(ctx context.Context, path string) (int, []byte, error)
	// db 业务库句柄（注册表自述类小查询直接走 SQL）。
	db *sql.DB
}

// ToolResult 归一化工具结果：Data 的 kind 键决定终态
// （denied/empty/failed→error/timeout，其余 ok）。
type ToolResult struct {
	Status Status
	Data   *Obj
	// ErrCode 失败归类（timeout/tool_error），成功态为空。
	ErrCode string
}

// deriveStatus handler 结果 → 状态（denied / empty / failed→error / timeout / ok）。
func deriveStatus(data *Obj) Status {
	switch data.get("kind") {
	case "denied":
		return StatusDenied
	case "empty":
		return StatusEmpty
	case "failed":
		return StatusError
	case "timeout":
		return StatusTimeout
	}
	return StatusOK
}

// guardWords 守卫词表（自 intent 收编）：写工具 + 守卫问句 → deny。
var guardWords = []string{"清空", "删除", "移除", "移出", "停用", "停止", "批量", "全部", "所有"}

// isGuarded 文本是否命中守卫词。
func isGuarded(text string) bool {
	for _, w := range guardWords {
		if strings.Contains(text, w) {
			return true
		}
	}
	return false
}

// decide 风险判定：allow / deny。P3 语义：写工具 + 守卫 → deny（审批面 P5 扩展）。
func decide(spec *ToolSpec, guarded bool) string {
	if spec.Group == GroupWrite && guarded {
		return "deny"
	}
	return "allow"
}

// denyResult 与 Python policy.DENY_RESULT 同形。
func denyResult() *Obj {
	obj := newObj()
	obj.set("kind", "denied")
	obj.set("note", "bulk_guard")
	obj.set("via", "propose_bulk")
	return obj
}

// readRowsLimit 卡片行截断线（与 Python _shared._READ_ROWS_LIMIT 一致）。
const readRowsLimit = 8
