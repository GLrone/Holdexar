package dbwriter

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

// 社区历史源观测合流：来源侧只产出观测，身份/币种/时间范围校验与统一写入
// 全部在核心执行；INSERT OR IGNORE 语义保证社区点永不覆盖本地官方观测，
// 卸载数据源不丢任何已合流历史。

// CommunityObservation 单条社区历史观测（appid × region 级回填批次的成员）。
type CommunityObservation struct {
	SnapshotAt     time.Time // 裸北京时间（与官方观测同一把钟）
	SubID          *int64    // 商品身份未知序列必须为 nil
	PriceMinor     *int64    // 本币最小单位；缺失=无价点
	IsGold         bool
	Currency       string // 本币币种（有价必填）
	ProductUnknown bool   // 商品身份未知 → 独立序列，永不并入标准版
	CnyFen         *int64 // 来源侧可自带折算；缺失时读路径按历史汇率回填
	Source         string // 来源标注（行级 source 列），缺省 community
}

// mergeBatchLimit 单批上限：超限拒绝，防一次性巨批长期占用写闸。
const mergeBatchLimit = 5000

// mergeChunk 每条 INSERT 语句的行数分块（只为语句规模；原子性由单事务保证）。
const mergeChunk = 400

// beijingFixedZone 裸北京时间（观测时间戳的规范位置，与官方观测同一把钟）。
var beijingFixedZone = time.FixedZone("UTC+8", 8*3600)

// MergeCommunityHistory 校验并合流一批社区历史观测（BACKGROUND 写闸，单事务
// 覆盖去重读、写入与派生重算）。返回新增行数。校验失败整批拒绝（错误带
// 用户语言原因，由回填执行者记入同步状态），绝不落半批。
func (w *Writer) MergeCommunityHistory(ctx context.Context, appid int64, region string, obs []CommunityObservation) (int, error) {
	regionUpper := strings.ToUpper(strings.TrimSpace(region))
	if regionUpper == "" {
		return 0, errors.New("历史回填缺少地区")
	}
	if len(obs) > mergeBatchLimit {
		return 0, fmt.Errorf("单次回填观测 %d 条超过上限 %d", len(obs), mergeBatchLimit)
	}
	rows := make([]communityRow, 0, len(obs))
	for i := range obs {
		row, err := w.validateCommunityRow(obs[i])
		if err != nil {
			return 0, err
		}
		rows = append(rows, row)
	}

	var inserted int64
	err := w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		// 身份核验：游戏必须存在——未知身份的观测不落库。
		var exists int
		if e := conn.QueryRowContext(ctx, `SELECT 1 FROM games WHERE appid=?`, appid).Scan(&exists); e != nil {
			if errors.Is(e, sql.ErrNoRows) {
				return fmt.Errorf("游戏 %d 不在目录内，历史观测被拒绝", appid)
			}
			return e
		}
		if len(rows) == 0 {
			return nil
		}
		// 佐证去重：与本地已识别行（官方观测/包通道）同日同价的点视为
		// 同一观测的重复表达，直接跳过不落。
		seen, e := identifiedDayPrices(ctx, conn, appid, regionUpper, rows)
		if e != nil {
			return e
		}
		insertable := make([]communityRow, 0, len(rows))
		for _, row := range rows {
			if row.price != nil {
				if _, dup := seen[dayPriceKey{dayOf(row.snapshotAt), *row.price}]; dup {
					continue
				}
			}
			insertable = append(insertable, row)
		}
		if len(insertable) == 0 {
			return nil
		}
		inserted, e = w.insertCommunityRows(ctx, conn, appid, regionUpper, insertable)
		if e != nil {
			return e
		}
		if inserted > 0 {
			derived.SetClock(w.Now)
			if _, e = derived.UpdatePriceDerived(ctx, conn, []int64{appid}, derived.Options{
				Now: w.Now(), History: true, Overrides: w.SeriesOverrides,
			}); e != nil {
				return e
			}
		}
		return nil
	})
	if err != nil {
		return 0, err
	}
	return int(inserted), nil
}

type communityRow struct {
	snapshotAt     string
	subID          *int64
	price          *int64
	isGold         int
	currency       string
	productUnknown int
	cnyFen         *int64
	source         string
}

