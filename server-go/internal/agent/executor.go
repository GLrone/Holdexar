package agent

import (
	"context"
	"errors"
	"reflect"
	"time"
)

var errNoFetch = errors.New("执行环境未注入读路径")

// Execute 工具执行边界：策略判定 → 超时包裹 → 失败归一化。
//
// 铁律：工具失败 / 超时 = 归一化 ToolResult 回灌客户端，绝不终结调用。
// 结果形状（kind=failed/timeout）与现网执行器逐字对齐。
func Execute(cctx *CallContext, spec *ToolSpec, guarded bool) *ToolResult {
	if decide(spec, guarded) == "deny" {
		return &ToolResult{Status: StatusDenied, Data: denyResult()}
	}
	if cctx.fetch == nil {
		cctx.fetch = noFetch
	}
	type outcome struct {
		res *ToolResult
	}
	done := make(chan outcome, 1)
	budget := spec.TimeoutS
	if budget <= 0 {
		budget = 20.0
	}
	runCtx, cancel := context.WithTimeout(cctx.Ctx, time.Duration(budget*float64(time.Second)))
	defer cancel()
	cctx.Ctx = runCtx
	go func() {
		defer func() {
			if r := recover(); r != nil {
				done <- outcome{res: failedResult(spec.Name, r)}
			}
		}()
		res, err := spec.Handler(cctx)
		if err != nil {
			done <- outcome{res: failedResult(spec.Name, err)}
			return
		}
		done <- outcome{res: res}
	}()
	select {
	case <-runCtx.Done():
		if errors.Is(runCtx.Err(), context.Canceled) {
			// 调用方整体取消（连接断开）：按失败归一，等价「异常 = 结果」。
			return &ToolResult{Status: StatusError,
				Data:    failedData(spec.Name, "context canceled"),
				ErrCode: "tool_error"}
		}
		data := newObj()
		data.set("kind", "timeout")
		data.set("tool", spec.Name)
		data.set("budget_s", int(budget))
		return &ToolResult{Status: StatusTimeout, Data: data, ErrCode: "timeout"}
	case oc := <-done:
		res := oc.res
		if res == nil {
			return &ToolResult{Status: StatusOK, Data: newObj()}
		}
		if res.Data == nil {
			res.Data = newObj()
		}
		if res.Status == "" {
			res.Status = deriveStatus(res.Data)
		}
		return res
	}
}

// failedResult 异常归一化：kind=failed + 类型名注记（Python type(e).__name__
// 的等位；Go 侧承载的是 Go 错误类型名，属既定差异）。
func failedResult(name string, cause any) *ToolResult {
	return &ToolResult{Status: StatusError, Data: failedData(name, typeName(cause)), ErrCode: "tool_error"}
}

func failedData(name, note string) *Obj {
	data := newObj()
	data.set("kind", "failed")
	data.set("tool", name)
	data.set("note", note)
	return data
}

// typeName 异常类型名（Python type(e).__name__ 的等位投影）。
func typeName(cause any) string {
	t := reflect.TypeOf(cause)
	if t == nil {
		return "unknown"
	}
	return t.String()
}

func noFetch(context.Context, string) (int, []byte, error) {
	return 0, nil, errNoFetch
}
