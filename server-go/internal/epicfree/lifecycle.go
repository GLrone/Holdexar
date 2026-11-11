package epicfree

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"

	"holdexar/server-go/internal/store"
)

const ModuleEnabledKey = "module.epic_free.enabled"

var ErrDisabled = errors.New("Epic 免费游戏功能已停用")

func (s *Service) Initialize(ctx context.Context) error {
	_, err := s.Enabled(ctx)
	return err
}

func (s *Service) Enabled(ctx context.Context) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.initialized {
		var raw string
		err := s.db.QueryRowContext(ctx, "SELECT value_json FROM app_settings WHERE key=?", ModuleEnabledKey).Scan(&raw)
		if err != nil && !errors.Is(err, sql.ErrNoRows) {
			return false, err
		}
		if err == nil {
			if err := json.Unmarshal([]byte(raw), &s.enabled); err != nil {
				return false, err
			}
		}
		if !s.enabled {
			s.stopModule()
		}
		s.initialized = true
	}
	return s.enabled, nil
}

func (s *Service) begin(parent context.Context) (context.Context, func(), error) {
	if _, err := s.Enabled(parent); err != nil {
		return nil, nil, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if !s.enabled {
		return nil, nil, ErrDisabled
	}
	if s.closed || s.life.Err() != nil {
		return nil, nil, context.Canceled
	}
	ctx, cancel := context.WithCancel(parent)
	stop := context.AfterFunc(s.module, cancel)
	s.jobs.Add(1)
	s.operations++
	return ctx, func() {
		stop()
		cancel()
		s.mu.Lock()
		s.operations--
		s.mu.Unlock()
		s.jobs.Done()
	}, nil
}

func (s *Service) State(ctx context.Context) (bool, bool, error) {
	if _, err := s.Enabled(ctx); err != nil {
		return false, false, err
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.enabled, !s.closed && (s.enabled || s.active != nil || s.operations > 0), nil
}

func (s *Service) SetEnabled(ctx context.Context, enabled bool) error {
	s.transition.Lock()
	defer s.transition.Unlock()
	on, err := s.Enabled(ctx)
	if err != nil {
		return err
	}
	s.mu.Lock()
	closed := s.closed || s.life.Err() != nil
	s.mu.Unlock()
	if closed {
		return context.Canceled
	}
	if !on {
		if err := s.wait(ctx); err != nil {
			return err
		}
	}
	if on != enabled {
		raw, _ := json.Marshal(enabled)
		if err := s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
			_, err := conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,CURRENT_TIMESTAMP)
				ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, ModuleEnabledKey, string(raw))
			return err
		}); err != nil {
			return err
		}
		s.mu.Lock()
		s.enabled = enabled
		if enabled {
			s.module, s.stopModule = context.WithCancel(s.life)
		} else {
			s.stopModule()
		}
		s.mu.Unlock()
	}
	if !enabled {
		if err := s.wait(ctx); err != nil {
			return err
		}
		s.client.CloseIdleConnections()
	}
	return nil
}

func (s *Service) wait(ctx context.Context) error {
	done := make(chan struct{})
	go func() { s.jobs.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
