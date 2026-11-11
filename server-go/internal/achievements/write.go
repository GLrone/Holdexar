package achievements

import (
	"context"
	"database/sql"
	"encoding/json"

	"holdexar/server-go/internal/store"
)

func (s *SyncService) stamp() string {
	return s.opts.Now().UTC().Format("2006-01-02 15:04:05.000000")
}

func (s *SyncService) write(ctx context.Context, owner syncOwner, body func(*sql.Conn) error) error {
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := owner.check(ctx, conn); err != nil {
			return err
		}
		return body(conn)
	})
}

func (s *SyncService) writeOwned(ctx context.Context, owner syncOwner, owned []OwnedGame) error {
	rows := make([][4]any, 0, len(owned))
	for _, game := range owned {
		if game.AppID > 0 {
			rows = append(rows, [4]any{game.AppID, game.Name, game.Playtime, game.LastPlayed})
		}
	}
	if len(rows) == 0 {
		return nil
	}
	raw, err := json.Marshal(rows)
	if err != nil {
		return err
	}
	return s.write(ctx, owner, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `INSERT INTO achievement_games(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at)
			SELECT ?,json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]'),json_extract(value,'$[3]'),0,0,'owned',?
			FROM json_each(?) WHERE true ON CONFLICT(steamid,appid) DO UPDATE SET
			name=COALESCE(NULLIF(excluded.name,''),achievement_games.name,''),playtime_min=excluded.playtime_min,
			last_played=excluded.last_played,source='owned',updated_at=excluded.updated_at`, owner.id, s.stamp(), string(raw))
		return err
	})
}

func (s *SyncService) writeProgress(ctx context.Context, owner syncOwner, owned map[int64]bool, shared map[int64]string, sharedKnown bool, progress map[int64]Progress) error {
	batch := make([][5]any, 0, len(progress))
	for appID, p := range progress {
		name, external := shared[appID]
		if p.Total < 0 || p.Unlocked < 0 || p.Unlocked > p.Total || (!owned[appID] && !external) {
			continue
		}
		source := "shared"
		if owned[appID] {
			source = "owned"
		}
		batch = append(batch, [5]any{appID, p.Total, p.Unlocked, source, name})
	}
	ids := make([]int64, 0, len(shared))
	for id := range shared {
		ids = append(ids, id)
	}
	raw, err := json.Marshal(batch)
	if err != nil {
		return err
	}
	candidates, err := json.Marshal(ids)
	if err != nil {
		return err
	}
	return s.write(ctx, owner, func(conn *sql.Conn) error {
		stamp := s.stamp()
		_, err := conn.ExecContext(ctx, `INSERT INTO achievement_games(steamid,appid,name,playtime_min,last_played,total_achievements,unlocked,source,updated_at)
			SELECT ?,json_extract(p.value,'$[0]'),json_extract(p.value,'$[4]'),0,0,
			json_extract(p.value,'$[1]'),json_extract(p.value,'$[2]'),json_extract(p.value,'$[3]'),?
			FROM json_each(?) p WHERE json_extract(p.value,'$[3]')='owned' OR json_extract(p.value,'$[2]')>0
			OR EXISTS(SELECT 1 FROM achievement_games g WHERE g.steamid=? AND g.appid=json_extract(p.value,'$[0]'))
			ON CONFLICT(steamid,appid) DO UPDATE SET name=COALESCE(NULLIF(achievement_games.name,''),excluded.name),
			total_achievements=excluded.total_achievements,unlocked=excluded.unlocked,
			source=CASE WHEN achievement_games.source='owned' THEN 'owned' ELSE excluded.source END,updated_at=excluded.updated_at`,
			owner.id, stamp, string(raw), owner.id)
		if err != nil {
			return err
		}
		const staleShared = `steamid=? AND source='shared' AND (unlocked=0 OR (? AND appid NOT IN (SELECT value FROM json_each(?))))`
		_, err = conn.ExecContext(ctx, `DELETE FROM achievement_states WHERE steamid=? AND appid IN (SELECT appid FROM achievement_games WHERE `+staleShared+`)`,
			owner.id, owner.id, sharedKnown, string(candidates))
		if err != nil {
			return err
		}
		_, err = conn.ExecContext(ctx, `DELETE FROM achievement_games WHERE `+staleShared, owner.id, sharedKnown, string(candidates))
		return err
	})
}

func (s *SyncService) savedDefinitions(ctx context.Context, ids []int64) (map[int64][]Definition, error) {
	out := map[int64][]Definition{}
	if len(ids) == 0 {
		return out, nil
	}
	raw, err := json.Marshal(ids)
	if err != nil {
		return nil, err
	}
	rows, err := s.db.QueryContext(ctx, `SELECT appid,image_name,COALESCE(apiname,''),COALESCE(name,''),COALESCE(description,''),
		COALESCE(icon_url,''),COALESCE(icon_gray_url,''),global_percent,display_order
		FROM achievement_defs WHERE appid IN (SELECT value FROM json_each(?)) ORDER BY appid,display_order,image_name`, string(raw))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var d Definition
		if err := rows.Scan(&id, &d.ImageName, &d.APIName, &d.Name, &d.Description, &d.IconURL, &d.GrayIconURL, &d.Percent, &d.Order); err != nil {
			return nil, err
		}
		out[id] = append(out[id], d)
	}
	return out, rows.Err()
}

