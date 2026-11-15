// 捆绑包列表读面：ListBundles 大 SQL + fast path + ownership，逐条对齐
// Python app/domains/bundles/service.py::list_bundles / _aggregate / _load_bundles。
package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/monitoring"
)

// ── 响应形状（JSON 键与 Python 逐字段对齐） ──

// PriceData 价格观察状态（观察时刻 = 该包全部价格行 MAX(crawled_at)）；
// 三值全空 = 无任何观察记录（「没有数据」不冒充「数据很旧」）。
type PriceData struct {
	ObservedAt *string  `json:"observedAt"`
	AgeHours   *float64 `json:"ageHours"`
	Freshness  *string  `json:"freshness"`
}

// Ownership 拥有状态：type = null|"owned"|"family"；account 仅 family 时有值。
type Ownership struct {
	Type    *string `json:"type"`
	Account *string `json:"account"`
}

// ListRegionPrice 列表区价投影（详情的 appIds/lockedCount/formatted 不进列表）。
type ListRegionPrice struct {
	PriceMinor      *int64  `json:"priceMinor"`
	Currency        *string `json:"currency"`
	DiscountPercent int64   `json:"discountPercent"`
	BaseDiscount    int64   `json:"baseDiscount"`
	CnyFen          *int64  `json:"cnyFen"`
	DiscountEndsAt  *int64  `json:"discountEndsAt"`
}

// BundleListItem 列表项。
type BundleListItem struct {
	bundleSummary
	RegionPrices map[string]ListRegionPrice `json:"regionPrices"`
	Ownership    Ownership                  `json:"ownership"`
}

// ListResult 列表响应；total/completableCount 为全量计数（非页内）。
type ListResult struct {
	Bundles          []BundleListItem `json:"bundles"`
	Total            int64            `json:"total"`
	CompletableCount int64            `json:"completableCount"`
	Offset           int              `json:"offset"`
	Limit            int              `json:"limit"`
}

// bundleSummary 列表与详情共用的聚合字段（内嵌展开进 JSON）。
type bundleSummary struct {
	BundleID          int64    `json:"bundleId"`
	Name              string   `json:"name"`
	HeaderImage       string   `json:"headerImage"`
	URL               string   `json:"url"`
	MustPurchaseAsSet int64    `json:"mustPurchaseAsSet"`
	ItemKind          int64    `json:"itemKind"`
	AppIDs            []int64  `json:"appIds"`
	CnCnyFen          *int64   `json:"cnCnyFen"`
	LowestRegion      string   `json:"lowestRegion"`
	LowestCnyFen      *int64   `json:"lowestCnyFen"`
	DiffFen           int64    `json:"diffFen"`
	SmartScore        float64  `json:"smartScore"`
	PriceData         PriceData `json:"priceData"`
}

// ListOptions 列表参数；PrimarySteamID 由调用方（HTTP 层）解析传入
// （internal/account 无导出读取函数，对齐 Python service.py 主账户 + settings 兜底的取值来源）。
type ListOptions struct {
	Sort            string
	Offset          int
	Limit           int // <=0 → 默认 120（Python 签名默认值）
	Removed         bool
	Query           string
	Region          string
	RegionMode      string // cheaper|locked|其它=不过滤
	FollowedOnly    bool
	CompletableOnly bool
	CnLowestOnly    bool
	OnlyDiscounted  bool
	GiftOnly        bool
	HideOwned       bool
	HideFamily      bool
	PriceMin        *float64
	PriceMax        *float64
	DiffMin         *float64
	DiffMax         *float64
	DiffType        string // absolute|percent
	PrimarySteamID  string
}

// ── 参数绑定辅助（?N 位置参数，与 snapshot.go 同风格） ──

type sqlArgs struct{ args []any }

func (a *sqlArgs) add(v any) string {
	a.args = append(a.args, v)
	return "?" + strconv.Itoa(len(a.args))
}

func boolInt64(v bool) int64 {
	if v {
		return 1
	}
	return 0
}

// ── SQL 片段（CTE 逐条对齐 _region_rows_sql / _OWNERSHIP_SQL / _visible_bundle_sql） ──

func jsonArraySQL(column string) string {
	return "CASE WHEN json_valid(" + column + ") THEN CASE WHEN json_type(" + column + ") = 'array' " +
		"THEN " + column + " ELSE '[]' END ELSE '[]' END"
}

