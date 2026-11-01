package games

import (
	"context"
	"database/sql"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const toleranceFenDefault = 500 // 近似全区最低容差（分）
const dlcExemptAppids = 2778580

const (
	hlPriorityCase = "CASE WHEN cp1.discount_percent = 0 THEN 0 WHEN cp1.discount_percent IS NULL THEN 0" +
		" WHEN g.hl_flag = 1 THEN (CASE WHEN g.review_count > 1000 THEN 4 ELSE 3 END)" +
		" WHEN g.hl_flag = 2 THEN 3 WHEN g.hl_flag = 3 THEN 2 ELSE 1 END"
	regionGroupCase = "CASE WHEN g.hl_flag IN (1, 2) THEN 0 WHEN cp1.discount_percent > 0 THEN 1 ELSE 2 END"
	complexSort     = "(cp1.price IS NOT NULL AND cp1.price > 0) DESC, " + hlPriorityCase + " DESC," +
		" g.diff_fen / 1000 DESC, g.review_count / 1000 DESC, g.positive_rate DESC"
)

var (
	flagPattern       = regexp.MustCompile(`^(|hl|pp|any|(new|flat|nonhl)(,(new|flat|nonhl))*)$`)
	giftModePattern   = regexp.MustCompile(`^(|out|in)$`)
	giftSenderPattern = regexp.MustCompile(`^(|[a-z]{2})$`)
	giftReceiversPat  = regexp.MustCompile(`^([a-z]{2}(,[a-z]{2})*)?$`)
	diffTypePattern   = regexp.MustCompile(`^(absolute|percent)$`)
)

type listParams struct {
	sort           string
	limit          int
	after          string
	q              string
	region         string
	filterMode     string
	onlyDiscounted bool
	isLowest       bool
	flag           string
	giftMode       string
	giftSender     string
	giftReceivers  string
	minRating      int
	maxRating      *int
	minReviews     int
	maxReviews     *int
	minPrice       *int
	maxPrice       *int
	onlyHb         bool
	onlyEpic       bool
	onlyXgp        bool
	diffMinFen     *int
	diffMaxFen     *int
	diffType       string
	toleranceFen   *int
	strictLowest   bool
	excludeDlc     bool
	removed        bool
}

// parseListParams 查询参数解析；形状越界回 false（400）。
// hideOwned / hideFamilySharing / wishlistPriority 依赖其他域（wishlist / account），
// 本包不实现：对应开关忽略，行为以无关注集 / 无愿望单集的降级路径呈现。
func parseListParams(r *http.Request) (listParams, bool) {
	q := r.URL.Query()
	p := listParams{
		sort:          q.Get("sort"),
		after:         q.Get("after"),
		q:             q.Get("q"),
		region:        q.Get("region"),
		filterMode:    q.Get("filterMode"),
		flag:          q.Get("flag"),
		giftMode:      q.Get("giftMode"),
		giftSender:    q.Get("giftSender"),
		giftReceivers: q.Get("giftReceivers"),
		diffType:      q.Get("diffType"),
	}
	if p.sort == "" {
		p.sort = "default"
	}
	if p.filterMode == "" {
		p.filterMode = "global"
	}
	if p.diffType == "" {
		p.diffType = "absolute"
	}
	if len(p.q) > 200 || len(p.region) > 6 {
		return p, false
	}
	if !flagPattern.MatchString(p.flag) || !giftModePattern.MatchString(p.giftMode) ||
		!giftSenderPattern.MatchString(p.giftSender) || !giftReceiversPat.MatchString(p.giftReceivers) ||
		!diffTypePattern.MatchString(p.diffType) {
		return p, false
	}
	p.region = strings.ToUpper(strings.TrimSpace(p.region))

	limit := 40
	if v, ok := intParam(q, "limit"); ok {
		if v == nil {
			return p, false
		}
		limit = *v
	}
	if limit < 1 || limit > 100 {
		return p, false
	}
	p.limit = limit

	var err error
	if p.onlyDiscounted, err = boolParam(q, "onlyDiscounted"); err != nil {
		return p, false
	}
	if p.isLowest, err = boolParam(q, "isLowest"); err != nil {
		return p, false
	}
	if p.onlyHb, err = boolParam(q, "onlyHb"); err != nil {
		return p, false
	}
	if p.onlyEpic, err = boolParam(q, "onlyEpic"); err != nil {
		return p, false
	}
	if p.onlyXgp, err = boolParam(q, "onlyXgp"); err != nil {
		return p, false
	}
	if p.strictLowest, err = boolParam(q, "strictLowest"); err != nil {
		return p, false
	}
	if p.excludeDlc, err = boolParam(q, "excludeDlc"); err != nil {
		return p, false
	}
	if p.removed, err = boolParam(q, "removed"); err != nil {
		return p, false
	}

	minRating := 0
	if v, ok := intParam(q, "minRating"); ok {
		if v == nil {
			return p, false
		}
		minRating = *v
	}
	if minRating < 0 || minRating > 100 {
		return p, false
	}
	p.minRating = minRating
	if v, ok := intParam(q, "maxRating"); ok {
		if v == nil || *v < 0 || *v > 100 {
			return p, false
		}
		p.maxRating = v
	}
	minReviews := 0
	if v, ok := intParam(q, "minReviews"); ok {
		if v == nil {
			return p, false
		}
		minReviews = *v
	}
	if minReviews < 0 {
		return p, false
	}
	p.minReviews = minReviews
	if v, ok := intParam(q, "maxReviews"); ok {
		if v == nil || *v < 0 {
			return p, false
		}
		p.maxReviews = v
	}
	if v, ok := intParam(q, "minPrice"); ok {
		if v == nil {
			return p, false
		}
		p.minPrice = v
	}
	if v, ok := intParam(q, "maxPrice"); ok {
		if v == nil {
			return p, false
		}
		p.maxPrice = v
	}
	if v, ok := intParam(q, "diffMin"); ok {
		if v == nil {
			return p, false
		}
		p.diffMinFen = v
	}
	if v, ok := intParam(q, "diffMax"); ok {
		if v == nil {
			return p, false
		}
		p.diffMaxFen = v
	}
	if v, ok := intParam(q, "toleranceFen"); ok {
		if v == nil || *v < 0 {
			return p, false
		}
		p.toleranceFen = v
	}
	return p, true
}

func intParam(q map[string][]string, key string) (*int, bool) {
	vals, ok := q[key]
	if !ok {
		return nil, false
	}
	s := vals[0]
	if s == "" {
		return nil, true
	}
	v, err := strconv.Atoi(s)
	if err != nil {
		return nil, true
	}
	return &v, true
}

func boolParam(q map[string][]string, key string) (bool, error) {
	vals, ok := q[key]
	if !ok {
		return false, nil
	}
	v, err := strconv.ParseBool(vals[0])
	if err != nil {
		return false, err
	}
	return v, nil
}

func parseAppID(s string) (int64, error) {
	return strconv.ParseInt(s, 10, 64)
}

// decodeCursor after = base64({"offset": N})；解析失败从 0 开始。
func decodeCursor(after string) int {
	if after == "" {
		return 0
	}
	raw, err := base64.URLEncoding.DecodeString(after)
	if err != nil {
		raw, err = base64.RawURLEncoding.DecodeString(after)
		if err != nil {
			return 0
		}
	}
	var data struct {
		Offset int `json:"offset"`
	}
	if json.Unmarshal(raw, &data) != nil {
		return 0
	}
	if data.Offset < 0 {
		return 0
	}
	return data.Offset
}

// encodeCursor 与 Python 侧游标字节兼容：{"offset": N}（冒号后带空格）。
func encodeCursor(offset int) string {
	return base64.URLEncoding.EncodeToString([]byte(fmt.Sprintf(`{"offset": %d}`, offset)))
}

// beijingCutoff 永降徽章时效窗起点（UTC+8 naive，天窗 14 天）。
func beijingCutoff() string {
	t := time.Now().UTC().Add(8*time.Hour).AddDate(0, 0, -14)
	return t.Format("2006-01-02 15:04:05.000000")
}

// listGames 游戏列表：全部筛选 / 排序 / 分页在 SQL 完成，价格明细按页取。
// sort=top100 依赖外部榜单抓取，本包按「榜不可用回空集」的降级路径返回。
func (h *handler) listGames(ctx context.Context, p listParams) (map[string]any, error) {
	if p.sort == "top100" {
		return map[string]any{"items": []listItem{}, "total": 0, "hasMore": false, "nextCursor": nil}, nil
	}

	offset := decodeCursor(p.after)
	regionCode := p.region
	isLocked := regionCode == "LOCKED"
	hasRegion := regionCode != "" && regionCode != "CN" && regionCode != "LOCKED"

	var conds []string
	var args []any
	conds = append(conds, "g.name IS NOT NULL", "g.name != ''")
	tolerance := toleranceFenDefault
	if p.toleranceFen != nil {
		tolerance = *p.toleranceFen
	}
	if isLocked {
		conds = append(conds, "cp1.appid IS NULL")
	} else {
		conds = append(conds, "cp1.price IS NOT NULL", "cp1.price > 0")
		if regionCode == "CN" {
			conds = append(conds, "COALESCE(g.min_cny_fen, cp1.price) >= cp1.price - ?")
			args = append(args, tolerance)
		}
		if p.isLowest {
			conds = append(conds, "COALESCE(g.min_cny_fen, cp1.price) >= cp1.price - ?",
				"cp1.cny_fen IS NOT NULL")
			args = append(args, tolerance)
		}
	}
	if p.strictLowest {
		t := 0
		if p.toleranceFen != nil {
			t = *p.toleranceFen
		}
		conds = append(conds, "g.diff_fen > ?")
		args = append(args, t)
	}
	if hasRegion {
		conds = append(conds, "cp1.cny_fen IS NOT NULL")
		switch p.filterMode {
		case "cheaper":
			conds = append(conds, "cp2.cny_fen < cp1.price - 100")
		case "highdiff":
			conds = append(conds,
				"(cp1.discount_percent = 0 OR cp1.discount_percent IS NULL)",
				"cp1.price - cp2.cny_fen >= 5000",
				"COALESCE(g.min_cny_fen, cp2.cny_fen) >= cp2.cny_fen - ?")
			args = append(args, tolerance)
		default:
			conds = append(conds,
				"cp2.cny_fen < cp1.price - 100",
				"COALESCE(g.min_cny_fen, cp2.cny_fen) >= cp2.cny_fen - ?")
			args = append(args, tolerance)
		}
	}
	if p.q != "" {
		like := "%" + p.q + "%"
		conds = append(conds, "(g.name LIKE ? OR g.name_en LIKE ?)")
		args = append(args, like, like)
	}
	if p.onlyDiscounted {
		conds = append(conds, "cp1.discount_percent > 0")
	}
	if p.minRating > 0 {
		conds = append(conds, "g.positive_rate >= ?")
		args = append(args, p.minRating*100)
	}
	if p.maxRating != nil && *p.maxRating < 100 {
		conds = append(conds, "g.positive_rate <= ?")
		args = append(args, *p.maxRating*100)
	}
	if p.minReviews > 0 {
		conds = append(conds, "g.review_count >= ?")
		args = append(args, p.minReviews)
	}
	if p.maxReviews != nil && *p.maxReviews > 0 {
		conds = append(conds, "g.review_count <= ?")
		args = append(args, *p.maxReviews)
	}
	if p.minPrice != nil {
		conds = append(conds, "cp1.price >= ?")
		args = append(args, *p.minPrice)
	}
	if p.maxPrice != nil {
		conds = append(conds, "cp1.price <= ?")
		args = append(args, *p.maxPrice)
	}
	if p.diffMinFen != nil || p.diffMaxFen != nil {
		expr := "g.diff_fen"
		if hasRegion {
			expr = "cp1.price - cp2.cny_fen"
		}
		if p.diffType == "percent" {
			if p.diffMinFen != nil {
				conds = append(conds, "("+expr+") * 100 >= ? * cp1.price")
				args = append(args, *p.diffMinFen)
			}
			if p.diffMaxFen != nil {
				conds = append(conds, "("+expr+") * 100 <= ? * cp1.price")
				args = append(args, *p.diffMaxFen)
			}
		} else {
			if p.diffMinFen != nil {
				conds = append(conds, "("+expr+") >= ?")
				args = append(args, *p.diffMinFen)
			}
			if p.diffMaxFen != nil {
				conds = append(conds, "("+expr+") <= ?")
				args = append(args, *p.diffMaxFen)
			}
		}
	}
	if p.sort == "new2026" {
		conds = append(conds, "g.release_date LIKE '2026%'")
	}
	if p.onlyHb {
		conds = append(conds, "g.is_hb = 1")
	}
	if p.onlyEpic {
		conds = append(conds, "g.is_epic = 1")
	}
	if p.onlyXgp {
		conds = append(conds, "g.xgp_tier IS NOT NULL")
	}
	if err := appendFlagCondition(p.flag, &conds, &args); err != nil {
		return nil, err
	}
	appendGiftCondition(p, &conds, &args)
	if p.excludeDlc {
		conds = append(conds, fmt.Sprintf(
			"(g.type IS NULL OR g.type != 'DLC' OR g.appid IN (%d))", dlcExemptAppids))
	}
	if p.removed {
		conds = append(conds, "g.appid IN (SELECT appid FROM catalog_removals)")
	} else {
		conds = append(conds, "g.appid NOT IN (SELECT appid FROM catalog_removals)")
	}

	join := " JOIN game_current_prices cp1 ON cp1.appid = g.appid AND cp1.region_code = 'CN' AND cp1.price_status = 'ok'"
	if isLocked {
		join = " LEFT" + join
	}
	srCol, srJoin, headArgs := "", "", args
	if hasRegion {
		srCol = ", cp2.cny_fen AS sr_cny_fen"
		srJoin = " JOIN game_current_prices cp2 ON cp2.appid = g.appid AND cp2.region_code = ?" +
			" AND cp2.price_status = 'ok' AND cp2.cny_fen IS NOT NULL"
		headArgs = append([]any{regionCode}, args...)
	}
	where := strings.Join(conds, " AND ")

	base := "SELECT 1 FROM games g" + join + srJoin + " WHERE " + where
	var total int64
	if err := h.db.QueryRowContext(ctx, "SELECT COUNT(*) FROM ("+base+")", headArgs...).Scan(&total); err != nil {
		return nil, fmt.Errorf("游戏列表计数查询失败: %w", err)
	}

	order := buildOrderBy(p.sort, hasRegion)
	if isLocked {
		order = "COALESCE(g.min_cny_fen, 999999) ASC, g.appid DESC"
	}
	query := "SELECT g.appid, g.name, g.name_en, g.type, g.positive_rate, g.review_count," +
		" g.is_adult, g.xgp_tier, g.is_visual_novel, g.release_date, g.min_cny_fen, g.diff_fen," +
		" g.smart_score, g.header_image, g.hl_flag, g.pp_flag, CAST(g.pp_changed_at AS TEXT), CAST(g.updated_at AS TEXT)," +
		" g.family_sharing, g.trading_cards, g.series_id, g.is_hb, g.is_epic, g.epic_date," +
		" g.hb_data, g.bundle_count, g.removed_at," +
		" cp1.appid AS cn_appid, cp1.price AS cn_price, cp1.discount_percent AS cn_discount_percent," +
		" cp1.discount_end_ts AS cn_discount_end_ts, cp1.cny_fen AS cn_cny_fen" + srCol +
		" FROM games g" + join + srJoin + " WHERE " + where +
		" ORDER BY " + order + " LIMIT ? OFFSET ?"
	rows, err := h.db.QueryContext(ctx, query, append(headArgs, p.limit+1, offset)...)
	if err != nil {
		return nil, fmt.Errorf("游戏列表查询失败: %w", err)
	}
	defer rows.Close()

	type listRow struct {
		game gameRow
		cn   *cnRow
		sr   *int64
	}
	var listRows []listRow
	for rows.Next() {
		var gr gameRow
		var cnAppid, cnPrice, cnDiscount, cnDiscountEnd, cnCnyFen sql.NullInt64
		dest := []any{
			&gr.appid, &gr.name, &gr.nameEn, &gr.typ, &gr.positiveRate, &gr.reviewCount,
			&gr.isAdult, &gr.xgpTier, &gr.isVisualNovel, &gr.releaseDate, &gr.minCnyFen, &gr.diffFen,
			&gr.smartScore, &gr.headerImage, &gr.hlFlag, &gr.ppFlag, &gr.ppChangedAt, &gr.updatedAt,
			&gr.familySharing, &gr.tradingCards, &gr.seriesID, &gr.isHb, &gr.isEpic, &gr.epicDate,
			&gr.hbData, &gr.bundleCount, &gr.removedAt,
			&cnAppid, &cnPrice, &cnDiscount, &cnDiscountEnd, &cnCnyFen,
		}
		var sr sql.NullInt64
		if hasRegion {
			dest = append(dest, &sr)
		}
		if err := rows.Scan(dest...); err != nil {
			return nil, fmt.Errorf("游戏列表行读取失败: %w", err)
		}
		lr := listRow{game: gr}
		if cnAppid.Valid {
			lr.cn = &cnRow{
				price:           nullableInt64(cnPrice),
				discountPercent: cnDiscount.Int64,
				discountEndTs:   nullableInt64(cnDiscountEnd),
				cnyFen:          nullableInt64(cnCnyFen),
			}
		}
		if hasRegion && sr.Valid {
			v := sr.Int64
			lr.sr = &v
		}
		listRows = append(listRows, lr)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("游戏列表行遍历失败: %w", err)
	}

	hasMore := len(listRows) > p.limit
	if hasMore {
		listRows = listRows[:p.limit]
	}

	appids := make([]int64, 0, len(listRows))
	for _, r := range listRows {
		appids = append(appids, r.game.appid)
	}
	priceRows, err := h.loadPagePrices(ctx, appids)
	if err != nil {
		return nil, err
	}
	regionsExpected := h.enabledRegions(ctx)
	pending, err := h.pendingUnits(ctx, appids)
	if err != nil {
		return nil, err
	}

	items := make([]listItem, 0, len(listRows))
	for _, r := range listRows {
		items = append(items, buildListItem(r.game, r.cn, priceRows[r.game.appid], regionsExpected, pending))
	}
	var nextCursor any
	if hasMore {
		nextCursor = encodeCursor(offset + p.limit)
	}
	return map[string]any{
		"items":      items,
		"total":      total,
		"hasMore":    hasMore,
		"nextCursor": nextCursor,
	}, nil
}

// appendFlagCondition 史低 / 永降标记过滤（三值 + 可组合三态）。
func appendFlagCondition(flag string, conds *[]string, args *[]any) error {
	switch flag {
	case "":
	case "hl":
		*conds = append(*conds, "g.hl_flag IN (1, 2)")
	case "pp":
		*conds = append(*conds, "g.pp_flag = 1")
	case "any":
		*conds = append(*conds,
			"(g.hl_flag IN (1, 2) OR (g.pp_flag = 1 AND COALESCE(cp1.discount_percent, 0) = 0"+
				" AND g.pp_changed_at IS NOT NULL AND g.pp_changed_at >= ?))")
		*args = append(*args, beijingCutoff())
	default:
		var parts []string
		for _, part := range strings.Split(flag, ",") {
			switch part {
			case "new":
				parts = append(parts, "g.hl_flag = 1")
			case "flat":
				parts = append(parts, "g.hl_flag = 2")
			case "nonhl":
				parts = append(parts, "g.hl_flag NOT IN (1, 2)")
			}
		}
		if len(parts) > 0 {
			*conds = append(*conds, "("+strings.Join(parts, " OR ")+")")
		}
	}
	return nil
}

// appendGiftCondition 送礼价轨 EXISTS（收礼侧 ≤ 送礼侧 × 1.15）。
func appendGiftCondition(p listParams, conds *[]string, args *[]any) {
	var targets []string
	for _, part := range strings.Split(p.giftReceivers, ",") {
		t := strings.ToUpper(strings.TrimSpace(part))
		if t != "" {
			targets = append(targets, t)
		}
	}
	sender := strings.ToUpper(strings.TrimSpace(p.giftSender))
	priced := "s1.price_status = 'ok' AND s1.cny_fen IS NOT NULL AND s1.cny_fen > 0" +
		" AND t1.price_status = 'ok' AND t1.cny_fen IS NOT NULL AND t1.cny_fen > 0"
	pair := "s1.appid = g.appid AND t1.appid = g.appid"
	switch {
	case p.giftMode == "out" && sender != "" && len(targets) > 0:
		ph := strings.TrimSuffix(strings.Repeat("?,", len(targets)), ",")
		*conds = append(*conds,
			"EXISTS (SELECT 1 FROM game_current_prices s1, game_current_prices t1 WHERE "+pair+
				" AND s1.region_code = ? AND t1.region_code != ? AND t1.region_code IN ("+ph+")"+
				" AND t1.cny_fen * 100 <= s1.cny_fen * 115 AND "+priced+")")
		*args = append(*args, sender, sender)
		for _, t := range targets {
			*args = append(*args, t)
		}
	case p.giftMode == "in" && len(targets) > 0:
		*conds = append(*conds,
			"EXISTS (SELECT 1 FROM game_current_prices s1, game_current_prices t1 WHERE "+pair+
				" AND t1.region_code = ? AND s1.region_code != ?"+
				" AND t1.cny_fen * 100 <= s1.cny_fen * 115 AND "+priced+")")
		*args = append(*args, targets[0], targets[0])
	}
}

// buildOrderBy 排序规则：地区模式带 3-group 前缀，其余按 sort 分支。
func buildOrderBy(sort string, regionMode bool) string {
	if sort == "smart" {
		return "(cp1.price IS NOT NULL AND cp1.price > 0) DESC, g.smart_score DESC, g.appid DESC"
	}
	prefix := ""
	if regionMode {
		prefix = regionGroupCase + ", "
		if sort == "rate" {
			return prefix + "g.review_count / 1000 DESC, g.positive_rate DESC, g.appid DESC"
		}
		if sort == "new2026" {
			return prefix + "g.release_date DESC, g.appid DESC"
		}
		return prefix + "(cp1.price - cp2.cny_fen) DESC, g.positive_rate DESC, g.appid DESC"
	}
	switch sort {
	case "rate":
		return "g.review_count / 1000 DESC, g.positive_rate DESC, g.appid DESC"
	case "discount":
		return "cp1.discount_percent DESC, " + complexSort + ", g.appid DESC"
	case "new2026":
		return "g.release_date DESC, g.appid DESC"
	case "updated":
		return "g.updated_at DESC, g.appid DESC"
	case "diff":
		return "g.diff_fen / 500 DESC, " + complexSort + ", g.appid DESC"
	default:
		return complexSort + ", g.appid DESC"
	}
}

// jobTargetUnitsSQL 失败作业的目标单位 CTE（对齐 crawl service 同名常量）。
const jobTargetUnitsSQL = `
WITH target_jobs AS MATERIALIZED (
    SELECT id, json_extract(stats_json, '$.targets.appids') AS appids,
           json_extract(stats_json, '$.targets.regions') AS regions,
           json_extract(stats_json, '$.targets.tasks') AS tasks,
           replace(json_extract(stats_json, '$.targetStartedAt'), 'T', ' ') AS threshold
    FROM crawl_jobs
    WHERE status IN ('failed','interrupted')
      AND NOT EXISTS (SELECT 1 FROM price_cycles c WHERE c.id = crawl_jobs.cycle_id AND c.status = 'cancelled')
      AND json_type(stats_json, '$.targets') = 'object'
      AND json_extract(stats_json, '$.targetStartedAt') IS NOT NULL
      AND (?1 IS NULL OR id = ?1)
), units AS (
    SELECT j.id AS job_id, CAST(a.value AS INTEGER) AS appid,
           upper(r.value) AS region, j.threshold
    FROM target_jobs j, json_each(j.appids) a, json_each(j.regions) r
    UNION ALL
    SELECT j.id, CAST(a.value AS INTEGER),
           upper(json_extract(t.value, '$.region')), j.threshold
    FROM target_jobs j, json_each(j.tasks) t,
         json_each(t.value, '$.appids') a
), skipped AS (
    SELECT s.value AS unit,
           MAX(replace(json_extract(j.stats_json, '$.targetStartedAt'), 'T', ' ')) AS threshold
    FROM crawl_jobs j, json_each(j.stats_json, '$.skipUnits') s
    WHERE replace(json_extract(j.stats_json, '$.targetStartedAt'), 'T', ' ')
          >= (SELECT MIN(threshold) FROM target_jobs)
    GROUP BY s.value
), unit_results AS (
    SELECT u.*, CASE WHEN (
        p.price_status IN ('ok','locked','missing','blocked')
        AND (p.updated_at >= u.threshold OR p.last_success_at >= u.threshold)
    ) OR s.threshold >= u.threshold THEN 1 ELSE 0 END AS settled
    FROM units u
    LEFT JOIN game_current_prices p ON p.appid = u.appid AND p.region_code = u.region
    LEFT JOIN skipped s ON s.unit = CAST(u.appid AS TEXT) || ':' || u.region
), unresolved AS (
    SELECT DISTINCT appid, region, threshold FROM unit_results WHERE settled = 0
)`

// pendingUnits 未观察欠账集（对齐 _unobserved_job_targets 的 cooldown=0 /
// 页内 appid / 不限行形态）：失败作业目标单位减去已观察单位。
func (h *handler) pendingUnits(ctx context.Context, appids []int64) (map[string]bool, error) {
	pending := map[string]bool{}
	if len(appids) == 0 {
		return pending, nil
	}
	ph := strings.TrimSuffix(strings.Repeat("?,", len(appids)), ",")
	cutoff := time.Now().UTC().Add(8 * time.Hour).Format("2006-01-02 15:04:05.000000")
	query := jobTargetUnitsSQL + " SELECT DISTINCT appid, region FROM unresolved" +
		" WHERE threshold < ?1 AND appid IN (" + ph + ")"
	args := make([]any, 0, len(appids)+2)
	args = append(args, nil, cutoff)
	for _, a := range appids {
		args = append(args, a)
	}
	rows, err := h.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("欠账单位查询失败: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var appid int64
		var region string
		if err := rows.Scan(&appid, &region); err != nil {
			return nil, fmt.Errorf("欠账单位行读取失败: %w", err)
		}
		pending[strconv.FormatInt(appid, 10)+":"+region] = true
	}
	return pending, rows.Err()
}

// loadPagePrices 页级取价：只拉给定 appid 的全区价格行（含非 ok 状态行）。
func (h *handler) loadPagePrices(ctx context.Context, appids []int64) (map[int64][]priceRow, error) {
	out := make(map[int64][]priceRow)
	if len(appids) == 0 {
		return out, nil
	}
	ph := strings.TrimSuffix(strings.Repeat("?,", len(appids)), ",")
	query := "SELECT appid, region_code, currency, price, discount_percent, price_status, cny_fen," +
		" attempt_outcome, steam_answer, CAST(last_success_at AS TEXT), CAST(updated_at AS TEXT)" +
		" FROM game_current_prices WHERE appid IN (" + ph + ")"
	args := make([]any, 0, len(appids))
	for _, a := range appids {
		args = append(args, a)
	}
	rows, err := h.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, fmt.Errorf("页级价格查询失败: %w", err)
	}
	defer rows.Close()
	for rows.Next() {
		var p priceRow
		if err := rows.Scan(&p.appid, &p.regionCode, &p.currency, &p.price, &p.discountPercent,
			&p.priceStatus, &p.cnyFen, &p.attemptOutcome, &p.steamAnswer, &p.lastSuccessAt, &p.updatedAt); err != nil {
			return nil, fmt.Errorf("页级价格行读取失败: %w", err)
		}
		out[p.appid] = append(out[p.appid], p)
	}
	return out, rows.Err()
}

// enabledRegions 启用区服（覆盖分母）；读取失败按空集降级，列表照常服务。
func (h *handler) enabledRegions(ctx context.Context) []string {
	rows, err := h.db.QueryContext(ctx,
		"SELECT code FROM crawl_regions WHERE enabled = 1 ORDER BY sort, code")
	if err != nil {
		return nil
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var code string
		if err := rows.Scan(&code); err != nil {
			return nil
		}
		out = append(out, strings.ToUpper(code))
	}
	if err := rows.Err(); err != nil {
		return nil
	}
	return out
}
