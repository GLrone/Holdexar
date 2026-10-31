// Package dbwriter：价格批量写层（games 元数据 / 现价 / 历史切片 / 区级欠账）。
// 写语义对齐现网同链路：批量形状、失败观察保底、同观测重放合并、
// 连续缺价穷尽转终态。所有库写必须经 WriteGate；闸内只做库写，
// 禁止网络与文件 IO。
package dbwriter

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

const (
	batchWriteChunk   = 100  // 一段事务最多写入的款数：写者位按段交还，交互写可在段间插入
	insertChunkRows   = 2000 // 批量参数列表分块，限制单段内存与驱动调用规模
	missingMaxRetries = 5    // 连续「真实响应缺价」次数上限，穷尽转 blocked
	baselineChunkSize = 400  // 基线查询按 appid 分块
)

// Writer 价格批量写层。Now 可注入便于测试；EventKey 供活动窗口标签接入
// （未迁移 steam_events 域时保持 nil，观测行不打活动标）。
type Writer struct {
	DB       *sql.DB
	Gate     *store.WriteGate
	Now      func() time.Time                 // 北京挂钟；缺省 UTC+8 固定偏移
	EventKey func(time.Time) *string          // 观测时刻所属活动窗口 key；nil = 无活动
	FxRates  map[string]float64               // 汇率缓存；LoadFxRates 装载
	Log      func(format string, args ...any) // 运行事件出口；缺省 stderr
}

// NewWriter 构造写层。
func NewWriter(db *sql.DB, gate *store.WriteGate) *Writer {
	return &Writer{
		DB:   db,
		Gate: gate,
		Now:  func() time.Time { return time.Now().UTC().Add(8 * time.Hour) },
		Log:  func(format string, args ...any) { fmt.Fprintf(os.Stderr, format+"\n", args...) },
	}
}

// LoadFxRates 装载汇率缓存（对齐 connect() 语义）：失败清空缓存走兜底
// 汇率（仅 CNY=1.0），不阻断写链路。
func (w *Writer) LoadFxRates(ctx context.Context) error {
	rows, err := w.DB.QueryContext(ctx, "SELECT currency_code, rate_to_cny FROM fx_rates")
	if err != nil {
		w.FxRates = map[string]float64{}
		return fmt.Errorf("加载汇率失败: %w", err)
	}
	defer rows.Close()
	rates := map[string]float64{}
	for rows.Next() {
		var cc string
		var rate float64
		if err := rows.Scan(&cc, &rate); err != nil {
			return fmt.Errorf("汇率行读取失败: %w", err)
		}
		rates[cc] = rate
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("汇率遍历失败: %w", err)
	}
	w.FxRates = rates
	return nil
}

// UpsertTaskBatch 一区一批（≤300 款）写完：拆 ~100 款的段逐段过写闸提交。
// 每段独立事务（基线一次 + 决策纯函数化 + 四条批量语句 games / 现价 /
// 欠账结转 / history，段内含收尾保存点），段与段之间写者位空出。
// 返回逐款写入结果：段体失败整段作废（逐款隔离写回退未迁移）。
func (w *Writer) UpsertTaskBatch(ctx context.Context, entries []BatchEntry) ([]bool, error) {
	results := make([]bool, len(entries))
	for start := 0; start < len(entries); start += batchWriteChunk {
		end := min(start+batchWriteChunk, len(entries))
		chunk := entries[start:end]
		ok, err := w.writeChunk(ctx, chunk)
		if err != nil {
			return nil, err
		}
		copy(results[start:], ok)
	}
	return results, nil
}

// writeChunk 单段写入（≤batchWriteChunk 款）一次事务写完（含收尾保存点）。
func (w *Writer) writeChunk(ctx context.Context, entries []BatchEntry) ([]bool, error) {
	results := make([]bool, len(entries))
	for i := range results {
		results[i] = true
	}
	var bodyErr error
	err := w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		bodyErr = w.writeChunkTx(ctx, conn, entries)
		return bodyErr // 非空即回滚整段
	})
	if err != nil {
		if bodyErr != nil {
			// 段体失败：整段作废不落半批（逐款隔离写回退未迁移）
			w.Log("价格批量写段失败，整段作废（%d 款）: %v", len(entries), bodyErr)
			for i := range results {
				results[i] = false
			}
			return results, nil
		}
		return nil, err
	}
	return results, nil
}