func regionRowsSQL(targetWhere, trackedPH, regionPH string) string {
	return `
WITH targets AS MATERIALIZED (
    SELECT bundle_id, name, must_purchase_as_set, min_cny_fen, diff_fen, smart_score
    FROM bundles b WHERE ` + targetWhere + `
), raw AS MATERIALIZED (
    SELECT p.id, p.bundle_id, p.cny_fen, p.discount_percent, p.bundle_base_discount,
           UPPER(p.region_code) AS code,
           ` + jsonArraySQL("p.app_ids") + ` AS aids
    FROM targets t JOIN bundle_region_prices p USING (bundle_id)
    WHERE TRIM(COALESCE(p.region_code, '')) != ''
), ranked AS (
    SELECT bundle_id, aids, ROW_NUMBER() OVER (
        PARTITION BY bundle_id ORDER BY json_array_length(aids) DESC, id
    ) AS n FROM raw
), baseline AS MATERIALIZED (
    SELECT bundle_id, aids FROM ranked WHERE n = 1
), family AS (
    SELECT r.*, ROW_NUMBER() OVER (
        PARTITION BY r.bundle_id, r.code ORDER BY r.cny_fen IS NULL, r.id
    ) AS n
    FROM raw r JOIN baseline b USING (bundle_id)
    WHERE (json_array_length(b.aids) = 0 OR EXISTS (
        SELECT 1 FROM json_each(r.aids) a JOIN json_each(b.aids) m
        ON CAST(a.value AS INTEGER) = CAST(m.value AS INTEGER)
    )) AND (` + trackedPH + ` IS NULL OR r.code = 'CN' OR
        (r.code IN ('PK', 'BD') AND EXISTS (
            SELECT 1 FROM json_each(` + trackedPH + `) WHERE value = 'PK'
        )) OR (r.code NOT IN ('PK', 'BD') AND EXISTS (
            SELECT 1 FROM json_each(` + trackedPH + `) WHERE value = r.code
        )))
), regions AS MATERIALIZED (
    SELECT * FROM family WHERE n = 1
), prices AS (
    SELECT bundle_id, MAX(CASE WHEN code = 'CN' THEN cny_fen END) AS cn,
           MAX(CASE WHEN code = ` + regionPH + ` THEN cny_fen END) AS regional,
           MAX(COALESCE(discount_percent, 0)) AS discount,
           COALESCE(MAX(CASE WHEN code = ` + regionPH + ` THEN discount_percent END), 0) AS regional_discount,
           MAX(CASE WHEN code != 'CN' AND cny_fen > 0 THEN 1 ELSE 0 END) AS gift,
           MIN(CASE WHEN code != 'CN' AND cny_fen > 0 THEN cny_fen END) AS minimum,
           MAX(CASE WHEN cny_fen IS NOT NULL AND COALESCE(discount_percent, 0)
               > COALESCE(bundle_base_discount, 0) THEN 1 ELSE 0 END) AS timing
    FROM regions GROUP BY bundle_id
)`
}

// ownershipSQLTemplate 拥有状态 CTE（@primary@ 为占位符）；列表主 SQL 与
// 页级补算（pageOwnership 的 baseline 前置）共用同一份。
const ownershipSQLTemplate = `
, owned_apps AS MATERIALIZED (
    SELECT w.appid, MAX(@primary@ = '' OR w.steamid = @primary@) AS self_owned,
        MIN(CASE WHEN @primary@ != '' AND w.steamid != @primary@ THEN
            COALESCE(NULLIF(a.label, ''), NULLIF(a.persona_name, ''), a.steamid) END) AS family_account
    FROM wishlist_items w JOIN tracked_accounts a ON a.steamid = w.steamid
    WHERE w.active = 1 AND w.owned = 1 GROUP BY w.appid
), ownership AS MATERIALIZED (
    SELECT b.bundle_id,
        CASE WHEN COUNT(*)=COUNT(o.appid) THEN CASE WHEN SUM(COALESCE(o.self_owned,0))>0 THEN 'owned'
            WHEN MIN(o.family_account) IS NOT NULL THEN 'family' ELSE 'owned' END END AS ownership_type,
        MIN(o.family_account) AS family_account
    FROM baseline b JOIN json_each(b.aids) m
    JOIN games g ON g.appid=CAST(m.value AS INTEGER) AND COALESCE(g.name,'')!=''
    LEFT JOIN owned_apps o ON o.appid=g.appid GROUP BY b.bundle_id
)`

func visibleBundleSQL(trackedPH string) string {
	basis := "(SELECT " + jsonArraySQL("m.app_ids") + " FROM bundle_region_prices m " +
		"WHERE m.bundle_id=b.bundle_id AND TRIM(COALESCE(m.region_code,''))!='' " +
		"ORDER BY json_array_length(" + jsonArraySQL("m.app_ids") + ") DESC,m.id LIMIT 1)"
	return "EXISTS (SELECT 1 FROM bundle_region_prices p " +
		"WHERE p.bundle_id=b.bundle_id AND TRIM(COALESCE(p.region_code,''))!='' " +
		"AND (" + trackedPH + " IS NULL OR UPPER(p.region_code)='CN' OR " +
		"(UPPER(p.region_code) IN ('PK','BD') AND EXISTS (" +
		"SELECT 1 FROM json_each(" + trackedPH + ") WHERE value='PK')) OR " +
		"(UPPER(p.region_code) NOT IN ('PK','BD') AND EXISTS (" +
		"SELECT 1 FROM json_each(" + trackedPH + ") WHERE value=UPPER(p.region_code)))) " +
		"AND (EXISTS (SELECT 1 FROM json_each(" + jsonArraySQL("p.app_ids") + ") a " +
		"JOIN json_each(" + basis + ") m ON CAST(a.value AS INTEGER)=CAST(m.value AS INTEGER)) " +
		"OR json_array_length(" + basis + ")=0))"
}

// blockedBundleSQL 软删/释放可见性子查询：monitor_targets 中 target_type='bundle'
// 且状态 ∈ {released,excluded}（NON_CRAWL_STATES）。默认视图 = blocked 对 removed 取反。
func blockedBundleSQL() string {
	return "(EXISTS (SELECT 1 FROM monitor_targets t WHERE t.target_type = 'bundle' " +
		"AND t.target_id = b.bundle_id AND " + monitoring.StateExpression +
		" IN ('released','excluded')))"
}

