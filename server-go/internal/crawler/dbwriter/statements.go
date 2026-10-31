package dbwriter

// 批量语句体：games / 现价 / 历史 / 欠账 / 基线 / 复活清标。
// UPSERT 键与 SET 子句逐字对齐现网语义，禁随意增删列。

import (
	"context"
	"database/sql"
)

// missingKey 批量 missing 记账的去重键（保首个出现序）。
type missingKey struct {
	appid int64
	cc    string
}

// gameUpsertSQL games 批量 UPSERT。带模型缺省的 NOT NULL 列显式落缺省值
// （对齐 Python 侧 INSERT 缺参时自动补默认）；SET 子句固定可更新列
// （created_at 冲突保留）；hasFree 组追加免费态两键以 excluded 引用逐行携带。
func gameUpsertSQL(hasFree bool) string {
	set := `name=excluded.name, name_en=excluded.name_en, type=excluded.type,
header_image=excluded.header_image, store_url=excluded.store_url,
chinese_support=excluded.chinese_support, family_sharing=excluded.family_sharing,
trading_cards=excluded.trading_cards, release_date=excluded.release_date,
is_adult=excluded.is_adult, is_visual_novel=excluded.is_visual_novel,
developers=excluded.developers, publishers=excluded.publishers,
positive_rate=excluded.positive_rate, positive_reviews=excluded.positive_reviews,
review_count=excluded.review_count, updated_at=excluded.updated_at`
	cols := `appid, name, name_en, type, header_image, store_url, chinese_support,
family_sharing, trading_cards, is_epic, is_hb, hl_flag, pp_flag, diff_fen,
is_adult, is_visual_novel, release_date, removed_strikes, developers, publishers,
positive_rate, positive_reviews, review_count, view_count, created_at, updated_at`
	extra := ""
	if hasFree {
		cols += ", free_kind, promo_end_at"
		extra = ",?,?"
		set += ", free_kind=excluded.free_kind, promo_end_at=excluded.promo_end_at"
	}
	return "INSERT INTO games (" + cols + ") VALUES (" +
		inPlaceholders(26) + extra +
		") ON CONFLICT(appid) DO UPDATE SET " + set
}

// upsertGames 按组批量写 games 元数据（可空列显式渲染保持批量形状）。
func upsertGames(ctx context.Context, conn *sql.Conn, rows []gameRowValues, hasFree bool) error {
	stmt, err := conn.PrepareContext(ctx, gameUpsertSQL(hasFree))
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, r := range rows {
		args := []any{
			r.appid, r.name, r.nameEn, r.typ, r.headerImage, r.storeURL,
			r.chineseSupp, r.familySharing, r.tradingCards, r.isEpic, r.isHb,
			r.hlFlag, r.ppFlag, r.diffFen, r.isAdult, r.isVisualNovel,
			r.releaseDate, r.removedStrik, r.developers, r.publishers,
			r.positiveRate, r.positiveRev, r.reviewCount, r.viewCount,
			r.createdAt, r.updatedAt,
		}
		if hasFree {
			args = append(args, r.freeKind, r.promoEnd)
		}
		if _, err := stmt.ExecContext(ctx, args...); err != nil {
			return err
		}
	}
	return nil
}

// currentUpsertSQL 现价 UPSERT：失败观察只推进尝试，冲突更新保留上次成功
// 事实（价格三件套/汇率/促销截止/last_success_at 在 excluded.attempt_outcome
// ='failed' 时保留旧行值）。fail_count 不进 SET（避免误清），新行落缺省 0
// （对齐 Python 侧 INSERT 缺参自动补默认）。
const currentUpsertSQL = `INSERT INTO game_current_prices
(appid, region_code, currency, price, original_price, discount_percent, sub_id,
price_status, fail_count, cny_fen, discount_end_ts, updated_at, attempt_outcome, steam_answer, last_success_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(appid, region_code) DO UPDATE SET
currency=CASE WHEN excluded.attempt_outcome='failed' THEN game_current_prices.currency ELSE excluded.currency END,
price=CASE WHEN excluded.attempt_outcome='failed' THEN game_current_prices.price ELSE excluded.price END,
original_price=CASE WHEN excluded.attempt_outcome='failed' THEN game_current_prices.original_price ELSE excluded.original_price END,
discount_percent=CASE WHEN excluded.attempt_outcome='failed' THEN game_current_prices.discount_percent ELSE excluded.discount_percent END,
sub_id=CASE WHEN excluded.attempt_outcome='failed' THEN game_current_prices.sub_id ELSE excluded.sub_id END,
price_status=excluded.price_status,
cny_fen=CASE WHEN excluded.attempt_outcome='failed' THEN game_current_prices.cny_fen ELSE excluded.cny_fen END,
discount_end_ts=CASE WHEN excluded.attempt_outcome='failed' THEN game_current_prices.discount_end_ts ELSE excluded.discount_end_ts END,
updated_at=excluded.updated_at,
attempt_outcome=excluded.attempt_outcome,
steam_answer=excluded.steam_answer,
last_success_at=CASE WHEN excluded.attempt_outcome='failed' THEN game_current_prices.last_success_at ELSE excluded.last_success_at END`

