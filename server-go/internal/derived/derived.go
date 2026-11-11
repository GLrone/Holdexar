package derived

// 价格派生主链：对齐现网 update_price_derived——现价/历史/游戏行就绪后
// 在同一事务连接上重算六列（hl_flag/pp_flag/pp_changed_at/min_cny_fen/
// diff_fen/smart_score），目录输入变化时联动系列重算（series_id）。

import (
	"context"
	"database/sql"
	"fmt"
	"time"
)

// 标准版谓词（与写入/读取侧共用口径同源）：商品身份未知的行永不并入。
const standardProductSQL = "(is_gold IS NULL OR is_gold = 0) " +
	"AND (version_suffix IS NULL OR version_suffix = '') " +
	"AND (is_bundle IS NULL OR is_bundle = 0) " +
	"AND (product_unknown IS NULL OR product_unknown = 0)"

// CatalogRow 目录输入快照（系列联动与联动范围的判定输入）。
type CatalogRow struct {
	Name, NameEn, Type        *string
	PositiveRate, ReviewCount *int64
	ReleaseDate               *string
	SeriesID                  *string
}

// Options 派生调用参数。Now = era_cutoff/发行年龄基准挂钟；Overrides =
// 系列覆盖表（宿主在闸外装填，闸内零文件 IO）；CatalogBefore = 目录输入
// 快照（nil = 不做系列联动）。
type Options struct {
	Now           time.Time
	Overrides     map[string][]int64
	OverrideOrder []string
	History       bool
	CatalogBefore map[int64]CatalogRow
}

// CatalogInputs 读取目录输入快照（写入前的旧值；新行无键）。
func CatalogInputs(ctx context.Context, conn *sql.Conn, appids []int64) (map[int64]CatalogRow, error) {
	ids := dedupIDs(appids)
	if len(ids) == 0 {
		return map[int64]CatalogRow{}, nil
	}
	q := "SELECT appid, name, name_en, type, positive_rate, review_count, release_date, series_id " +
		"FROM games WHERE appid IN (SELECT value FROM json_each(?))"
	rows, err := conn.QueryContext(ctx, q, pyJSONSorted(intSlice(ids)))
	if err != nil {
		return nil, fmt.Errorf("目录输入读取失败: %w", err)
	}
	defer rows.Close()
	out := map[int64]CatalogRow{}
	for rows.Next() {
		var appid int64
		var r CatalogRow
		var name, nameEn, typ, release, sid sql.NullString
		var pr, rc sql.NullInt64
		if err := rows.Scan(&appid, &name, &nameEn, &typ, &pr, &rc, &release, &sid); err != nil {
			return nil, fmt.Errorf("目录输入行读取失败: %w", err)
		}
		r.Name, r.NameEn, r.Type = nullStrPtr(name), nullStrPtr(nameEn), nullStrPtr(typ)
		r.PositiveRate, r.ReviewCount = nullIntPtr(pr), nullIntPtr(rc)
		r.ReleaseDate, r.SeriesID = nullStrPtr(release), nullStrPtr(sid)
		out[appid] = r
	}
	return out, rows.Err()
}

// UpdatePriceDerived 重算派生列并按需联动系列重算。必须在调用方事务
// 连接与写闸内执行（前置写入已在同一连接生效）。返回 UPDATE 覆盖行数。
func UpdatePriceDerived(ctx context.Context, conn *sql.Conn, appids []int64, opts Options) (int64, error) {
	if appids != nil && len(appids) == 0 {
		return 0, nil
	}
	count, err := priceSnapshot(ctx, conn, appids, opts.Now, opts.History)
	if err != nil {
		return 0, err
	}
	if opts.CatalogBefore != nil {
		current, err := CatalogInputs(ctx, conn, appids)
		if err != nil {
			return 0, err
		}
		before := opts.CatalogBefore
		seriesChanged := len(current) != len(before)
		changed := map[int64]string{}
		for aid, cur := range current {
			old, ok := before[aid]
			if !ok || catalogIdentChanged(cur, old) {
				seriesChanged = true
				changed[aid] = oldSeriesID(before, aid)
			}
		}
		for aid := range before {
			if _, ok := current[aid]; !ok {
				seriesChanged = true
				changed[aid] = oldSeriesID(before, aid)
			}
		}
		if seriesChanged {
			if _, err := RefreshSeries(ctx, conn, changed, opts.Overrides, opts.OverrideOrder); err != nil {
				return 0, err
			}
		}
		// 评测/发行日变化的捆绑包排序缓存联动未迁移（bundles 域未到）
	}
	return count, nil
}

