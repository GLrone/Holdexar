package pilot

import (
	"context"
	"errors"
	"time"
)

const ModuleEnabledKey = "module.pilot.enabled"

var ErrModuleDisabled = errors.New("领航员功能已停用，可在设置 → 插件中开启")

// SetEnabled 切换开关；关闭时等待全部在飞轮次收尾，此后拒绝新发问。
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
