package wishlist

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"time"

	"holdexar/server-go/internal/store"
)

type membership struct {
	ID                              int64
	Added                           sql.NullString
	Active, Owned, Wished, Excluded bool
}

func (s *Service) SyncAccount(parent context.Context, sid string, autoCrawl bool) (map[string]any, error) {
	return s.syncAccount(parent, sid, autoCrawl, false)
}
func (s *Service) syncAccount(parent context.Context, sid string, autoCrawl, reserved bool) (map[string]any, error) {
	ctx, done, e := s.work(parent)
	if e != nil {
		return nil, e
	}
	defer done()
	if !reserved {
		s.mu.Lock()
		if s.active[sid] {
			s.mu.Unlock()
			return map[string]any{"steamid": sid, "inflight": true, "wishlistCount": 0, "ownedCount": 0, "added": 0, "addedOwned": 0, "active": 0, "newAppids": []int64{}, "newOwnedAppids": []int64{}}, nil
		}
		s.active[sid] = true
		s.mu.Unlock()
		defer s.finishSync(sid)
	}
	before, e := s.tracked(ctx, s.pool.DB, sid)
	if e != nil {
		return nil, e
	}
	kinds := syncKinds(before.Kinds)
	wish := []Wish{}
	if kinds["wishlist"] {
		wish, e = s.opts.Steam.Wishlist(ctx, sid)
		if e != nil {
			return nil, e
		}
	}
	owned := []OwnedGame{}
	source := ""
	var ownedError error
	if kinds["owned"] {
		owned, source, ownedError = s.opts.Steam.Owned(ctx, sid)
	}
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	profile := map[string]any{}
	if s.opts.Persona != nil {
		if data, e := s.opts.Persona(ctx, sid); e == nil {
			profile = data
		}
	}
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	newIDs, newOwned := []int64{}, []int64{}
	wishCrawl, ownedCrawl := []int64{}, []int64{}
	activeCount := 0
	e = s.pool.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		current, e := s.tracked(ctx, conn, sid)
		if e != nil {
			return e
		}
		if current.Created != before.Created {
			return fmt.Errorf("%w: %s", ErrAccountMissing, sid)
		}
		latest := syncKinds(current.Kinds)
		ownedSynced := kinds["owned"] && latest["owned"] && ownedError == nil
		ownedFailed := latest["owned"] && ownedError != nil
		rows, e := conn.QueryContext(ctx, `SELECT appid,CAST(added_at AS TEXT),active,owned,wishlisted,excluded FROM wishlist_items WHERE steamid=?`, sid)
		if e != nil {
			return e
		}
		items := map[int64]*membership{}
		for rows.Next() {
			row := &membership{}
			if e = rows.Scan(&row.ID, &row.Added, &row.Active, &row.Owned, &row.Wished, &row.Excluded); e != nil {
				rows.Close()
				return e
			}
			items[row.ID] = row
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return e
		}
		old := map[int64]bool{}
		for id := range items {
			old[id] = true
		}
		wishedIDs, ownedIDs := map[int64]bool{}, map[int64]bool{}
		if kinds["wishlist"] && latest["wishlist"] {
			for _, entry := range wish {
				wishedIDs[entry.AppID] = true
				row := items[entry.AppID]
				if row == nil {
					at := s.now()
					if entry.AddedAt != 0 {
						at = time.Unix(entry.AddedAt, 0).In(wishlistZone)
					}
					if at.Year() < 1 || at.Year() > 9999 {
						return fmt.Errorf("Steam 愿望单时间无效")
					}
					row = &membership{ID: entry.AppID, Added: sql.NullString{String: trackedStamp(at), Valid: true}, Active: true}
					items[entry.AppID] = row
					newIDs = append(newIDs, entry.AppID)
				}
				row.Wished = true
				if !row.Active && !row.Excluded {
					row.Active = true
				}
			}
		}
		if ownedSynced {
			for _, entry := range owned {
				ownedIDs[entry.AppID] = true
				row := items[entry.AppID]
				if !old[entry.AppID] {
					newOwned = append(newOwned, entry.AppID)
				}
				if row == nil {
					row = &membership{ID: entry.AppID, Added: sql.NullString{String: trackedStamp(s.now()), Valid: true}, Active: true}
					items[entry.AppID] = row
					newIDs = append(newIDs, entry.AppID)
				} else if !row.Active && !row.Excluded {
					row.Active = true
				}
			}
			for id, row := range items {
				row.Owned = ownedIDs[id]
			}
		}
		if len(wishedIDs) > 0 {
			for id, row := range items {
				if wishedIDs[id] {
					continue
				}
				row.Wished = false
				if ownedIDs[id] || !row.Active || ownedFailed && row.Owned {
					continue
				}
				row.Active = false
			}
		}
		ids := make([]int64, 0, len(items))
		for id := range items {
			ids = append(ids, id)
		}
		sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
		updates := [][7]any{}
		for _, id := range ids {
			row := items[id]
			var at any
			if row.Added.Valid {
				at = row.Added.String
			}
			updates = append(updates, [7]any{id, at, row.Active, row.Owned, row.Wished, row.Excluded, sid})
			if row.Active {
				activeCount++
			}
		}
		if len(updates) > 0 {
			raw, e := json.Marshal(updates)
			if e != nil {
				return e
			}
			_, e = conn.ExecContext(ctx, `INSERT INTO wishlist_items(steamid,appid,added_at,active,owned,wishlisted,excluded,manual,manual_pool,board_pool)
 SELECT json_extract(j.value,'$[6]'),CAST(json_extract(j.value,'$[0]') AS INTEGER),json_extract(j.value,'$[1]'),json_extract(j.value,'$[2]'),json_extract(j.value,'$[3]'),json_extract(j.value,'$[4]'),json_extract(j.value,'$[5]'),0,0,0 FROM json_each(?) j WHERE 1
 ON CONFLICT(steamid,appid) DO UPDATE SET active=excluded.active,owned=excluded.owned,wishlisted=excluded.wishlisted`, string(raw))
			if e != nil {
				return e
			}
		}
		states, e := s.pool.Monitor.SyncGameSourcesTx(ctx, conn, ids)
		if e != nil {
			return e
		}
		for _, id := range newIDs {
			if states[id] == "active" {
				if items[id].Wished {
					wishCrawl = append(wishCrawl, id)
				} else if items[id].Owned {
					ownedCrawl = append(ownedCrawl, id)
				}
			}
		}
		name, avatar := text(profile["persona_name"]), s.opts.Avatar(text(profile["avatar_url"]))
		_, e = conn.ExecContext(ctx, `UPDATE tracked_accounts SET last_sync_at=?,persona_name=CASE WHEN ?!='' THEN ? ELSE persona_name END,avatar_url=CASE WHEN ?!='' THEN ? ELSE avatar_url END WHERE steamid=?`, trackedStamp(s.now()), name, name, avatar, avatar, sid)
		return e
	})
	if e != nil {
		return nil, e
	}
	var failure any
	var ownedSource any
	if ownedError != nil {
		failure = ownedError.Error()
		s.opts.Log("已购清单未更新，本次保留既有数据 │ SteamID=%s，原因=%v", sid, failure)
	} else if source != "" {
		ownedSource = source
	}
	result := map[string]any{"steamid": sid, "wishlistCount": len(wish), "ownedCount": len(owned), "ownedSource": ownedSource, "ownedError": failure, "added": len(newIDs), "addedOwned": len(newOwned), "active": activeCount, "newAppids": newIDs, "newOwnedAppids": newOwned}
	if autoCrawl && len(newIDs) > 0 {
		triggered := false
		if s.opts.Crawl != nil && (len(wishCrawl) > 0 || len(ownedCrawl) > 0) {
			triggered, e = s.startPrices(ctx, wishCrawl, ownedCrawl)
			if e != nil {
				s.opts.Log("账号数据已保存，首次价格更新尚未开始 │ SteamID=%s，原因=%v", sid, e)
			}
		}
		result["crawlTriggered"] = triggered
	}
	s.opts.Log("Steam 账户同步完成 │ SteamID=%s，愿望单=%d，已购=%d，新增=%d，活跃=%d", sid, len(wish), len(owned), len(newIDs), activeCount)
	return result, nil
}