func upsertCurrentPrices(ctx context.Context, conn *sql.Conn, rows []currentRow) error {
	stmt, err := conn.PrepareContext(ctx, currentUpsertSQL)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for start := 0; start < len(rows); start += insertChunkRows {
		end := min(start+insertChunkRows, len(rows))
		for _, r := range rows[start:end] {
			var cnyFen, discountEnd any
			if r.cnyFen != nil {
				cnyFen = *r.cnyFen
			}
			if r.discountEnd != nil {
				discountEnd = *r.discountEnd
			}
			if _, err := stmt.ExecContext(ctx,
				r.appid, r.region, r.currency, r.price, r.original,
				r.discount, r.subID, r.status, int64(0), cnyFen, discountEnd,
				r.updatedAt, r.attemptOutcome, r.steamAnswer, r.lastSuccessAt,
			); err != nil {
				return err
			}
		}
	}
	return nil
}

// historyUpsertSQL 历史切片 UPSERT：唯一观测键（同游戏/同区/同时刻/同购买包/
// 同价与史低标记）冲突时更新非键详情保留原行身份；促销三列保留旧值兜底。
const historyUpsertSQL = `INSERT INTO game_price_history
(appid, region_code, currency, price, original_price, discount_percent, sub_id,
is_gold, version_suffix, is_bundle, price_status, cny_fen, discount_end_ts,
discount_desc, bundle_id, bundle_discount_pct, steam_event_key, source, snapshot_at)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(appid, region_code, snapshot_at, COALESCE(sub_id, -1), COALESCE(price, 0), COALESCE(is_gold, 0)) DO UPDATE SET
currency=excluded.currency,
original_price=excluded.original_price,
discount_percent=excluded.discount_percent,
version_suffix=excluded.version_suffix,
is_bundle=excluded.is_bundle,
price_status=excluded.price_status,
cny_fen=excluded.cny_fen,
discount_end_ts=excluded.discount_end_ts,
steam_event_key=excluded.steam_event_key,
discount_desc=COALESCE(excluded.discount_desc, game_price_history.discount_desc),
bundle_id=COALESCE(excluded.bundle_id, game_price_history.bundle_id),
bundle_discount_pct=COALESCE(excluded.bundle_discount_pct, game_price_history.bundle_discount_pct)`

func upsertHistory(ctx context.Context, conn *sql.Conn, rows []historyRow) error {
	stmt, err := conn.PrepareContext(ctx, historyUpsertSQL)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for start := 0; start < len(rows); start += insertChunkRows {
		end := min(start+insertChunkRows, len(rows))
		for _, r := range rows[start:end] {
			var original, cnyFen, discountEnd, discountDesc, bundleID, bundlePct, eventKey any
			if r.original != nil {
				original = *r.original
			}
			if r.cnyFen != nil {
				cnyFen = *r.cnyFen
			}
			if r.discountEnd != nil {
				discountEnd = *r.discountEnd
			}
			if r.discountDesc != nil {
				discountDesc = *r.discountDesc
			}
			if r.bundleID != nil {
				bundleID = *r.bundleID
			}
			if r.bundleDiscPct != nil {
				bundlePct = *r.bundleDiscPct
			}
			if r.eventKey != nil {
				eventKey = *r.eventKey
			}
			if _, err := stmt.ExecContext(ctx,
				r.appid, r.region, r.currency, r.price, original, r.discount,
				r.subID, b2i(r.isGold), r.suffix, b2i(r.isBundle), r.status,
				cnyFen, discountEnd, discountDesc, bundleID, bundlePct,
				eventKey, r.source, r.snapshotAt,
			); err != nil {
				return err
			}
		}
	}
	return nil
}

