package achievements

import (
	"context"
	"errors"

	"holdexar/server-go/internal/modulectl"
)

const ModuleEnabledKey = "module.achievements.enabled"

var ErrModuleDisabled = errors.New("成就功能已停用，可在设置 → 插件中开启")

// SetEnabled 切换开关；关闭时等待在途同步完成，之后不再发起新同步。
func (s *SyncService) SetEnabled(ctx context.Context, enabled bool) error {
	if e := s.ctl.SetEnabled(ctx, s.db, s.gate, enabled); e != nil {
		return e
	}
	if !enabled {
		if f := s.flightSnapshot(); f != nil {
			select {
			case <-f.done:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
	}
	s.opts.Log("成就功能状态已更新 │ 开启=%t", enabled)
	return nil
}

func (s *SyncService) State(ctx context.Context) (bool, bool, error) {
	enabled, e := s.ctl.Enabled(ctx, s.db)
	if e != nil {
		return false, false, e
	}
	return enabled, s.flightSnapshot() != nil, nil
}

func (s *SyncService) moduleEnabled(ctx context.Context) (bool, error) {
	return s.ctl.Enabled(ctx, s.db)
}

var _ = modulectl.New
