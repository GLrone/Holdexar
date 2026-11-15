package dbwriter

// 逐款隔离写（回退路径）：对齐 Python _upsert_task_batch_slow——批量快路径
// 的段级异常退到这里，按 batchWriteChunk 分段，每段一闸一事务；段内一整批
// 基线一次 + 单款 SAVEPOINT，单款失败记日志后回滚该款不拖垮整段；段间交还
// 写者位（兜底路径慢 ~10 倍，整批持闸会把交互写饿到超时）。段级异常只作废
// 本段，其余段照常。

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

// writeChunkSlow 逐款隔离写。entries 可为任意长度（快路径回退传单段，
// 直调时内部分段）；返回逐款写入结果。
func (w *Writer) writeChunkSlow(ctx context.Context, entries []BatchEntry) ([]bool, error) {
	results := make([]bool, len(entries))
	if len(entries) == 0 {
		return results, nil
	}
	t0 := time.Now()
	for start := 0; start < len(entries); start += batchWriteChunk {
		end := min(start+batchWriteChunk, len(entries))
		chunk := entries[start:end]
		w.backpressurePace(ctx)
		err := w.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			ids := make([]int64, 0, len(chunk))
			for i := range chunk {
				if chunk[i].Game.AppID != 0 {
					ids = append(ids, chunk[i].Game.AppID)
				}
			}
			known, latest, err := queryBaselines(ctx, conn, ids)
			if err != nil {
				return fmt.Errorf("基线查询失败: %w", err)
			}
			for i := range chunk {
				idx := start + i
				aid := chunk[i].Game.AppID
				ok, fatal := w.writeEntrySavepoint(ctx, conn, &chunk[i],
					baseline{known: known[aid], latest: latest[aid]}, i)
				results[idx] = ok
				if fatal != nil {
					// 外层事务已被连根回滚（保存点消失）：余款不再尝试，
					// 段级失败只作废本段
					return fatal
				}
			}
			// 收尾保存点：本段 ok 款复活清标与主段同事务提交；失败只回滚
			// 保存点不拖垮本段
			chunkOK := make([]int64, 0, len(chunk))
			for i := range chunk {
				if results[start+i] && chunk[i].Game.AppID != 0 {
					chunkOK = append(chunkOK, chunk[i].Game.AppID)
				}
			}
			if _, err := conn.ExecContext(ctx, "SAVEPOINT hx_clear_removed_slow"); err != nil {
				return fmt.Errorf("收尾保存点建立失败: %w", err)
			}
			if _, err := clearRemovedMarks(ctx, conn, chunkOK); err != nil {
				w.Log("逐款隔离写收尾保存点失败，已回滚（主段照常提交）: %v", err)
				_, _ = conn.ExecContext(context.Background(),
					"ROLLBACK TO SAVEPOINT hx_clear_removed_slow")
				_, _ = conn.ExecContext(context.Background(),
					"RELEASE SAVEPOINT hx_clear_removed_slow")
			} else if _, err := conn.ExecContext(ctx, "RELEASE SAVEPOINT hx_clear_removed_slow"); err != nil {
				return fmt.Errorf("收尾保存点释放失败: %w", err)
			}
			return nil
		})
		if err != nil {
			// 段级失败只作废本段（其余段照常），与快路径段语义对齐
			for i := start; i < end; i++ {
				results[i] = false
			}
			w.Log("逐款隔离写段失败 │ 起始款序=%d，原因=%v", start, err)
		}
	}
	w.Log("逐款隔离写完成：%d 款，共耗时 %d 毫秒", len(entries), time.Since(t0).Milliseconds())
	return results, nil
}

