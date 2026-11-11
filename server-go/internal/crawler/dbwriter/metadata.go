package dbwriter

import (
	"context"
	"database/sql"
	"encoding/json"
	"sort"
	"strings"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

func (w *Writer) ClearMissingRegionsBatch(ctx context.Context, pairs []RegionPair) (int, error) {
	if len(pairs) == 0 {
		return 0, nil
	}
	unique := map[missingKey]bool{}
	clean := []RegionPair{}
	ids := []int64{}
	seenIDs := map[int64]bool{}
	values := [][2]any{}
	for _, pair := range pairs {
		pair.RegionCode = strings.ToUpper(pair.RegionCode)
		key := missingKey{pair.AppID, pair.RegionCode}
		if unique[key] {
			continue
		}
		unique[key] = true
		clean = append(clean, pair)
		values = append(values, [2]any{pair.AppID, pair.RegionCode})
		if !seenIDs[pair.AppID] {
			seenIDs[pair.AppID] = true
			ids = append(ids, pair.AppID)
		}
	}
	raw, err := json.Marshal(values)
	if err != nil {
		return 0, err
	}
	count := int64(0)
	err = w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, err := conn.ExecContext(ctx, `DELETE FROM game_current_prices WHERE price_status='missing' AND (appid,region_code) IN
 (SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') FROM json_each(?))`, string(raw))
		if err != nil {
			return err
		}
		count, err = result.RowsAffected()
		if err != nil {
			return err
		}
		derived.SetClock(w.Now)
		_, err = derived.UpdatePriceDerived(ctx, conn, ids, derived.Options{Now: w.Now(), History: true, Overrides: w.SeriesOverrides})
		return err
	})
	if err != nil {
		sort.Slice(clean, func(i, j int) bool {
			if clean[i].AppID == clean[j].AppID {
				return clean[i].RegionCode < clean[j].RegionCode
			}
			return clean[i].AppID < clean[j].AppID
		})
		return 0, &RegionStatusWriteError{Pairs: clean}
	}
	return int(count), nil
}

func (w *Writer) MarkNonGameTypesBatch(ctx context.Context, types map[int64]string) error {
	if len(types) == 0 {
		return nil
	}
	ids := make([]int64, 0, len(types))
	for id := range types {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	values := make([][2]any, 0, len(ids))
	for _, id := range ids {
		var typ any
		upper := strings.ToUpper(types[id])
		if upper != "" {
			typ = upper
		}
		values = append(values, [2]any{id, typ})
	}
	raw, err := json.Marshal(values)
	if err != nil {
		return err
	}
	return w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		before, err := derived.CatalogInputs(ctx, conn, ids)
		if err != nil {
			return err
		}
		now := w.Now()
		at := FormatNaive(now)
		_, err = conn.ExecContext(ctx, `INSERT INTO games
 (appid,name,type,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,created_at,updated_at)
 SELECT CAST(json_extract(p.value,'$[0]') AS INTEGER),'AppID_'||json_extract(p.value,'$[0]'),json_extract(p.value,'$[1]'),
 0,0,0,0,0,0,0,0,0,0,0,0,0,?,? FROM json_each(?) p WHERE 1
 ON CONFLICT(appid) DO UPDATE SET type=excluded.type,updated_at=excluded.updated_at`, at, at, string(raw))
		if err != nil {
			return err
		}
		derived.SetClock(w.Now)
		_, err = derived.UpdatePriceDerived(ctx, conn, ids, derived.Options{Now: now, History: true, Overrides: w.SeriesOverrides, CatalogBefore: before})
		return err
	})
}

func (w *Writer) UpsertTagsBatch(ctx context.Context, entries map[int64][][2]int64) error {
	if len(entries) == 0 {
		return nil
	}
	ids := make([]int64, 0, len(entries))
	for id := range entries {
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	values := [][3]int64{}
	for _, id := range ids {
		for _, tag := range entries[id] {
			values = append(values, [3]int64{id, tag[0], tag[1]})
		}
	}
	rawIDs, err := json.Marshal(ids)
	if err != nil {
		return err
	}
	rawTags, err := json.Marshal(values)
	if err != nil {
		return err
	}
	return w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if _, err := conn.ExecContext(ctx, `DELETE FROM game_tags WHERE appid IN (SELECT value FROM json_each(?))`, string(rawIDs)); err != nil {
			return err
		}
		if len(values) == 0 {
			return nil
		}
		_, err := conn.ExecContext(ctx, `INSERT INTO game_tags(appid,tagid,weight)
 SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),json_extract(value,'$[2]') FROM json_each(?) WHERE 1
 ON CONFLICT(appid,tagid) DO NOTHING`, string(rawTags))
		return err
	})
}
