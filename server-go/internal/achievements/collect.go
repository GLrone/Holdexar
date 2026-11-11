package achievements

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"slices"
	"sync"
	"time"
)

const syncParallel = 5

type syncedGame struct {
	appID                  int64
	name                   string
	total, unlocked        int
	updated, schema, state sql.NullString
}

func (s *SyncService) games(ctx context.Context, sid string) (map[int64]syncedGame, []int64, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT appid,COALESCE(name,''),total_achievements,unlocked,
		CAST(updated_at AS TEXT),CAST(schema_synced_at AS TEXT),CAST(state_synced_at AS TEXT)
		FROM achievement_games WHERE steamid=? ORDER BY playtime_min DESC,appid`, sid)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	games := map[int64]syncedGame{}
	var ids []int64
	for rows.Next() {
		var row syncedGame
		if err := rows.Scan(&row.appID, &row.name, &row.total, &row.unlocked, &row.updated, &row.schema, &row.state); err != nil {
			return nil, nil, err
		}
		games[row.appID] = row
		ids = append(ids, row.appID)
	}
	return games, ids, rows.Err()
}

func within(now time.Time, raw string, limit time.Duration) bool {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999"} {
		if stamp, err := time.Parse(layout, raw); err == nil {
			age := now.Sub(stamp)
			return age >= 0 && age < limit
		}
	}
	return false
}

func (s *SyncService) credentials(ctx context.Context, owner syncOwner) (Credentials, error) {
	var creds Credentials
	var keyErr, tokenErr error
	if s.opts.APIKey != nil {
		creds.Key, keyErr = s.opts.APIKey(ctx)
	}
	if owner.tokenOwner != "" && s.opts.Token != nil {
		creds.Token, tokenErr = s.opts.Token(ctx, owner.tokenOwner)
	}
	if err := ctx.Err(); err != nil {
		return creds, err
	}
	if owner.id == "" || (creds.Key == "" && creds.Token == "") {
		if keyErr != nil || tokenErr != nil {
			s.opts.Log("Steam 凭据不可用 │ SteamID=%s", owner.id)
		}
		return creds, errNoCredential
	}
	return creds, nil
}

func (s *SyncService) syncAll(ctx context.Context, owner syncOwner, snap *Snapshot) error {
	creds, err := s.credentials(ctx, owner)
	if err != nil {
		return err
	}
	previous, _, err := s.games(ctx, owner.id)
	if err != nil {
		return err
	}
	owned, err := s.opts.Steam.Owned(ctx, owner.id, creds)
	if err != nil {
		return fmt.Errorf("读取已购名册：%w", err)
	}
	if err = s.writeOwned(ctx, owner, owned); err != nil {
		return err
	}
	ownedIDs := make(map[int64]bool, len(owned))
	for _, game := range owned {
		ownedIDs[game.AppID] = true
	}
	shared := map[int64]string{}
	sharedKnown := false
	if s.opts.Shared != nil {
		shared, sharedKnown, err = s.opts.Shared(ctx, owner.id)
		if err != nil {
			s.opts.Log("家庭共享成就候选不可用，保留原有库外成就 │ SteamID=%s", owner.id)
		}
	}
	candidates := make([]int64, 0, len(ownedIDs)+len(shared))
	for id := range ownedIDs {
		candidates = append(candidates, id)
	}
	for id := range shared {
		if !ownedIDs[id] {
			candidates = append(candidates, id)
		}
	}
	slices.Sort(candidates)
	snap.Stage, snap.Total = "progress", len(candidates)
	if err := s.saveSnapshot(ctx, *snap); err != nil {
		return err
	}
	progress := map[int64]Progress{}
	rawStates := map[int64]map[string]State{}
	if len(candidates) != 0 {
		progress, err = s.opts.Steam.Progress(ctx, owner.id, creds, candidates)
		if err != nil {
			if creds.Key == "" || ctx.Err() != nil {
				return fmt.Errorf("读取成就进度：%w", err)
			}
			if progress == nil {
				progress = map[int64]Progress{}
			}
			s.opts.Log("成就批量进度不可用，尝试单游戏接口 │ SteamID=%s", owner.id)
			if err = s.fallbackProgress(ctx, owner.id, creds, candidates, previous, progress, rawStates, snap); err != nil {
				return err
			}
		}
	}
	if err = s.writeProgress(ctx, owner, ownedIDs, shared, sharedKnown, progress); err != nil {
		return err
	}
	if err = s.details(ctx, owner, creds, previous, rawStates, snap); err != nil {
		return err
	}
	if s.opts.Award != nil {
		if err = s.opts.Award(ctx, owner.id); err != nil {
			s.opts.Log("成就经验或挑战进度暂未更新 │ SteamID=%s，原因=%v", owner.id, err)
		}
	}
	return ctx.Err()
}

func (s *SyncService) fallbackProgress(ctx context.Context, sid string, creds Credentials, candidates []int64, previous map[int64]syncedGame, progress map[int64]Progress, rawStates map[int64]map[string]State, snap *Snapshot) error {
	now := s.opts.Now()
	todo := make([]int64, 0, len(candidates))
	for _, id := range candidates {
		if _, known := progress[id]; !known && !within(now, previous[id].updated.String, time.Hour) {
			todo = append(todo, id)
		}
	}
	snap.Total = len(todo)
	rejected := 0
	for batch := range slices.Chunk(todo, syncParallel) {
		states := make([]map[string]State, len(batch))
		failures := make([]error, len(batch))
		parallelBatch(batch, func(i int, id int64) {
			states[i], failures[i] = s.opts.Steam.PlayerStates(ctx, sid, id, creds)
		})
		if err := ctx.Err(); err != nil {
			return err
		}
		for i, raw := range states {
			if failures[i] == steamStatus(401) || failures[i] == steamStatus(403) {
				rejected++
			}
			if len(raw) == 0 {
				continue
			}
			id := batch[i]
			rawStates[id] = raw
			p := Progress{Total: len(raw)}
			for _, state := range raw {
				if state.Achieved {
					p.Unlocked++
				}
			}
			progress[id] = p
		}
		snap.Done += len(batch)
		if snap.Done%25 == 0 || snap.Done == snap.Total {
			if err := s.saveSnapshot(ctx, *snap); err != nil {
				return err
			}
		}
	}
	if len(todo) > 0 && rejected == len(todo) && len(progress) == 0 {
		return errNoCredential
	}
	return nil
}

func parallelBatch[T any](batch []T, fn func(int, T)) {
	var group sync.WaitGroup
	for i, value := range batch {
		group.Go(func() { fn(i, value) })
	}
	group.Wait()
}

type syncedDetail struct {
	game    syncedGame
	defs    []Definition
	newDefs bool
	states  map[string]State
}

func (s *SyncService) details(ctx context.Context, owner syncOwner, creds Credentials, previous map[int64]syncedGame, rawStates map[int64]map[string]State, snap *Snapshot) error {
	current, ids, err := s.games(ctx, owner.id)
	if err != nil {
		return err
	}
	todo := make([]int64, 0, len(ids))
	now := s.opts.Now()
	for _, id := range ids {
		row, old := current[id], previous[id]
		if row.total > 0 && (row.total != old.total || row.unlocked != old.unlocked || !within(now, old.state.String, 24*time.Hour)) {
			todo = append(todo, id)
		}
	}
	snap.Stage, snap.Total, snap.Done = "details", len(todo), 0
	if err = s.saveSnapshot(ctx, *snap); err != nil {
		return err
	}
	defs, err := s.savedDefinitions(ctx, todo)
	if err != nil {
		return err
	}
	for batch := range slices.Chunk(todo, syncParallel) {
		results := make([]syncedDetail, len(batch))
		parallelBatch(batch, func(i int, id int64) {
			results[i] = s.detail(ctx, owner.id, creds, current[id], defs[id], rawStates[id], now)
		})
		if err := ctx.Err(); err != nil {
			return err
		}
		if err := s.writeDetails(ctx, owner, results); err != nil {
			return err
		}
		snap.Done += len(batch)
		last := results[len(results)-1].game
		snap.Current = last.name
		if snap.Current == "" {
			snap.Current = fmt.Sprint(last.appID)
		}
		if err := s.saveSnapshot(ctx, *snap); err != nil {
			return err
		}
	}
	return nil
}

func (s *SyncService) detail(ctx context.Context, sid string, creds Credentials, row syncedGame, defs []Definition, raw map[string]State, now time.Time) syncedDetail {
	result := syncedDetail{game: row, defs: defs}
	missingNames := creds.Key != "" && slices.ContainsFunc(defs, func(d Definition) bool { return d.APIName == "" })
	if len(defs) != row.total || !within(now, row.schema.String, 7*24*time.Hour) || missingNames {
		found, err := s.opts.Steam.Definitions(ctx, row.appID, creds)
		if err == nil && len(found) > 0 {
			result.defs, result.newDefs = found, true
		} else if err != nil && !errors.Is(err, context.Canceled) {
			s.opts.Log("游戏成就定义未更新 │ AppID=%d，原因=%v", row.appID, err)
		}
	}
	if len(result.defs) == 0 {
		return result
	}
	if len(raw) != 0 {
		result.states = MatchAPIStates(raw, result.defs)
	} else {
		states, err := s.opts.Steam.States(ctx, sid, row.appID, creds, result.defs)
		if err != nil && !errors.Is(err, context.Canceled) {
			s.opts.Log("游戏成就状态未更新 │ SteamID=%s，AppID=%d，原因=%v", sid, row.appID, err)
		}
		result.states = states
	}
	return result
}
