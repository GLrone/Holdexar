package family

import (
	"context"
	"database/sql"
)

type gameInfo struct {
	AppID    int64            `json:"appid"`
	Name     any              `json:"name"`
	Image    any              `json:"headerImage"`
	Release  any              `json:"releaseDate"`
	Tags     []map[string]any `json:"tags"`
	Price    any              `json:"cnPriceFen"`
	Original any              `json:"originalPriceFen"`
	Discount int              `json:"discount"`
	priced   bool
}

func (s *Service) gameMetadata(ctx context.Context, ids []int64) (map[int64]gameInfo, error) {
	games := make(map[int64]gameInfo, len(ids))
	if len(ids) == 0 {
		return games, nil
	}
	rows, err := s.db.QueryContext(ctx, `SELECT g.appid,g.name,g.header_image,g.release_date,p.cny_fen,p.original_price,COALESCE(p.discount_percent,0),p.appid IS NOT NULL
FROM games g LEFT JOIN game_current_prices p ON p.appid=g.appid AND p.region_code='CN'
WHERE g.appid IN (SELECT value FROM json_each(?))`, packed(ids))
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		info := gameInfo{Tags: []map[string]any{}}
		if err = rows.Scan(&info.AppID, &info.Name, &info.Image, &info.Release, &info.Price, &info.Original, &info.Discount, &info.priced); err != nil {
			rows.Close()
			return nil, err
		}
		games[info.AppID] = info
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	rows, err = s.db.QueryContext(ctx, `SELECT gt.appid,gt.tagid,t.name_zh,t.name_en FROM game_tags gt JOIN tags t ON t.tagid=gt.tagid
WHERE gt.appid IN (SELECT value FROM json_each(?)) AND (t.name_zh IS NOT NULL OR t.name_en IS NOT NULL) ORDER BY gt.appid,gt.weight DESC,gt.tagid`, packed(ids))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id, tag int64
		var zh, en any
		if err = rows.Scan(&id, &tag, &zh, &en); err != nil {
			return nil, err
		}
		if info, ok := games[id]; ok {
			info.Tags = append(info.Tags, map[string]any{"tagid": tag, "name": zh, "nameEn": en})
			games[id] = info
		}
	}
	return games, rows.Err()
}

func seedLibraryNames(ctx context.Context, conn *sql.Conn, names map[int64]string, stamp string) error {
	if len(names) == 0 {
		return nil
	}
	_, err := conn.ExecContext(ctx, `INSERT INTO games(appid,name,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,created_at,updated_at)
SELECT CAST(key AS INTEGER),value,0,0,0,0,0,0,0,0,0,0,0,0,0,?,? FROM json_each(?) WHERE value<>''
ON CONFLICT(appid) DO UPDATE SET name=excluded.name WHERE games.name=''`, stamp, stamp, packed(names))
	return err
}

func gameWithInfo(id int64, info gameInfo, fallback string) gameInfo {
	info.AppID = id
	if info.Name == nil || info.Name == "" {
		info.Name = optional(fallback)
	}
	if info.Tags == nil {
		info.Tags = []map[string]any{}
	}
	return info
}