// ── ListBundles 主流程 ──

func ListBundles(ctx context.Context, db *sql.DB, opts ListOptions) (*ListResult, error) {
	tracked := trackedRegionCodes(ctx, db)
	region := strings.ToUpper(strings.TrimSpace(opts.Region))
	queryRaw := strings.TrimSpace(opts.Query)
	query := strings.ToLower(queryRaw)
	sortKey := opts.Sort

	offset := opts.Offset
	if offset < 0 {
		offset = 0
	}
	limit := opts.Limit
	if limit <= 0 {
		limit = 120
	}
	if limit > 200 {
		limit = 200
	}

	blocked := blockedBundleSQL()
	// 目标可见性条件（嵌入 targets CTE 的 WHERE）
	var a sqlArgs
	targetConds := []string{"(" + blocked + ") = " + a.add(boolInt64(opts.Removed))}
	if queryRaw != "" {
		targetConds = append(targetConds, "instr(bundle_lower(b.name), "+a.add(query)+")>0")
	}
	if opts.FollowedOnly {
		targetConds = append(targetConds, "EXISTS (SELECT 1 FROM monitor_sources s WHERE s.target_type='bundle' "+
			"AND s.target_id=b.bundle_id AND s.source='favorite' AND s.active=1)")
	}
	if opts.CompletableOnly {
		targetConds = append(targetConds, "b.must_purchase_as_set=0")
	}
	var conds []string
	if region != "" && opts.RegionMode == "cheaper" {
		conds = append(conds, "cn > regional")
		targetConds = append(targetConds, "EXISTS (SELECT 1 FROM bundle_region_prices r WHERE r.bundle_id=b.bundle_id "+
			"AND UPPER(r.region_code)="+a.add(region)+" AND r.cny_fen IS NOT NULL)")
	} else if region != "" && opts.RegionMode == "locked" {
		conds = append(conds, "regional IS NULL")
	}
	trackedPH := a.add(trackedJSONParam(tracked))
	regionPH := a.add(region)
	if opts.CnLowestOnly {
		conds = append(conds, "cn IS NOT NULL AND (min_cny_fen IS NULL OR cn <= min_cny_fen)")
	}
	if opts.OnlyDiscounted {
		conds = append(conds, "discount > 0")
	}
	if opts.GiftOnly {
		conds = append(conds, "cn > 0 AND gift = 1")
	}
	ownershipFilter := opts.HideOwned || opts.HideFamily
	if opts.HideOwned {
		conds = append(conds, "COALESCE(ownership_type, '') != 'owned'")
	}
	if opts.HideFamily {
		conds = append(conds, "COALESCE(ownership_type, '') != 'family'")
	}
	if opts.PriceMin != nil {
		conds = append(conds, "cn >= "+a.add(*opts.PriceMin)+" * 100")
	}
	if opts.PriceMax != nil {
		conds = append(conds, "cn <= "+a.add(*opts.PriceMax)+" * 100")
	}
	diffValue := "diff_value / 100.0"
	if opts.DiffType == "percent" {
		diffValue = "diff_value * 100.0 / NULLIF(cn, 0)"
		if opts.DiffMin != nil || opts.DiffMax != nil {
			conds = append(conds, "cn > 0")
		}
	}
	if opts.DiffMin != nil {
		conds = append(conds, "("+diffValue+") >= "+a.add(*opts.DiffMin))
	}
	if opts.DiffMax != nil {
		conds = append(conds, "("+diffValue+") <= "+a.add(*opts.DiffMax))
	}

	condSQL := strings.Join(conds, " AND ")
	if condSQL == "" {
		condSQL = "1"
	}

	score := "b.diff_fen"
	if sortKey == "smart" {
		score = "b.smart_score"
	}
	var order string
	switch {
	case sortKey == "discount":
		discount := "discount"
		if region != "" {
			discount = "regional_discount"
		}
		order = discount + " DESC, diff_fen DESC, followed DESC, bundle_id"
	case region != "" && sortKey == "smart":
		order = "bundle_save_score(COALESCE(cn - regional, 0)) + " +
			"MAX(0, COALESCE(smart_score, 0) - bundle_save_score(COALESCE(diff_fen, 0))) DESC, " +
			"followed DESC, smart_score DESC, bundle_id"
	case region != "":
		order = "cn - regional DESC, followed DESC, diff_fen DESC, bundle_id"
	default:
		order = "followed DESC, score DESC, bundle_id"
	}

	ownershipCTE := ""
	ownershipColumns := "NULL AS ownership_type, NULL AS family_account"
	ownershipJoin := ""
	if ownershipFilter {
		ownershipCTE = strings.ReplaceAll(ownershipSQLTemplate, "@primary@", a.add(opts.PrimarySteamID))
		ownershipColumns = "o.ownership_type, o.family_account"
		ownershipJoin = "LEFT JOIN ownership o USING (bundle_id)"
	}
	limitPH := a.add(int64(limit))
	offsetPH := a.add(int64(offset))
	slowSQL := regionRowsSQL(strings.Join(targetConds, " AND "), trackedPH, regionPH) + ownershipCTE + `
    , candidates AS (
        SELECT b.*, p.*, ` + score + ` AS score,
               CASE WHEN ` + regionPH + ` != '' AND p.cn IS NOT NULL AND p.regional IS NOT NULL
                    THEN p.cn - p.regional ELSE COALESCE(b.diff_fen, 0) END AS diff_value,
               EXISTS (SELECT 1 FROM monitor_sources s WHERE s.target_type = 'bundle'
                   AND s.target_id = b.bundle_id AND s.source = 'favorite' AND s.active = 1) AS followed,
               ` + ownershipColumns + `
        FROM targets b JOIN prices p USING (bundle_id)
        ` + ownershipJoin + `
    ), matched AS MATERIALIZED (
        SELECT * FROM candidates WHERE ` + condSQL + `
    ), page AS (
        SELECT * FROM matched ORDER BY ` + order + `
        LIMIT ` + limitPH + ` OFFSET ` + offsetPH + `
    )
    SELECT COUNT(*) AS total, COALESCE(SUM(must_purchase_as_set = 0), 0) AS completable,
           (SELECT json_group_array(json_object('id', bundle_id, 'type', ownership_type,
                'account', CASE WHEN ownership_type = 'family' THEN family_account END)) FROM page) AS items FROM matched`

	fastEligible := region == "" && sortKey != "discount" && !opts.CnLowestOnly && !opts.OnlyDiscounted &&
		!opts.GiftOnly && opts.PriceMin == nil && opts.PriceMax == nil && opts.DiffMin == nil && opts.DiffMax == nil

	// fast path 参数独立分配（占位符编号与慢 SQL 不同链路）
	var f sqlArgs
	fastTargetConds := []string{"(" + blocked + ") = " + f.add(boolInt64(opts.Removed))}
	if queryRaw != "" {
		fastTargetConds = append(fastTargetConds, "instr(bundle_lower(b.name), "+f.add(query)+")>0")
	}
	fastTrackedPH := f.add(trackedJSONParam(tracked))
	fastLimitPH := f.add(int64(limit))
	fastOffsetPH := f.add(int64(offset))
	fastSQL := `WITH matched AS MATERIALIZED (
        SELECT b.bundle_id,b.must_purchase_as_set,` + score + ` AS score,
            EXISTS (SELECT 1 FROM monitor_sources s WHERE s.target_type='bundle' AND s.target_id=b.bundle_id
                AND s.source='favorite' AND s.active=1) AS followed
        FROM bundles b WHERE ` + strings.Join(fastTargetConds, " AND ") + ` AND ` + visibleBundleSQL(fastTrackedPH) + `
    ), page AS (SELECT * FROM matched ORDER BY followed DESC,score DESC,bundle_id LIMIT ` + fastLimitPH + ` OFFSET ` + fastOffsetPH + `)
    SELECT COUNT(*) AS total,COALESCE(SUM(must_purchase_as_set=0),0) AS completable,
        (SELECT json_group_array(json_object('id',bundle_id,'type',NULL,'account',NULL)) FROM page) AS items
    FROM matched`

	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return nil, fmt.Errorf("捆绑包列表事务失败: %w", err)
	}
	defer tx.Rollback()

	useFast := fastEligible
	if ownershipFilter {
		var owned int
		if err := tx.QueryRowContext(ctx, "SELECT EXISTS (SELECT 1 FROM wishlist_items w JOIN tracked_accounts a "+
			"ON a.steamid=w.steamid WHERE w.active=1 AND w.owned=1)").Scan(&owned); err != nil {
			return nil, fmt.Errorf("拥有行检查失败: %w", err)
		}
		useFast = fastEligible && owned == 0
	}
	sqlText, args := fastSQL, f.args
	if !useFast {
		sqlText, args = slowSQL, a.args
	}

	var total, completable int64
	var itemsJSON string
	if err := tx.QueryRowContext(ctx, sqlText, args...).Scan(&total, &completable, &itemsJSON); err != nil {
		return nil, fmt.Errorf("捆绑包列表查询失败: %w", err)
	}
	var page []struct {
		ID      int64   `json:"id"`
		Type    *string `json:"type"`
		Account *string `json:"account"`
	}
	if err := json.Unmarshal([]byte(itemsJSON), &page); err != nil {
		return nil, fmt.Errorf("捆绑包列表页解析失败: %w", err)
	}
	ids := make([]int64, 0, len(page))
	for _, row := range page {
		ids = append(ids, row.ID)
	}
	bundles, pricesByBundle, err := loadBundles(ctx, tx, ids)
	if err != nil {
		return nil, err
	}
	byID := make(map[int64]bundleRow, len(bundles))
	for _, b := range bundles {
		byID[b.BundleID] = b
	}
	items := make([]BundleListItem, 0, len(page))
	for _, row := range page {
		b, ok := byID[row.ID]
		if !ok {
			continue
		}
		item := aggregateBundle(b, pricesByBundle[row.ID], tracked).listItem()
		item.Ownership = Ownership{Type: row.Type, Account: row.Account}
		items = append(items, item)
	}
	if !ownershipFilter || useFast {
		if err := pageOwnership(ctx, tx, items, opts.PrimarySteamID); err != nil {
			return nil, err
		}
	}
	return &ListResult{
		Bundles:          items,
		Total:            total,
		CompletableCount: completable,
		Offset:           offset,
		Limit:            limit,
	}, nil
}