func (s *SyncService) writeDetails(ctx context.Context, owner syncOwner, batch []syncedDetail) error {
	defs, states, changes := [][9]any{}, [][5]any{}, [][3]any{}
	for _, detail := range batch {
		id := detail.game.appID
		var total, unlocked any
		if detail.newDefs {
			total = len(detail.defs)
			for _, def := range detail.defs {
				defs = append(defs, [9]any{id, def.ImageName, def.APIName, def.Name, def.Description, def.IconURL, def.GrayIconURL, def.Percent, def.Order})
			}
		}
		count := 0
		for image, state := range detail.states {
			if state.Achieved {
				count++
			}
			states = append(states, [5]any{id, image, state.Achieved, state.UnlockTime, state.GrayIconURL})
		}
		if len(detail.states) != 0 && len(detail.states) == len(detail.defs) && (detail.newDefs || len(detail.states) == detail.game.total) {
			unlocked = count
		}
		if total != nil || unlocked != nil {
			changes = append(changes, [3]any{id, total, unlocked})
		}
	}
	if len(defs) == 0 && len(states) == 0 && len(changes) == 0 {
		return nil
	}
	packed := make([]string, 0, 3)
	for _, value := range []any{defs, states, changes} {
		raw, err := json.Marshal(value)
		if err != nil {
			return err
		}
		packed = append(packed, string(raw))
	}
	return s.write(ctx, owner, func(conn *sql.Conn) error {
		stamp := s.stamp()
		_, err := conn.ExecContext(ctx, `INSERT INTO achievement_defs(appid,image_name,apiname,name,description,icon_url,icon_gray_url,global_percent,display_order,updated_at)
			SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),NULLIF(json_extract(value,'$[2]'),''),
			json_extract(value,'$[3]'),json_extract(value,'$[4]'),json_extract(value,'$[5]'),NULLIF(json_extract(value,'$[6]'),''),
			json_extract(value,'$[7]'),json_extract(value,'$[8]'),?
			FROM json_each(?) WHERE true ON CONFLICT(appid,image_name) DO UPDATE SET
			apiname=COALESCE(excluded.apiname,achievement_defs.apiname),
			name=COALESCE(NULLIF(excluded.name,''),achievement_defs.name),
			description=COALESCE(NULLIF(excluded.description,''),achievement_defs.description),icon_url=excluded.icon_url,
			icon_gray_url=COALESCE(excluded.icon_gray_url,achievement_defs.icon_gray_url),global_percent=excluded.global_percent,
			display_order=excluded.display_order,updated_at=excluded.updated_at`, stamp, packed[0])
		if err != nil {
			return err
		}
		_, err = conn.ExecContext(ctx, `INSERT INTO achievement_states(steamid,appid,image_name,achieved,unlock_time,synced_at)
			SELECT ?,json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]'),json_extract(value,'$[3]'),?
			FROM json_each(?) WHERE true ON CONFLICT(steamid,appid,image_name) DO UPDATE SET
			achieved=excluded.achieved,unlock_time=excluded.unlock_time,synced_at=excluded.synced_at`, owner.id, stamp, packed[1])
		if err != nil {
			return err
		}
		_, err = conn.ExecContext(ctx, `UPDATE achievement_defs AS d SET icon_gray_url=json_extract(p.value,'$[4]') FROM json_each(?) p
			WHERE d.appid=json_extract(p.value,'$[0]') AND d.image_name=json_extract(p.value,'$[1]')
			AND COALESCE(d.icon_gray_url,'')='' AND json_extract(p.value,'$[4]')<>''`, packed[1])
		if err != nil {
			return err
		}
		_, err = conn.ExecContext(ctx, `UPDATE achievement_games AS g SET
			total_achievements=COALESCE(json_extract(p.value,'$[1]'),g.total_achievements),
			unlocked=COALESCE(json_extract(p.value,'$[2]'),g.unlocked),
			schema_synced_at=CASE WHEN json_extract(p.value,'$[1]') IS NULL THEN g.schema_synced_at ELSE ? END,
			state_synced_at=CASE WHEN json_extract(p.value,'$[2]') IS NULL THEN g.state_synced_at ELSE ? END,
			updated_at=? FROM json_each(?) p WHERE g.steamid=? AND g.appid=json_extract(p.value,'$[0]')`,
			stamp, stamp, stamp, packed[2], owner.id)
		return err
	})
}
