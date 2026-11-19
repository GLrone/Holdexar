package agent

import (
	"context"
	"errors"
	"reflect"
	"time"
)

var errNoFetch = errors.New("执行环境未注入读路径")

// 失败或超时统一归一化为 ToolResult（kind=failed/timeout），绝不中断整次调用。
func Execute(cctx *CallContext, spec *ToolSpec, guarded bool) *ToolResult {
	if decide(spec, guarded) == "deny" {
		return &ToolResult{Status: StatusDenied, Data: denyResult()}
	}
	if cctx.fetch == nil {
		cctx.fetch = noFetch
	}
	if cctx.post == nil {
		cctx.post = noPost
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

// typeName 提取错误类型名；普通 errorString 泛化为 "error"。
func typeName(cause any) string {
	t := reflect.TypeOf(cause)
	if t == nil {
		return "unknown"
	}
	if t.String() == "*errors.errorString" || t.String() == "errors.errorString" {
		return "error"
	}
	return t.String()
}

func noFetch(context.Context, string) (int, []byte, error) {
	return 0, nil, errNoFetch
}

func noPost(context.Context, string, []byte) (int, []byte, error) {
	return 0, nil, errNoFetch
}