// pageOwnership 页级补算拥有状态（对齐 Python _page_ownership）：慢路径带
// ownership 过滤时由主 SQL 直出，其余路径按页补全。
func pageOwnership(ctx context.Context, q *sql.Tx, items []BundleListItem, primary string) error {
	if len(items) == 0 {
		return nil
	}
	page := make([]struct {
		ID   int64   `json:"id"`
		Aids []int64 `json:"aids"`
	}, 0, len(items))
	for _, item := range items {
		page = append(page, struct {
			ID   int64   `json:"id"`
			Aids []int64 `json:"aids"`
		}{ID: item.BundleID, Aids: item.AppIDs})
	}
	raw, err := json.Marshal(page)
	if err != nil {
		return fmt.Errorf("拥有状态页序列化失败: %w", err)
	}
	sqlText := "WITH baseline AS (\n" +
		"    SELECT json_extract(value,'$.id') AS bundle_id, json_extract(value,'$.aids') AS aids\n" +
		"    FROM json_each(?1)\n" +
		")" + strings.ReplaceAll(ownershipSQLTemplate, "@primary@", "?2") + " SELECT * FROM ownership"
	rows, err := q.QueryContext(ctx, sqlText, string(raw), primary)
	if err != nil {
		return fmt.Errorf("拥有状态页查询失败: %w", err)
	}
	defer rows.Close()
	type ownershipRow struct {
		ownershipType sql.NullString
		familyAccount sql.NullString
	}
	byID := map[int64]ownershipRow{}
	for rows.Next() {
		var id int64
		var row ownershipRow
		if err := rows.Scan(&id, &row.ownershipType, &row.familyAccount); err != nil {
			return fmt.Errorf("拥有状态行读取失败: %w", err)
		}
		byID[id] = row
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("拥有状态行遍历失败: %w", err)
	}
	for i := range items {
		var kind *string
		var account *string
		if row, ok := byID[items[i].BundleID]; ok && row.ownershipType.Valid {
			t := row.ownershipType.String
			kind = &t
			if t == "family" && row.familyAccount.Valid {
				acc := row.familyAccount.String
				account = &acc
			}
		}
		items[i].Ownership = Ownership{Type: kind, Account: account}
	}
	return nil
}

