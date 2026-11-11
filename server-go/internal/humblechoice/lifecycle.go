package humblechoice

import (
	"context"
	"errors"
)

// ModuleEnabledKey 内部模块开关键（与 Epic 模块同形，app_settings 单键布尔）。
const ModuleEnabledKey = "module.humble_choice.enabled"

// ErrDisabled 模块停用后的统一拒因。
var ErrDisabled = errors.New("Humble Choice 每月合集功能已停用")

// SetEnabled 切换模块开关；关闭时等在途一次执行收尾（slot 串行化，
// 借位即等待），此后不再有新执行。
func (s *Service) SetEnabled(ctx context.Context, enabled bool) error {
	if e := s.ctl.SetEnabled(ctx, s.db, s.gate, enabled); e != nil {
		return e
	}
	if !enabled {
		select {
		case s.slot <- struct{}{}:
			<-s.slot
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	s.opts.Log("Humble Choice 功能状态已更新 │ 开启=%t", enabled)
	return nil
}

// State 返回开关与运行态（在途执行占位中 = running）。
func (s *Service) State(ctx context.Context) (bool, bool, error) {
	enabled, e := s.ctl.Enabled(ctx, s.db)
	if e != nil {
		return false, false, e
	}
	return enabled, s.busy.Load(), nil
}

func (s *Service) moduleEnabled(ctx context.Context) (bool, error) {
	return s.ctl.Enabled(ctx, s.db)
}
