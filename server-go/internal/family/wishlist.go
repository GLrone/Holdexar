package family

import (
	"context"
	"database/sql"
	"sort"
	"time"
)

type familyWish struct {
	gameInfo
	WantCount int      `json:"wantCount"`
	Members   []string `json:"members"`
	AddedAt   any      `json:"addedAt"`
}

func (s *Service) Wishlist(parent context.Context, requested string) (map[string]any, error) {
	ctx, done, err := s.work(parent)
	if err != nil {
		return nil, err
	}
	defer done()
	owner, err := s.owner(ctx, requested)
	if err != nil {
		return nil, err
	}
	group, err := readGroup(ctx, s.db, owner)
	if err != nil {
		return nil, err
	}
	ids := []string{}
	var name any
	if group != nil {
		name = optional(group.Name.String)
		for _, m := range payload(group, nil) {
			ids = append(ids, str(m["steamid"]))
		}
	}
	fallback := len(ids) == 0 && (group == nil || len(group.Members) == 0 && group.Group.String == "")
	rows, err := s.db.QueryContext(ctx, `SELECT appid,steamid,CAST(added_at AS TEXT) FROM wishlist_items
WHERE active=1 AND owned=0 AND (? OR steamid IN(SELECT value FROM json_each(?))) ORDER BY appid,steamid`, fallback, packed(ids))
	if err != nil {
		return nil, err
	}
	items := []familyWish{}
	appids := []int64{}
	var earliest sql.NullString
	for rows.Next() {
		var id int64
		var sid string
		var added sql.NullString
		if err = rows.Scan(&id, &sid, &added); err != nil {
			rows.Close()
			return nil, err
		}
		if len(items) == 0 || items[len(items)-1].AppID != id {
			items = append(items, familyWish{gameInfo: gameInfo{AppID: id}})
			appids = append(appids, id)
			earliest = sql.NullString{}
		}
		item := &items[len(items)-1]
		item.Members = append(item.Members, sid)
		item.WantCount++
		if added.Valid && (!earliest.Valid || added.String < earliest.String) {
			earliest = added
			item.AddedAt = iso(added)
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	meta, err := s.gameMetadata(ctx, appids)
	if err != nil {
		return nil, err
	}
	missing := []int64{}
	for i := range items {
		id := items[i].AppID
		info := meta[id]
		if !info.priced {
			missing = append(missing, id)
		}
		items[i].gameInfo = gameWithInfo(id, info, "")
	}
	sort.SliceStable(items, func(i, j int) bool { return items[i].WantCount > items[j].WantCount })
	s.kickWishlist(missing)
	return map[string]any{"fallback": fallback, "familyName": name, "memberIds": ids, "items": items}, nil
}

func (s *Service) kickWishlist(ids []int64) {
	if len(ids) == 0 || s.opts.OnUncatalogued == nil {
		return
	}
	s.mu.Lock()
	now := s.opts.Now()
	if s.closing || !s.wishlistKick.IsZero() && now.Sub(s.wishlistKick) < 30*time.Minute {
		s.mu.Unlock()
		return
	}
	s.wishlistKick = now
	s.mu.Unlock()
	if err := s.opts.OnUncatalogued(s.life, ids); err != nil {
		s.opts.Log("家庭愿望单补抓尚未启动 │ 游戏=%d，原因=%v", len(ids), err)
	}
}
