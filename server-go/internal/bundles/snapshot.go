package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"

	_ "holdexar/server-go/internal/derived"
)

const bundleSnapshotSQL = `
WITH targets AS MATERIALIZED (
    SELECT bundle_id,name,must_purchase_as_set,min_cny_fen,diff_fen,smart_score
    FROM bundles b WHERE ?1 IS NULL OR b.bundle_id IN (SELECT value FROM json_each(?1))
), raw AS MATERIALIZED (
    SELECT p.id,p.bundle_id,p.cny_fen,p.discount_percent,p.bundle_base_discount,
           UPPER(p.region_code) AS code,
           CASE WHEN json_valid(p.app_ids) THEN CASE WHEN json_type(p.app_ids)='array'
                THEN p.app_ids ELSE '[]' END ELSE '[]' END AS aids
    FROM targets t JOIN bundle_region_prices p USING(bundle_id)
    WHERE TRIM(COALESCE(p.region_code,''))!=''
), ranked AS (
    SELECT bundle_id,aids,ROW_NUMBER() OVER (
        PARTITION BY bundle_id ORDER BY json_array_length(aids) DESC,id
    ) AS n FROM raw
), baseline AS MATERIALIZED (
    SELECT bundle_id,aids FROM ranked WHERE n=1
), family AS (
    SELECT r.*,ROW_NUMBER() OVER (
        PARTITION BY r.bundle_id,r.code ORDER BY r.cny_fen IS NULL,r.id
    ) AS n
    FROM raw r JOIN baseline b USING(bundle_id)
    WHERE (json_array_length(b.aids)=0 OR EXISTS (
        SELECT 1 FROM json_each(r.aids) a JOIN json_each(b.aids) m
        ON CAST(a.value AS INTEGER)=CAST(m.value AS INTEGER)
    )) AND (?2 IS NULL OR r.code='CN' OR
        (r.code IN ('PK','BD') AND EXISTS (SELECT 1 FROM json_each(?2) WHERE value='PK')) OR
        (r.code NOT IN ('PK','BD') AND EXISTS (SELECT 1 FROM json_each(?2) WHERE value=r.code)))
), regions AS MATERIALIZED (
    SELECT * FROM family WHERE n=1
), prices AS (
    SELECT bundle_id,MAX(CASE WHEN code='CN' THEN cny_fen END) AS cn,
           MIN(CASE WHEN code!='CN' AND cny_fen>0 THEN cny_fen END) AS minimum,
           MAX(CASE WHEN cny_fen IS NOT NULL AND COALESCE(discount_percent,0)
               > COALESCE(bundle_base_discount,0) THEN 1 ELSE 0 END) AS timing
    FROM regions GROUP BY bundle_id
), reviews AS (
    SELECT b.bundle_id,
        AVG(bundle_quality_score(g.positive_rate,g.review_count,g.release_date)) AS quality,
        AVG(bundle_familiarity_score(g.review_count)) AS familiarity
    FROM baseline b JOIN json_each(b.aids) a
    JOIN games g ON g.appid=CAST(a.value AS INTEGER) GROUP BY b.bundle_id
), result AS (
    SELECT b.bundle_id,p.minimum,MAX(COALESCE(p.cn-p.minimum,0),0) AS difference,
        p.cn IS NOT NULL AND COALESCE(p.minimum,p.cn)>=p.cn-500 AS lowest,
        COALESCE(v.quality,0.5) AS quality,COALESCE(v.familiarity,0) AS familiarity,
        COALESCE(p.timing,0) AS timing
    FROM targets b LEFT JOIN prices p USING(bundle_id) LEFT JOIN reviews v USING(bundle_id)
)
UPDATE bundles AS b SET min_cny_fen=r.minimum,diff_fen=r.difference,is_lowest=r.lowest,
    smart_score=bundle_smart_score(r.difference,r.quality,r.timing,r.familiarity)
FROM result r WHERE b.bundle_id=r.bundle_id`

func RefreshSortCache(ctx context.Context, conn *sql.Conn, bundleIDs []int64, tracked []string) (int64, error) {
	if bundleIDs != nil && len(bundleIDs) == 0 {
		return 0, nil
	}
	var idsValue, trackedValue any
	if bundleIDs != nil {
		raw, err := json.Marshal(bundleIDs)
		if err != nil {
			return 0, err
		}
		idsValue = string(raw)
	}
	if len(tracked) > 0 {
		codes := make([]string, len(tracked))
		for i, code := range tracked {
			codes[i] = strings.ToUpper(code)
		}
		raw, err := json.Marshal(codes)
		if err != nil {
			return 0, err
		}
		trackedValue = string(raw)
	}
	result, err := conn.ExecContext(ctx, bundleSnapshotSQL, idsValue, trackedValue)
	if err != nil {
		return 0, err
	}
	return result.RowsAffected()
}
