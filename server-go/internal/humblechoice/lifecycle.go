package humblechoice

import (
	"context"
	"errors"
)

const ModuleEnabledKey = "module.humble_choice.enabled"

var ErrDisabled = errors.New("Humble Choice 每月合集功能已停用")

// SetEnabled 停用时经 slot 等待在途执行收尾。
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
