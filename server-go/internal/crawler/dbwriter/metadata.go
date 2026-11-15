package dbwriter

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

// MarkComingSoon coming_soon 无包（未开放预购/未上线）暂缓语义：type=COMING_SOON
// 挂名行（对齐 Python mark_coming_soon）。与 mark_non_game_type 的脱池机制相同
// （updated_at 落值防反哺空转），但 type 单独成类——重探通道按 type=COMING_SOON
// 选候选，开放预购/上线后正常入库覆盖此行。冲突只更 type/时间戳并一并清复活
// 痕迹（正常入库路径覆盖过此行时 removed 可能被误标过）；名字保持挂名值。
func (w *Writer) MarkComingSoon(ctx context.Context, appid int64) error {
	return w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		before, err := derived.CatalogInputs(ctx, conn, []int64{appid})
		if err != nil {
			return err
		}
		now := w.Now()
		at := FormatNaive(now)
		if _, err := conn.ExecContext(ctx, `INSERT INTO games
 (appid,name,type,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,created_at,updated_at)
 VALUES (?, 'AppID_'||?, 'COMING_SOON', 0,0,0,0,0,0,0,0,0,0,0,0,0,?,?)
 ON CONFLICT(appid) DO UPDATE SET type=excluded.type, updated_at=excluded.updated_at,
 removed_at=NULL, removed_strikes=0`, appid, appid, at, at); err != nil {
			return err
		}
		derived.SetClock(w.Now)
		_, err = derived.UpdatePriceDerived(ctx, conn, []int64{appid}, derived.Options{
			Now: now, History: true, Overrides: w.SeriesOverrides, CatalogBefore: before,
		})
		return err
	})
}

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

// BundleDiscovery 捆绑包发现桩（游戏条目 purchase_options 白送的数据）。
type BundleDiscovery struct {
	BundleID int64
	Name     string
	MPS      int64 // must_purchase_as_set
	ItemKind int64
	AppIDs   []int64
}

// RecordBundleDiscoveries 捆绑包发现桩落库。只 INSERT 库内没有的包
// （冲突 DO NOTHING）：既有完整主档不覆盖——发现渠道只负责「把新包带
// 进门」，无价桩不随轮刷新，用户关注/导入该包时才整区抓取补价。
// app_ids 只存本游戏一个 id 当种子，抓取时会被 included_appids 校正。
// updated_at=NULL（发现桩：播种查询按 NULL 捞）。返回新插数（调用方做
// 发现计数）。
func (w *Writer) RecordBundleDiscoveries(ctx context.Context, discoveries []BundleDiscovery) (int, error) {
	if len(discoveries) == 0 {
		return 0, nil
	}
	ids := make([]int64, 0, len(discoveries))
	for _, d := range discoveries {
		ids = append(ids, d.BundleID)
	}
	known := map[int64]bool{}
	rawIDs, err := json.Marshal(ids)
	if err != nil {
		return 0, err
	}
	if err := w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		rows, err := conn.QueryContext(ctx,
			"SELECT bundle_id FROM bundles WHERE bundle_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?))",
			string(rawIDs))
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var id int64
			if err := rows.Scan(&id); err != nil {
				return err
			}
			known[id] = true
		}
		return rows.Err()
	}); err != nil {
		return 0, err
	}
	fresh := make([]BundleDiscovery, 0, len(discoveries))
	for _, d := range discoveries {
		if !known[d.BundleID] {
			fresh = append(fresh, d)
		}
	}
	if len(fresh) == 0 {
		return 0, nil
	}
	err = w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		for _, d := range fresh {
			name := d.Name
			if name == "" {
				name = fmt.Sprintf("Bundle_%d", d.BundleID)
			}
			if runes := []rune(name); len(runes) > 512 {
				name = string(runes[:512])
			}
			appIDs, err := json.Marshal(d.AppIDs)
			if err != nil {
				return err
			}
			if d.AppIDs == nil {
				appIDs = []byte("[]")
			}
			if _, err := conn.ExecContext(ctx, `INSERT INTO bundles
 (bundle_id, name, must_purchase_as_set, item_kind, is_lowest, app_ids, view_count)
 VALUES (?,?,?,?,?,?,?)
 ON CONFLICT(bundle_id) DO NOTHING`, d.BundleID, name, d.MPS, d.ItemKind, 0, string(appIDs), 0); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return len(fresh), nil
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