func (w *Writer) writeChunkTx(ctx context.Context, conn *sql.Conn, entries []BatchEntry) error {
	okIDs := make([]int64, 0, len(entries))
	for _, e := range entries {
		if e.Game.AppID != 0 {
			okIDs = append(okIDs, e.Game.AppID)
		}
	}

	known, latest, err := queryBaselines(ctx, conn, okIDs)
	if err != nil {
		return fmt.Errorf("基线查询失败: %w", err)
	}

	// 归一化 + 免费态分组：免费态字段缺省的行保留已有标记，两组列形状不同
	var freeRows, plainRows []gameRowValues
	type entryPlan struct {
		entry    *BatchEntry
		now      time.Time
		eventKey *string
	}
	plans := make([]entryPlan, len(entries))
	eventKeys := map[string]*string{}
	for i := range entries {
		e := &entries[i]
		row := w.normalizeGame(e.Game, e.Prices)
		if row.hasFree {
			freeRows = append(freeRows, row)
		} else {
			plainRows = append(plainRows, row)
		}
		ek, cached := eventKeys[row.updatedAt]
		if !cached {
			if w.EventKey != nil {
				ek = w.EventKey(row.nowTime)
			}
			eventKeys[row.updatedAt] = ek
		}
		plans[i] = entryPlan{entry: e, now: row.nowTime, eventKey: ek}
	}

	// games 元数据：免费态字段组先行（对齐 Python {True:…, False:…} 组序）
	if len(freeRows) > 0 {
		if err := upsertGames(ctx, conn, freeRows, true); err != nil {
			return fmt.Errorf("游戏元数据写入失败: %w", err)
		}
	}
	if len(plainRows) > 0 {
		if err := upsertGames(ctx, conn, plainRows, false); err != nil {
			return fmt.Errorf("游戏元数据写入失败: %w", err)
		}
	}

	var allCurrent []currentRow
	var allHistory []historyRow
	okGroups := map[string][]int64{}
	degradedGroups := map[string][]int64{}
	okRegionSets := map[string]map[string]bool{}
	degradedRegionSets := map[string]map[string]bool{}
	for _, p := range plans {
		if len(p.entry.Prices) == 0 {
			continue
		}
		cur, hist, okRegions, degradedRegions := w.planPriceRows(
			p.entry.Prices,
			baseline{known: known[p.entry.Game.AppID], latest: latest[p.entry.Game.AppID]},
			p.now, p.eventKey,
		)
		allCurrent = append(allCurrent, cur...)
		allHistory = append(allHistory, hist...)
		if len(okRegions) > 0 {
			key := regionSetKey(okRegions)
			okGroups[key] = append(okGroups[key], p.entry.Game.AppID)
			okRegionSets[key] = okRegions
		}
		if len(degradedRegions) > 0 {
			key := regionSetKey(degradedRegions)
			degradedGroups[key] = append(degradedGroups[key], p.entry.Game.AppID)
			degradedRegionSets[key] = degradedRegions
		}
	}

	if len(allCurrent) > 0 {
		if err := upsertCurrentPrices(ctx, conn, allCurrent); err != nil {
			return fmt.Errorf("现价写入失败: %w", err)
		}
		// 补抓成功结转清账：本批有 ok 价的区 fail_count 归零；按「区集合
		// 相同的款」分组成 IN 更新，避免 appid × 区的笛卡尔积误清
		for _, key := range sortedKeys(okGroups) {
			if err := clearFailCount(ctx, conn, okGroups[key], okRegionSets[key]); err != nil {
				return fmt.Errorf("欠账清账失败: %w", err)
			}
		}
		// 欠账结转：门禁降级行计一次失败（递增 + 穷尽转 blocked）。
		// 常规 upsert 不带 fail_count（避免误清），账本专门走 bump
		for _, key := range sortedKeys(degradedGroups) {
			if err := bumpMissingLedger(ctx, conn, degradedGroups[key], degradedRegionSets[key]); err != nil {
				return fmt.Errorf("欠账结转失败: %w", err)
			}
		}
	}

	if len(allHistory) > 0 {
		if err := upsertHistory(ctx, conn, allHistory); err != nil {
			return fmt.Errorf("价格历史写入失败: %w", err)
		}
	}

	// 收尾（复活清标）保存点：与主段同事务一次提交；失败只回滚保存点
	// 不拖垮主段
	if _, err := conn.ExecContext(ctx, "SAVEPOINT hx_clear_removed"); err != nil {
		return fmt.Errorf("收尾保存点建立失败: %w", err)
	}
	if _, err := clearRemovedMarks(ctx, conn, okIDs); err != nil {
		_, _ = conn.ExecContext(context.Background(),
			"ROLLBACK TO SAVEPOINT hx_clear_removed")
		_, _ = conn.ExecContext(context.Background(),
			"RELEASE SAVEPOINT hx_clear_removed")
	} else if _, err := conn.ExecContext(ctx, "RELEASE SAVEPOINT hx_clear_removed"); err != nil {
		return fmt.Errorf("收尾保存点释放失败: %w", err)
	}
	// 派生列维护（hl_flag/系列识别等）未迁移，接线上随 update_price_derived 落地
	return nil
}