// ── 行装载 ──

type bundleRow struct {
	BundleID          int64
	Name              string
	MustPurchaseAsSet sql.NullInt64
	ItemKind          sql.NullInt64
	HeaderImage       sql.NullString
	URL               sql.NullString
	MinCnyFen         sql.NullInt64
	DiffFen           sql.NullInt64
	SmartScore        sql.NullFloat64
}

type bundlePriceRow struct {
	id                 int64
	regionCode         string
	currency           sql.NullString
	price              sql.NullInt64
	discountPercent    sql.NullInt64
	bundleBaseDiscount sql.NullInt64
	cnyFen             sql.NullInt64
	discountEndTs      sql.NullInt64
	appIDs             sql.NullString
	crawledAt          sql.NullString
}

// loadBundles 主档 + 区价（按 id 排序，对齐 _load_bundles）。
func loadBundles(ctx context.Context, q interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}, ids []int64) ([]bundleRow, map[int64][]bundlePriceRow, error) {
	if len(ids) == 0 {
		return nil, map[int64][]bundlePriceRow{}, nil
	}
	ph := strings.TrimSuffix(strings.Repeat("?,", len(ids)), ",")
	bundleArgs := make([]any, 0, len(ids))
	for _, id := range ids {
		bundleArgs = append(bundleArgs, id)
	}
	bundleRows, err := q.QueryContext(ctx,
		"SELECT bundle_id,name,must_purchase_as_set,item_kind,header_image,url,min_cny_fen,diff_fen,smart_score "+
			"FROM bundles WHERE bundle_id IN ("+ph+")", bundleArgs...)
	if err != nil {
		return nil, nil, fmt.Errorf("捆绑包主档读取失败: %w", err)
	}
	defer bundleRows.Close()
	var bundles []bundleRow
	for bundleRows.Next() {
		var b bundleRow
		var name sql.NullString
		if err := bundleRows.Scan(&b.BundleID, &name, &b.MustPurchaseAsSet, &b.ItemKind, &b.HeaderImage,
			&b.URL, &b.MinCnyFen, &b.DiffFen, &b.SmartScore); err != nil {
			return nil, nil, fmt.Errorf("捆绑包主档行读取失败: %w", err)
		}
		b.Name = name.String
		bundles = append(bundles, b)
	}
	if err := bundleRows.Err(); err != nil {
		return nil, nil, fmt.Errorf("捆绑包主档行遍历失败: %w", err)
	}
	priceRows, err := q.QueryContext(ctx,
		"SELECT id,bundle_id,region_code,currency,price,discount_percent,bundle_base_discount,"+
			"cny_fen,discount_end_ts,app_ids,CAST(crawled_at AS TEXT) "+
			"FROM bundle_region_prices WHERE bundle_id IN ("+ph+") ORDER BY id", bundleArgs...)
	if err != nil {
		return nil, nil, fmt.Errorf("捆绑包区价读取失败: %w", err)
	}
	defer priceRows.Close()
	byBundle := map[int64][]bundlePriceRow{}
	for priceRows.Next() {
		var p bundlePriceRow
		var bundleID int64
		var regionCode sql.NullString
		if err := priceRows.Scan(&p.id, &bundleID, &regionCode, &p.currency, &p.price, &p.discountPercent,
			&p.bundleBaseDiscount, &p.cnyFen, &p.discountEndTs, &p.appIDs, &p.crawledAt); err != nil {
			return nil, nil, fmt.Errorf("捆绑包区价行读取失败: %w", err)
		}
		p.regionCode = regionCode.String
		byBundle[bundleID] = append(byBundle[bundleID], p)
	}
	if err := priceRows.Err(); err != nil {
		return nil, nil, fmt.Errorf("捆绑包区价行遍历失败: %w", err)
	}
	return bundles, byBundle, nil
}