// writeEntrySavepoint 单款写入包在显式 SAVEPOINT 里：成功释放保存点返回
// (true, nil)；普通单款失败记日志、回滚该款、返回 (false, nil)——不拖垮整段；
// 保存点消失（外层事务被 RAISE(ROLLBACK) 等连根回滚）返回 (false, fatal)，
// 调用方按段级失败处理。
func (w *Writer) writeEntrySavepoint(ctx context.Context, conn *sql.Conn, entry *BatchEntry, base baseline, i int) (bool, error) {
	name := fmt.Sprintf("hx_entry_%d", i)
	if _, err := conn.ExecContext(ctx, "SAVEPOINT "+name); err != nil {
		w.Log("批量写入时单款失败：游戏 %d │ 原因=%v", entry.Game.AppID, err)
		return false, err
	}
	err := w.upsertEntryTx(ctx, conn, entry, base)
	if err == nil {
		if _, err = conn.ExecContext(ctx, "RELEASE SAVEPOINT "+name); err == nil {
			return true, nil
		}
		return false, err
	}
	w.Log("批量写入时单款失败：游戏 %d │ 原因=%v", entry.Game.AppID, err)
	if _, rerr := conn.ExecContext(context.Background(), "ROLLBACK TO SAVEPOINT "+name); rerr != nil {
		return false, rerr
	}
	if _, rerr := conn.ExecContext(context.Background(), "RELEASE SAVEPOINT "+name); rerr != nil {
		return false, rerr
	}
	return false, nil
}

// upsertEntryTx 单款写事务体（对齐 Python upsert_game_and_prices commit=False
// 的闸内段）：games 元数据 + 现价 + 欠账清账/结转 + 历史切片 + 派生列，
// 全部在调用方事务连接上执行，不提交。
func (w *Writer) upsertEntryTx(ctx context.Context, conn *sql.Conn, entry *BatchEntry, base baseline) error {
	aid := entry.Game.AppID
	row := w.normalizeGame(entry.Game, entry.Prices)
	before, err := derived.CatalogInputs(ctx, conn, []int64{aid})
	if err != nil {
		return fmt.Errorf("派生目录快照读取失败: %w", err)
	}
	if row.hasFree {
		if err := upsertGames(ctx, conn, []gameRowValues{row}, true); err != nil {
			return fmt.Errorf("游戏元数据写入失败: %w", err)
		}
	} else {
		if err := upsertGames(ctx, conn, []gameRowValues{row}, false); err != nil {
			return fmt.Errorf("游戏元数据写入失败: %w", err)
		}
	}
	if len(entry.Prices) > 0 {
		var eventKey *string
		if w.EventKey != nil {
			eventKey = w.EventKey(row.nowTime)
		}
		cur, hist, okRegions, degradedRegions := w.planPriceRows(entry.Prices, base, row.nowTime, eventKey)
		if len(cur) > 0 {
			if err := upsertCurrentPrices(ctx, conn, cur); err != nil {
				return fmt.Errorf("现价写入失败: %w", err)
			}
			// 补抓成功结转清账：本款有 ok 价的区 fail_count 归零
			if len(okRegions) > 0 && aid != 0 {
				if err := clearFailCount(ctx, conn, []int64{aid}, okRegions); err != nil {
					return fmt.Errorf("欠账清账失败: %w", err)
				}
			}
			// 欠账结转：门禁降级行计一次失败（递增 + 穷尽转 blocked）
			if len(degradedRegions) > 0 && aid != 0 {
				if err := bumpMissingLedger(ctx, conn, []int64{aid}, degradedRegions); err != nil {
					return fmt.Errorf("欠账结转失败: %w", err)
				}
			}
		}
		if len(hist) > 0 {
			if err := upsertHistory(ctx, conn, hist); err != nil {
				return fmt.Errorf("价格历史写入失败: %w", err)
			}
		}
	}
	derived.SetClock(w.Now)
	if _, err := derived.UpdatePriceDerived(ctx, conn, []int64{aid}, derived.Options{
		Now:           row.nowTime,
		Overrides:     w.SeriesOverrides,
		History:       true,
		CatalogBefore: before,
	}); err != nil {
		return fmt.Errorf("派生列维护失败: %w", err)
	}
	return nil
}