func (w *Writer) validateCommunityRow(obs CommunityObservation) (communityRow, error) {
	var row communityRow
	if obs.SnapshotAt.IsZero() {
		return row, errors.New("观测缺少时间戳")
	}
	row.snapshotAt = obs.SnapshotAt.In(beijingFixedZone).Format("2006-01-02 15:04:05")
	if obs.SnapshotAt.After(w.Now().Add(24 * time.Hour)) {
		return row, fmt.Errorf("观测时间 %s 晚于当前时间，超出可接受范围", row.snapshotAt)
	}
	row.source = strings.TrimSpace(obs.Source)
	if row.source == "" {
		row.source = "community"
	}
	if len(row.source) > 32 {
		return row, errors.New("来源标注过长")
	}
	row.currency = strings.ToUpper(strings.TrimSpace(obs.Currency))
	if obs.ProductUnknown {
		if obs.SubID != nil {
			return row, errors.New("商品身份未知的观测不得携带购买包标识")
		}
		if obs.IsGold {
			return row, errors.New("商品身份未知的观测不得标记金色版本")
		}
		row.productUnknown = 1
	}
	if obs.SubID != nil {
		if *obs.SubID < 0 {
			return row, errors.New("购买包标识不合法")
		}
		sub := *obs.SubID
		row.subID = &sub
	}
	if obs.PriceMinor != nil {
		if *obs.PriceMinor <= 0 {
			return row, errors.New("观测价格必须为正数或缺失")
		}
		if row.currency == "" {
			return row, errors.New("有价观测缺少币种")
		}
		price := *obs.PriceMinor
		row.price = &price
	}
	if obs.IsGold {
		row.isGold = 1
	}
	row.cnyFen = obs.CnyFen
	return row, nil
}

// identifiedDayPrices 收集窗口内已识别行（官方观测/包通道）的（日，价）集合。
func identifiedDayPrices(ctx context.Context, conn *sql.Conn, appid int64, regionUpper string, rows []communityRow) (map[dayPriceKey]struct{}, error) {
	lo, hi := rows[0].snapshotAt, rows[0].snapshotAt
	for _, row := range rows {
		if row.snapshotAt < lo {
			lo = row.snapshotAt
		}
		if row.snapshotAt > hi {
			hi = row.snapshotAt
		}
	}
	cursor, e := conn.QueryContext(ctx,
		`SELECT snapshot_at, price FROM game_price_history
		 WHERE appid=? AND region_code=? AND price IS NOT NULL
		   AND (product_unknown IS NULL OR product_unknown = 0)
		   AND snapshot_at >= ? AND snapshot_at <= ?`,
		appid, regionUpper, dayFloor(lo), dayCeil(hi))
	if e != nil {
		return nil, e
	}
	defer cursor.Close()
	seen := map[dayPriceKey]struct{}{}
	for cursor.Next() {
		var snapshotAt string
		var price int64
		if e := cursor.Scan(&snapshotAt, &price); e != nil {
			return nil, e
		}
		seen[dayPriceKey{dayOf(snapshotAt), price}] = struct{}{}
	}
	return seen, cursor.Err()
}

// communityInsertSQL 与官方 upsert 同一六段唯一键；社区行冲突即放弃
//（DO NOTHING）——绝不覆盖官方观测的任何列。
const communityInsertSQL = `INSERT INTO game_price_history
(appid, region_code, currency, price, original_price, discount_percent, sub_id,
is_gold, version_suffix, is_bundle, price_status, cny_fen, discount_end_ts,
discount_desc, bundle_id, bundle_discount_pct, steam_event_key, source, snapshot_at, product_unknown)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(appid, region_code, snapshot_at, COALESCE(sub_id, -1), COALESCE(price, 0), COALESCE(is_gold, 0)) DO NOTHING`

func (w *Writer) insertCommunityRows(ctx context.Context, conn *sql.Conn, appid int64, regionUpper string, rows []communityRow) (int64, error) {
	stmt, e := conn.PrepareContext(ctx, communityInsertSQL)
	if e != nil {
		return 0, e
	}
	defer stmt.Close()
	var total int64
	for start := 0; start < len(rows); start += mergeChunk {
		end := start + mergeChunk
		if end > len(rows) {
			end = len(rows)
		}
		for _, row := range rows[start:end] {
			// original_price/discount_percent 等促销详情是官方观测的职责，
			// 社区点一律 NULL/0；冲突放弃语义同时保证不覆盖官方值。
			result, e := stmt.ExecContext(ctx,
				appid, regionUpper, row.currency, row.price, nil, 0, row.subID,
				row.isGold, nil, 0, priceStatusOf(row.price), row.cnyFen, nil,
				nil, nil, nil, nil, row.source, row.snapshotAt, row.productUnknown)
			if e != nil {
				return total, e
			}
			n, _ := result.RowsAffected()
			total += n
		}
	}
	return total, nil
}

func priceStatusOf(price *int64) string {
	if price != nil {
		return "ok"
	}
	return "unknown"
}

type dayPriceKey struct {
	day   string
	price int64
}

func dayOf(snapshotAt string) string {
	if len(snapshotAt) < 10 {
		return snapshotAt
	}
	return snapshotAt[:10]
}

func dayFloor(snapshotAt string) string {
	return dayOf(snapshotAt) + " 00:00:00"
}

func dayCeil(snapshotAt string) string {
	return dayOf(snapshotAt) + " 23:59:59.999999"
}