func SaveInputVersion(ctx context.Context, conn *sql.Conn, now time.Time, overrides map[string][]int64) error {
	if overrides == nil {
		overrides = map[string][]int64{}
	}
	_, err := conn.ExecContext(ctx, "INSERT INTO app_settings(key,value_json) VALUES('games.derived_inputs',?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", pyJSONSorted(map[string]any{"day": now.Format("2006-01-02"), "series": overrides}))
	return err
}

// catalogIdentChanged 系列联动判定：(name, name_en, type) 任一变化。
func catalogIdentChanged(cur, old CatalogRow) bool {
	return !strEqual(cur.Name, old.Name) ||
		!strEqual(cur.NameEn, old.NameEn) ||
		!strEqual(cur.Type, old.Type)
}

func oldSeriesID(before map[int64]CatalogRow, aid int64) string {
	if old, ok := before[aid]; ok && old.SeriesID != nil {
		return *old.SeriesID
	}
	return ""
}

func strEqual(a, b *string) bool {
	if a == nil || b == nil {
		return a == b
	}
	return *a == *b
}

// ── price_snapshot：六列一次 UPDATE ────────────────────────────

func priceSnapshot(ctx context.Context, conn *sql.Conn, appids []int64, now time.Time, history bool) (int64, error) {
	ids := dedupIDs(appids)
	filter := ""
	var args []any
	if ids != nil {
		filter = "WHERE g.appid IN (SELECT value FROM json_each(?))"
		args = append(args, pyJSONSorted(intSlice(ids)))
	}
	historyCTEs := ""
	flag := "t.hl_flag"
	pp := "t.pp_flag"
	changed := "t.pp_changed_at"
	joins := ""
	if history {
		eraCutoff := pyIsoDateTime(now.Add(-30 * 24 * time.Hour))
		historyCTEs = `
    , h AS MATERIALIZED (
        SELECT h.appid,h.id,h.snapshot_at,h.original_price,h.sub_id,
          COALESCE(h.cny_fen,CASE WHEN h.currency='CNY' THEN h.price END) AS cny_fen
        FROM targets t CROSS JOIN game_price_history h ON h.appid=t.appid
        WHERE h.region_code='CN' AND h.price_status='ok'
          AND ` + standardProductSQL + `
    ), lastdiff AS (
        SELECT h.appid, MAX(h.snapshot_at) AS at FROM h JOIN targets t ON t.appid=h.appid
        WHERE h.cny_fen>0 AND CAST(ROUND(h.cny_fen/10.0) AS INTEGER)
          !=CAST(ROUND(t.cn_fen/10.0) AS INTEGER) GROUP BY h.appid
    ), era AS (
        SELECT h.appid, MIN(h.snapshot_at) AS at FROM h
        JOIN targets t ON t.appid=h.appid LEFT JOIN lastdiff d ON d.appid=h.appid
        WHERE h.cny_fen>0 AND CAST(ROUND(h.cny_fen/10.0) AS INTEGER)
          =CAST(ROUND(t.cn_fen/10.0) AS INTEGER)
          AND (d.at IS NULL OR h.snapshot_at>=d.at) GROUP BY h.appid
    ), prior AS (
        SELECT h.appid, MIN(h.cny_fen) AS fen FROM h JOIN era e ON e.appid=h.appid
        WHERE h.cny_fen>0 AND h.snapshot_at<e.at GROUP BY h.appid
    ), original AS (
        SELECT h.*, t.original_price AS current_original,
          ROW_NUMBER() OVER (PARTITION BY h.appid ORDER BY h.snapshot_at DESC,h.id DESC) AS rn
        FROM h JOIN targets t ON t.appid=h.appid
        WHERE h.original_price IS NOT NULL AND t.original_price IS NOT NULL
          AND h.original_price!=t.original_price AND h.snapshot_at<t.observed_at
          AND (COALESCE(t.sub_id,0)=0 OR h.sub_id=t.sub_id)
    ), sequence AS (
        SELECT h.appid,h.original_price,h.snapshot_at,
          LAG(h.original_price) OVER (PARTITION BY h.appid ORDER BY h.snapshot_at,h.id) AS prev
        FROM h JOIN targets t ON t.appid=h.appid
        WHERE h.original_price IS NOT NULL AND t.original_price IS NOT NULL
          AND (COALESCE(t.sub_id,0)=0 OR h.sub_id=t.sub_id)
    ), jumps AS (
        SELECT appid,MAX(snapshot_at) AS at FROM sequence
        WHERE prev IS NOT NULL AND prev!=original_price GROUP BY appid
    )
    `
		flag = `CASE WHEN t.cn_fen IS NULL OR t.cn_fen<=0 THEN 0
        WHEN e.at IS NULL THEN CASE WHEN t.discount>0 THEN 3 ELSE 0 END
        WHEN p.fen IS NULL THEN CASE WHEN t.discount>0 THEN
          CASE WHEN e.at>=? THEN 1 ELSE 2 END ELSE 0 END
        WHEN p.fen-t.cn_fen>=10 THEN 1 WHEN p.fen-t.cn_fen>-10 THEN 2
        ELSE CASE WHEN t.discount>0 THEN 3 ELSE 0 END END`
		pp = "CASE WHEN o.rn=1 THEN CASE WHEN t.original_price<o.original_price THEN 1 ELSE 2 END ELSE 0 END"
		changed = "j.at"
		joins = "LEFT JOIN era e ON e.appid=t.appid LEFT JOIN prior p ON p.appid=t.appid " +
			"LEFT JOIN original o ON o.appid=t.appid AND o.rn=1 LEFT JOIN jumps j ON j.appid=t.appid"
		args = append(args, eraCutoff)
	}
	sqlText := `WITH targets AS MATERIALIZED (
        SELECT g.*, c.cny_fen AS cn_fen,c.price AS cn_price,c.original_price,
          COALESCE(c.discount_percent,0) AS discount,c.sub_id,c.updated_at AS observed_at
        FROM games g LEFT JOIN game_current_prices c
          ON c.appid=g.appid AND c.region_code='CN' AND c.price_status='ok'
        ` + filter + `
    )` + historyCTEs + `, lowest AS (
        SELECT c.appid,MIN(c.cny_fen) AS fen FROM targets t
        CROSS JOIN game_current_prices c ON c.appid=t.appid
        WHERE c.region_code!='CN' AND c.price_status='ok' AND c.cny_fen>0
          AND c.attempt_outcome='success' GROUP BY c.appid
    ), result AS MATERIALIZED (
        SELECT t.appid,` + flag + ` AS hl,` + pp + ` AS pp,` + changed + ` AS changed,
          l.fen AS minimum,MAX(COALESCE(t.cn_price-l.fen,0),0) AS difference,
          t.positive_rate,t.review_count,t.release_date,t.discount
        FROM targets t LEFT JOIN lowest l ON l.appid=t.appid ` + joins + `
    )
    UPDATE games AS g SET hl_flag=r.hl,pp_flag=r.pp,pp_changed_at=r.changed,
      min_cny_fen=r.minimum,diff_fen=r.difference,
      smart_score=game_smart_score(r.difference,r.positive_rate,r.review_count,
        r.hl,r.discount,r.release_date)
    FROM result r WHERE g.appid=r.appid`
	res, err := conn.ExecContext(ctx, sqlText, args...)
	if err != nil {
		return 0, fmt.Errorf("派生列更新失败: %w", err)
	}
	n, err := res.RowsAffected()
	if err != nil {
		return 0, fmt.Errorf("派生列更新行数读取失败: %w", err)
	}
	return n, nil
}

// pyIsoDateTime 对齐 datetime.isoformat(" ")：微秒非零才带小数节。
func pyIsoDateTime(t time.Time) string {
	s := t.Format("2006-01-02 15:04:05")
	if ns := t.Nanosecond(); ns != 0 {
		s += fmt.Sprintf(".%06d", ns/1000)
	}
	return s
}

func dedupIDs(appids []int64) []int64 {
	if appids == nil {
		return nil
	}
	seen := map[int64]bool{}
	out := make([]int64, 0, len(appids))
	for _, a := range appids {
		if seen[a] {
			continue
		}
		seen[a] = true
		out = append(out, a)
	}
	return out
}

func intSlice(ids []int64) []any { return int64sToAny(ids) }

func nullIntPtr(n sql.NullInt64) *int64 {
	if !n.Valid {
		return nil
	}
	v := n.Int64
	return &v
}
