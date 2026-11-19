package dbwriter

// 逐款隔离回退路径：按 batchWriteChunk 分段过写闸，段内单款包 SAVEPOINT 隔离，
// 段间交还写者位防饿死交互写；段级异常仅作废本段。

import (
	"context"
	"database/sql"
	"fmt"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

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
					return fatal
				}
			}
			// 复活清标包在 SAVEPOINT 中，失败仅回滚清标不拖垮本段。
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
			for i := start; i < end; i++ {
				results[i] = false
			}
			w.Log("逐款隔离写段失败 │ 起始款序=%d，原因=%v", start, err)
		}
	}
	w.Log("逐款隔离写完成：%d 款，共耗时 %d 毫秒", len(entries), time.Since(t0).Milliseconds())
	return results, nil
}

// writeEntrySavepoint 单款写入包在 SAVEPOINT 中：普通错误回滚本款并返回 (false, nil)；
// 保存点失效（外层事务已回滚）返回错误由调用方作废整段。
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
			if len(okRegions) > 0 && aid != 0 {
				if err := clearFailCount(ctx, conn, []int64{aid}, okRegions); err != nil {
					return fmt.Errorf("欠账清账失败: %w", err)
				}
			}
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