// ── 单包聚合（_family_region_prices + _aggregate） ──

type regionEntry struct {
	code            string
	priceMinor      *int64
	currency        *string
	discountPercent int64
	baseDiscount    int64
	cnyFen          *int64
	discountEndsAt  *int64
	appIDs          []int64
	lockedCount     int
	formatted       string
}

type bundleAggregate struct {
	summary bundleSummary
	regions []regionEntry
}

// parseAppIDs app_ids JSON 列 → int 列表（脏数据容错：空/坏 JSON 回空表，
// 任一元素不可转 int 整体回空，对齐 _parse_app_ids）。
func parseAppIDs(raw sql.NullString) []int64 {
	out := []int64{}
	if !raw.Valid || raw.String == "" {
		return out
	}
	var items []any
	if json.Unmarshal([]byte(raw.String), &items) != nil {
		return out
	}
	for _, item := range items {
		switch n := item.(type) {
		case float64:
			out = append(out, int64(n))
		case string:
			v, err := strconv.ParseInt(strings.TrimSpace(n), 10, 64)
			if err != nil {
				return []int64{}
			}
			out = append(out, v)
		case bool:
			if n {
				out = append(out, 1)
			} else {
				out = append(out, 0)
			}
		default:
			return []int64{}
		}
	}
	return out
}

func nullInt64Ptr(v sql.NullInt64) *int64 {
	if !v.Valid {
		return nil
	}
	value := v.Int64
	return &value
}

// truthyIntPtr 0 与 NULL 都回 nil（对齐 int(p.discount_end_ts) if p.discount_end_ts else None）。
func truthyIntPtr(v sql.NullInt64) *int64 {
	if !v.Valid || v.Int64 == 0 {
		return nil
	}
	value := v.Int64
	return &value
}

func upperOrNull(v sql.NullString) *string {
	if !v.Valid || v.String == "" {
		return nil
	}
	s := strings.ToUpper(v.String)
	return &s
}

func aggregateBundle(b bundleRow, prices []bundlePriceRow, tracked map[string]bool) *bundleAggregate {
	mps := int64(-1)
	if b.MustPurchaseAsSet.Valid {
		mps = b.MustPurchaseAsSet.Int64
	}
	itemKind := int64(-1)
	if b.ItemKind.Valid {
		itemKind = b.ItemKind.Int64
	}
	// v3 之前的行 item_kind 尚未回填（-1）：形态沿用 mps 旧值兜底
	if itemKind != 0 && itemKind != 1 {
		itemKind = mps
	}

	// 基准行（appids 最多，先到者胜）+ 家族过滤（双产品隔离）
	type parsedRow struct {
		row  *bundlePriceRow
		aids []int64
	}
	var rows []parsedRow
	for i := range prices {
		p := &prices[i]
		if strings.TrimSpace(p.regionCode) == "" {
			continue
		}
		rows = append(rows, parsedRow{row: p, aids: parseAppIDs(p.appIDs)})
	}
	baselineAppIDs := []int64{}
	if len(rows) > 0 {
		best := 0
		for i := 1; i < len(rows); i++ {
			if len(rows[i].aids) > len(rows[best].aids) {
				best = i
			}
		}
		baselineAppIDs = rows[best].aids
	}
	active := rows
	if len(baselineAppIDs) > 0 {
		baselineSet := map[int64]bool{}
		for _, aid := range baselineAppIDs {
			baselineSet[aid] = true
		}
		var family []parsedRow
		for _, r := range rows {
			for _, aid := range r.aids {
				if baselineSet[aid] {
					family = append(family, r)
					break
				}
			}
		}
		if len(family) > 0 {
			active = family
		}
	}

	// 区表构建：同区去重（有价者优先），保留插入序（最低区命中取首条）
	var order []string
	entries := map[string]*regionEntry{}
	for _, r := range active {
		code := strings.ToUpper(r.row.regionCode)
		if len(r.aids) > len(baselineAppIDs) {
			baselineAppIDs = r.aids
		}
		entry := &regionEntry{
			code:           code,
			priceMinor:     nullInt64Ptr(r.row.price),
			currency:       upperOrNull(r.row.currency),
			cnyFen:         nullInt64Ptr(r.row.cnyFen),
			discountEndsAt: truthyIntPtr(r.row.discountEndTs),
			appIDs:         r.aids,
		}
		if r.row.discountPercent.Valid {
			entry.discountPercent = r.row.discountPercent.Int64
		}
		if r.row.bundleBaseDiscount.Valid {
			entry.baseDiscount = r.row.bundleBaseDiscount.Int64
		}
		if existing, ok := entries[code]; ok {
			if existing.cnyFen == nil && entry.cnyFen != nil {
				entries[code] = entry
			}
			continue
		}
		entries[code] = entry
		order = append(order, code)
	}

	// 追踪区过滤（CN 恒保留；PK/BD 同进同出）
	regions := make([]regionEntry, 0, len(order))
	for _, code := range order {
		if !keepRegion(code, tracked) {
			continue
		}
		entry := *entries[code]
		regions = append(regions, entry)
	}

	// 锁区数 / 格式化价（详情报文级字段；列表投影不带）
	baselineSet := map[int64]bool{}
	for _, aid := range baselineAppIDs {
		baselineSet[aid] = true
	}
	for i := range regions {
		missing := len(baselineSet)
		for _, aid := range regions[i].appIDs {
			if baselineSet[aid] {
				missing--
			}
		}
		regions[i].lockedCount = missing
		regions[i].formatted = ""
		if regions[i].priceMinor != nil {
			regions[i].formatted = formatMinorUnits(*regions[i].priceMinor, stringOrEmpty(regions[i].currency))
		}
	}

	// 最低价/最低区：min(国区价, 非国区最低快照)，展示级查表
	var cnCnyFen *int64
	if e, ok := entries["CN"]; ok {
		cnCnyFen = e.cnyFen
	}
	minCnyFen := nullInt64Ptr(b.MinCnyFen)
	var lowestCnyFen *int64
	for _, candidate := range []*int64{cnCnyFen, minCnyFen} {
		if candidate == nil {
			continue
		}
		if lowestCnyFen == nil || *candidate < *lowestCnyFen {
			lowestCnyFen = candidate
		}
	}
	lowestCode := ""
	if lowestCnyFen != nil {
		if cnCnyFen != nil && *cnCnyFen <= *lowestCnyFen {
			lowestCode = "cn"
		} else {
			for _, e := range regions {
				if e.code != "CN" && e.cnyFen != nil && *e.cnyFen == *lowestCnyFen {
					lowestCode = strings.ToLower(e.code)
					break
				}
			}
		}
	}

	var diffFen int64
	if b.DiffFen.Valid {
		diffFen = b.DiffFen.Int64
	}
	var smartScore float64
	if b.SmartScore.Valid {
		smartScore = b.SmartScore.Float64
	}

	header := normalizeImage(b.HeaderImage)
	if header == nil {
		header = fallbackImage(b.BundleID, itemKind)
	}
	return &bundleAggregate{
		summary: bundleSummary{
			BundleID:          b.BundleID,
			Name:              b.Name,
			HeaderImage:       *header,
			URL:               storeURL(b, itemKind),
			MustPurchaseAsSet: mps,
			ItemKind:          itemKind,
			AppIDs:            baselineAppIDs,
			CnCnyFen:          cnCnyFen,
			LowestRegion:      lowestCode,
			LowestCnyFen:      lowestCnyFen,
			DiffFen:           diffFen,
			SmartScore:        smartScore,
			PriceData:         priceDataOf(prices),
		},
		regions: regions,
	}
}

