package account

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"slices"
	"strings"

	"holdexar/server-go/internal/store"
)

func profileGame(name string) string {
	chars := []rune(strings.TrimSpace(name))
	return string(chars[:min(200, len(chars))])
}

type onlineUpdate struct {
	Online          bool
	InGame          string
	SteamID         string
	Bound, Previous *string
}

func (s *Service) refreshOnline(ctx context.Context, rows []accountRow) error {
	if len(rows) == 0 || s.opts.APIKey == nil || s.opts.Summaries == nil {
		return nil
	}
	key, err := s.opts.APIKey(ctx)
	if err != nil {
		return errors.New("Steam 在线状态凭据读取失败")
	}
	if key == "" {
		return nil
	}
	var firstErr error
	updates := make([]onlineUpdate, 0, len(rows))
	for group := range slices.Chunk(rows, 100) {
		ids := make([]string, 0, len(group))
		for _, row := range group {
			ids = append(ids, row.ID)
		}
		states, err := s.opts.Summaries(ctx, ids, key)
		if err == nil && len(states) == 0 {
			err = errors.New("Steam 未返回可用的在线状态")
		}
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if firstErr == nil {
				firstErr = err
			}
			continue
		}
		for _, row := range group {
			state := states[row.ID]
			update := onlineUpdate{SteamID: row.ID, Online: state.State > 0, InGame: profileGame(state.InGame)}
			if row.Bound.Valid {
				update.Bound = &row.Bound.String
			}
			if row.OnlineChecked.Valid {
				update.Previous = &row.OnlineChecked.String
			}
			updates = append(updates, update)
		}
	}
	if len(updates) == 0 {
		return firstErr
	}
	raw, err := json.Marshal(updates)
	if err != nil {
		return err
	}
	err = s.gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE steam_accounts AS a SET is_online=value->>'Online',in_game=value->>'InGame',online_checked_at=?
 FROM json_each(?) WHERE a.steam_id=value->>'SteamID' AND CAST(a.bound_at AS TEXT) IS value->>'Bound'
 AND CAST(a.online_checked_at AS TEXT) IS value->>'Previous'`, accountStamp(s.now()), string(raw))
		return err
	})
	return errors.Join(firstErr, err)
}