// clearFailCount 补抓成功结转清账：本批有 ok 价的区 fail_count 归零。
func clearFailCount(ctx context.Context, conn *sql.Conn, appids []int64, regions map[string]bool) error {
	sqlText := "UPDATE game_current_prices SET fail_count=0 WHERE appid IN (" +
		inPlaceholders(len(appids)) + ") AND region_code IN (" +
		inPlaceholders(len(regions)) + ")"
	args := make([]any, 0, len(appids)+len(regions))
	for _, a := range appids {
		args = append(args, a)
	}
	for r := range regions {
		args = append(args, r)
	}
	if _, err := conn.ExecContext(ctx, sqlText, args...); err != nil {
		return err
	}
	return nil
}

// bumpMissingLedger 欠账结转：fail_count 语义 = 连续「真实响应缺价」次数，
// 第 missingMaxRetries 次转 blocked（该区大概率无货的终态）。价格与
// last_success_at 不在此列，由调用方语义决定保留。
func bumpMissingLedger(ctx context.Context, conn *sql.Conn, appids []int64, regions map[string]bool) error {
	sqlText := `UPDATE game_current_prices SET
price_status=CASE WHEN game_current_prices.fail_count + 1 >= ? THEN 'blocked' ELSE 'missing' END,
fail_count=game_current_prices.fail_count + 1,
attempt_outcome='failed',
steam_answer=NULL
WHERE appid IN (` + inPlaceholders(len(appids)) + `) AND region_code IN (` +
		inPlaceholders(len(regions)) + `)`
	args := make([]any, 0, len(appids)+len(regions)+1)
	args = append(args, int64(missingMaxRetries))
	for _, a := range appids {
		args = append(args, a)
	}
	for r := range regions {
		args = append(args, r)
	}
	if _, err := conn.ExecContext(ctx, sqlText, args...); err != nil {
		return err
	}
	return nil
}

// missingBatchUpsertSQL 批量记 missing：首次插入 fail_count=1；已有行只盖
// 失败章刷新时刻（传输类失败不消耗补抓预算），价格三件套不在 SET 里。
const missingBatchUpsertSQL = `INSERT INTO game_current_prices
(appid, region_code, currency, price, original_price, discount_percent, sub_id,
price_status, fail_count, cny_fen, attempt_outcome, last_success_at, updated_at)
VALUES (?,?,  '',  NULL,NULL,0,NULL,'missing',1,NULL,'failed',NULL,?)
ON CONFLICT(appid, region_code) DO UPDATE SET
attempt_outcome='failed', steam_answer=NULL, updated_at=excluded.updated_at`

func markMissingBatch(ctx context.Context, conn *sql.Conn, keys []missingKey, nowStr string) error {
	stmt, err := conn.PrepareContext(ctx, missingBatchUpsertSQL)
	if err != nil {
		return err
	}
	defer stmt.Close()
	for _, k := range keys {
		if _, err := stmt.ExecContext(ctx, k.appid, k.cc, nowStr); err != nil {
			return err
		}
	}
	return nil
}

