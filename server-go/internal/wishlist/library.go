package wishlist

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"sort"
)

func (s *Service) primary(ctx context.Context, legacy bool) (string, error) {
	var sid string
	e := s.pool.DB.QueryRowContext(ctx, `SELECT steam_id FROM steam_accounts ORDER BY bound_at,steam_id LIMIT 1`).Scan(&sid)
	if e != nil && !errors.Is(e, sql.ErrNoRows) {
		return "", e
	}
	if sid == "" && legacy {
		var raw string
		e = s.pool.DB.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key='account.steam_id'`).Scan(&raw)
		if e != nil && !errors.Is(e, sql.ErrNoRows) {
			return "", e
		}
		if raw != "" {
			if e = json.Unmarshal([]byte(raw), &sid); e != nil {
				return "", e
			}
		}
	}
	return sid, nil
}
func (s *Service) Ownership(ctx context.Context, ids []int64) (map[string]any, error) {
	primary, e := s.primary(ctx, true)
	if e != nil {
		return nil, e
	}
	rows, e := s.pool.DB.QueryContext(ctx, `SELECT w.appid,w.owned,w.wishlisted,a.steamid,a.label,a.persona_name,a.avatar_url FROM wishlist_items w JOIN tracked_accounts a ON a.steamid=w.steamid WHERE w.active=1 AND w.appid IN(SELECT value FROM json_each(?)) ORDER BY a.created_at,a.steamid`, poolJSON(ids))
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	type group struct {
		kind           string
		names, avatars []string
	}
	groups := map[int64]*group{}
	rank := map[string]int{"wishlist": 1, "family": 2, "owned": 3}
	for rows.Next() {
		var id int64
		var owned, wished bool
		var sid string
		var label, name, avatar sql.NullString
		if e = rows.Scan(&id, &owned, &wished, &sid, &label, &name, &avatar); e != nil {
			return nil, e
		}
		kind := "wishlist"
		if owned {
			kind = "owned"
			if primary != "" && primary != sid {
				kind = "family"
			}
		} else if !wished {
			continue
		}
		who := label.String
		if who == "" {
			who = name.String
		}
		if who == "" {
			who = sid
		}
		if primary == sid {
			who = "我"
		}
		g := groups[id]
		if g == nil || rank[kind] > rank[g.kind] {
			g = &group{kind: kind, names: []string{}, avatars: []string{}}
			groups[id] = g
		}
		if g.kind == kind {
			g.names = append(g.names, who)
			g.avatars = append(g.avatars, s.opts.Avatar(avatar.String))
		}
	}
	if e = rows.Err(); e != nil {
		return nil, e
	}
	out := map[int64]any{}
	for id, g := range groups {
		out[id] = map[string]any{"type": g.kind, "owners": g.names, "ownerAvatars": g.avatars}
	}
	return map[string]any{"ownerships": out}, nil
}
func (s *Service) OwnedLibrary(ctx context.Context) (map[string]any, error) {
	accounts, e := s.Accounts(ctx)
	if e != nil {
		return nil, e
	}
	// Stable creation order is preserved for accounts with equal ownership counts.
	for i, j := 0, len(accounts)-1; i < j; i, j = i+1, j-1 {
		accounts[i], accounts[j] = accounts[j], accounts[i]
	}
	primary, e := s.primary(ctx, false)
	if e != nil {
		return nil, e
	}
	stats := map[string]map[string]any{}
	for _, a := range accounts {
		sid := a["steamid"].(string)
		a["isPrimary"] = primary != "" && sid == primary
		a["ownedCount"] = 0
		a["valueFen"] = int64(0)
		a["freeCount"] = 0
		delete(a, "itemCount")
		stats[sid] = a
	}
	rows, e := s.pool.DB.QueryContext(ctx, `SELECT w.appid,w.steamid,CAST(w.added_at AS TEXT),g.name,g.name_en,g.header_image,g.release_date,p.cny_fen,p.original_price,p.discount_percent FROM wishlist_items w LEFT JOIN games g ON g.appid=w.appid LEFT JOIN game_current_prices p ON p.appid=w.appid AND p.region_code='CN' WHERE w.active=1 AND w.owned=1 ORDER BY w.appid,w.added_at IS NULL,w.added_at,w.steamid`)
	if e != nil {
		return nil, e
	}
	games := []map[string]any{}
	index := map[int64]map[string]any{}
	ids := []int64{}
	for rows.Next() {
		var id int64
		var sid string
		var at, name, en, image, date sql.NullString
		var price, original, discount sql.NullInt64
		if e = rows.Scan(&id, &sid, &at, &name, &en, &image, &date, &price, &original, &discount); e != nil {
			rows.Close()
			return nil, e
		}
		g := index[id]
		if g == nil {
			g = map[string]any{"appid": id, "name": nullPoolString(name), "nameEn": nullPoolString(en), "headerImage": nullPoolString(image), "releaseDate": nullPoolString(date), "cnPriceFen": nullNumber(price), "originalPriceFen": nullNumber(original), "discount": discount.Int64, "tags": []map[string]any{}, "owners": []map[string]any{}}
			index[id] = g
			games = append(games, g)
			ids = append(ids, id)
		}
		stamp, _ := poolISO(at)
		g["owners"] = append(g["owners"].([]map[string]any), map[string]any{"steamid": sid, "addedAt": stamp})
		if a := stats[sid]; a != nil {
			a["ownedCount"] = a["ownedCount"].(int) + 1
			if price.Valid {
				a["valueFen"] = a["valueFen"].(int64) + price.Int64
				if price.Int64 == 0 {
					a["freeCount"] = a["freeCount"].(int) + 1
				}
			}
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	if len(ids) > 0 {
		rows, e = s.pool.DB.QueryContext(ctx, `SELECT gt.appid,gt.tagid,t.name_zh,t.name_en FROM game_tags gt JOIN tags t ON t.tagid=gt.tagid WHERE gt.appid IN(SELECT value FROM json_each(?)) AND (t.name_zh IS NOT NULL OR t.name_en IS NOT NULL) ORDER BY gt.appid,gt.weight DESC,gt.tagid`, poolJSON(ids))
		if e != nil {
			return nil, e
		}
		for rows.Next() {
			var id, tag int64
			var zh, en sql.NullString
			if e = rows.Scan(&id, &tag, &zh, &en); e != nil {
				rows.Close()
				return nil, e
			}
			g := index[id]
			g["tags"] = append(g["tags"].([]map[string]any), map[string]any{"tagid": tag, "name": nullPoolString(zh), "nameEn": nullPoolString(en)})
		}
		e = rows.Err()
		rows.Close()
		if e != nil {
			return nil, e
		}
	}
	sort.SliceStable(accounts, func(i, j int) bool {
		a, b := accounts[i], accounts[j]
		if a["isPrimary"] != b["isPrimary"] {
			return a["isPrimary"].(bool)
		}
		return a["ownedCount"].(int) > b["ownedCount"].(int)
	})
	return map[string]any{"accounts": accounts, "games": games, "generatedAt": s.now().Format("2006-01-02T15:04:05.000000")}, nil
}
func nullNumber(n sql.NullInt64) any {
	if n.Valid {
		return n.Int64
	}
	return nil
}