// ── 区级欠账批量记账 ────────────────────────────────────────────

// RegionPair 一条 (appid, 区) 欠账。
type RegionPair struct {
	AppID      int64
	RegionCode string
}

// RegionStatusWriteError 区级记账失败：携带去重排序后的成对清单。
type RegionStatusWriteError struct {
	Pairs []RegionPair
}

func (e *RegionStatusWriteError) Error() string {
	return fmt.Sprintf("地区数据未能保存：%d 项", len(e.Pairs))
}

// MarkRegionStatusMissingBatch 批量记 missing 欠账（一次闸一事务）：行形状
// 与单款 missing 分支同口径（failed 章 + 保留 last good price）。传输类失败
// 不消耗补抓预算（fail_count 不递增），(appid, 区) 去重防同批双章。
// 返回写入行数。
func (w *Writer) MarkRegionStatusMissingBatch(ctx context.Context, pairs []RegionPair) (int, error) {
	if len(pairs) == 0 {
		return 0, nil
	}
	nowStr := FormatNaive(w.Now())
	seen := map[missingKey]bool{}
	var order []missingKey
	for _, p := range pairs {
		cc := strings.ToUpper(p.RegionCode)
		k := missingKey{p.AppID, cc}
		if seen[k] {
			continue
		}
		seen[k] = true
		order = append(order, k)
	}
	err := w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		return markMissingBatch(ctx, conn, order, nowStr)
	})
	if err != nil {
		sorted := make([]RegionPair, 0, len(order))
		for _, k := range order {
			sorted = append(sorted, RegionPair{AppID: k.appid, RegionCode: k.cc})
		}
		sort.Slice(sorted, func(i, j int) bool {
			if sorted[i].AppID != sorted[j].AppID {
				return sorted[i].AppID < sorted[j].AppID
			}
			return sorted[i].RegionCode < sorted[j].RegionCode
		})
		return 0, &RegionStatusWriteError{Pairs: sorted}
	}
	return len(order), nil
}

// ── 小工具 ──────────────────────────────────────────────────────

func regionSetKey(regions map[string]bool) string {
	list := make([]string, 0, len(regions))
	for r := range regions {
		list = append(list, r)
	}
	sort.Strings(list)
	return strings.Join(list, ",")
}

func sortedKeys(m map[string][]int64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// inPlaceholders 生成 n 个 "?" 占位符的逗号串。
func inPlaceholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}

// rowPlaceholders 生成 n 个单值行的 VALUES 串：(?),(?),...
func rowPlaceholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("(?),", n), ",")
}