// queryBaselines 沿索引跳过旧历史，读取各区最新快照的全部版本：
// known = 同 sub 最新非空版本名（丢名行回填），latest = 差量门禁 prev。
func queryBaselines(ctx context.Context, conn *sql.Conn, appids []int64) (
	map[int64]map[int64]string, map[int64]map[obsKey]snapTuple, error,
) {
	known := map[int64]map[int64]string{}
	latest := map[int64]map[obsKey]snapTuple{}
	seen := map[int64]bool{}
	ids := make([]int64, 0, len(appids))
	for _, a := range appids {
		if a != 0 && !seen[a] {
			seen[a] = true
			ids = append(ids, a)
		}
	}
	for start := 0; start < len(ids); start += baselineChunkSize {
		end := min(start+baselineChunkSize, len(ids))
		chunk := ids[start:end]
		sqlText := `WITH RECURSIVE requested(appid) AS (VALUES ` + rowPlaceholders(len(chunk)) + `),
history_regions(appid, region_code) AS (
 SELECT appid, (SELECT MIN(region_code) FROM game_price_history
 WHERE appid=requested.appid) FROM requested
 UNION ALL
 SELECT appid, (SELECT MIN(region_code) FROM game_price_history
 WHERE appid=history_regions.appid
 AND region_code>history_regions.region_code)
 FROM history_regions WHERE region_code IS NOT NULL)
SELECT h.appid, h.region_code, h.sub_id, h.price,
h.original_price, h.discount_percent, h.version_suffix,
h.is_gold FROM history_regions r JOIN game_price_history h
 ON h.appid=r.appid AND h.region_code=r.region_code
 AND h.snapshot_at=(SELECT MAX(snapshot_at) FROM game_price_history
 WHERE appid=r.appid AND region_code=r.region_code)
 WHERE r.region_code IS NOT NULL
 ORDER BY h.appid, h.region_code, COALESCE(h.sub_id,-1),
 COALESCE(h.price,0), COALESCE(h.is_gold,0)`
		args := make([]any, 0, len(chunk))
		for _, a := range chunk {
			args = append(args, a)
		}
		rows, err := conn.QueryContext(ctx, sqlText, args...)
		if err != nil {
			return nil, nil, err
		}
		for rows.Next() {
			var appid int64
			var region string
			var subID, price, discount sql.NullInt64
			var original sql.NullInt64
			var suffix sql.NullString
			var gold int64
			if err := rows.Scan(&appid, &region, &subID, &price, &original, &discount, &suffix, &gold); err != nil {
				rows.Close()
				return nil, nil, err
			}
			sub := int64(0)
			if subID.Valid {
				sub = subID.Int64
			}
			var suffixPtr *string
			if suffix.Valid {
				s := suffix.String
				suffixPtr = &s
			}
			var originalPtr *int64
			if original.Valid {
				v := original.Int64
				originalPtr = &v
			}
			latestAt := latest[appid]
			if latestAt == nil {
				latestAt = map[obsKey]snapTuple{}
				latest[appid] = latestAt
			}
			latestAt[obsKey{region, sub}] = snapTuple{
				price:    price.Int64,
				original: originalPtr,
				discount: discount.Int64,
				suffix:   suffixPtr,
				isGold:   gold != 0,
			}
			if suffixPtr != nil {
				knownAt := known[appid]
				if knownAt == nil {
					knownAt = map[int64]string{}
					known[appid] = knownAt
				}
				knownAt[sub] = *suffixPtr
			}
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return nil, nil, err
		}
		rows.Close()
	}
	return known, latest, nil
}

// clearRemovedMarks 复活清标：只碰真正带标记的行，返回清标数。
// 不提交不建会话。
func clearRemovedMarks(ctx context.Context, conn *sql.Conn, appids []int64) (int, error) {
	seen := map[int64]bool{}
	ids := make([]int64, 0, len(appids))
	for _, a := range appids {
		if a != 0 && !seen[a] {
			seen[a] = true
			ids = append(ids, a)
		}
	}
	if len(ids) == 0 {
		return 0, nil
	}
	query := "SELECT appid FROM games WHERE appid IN (" + inPlaceholders(len(ids)) +
		") AND (removed_at IS NOT NULL OR removed_strikes != 0)"
	args := make([]any, 0, len(ids))
	for _, a := range ids {
		args = append(args, a)
	}
	rows, err := conn.QueryContext(ctx, query, args...)
	if err != nil {
		return 0, err
	}
	var marked []int64
	for rows.Next() {
		var a int64
		if err := rows.Scan(&a); err != nil {
			rows.Close()
			return 0, err
		}
		marked = append(marked, a)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return 0, err
	}
	rows.Close()
	if len(marked) == 0 {
		return 0, nil
	}
	update := "UPDATE games SET removed_at=NULL, removed_strikes=0 WHERE appid IN (" +
		inPlaceholders(len(marked)) + ")"
	uargs := make([]any, 0, len(marked))
	for _, a := range marked {
		uargs = append(uargs, a)
	}
	if _, err := conn.ExecContext(ctx, update, uargs...); err != nil {
		return 0, err
	}
	return len(marked), nil
}