// keepRegion 追踪区保留判定：CN 恒保留；PK/BD 同进同出（启用含 PK 才放行两行）。
func keepRegion(code string, tracked map[string]bool) bool {
	if tracked == nil {
		return true
	}
	if code == "CN" {
		return true
	}
	if code == "PK" || code == "BD" {
		return tracked["PK"]
	}
	return tracked[code]
}

func (agg *bundleAggregate) listItem() BundleListItem {
	rp := make(map[string]ListRegionPrice, len(agg.regions))
	for _, e := range agg.regions {
		rp[e.code] = ListRegionPrice{
			PriceMinor:      e.priceMinor,
			Currency:        e.currency,
			DiscountPercent: e.discountPercent,
			BaseDiscount:    e.baseDiscount,
			CnyFen:          e.cnyFen,
			DiscountEndsAt:  e.discountEndsAt,
		}
	}
	return BundleListItem{bundleSummary: agg.summary, RegionPrices: rp}
}

func stringOrEmpty(v *string) string {
	if v == nil {
		return ""
	}
	return *v
}

// ── 展示期归一与兜底 ──

var imgCDNHosts = regexp.MustCompile(`(?i)^https?://shared\.(?:fastly\.steamstatic|cdn\.queniuqe)\.com`)

const imgCDNReplacement = "https://shared.akamai.steamstatic.com"

// normalizeImage 图片 CDN 域归一化：fastly/queniuqe → akamai（路径不变）。
// 空/无匹配回 nil（调用方兜底）。
func normalizeImage(raw sql.NullString) *string {
	if !raw.Valid || raw.String == "" {
		return nil
	}
	normalized := imgCDNHosts.ReplaceAllString(raw.String, imgCDNReplacement)
	return &normalized
}

func storeURL(b bundleRow, itemKind int64) string {
	if b.URL.Valid && b.URL.String != "" {
		return b.URL.String
	}
	kind := "bundle"
	if itemKind == 1 {
		kind = "sub"
	}
	return fmt.Sprintf("https://store.steampowered.com/%s/%d/", kind, b.BundleID)
}

func fallbackImage(bundleID, itemKind int64) *string {
	kind := "bundles"
	if itemKind == 1 {
		kind = "subs"
	}
	url := fmt.Sprintf("https://shared.akamai.steamstatic.com/store_item_assets/steam/%s/%d/header.jpg", kind, bundleID)
	return &url
}

// ── priceData（freshness_of 同口径；辅助函数与 games/bills 包自持同款） ──

