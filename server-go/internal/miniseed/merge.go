package miniseed

import (
	"context"
	"database/sql"
	"encoding/json"
	"slices"
)

var merges = [...]struct{ read, write string }{
	{
		`SELECT CASE WHEN typeof(appid)='integer' AND appid>0 THEN json_array(appid,name,name_en,type) END FROM catalog`,
		`INSERT INTO games(appid,name,name_en,type,updated_at,family_sharing,trading_cards,is_epic,is_hb,hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count)
		SELECT value->>0,COALESCE(NULLIF(value->>1,''),CAST(value->>0 AS TEXT)),value->>2,value->>3,?,0,0,0,0,0,0,0,0,0,0,0,0,0 FROM json_each(?) WHERE 1
		ON CONFLICT(appid) DO NOTHING RETURNING appid`,
	},
	{
		`SELECT CASE WHEN typeof(appid)='integer' AND appid>0 THEN json_array(appid,xgp_tier,epic_date,is_epic,is_hb,hb_data,series_id) END FROM curated`,
		`UPDATE games SET xgp_tier=seed.value->>1,
			epic_date=COALESCE(NULLIF(games.epic_date,''),seed.value->>2),
			is_epic=CASE WHEN seed.value->>3=1 THEN 1 ELSE games.is_epic END,
			is_hb=CASE WHEN seed.value->>4=1 THEN 1 ELSE games.is_hb END,
			hb_data=COALESCE(NULLIF(games.hb_data,''),seed.value->>5),
			series_id=COALESCE(NULLIF(games.series_id,''),seed.value->>6)
		FROM json_each(?) seed WHERE games.appid=seed.value->>0`,
	},
	{
		`SELECT CASE WHEN typeof(bundle_id)='integer' AND bundle_id>0 THEN json_array(bundle_id,item_kind,name,app_ids,must_purchase_as_set) END FROM bundle_index`,
		`INSERT INTO bundles(bundle_id,item_kind,name,app_ids,must_purchase_as_set,is_lowest,view_count)
		SELECT value->>0,COALESCE(value->>1,-1),COALESCE(NULLIF(value->>2,''),'bundle-'||(value->>0)),
			CASE WHEN json_valid(value->>3) THEN json(value->>3) ELSE 'null' END,COALESCE(value->>4,-1),0,0 FROM json_each(?) WHERE 1
		ON CONFLICT(bundle_id) DO NOTHING`,
	},
}

func readRows(ctx context.Context, tx *sql.Tx, statement string) ([]json.RawMessage, error) {
	rows, err := tx.QueryContext(ctx, statement)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var result []json.RawMessage
	for rows.Next() {
		var row string
		if err := rows.Scan(&row); err != nil {
			return nil, err
		}
		result = append(result, json.RawMessage(row))
	}
	return result, rows.Err()
}

func insertCatalog(ctx context.Context, conn *sql.Conn, raw, stamp string) ([]int64, error) {
	rows, err := conn.QueryContext(ctx, merges[0].write, stamp, raw)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func applyBatches(ctx context.Context, conn *sql.Conn, statement string, rows []json.RawMessage) (int64, error) {
	var total int64
	for batch := range slices.Chunk(rows, chunk) {
		raw, err := json.Marshal(batch)
		if err != nil {
			return 0, err
		}
		result, err := conn.ExecContext(ctx, statement, string(raw))
		if err != nil {
			return 0, err
		}
		count, err := result.RowsAffected()
		if err != nil {
			return 0, err
		}
		total += count
	}
	return total, nil
}
