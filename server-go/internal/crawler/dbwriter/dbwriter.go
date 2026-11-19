// 所有库写必须经 WriteGate；闸内禁止网络与文件 IO。
package dbwriter

import (
	"cmp"
	"context"
	"database/sql"
	"fmt"
	"os"
	"slices"
	"strings"
	"sync/atomic"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

const (
	batchWriteChunk   = 100 // 一段事务最多写入的款数：写者位按段交还，交互写可在段间插入
	insertChunkRows   = 2000
	missingMaxRetries = 5 // 连续「真实响应缺价」次数上限，穷尽转 blocked
	baselineChunkSize = 400

	// 写闸积压背压：后台排队超阈值时按深度线性退避，降速不丢数据。
	backpressureDepth = 32
	backpressureStep  = 50 * time.Millisecond
	backpressureCap   = 2 * time.Second
)

var backpressureActive atomic.Bool

type Writer struct {
	DB       *sql.DB
	Gate     *store.WriteGate
	Now      func() time.Time // 北京挂钟；缺省 UTC+8 固定偏移
	EventKey func(time.Time) *string
	FxRates  map[string]float64
	Log      func(format string, args ...any)
	// SeriesOverrides 由宿主在闸外装填，派生链闸内不做文件 IO。
	SeriesOverrides map[string][]int64
	Diag            func(context.Context) map[string]any
}

func NewWriter(db *sql.DB, gate *store.WriteGate) *Writer {
	return &Writer{
		DB:   db,
		Gate: gate,
		Now:  func() time.Time { return time.Now().UTC().Add(8 * time.Hour) },
		Log:  func(format string, args ...any) { fmt.Fprintf(os.Stderr, format+"\n", args...) },
	}
}

// LoadFxRates 刷新汇率缓存（Query 失败清空缓存，缺汇率时仅 CNY=1.0 生效）。
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

// UpsertTaskBatch 按 batchWriteChunk 分段过写闸提交（段前按积压退避，段间交还写者位）；
// 段体失败时本段回退 writeChunkSlow。
func (w *Writer) UpsertTaskBatch(ctx context.Context, entries []BatchEntry) ([]bool, error) {
	results := make([]bool, len(entries))
	for start := 0; start < len(entries); start += batchWriteChunk {
		end := min(start+batchWriteChunk, len(entries))
		chunk := entries[start:end]
		w.backpressurePace(ctx)
		ok, degraded, err := w.writeChunk(ctx, chunk)
		if err != nil {
			return nil, err
		}
		if degraded {
			slow, err := w.writeChunkSlow(ctx, chunk)
			if err != nil {
				return nil, err
			}
			copy(results[start:], slow)
			continue
		}
		copy(results[start:], ok)
	}
	return results, nil
}

func (w *Writer) gateDiagnostics(ctx context.Context) map[string]any {
	if w.Diag != nil {
		return w.Diag(ctx)
	}
	if w.Gate == nil {
		return nil
	}
	return w.Gate.Diagnostics(ctx)
}

func (w *Writer) backpressurePace(ctx context.Context) {
	depth := 0
	switch v := w.gateDiagnostics(ctx)["waiting_background"].(type) {
	case int:
		depth = v
	case int64:
		depth = int(v)
	case float64:
		depth = int(v)
	}
	if depth <= backpressureDepth {
		if backpressureActive.CompareAndSwap(true, false) {
			w.Log("写库积压回落，抓取提交恢复正常节奏 │ 积压段数=%d", depth)
		}
		return
	}
	if backpressureActive.CompareAndSwap(false, true) {
		w.Log("写库积压 %d 段，抓取提交自行放慢（背压生效） │ 积压段数=%d", depth, depth)
	}
	sleep := min(time.Duration(depth-backpressureDepth)*backpressureStep, backpressureCap)
	timer := time.NewTimer(sleep)
	defer timer.Stop()
	select {
	case <-ctx.Done():
	case <-timer.C:
	}
}

func (w *Writer) writeChunk(ctx context.Context, entries []BatchEntry) ([]bool, bool, error) {
	results := make([]bool, len(entries))
	for i := range results {
		results[i] = true
	}
	var bodyErr error
	err := w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		bodyErr = w.writeChunkTx(ctx, conn, entries)
		return bodyErr
	})
	if err != nil {
		if bodyErr != nil {
			w.Log("价格批量写段失败，整段作废（%d 款）: %v", len(entries), bodyErr)
			for i := range results {
				results[i] = false
			}
			return results, true, nil
		}
		return nil, false, err
	}
	return results, false, nil
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

	var catalogBefore map[int64]derived.CatalogRow
	if len(okIDs) > 0 {
		if catalogBefore, err = derived.CatalogInputs(ctx, conn, okIDs); err != nil {
			return fmt.Errorf("派生目录快照读取失败: %w", err)
		}
	}

	// 缺失免费态字段的行保留库内已有标记，按列形状分两组写入。
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
		// 按「ok 区集合相同的款」分组清零 fail_count，避免 appid×区笛卡尔积误清。
		for _, key := range sortedKeys(okGroups) {
			if err := clearFailCount(ctx, conn, okGroups[key], okRegionSets[key]); err != nil {
				return fmt.Errorf("欠账清账失败: %w", err)
			}
		}
		// 降级行经 bump 递增 fail_count（常规 upsert 不带 fail_count 防误清）。
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

	// 复活清标包在 SAVEPOINT 中，失败仅回滚清标不拖垮主段。
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
	// 派生列维护与批写同事务执行，SeriesOverrides 由宿主闸外装填。
	if len(okIDs) > 0 {
		derived.SetClock(w.Now)
		if _, err := derived.UpdatePriceDerived(ctx, conn, okIDs, derived.Options{
			Now:           w.Now(),
			Overrides:     w.SeriesOverrides,
			History:       true,
			CatalogBefore: catalogBefore,
		}); err != nil {
			return fmt.Errorf("派生列维护失败: %w", err)
		}
	}
	return nil
}

type RegionPair struct {
	AppID      int64
	RegionCode string
}

type RegionStatusWriteError struct {
	Pairs []RegionPair
}

func (e *RegionStatusWriteError) Error() string {
	return fmt.Sprintf("地区数据未能保存：%d 项", len(e.Pairs))
}

// MarkRegionStatusMissingBatch 批量记 missing 欠账：保留原价与 last_success_at，传输失败不递增 fail_count，同批按 (appid,区) 去重。
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
		slices.SortFunc(sorted, func(a, b RegionPair) int {
			if c := cmp.Compare(a.AppID, b.AppID); c != 0 {
				return c
			}
			return strings.Compare(a.RegionCode, b.RegionCode)
		})
		return 0, &RegionStatusWriteError{Pairs: sorted}
	}
	return len(order), nil
}

func regionSetKey(regions map[string]bool) string {
	list := make([]string, 0, len(regions))
	for r := range regions {
		list = append(list, r)
	}
	slices.Sort(list)
	return strings.Join(list, ",")
}

func sortedKeys(m map[string][]int64) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

func inPlaceholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}

func rowPlaceholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("(?),", n), ",")
}