func priceDataOf(prices []bundlePriceRow) PriceData {
	var maxTime *time.Time
	var maxText string
	for i := range prices {
		if !prices[i].crawledAt.Valid || prices[i].crawledAt.String == "" {
			continue
		}
		t, ok := parseStoredTime(prices[i].crawledAt.String)
		if !ok {
			continue
		}
		if maxTime == nil || t.After(*maxTime) {
			tt := t
			maxTime = &tt
			maxText = prices[i].crawledAt.String
		}
	}
	if maxTime == nil {
		return PriceData{}
	}
	observedAt := goIsoformat(maxText)
	age := roundN(pyHours(time.Now().Sub(*maxTime)), 3)
	bucket := "stale"
	switch {
	case age < 6.0:
		bucket = "fresh"
	case age < 12.0:
		bucket = "lagging"
	}
	return PriceData{ObservedAt: &observedAt, AgeHours: &age, Freshness: &bucket}
}

// goIsoformat 库内 naive 文本（空格分隔）→ Python datetime.isoformat 形态
// （T 分隔；微秒为 0 时省略小数段）。无法解析时原样返回。
func goIsoformat(text string) string {
	t, ok := parseStoredTime(text)
	if !ok {
		return text
	}
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05")
	}
	return t.Format("2006-01-02T15:04:05.000000")
}

// pyHours 复刻 Python (reference - observed).total_seconds() / 3600.0 的浮点
// 路径：微秒整数（datetime 精度）→ /1e6 → /3600 两次除法。
func pyHours(d time.Duration) float64 {
	return (float64(int64(d)/1000) / 1e6) / 3600.0
}

// roundN 四舍六入五成双保留 n 位小数（Python round 口径）。
func roundN(x float64, n int) float64 {
	p := math.Pow10(n)
	return math.RoundToEven(x*p) / p
}

// parseStoredTime 库内 datetime 文本（naive）解析；按服务器本地时区解释。
func parseStoredTime(text string) (time.Time, bool) {
	t, err := time.ParseInLocation("2006-01-02 15:04:05.999999999", text, time.Local)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

// ── formatMinorUnits：cents → 本币字符串（Python pricing.format_minor_units 同式；
// games 包反向依赖本包，不能 import，自持一份） ──

// displayDecimals 各币种显示小数位（存储统一 2 位小数 cents）；缺省 2。
var displayDecimals = map[string]int{
	"CLP": 0, "VND": 0, "IDR": 0, "JPY": 0, "KZT": 0, "UAH": 0,
	"CNY": 2, "USD": 2, "RUB": 2, "INR": 2, "BRL": 2, "HKD": 2,
	"PHP": 2, "TWD": 2,
}

var currencySymbols = map[string]string{
	"CNY": "¥", "RUB": "₽", "KZT": "₸", "UAH": "₴",
	"USD": "$", "VND": "₫", "IDR": "Rp", "INR": "₹",
	"BRL": "R$", "CLP": "$", "JPY": "¥", "HKD": "HK$", "PHP": "₱",
	"TWD": "NT$", "KWD": "KD", "SAR": "SR", "ZAR": "R", "QAR": "QR",
	"MYR": "RM", "THB": "฿", "PEN": "S/", "MXN": "MX$", "SGD": "S$",
	"AED": "AED", "UYU": "$U", "COP": "COL$", "KRW": "₩", "NZD": "NZ$",
	"PLN": "zł", "CRC": "₡", "CAD": "C$", "AUD": "A$", "EUR": "€",
	"GBP": "£", "NOK": "kr", "ILS": "₪", "CHF": "CHF",
}

// suffixCurrencies 符号后置的币种（"55.00 ₽"）。
var suffixCurrencies = map[string]struct{}{
	"RUB": {}, "KZT": {}, "UAH": {}, "PLN": {}, "NOK": {},
}

// formatMinorUnits cents → 本币字符串（"¥42.00" / "608" / "55.00 ₽"）；空值回空串。
func formatMinorUnits(minor int64, currency string) string {
	c := strings.ToUpper(currency)
	symbol := currencySymbols[c]
	decimals, ok := displayDecimals[c]
	if !ok {
		decimals = 2
	}
	major := float64(minor) / 100
	sign := ""
	if major < 0 {
		sign = "-"
		major = -major
	}
	var formatted string
	if decimals == 0 {
		formatted = strconv.FormatInt(int64(math.RoundToEven(major)), 10)
		formatted = groupCommas(formatted)
	} else {
		s := strconv.FormatFloat(major, 'f', decimals, 64)
		if dot := strings.IndexByte(s, '.'); dot >= 0 {
			formatted = groupCommas(s[:dot]) + s[dot:]
		} else {
			formatted = groupCommas(s)
		}
	}
	if _, ok := suffixCurrencies[c]; ok {
		if symbol == "" {
			return sign + formatted
		}
		return sign + formatted + " " + symbol
	}
	return sign + symbol + formatted
}

// groupCommas 整数部分千分位逗号分组。
func groupCommas(s string) string {
	n := len(s)
	if n <= 3 {
		return s
	}
	var b strings.Builder
	pre := n % 3
	if pre > 0 {
		b.WriteString(s[:pre])
		if n > pre {
			b.WriteByte(',')
		}
	}
	for i := pre; i < n; i += 3 {
		b.WriteString(s[i : i+3])
		if i+3 < n {
			b.WriteByte(',')
		}
	}
	return b.String()
}
