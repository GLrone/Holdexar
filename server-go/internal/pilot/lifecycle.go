package pilot

import (
	"context"
	"errors"
	"time"

	"holdexar/server-go/internal/modulectl"
)

// ModuleEnabledKey 内部模块开关键（与 Epic/Humble/成就同形）。
const ModuleEnabledKey = "module.pilot.enabled"

// ErrModuleDisabled 模块停用后的统一拒因（路由层转 409 用户语言）。
var ErrModuleDisabled = errors.New("领航员功能已停用，可在设置 → 插件中开启")

// SetEnabled 切换模块开关；关闭时等全部在飞轮收尾（领航员无定时任务，
// 在飞轮是唯一执行形态），此后发问入口一律拒绝。
func (s *Service) SetEnabled(ctx context.Context, enabled bool) error {
	if e := s.ctl.SetEnabled(ctx, s.deps.DB, s.gate, enabled); e != nil {
		return e
	}
	if !enabled {
		for {
			if s.inflightTotal() == 0 {
				break
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(50 * time.Millisecond):
			}
		}
	}
	s.log("领航员功能状态已更新 │ 开启=%t", enabled)
	return nil
}

// State 返回开关与运行态（任一会话在飞 = running）。
func (s *Service) State(ctx context.Context) (bool, bool, error) {
	enabled, e := s.ctl.Enabled(ctx, s.deps.DB)
	if e != nil {
		return false, false, e
	}
	return enabled, s.inflightTotal() > 0, nil
}

func (s *Service) moduleEnabled(ctx context.Context) (bool, error) {
	return s.ctl.Enabled(ctx, s.deps.DB)
}
