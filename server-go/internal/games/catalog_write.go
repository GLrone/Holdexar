package games

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"holdexar/server-go/internal/monitoring"
	"holdexar/server-go/internal/store"
)

// 目录移除（假删除）/ 恢复 / 下架复探。games 行与价格历史全保留；移除挂
// 监控排除挡爬取主链与账号同步复活，恢复只解除本链路所挂的排除（用户
// 显式设置的排除语义保留）。写链全部进写闸事务（与 Pool.Remove 的
// ApplyTx 形态对齐），爬取派发经注入的 StartJob 出口。

const (
	// catalogRemovalReason 移除链挂的监控排除 reason：恢复只解这一档。
	catalogRemovalReason = "catalog_removed"
	// catalogRestoreReason 恢复链解除排除的登记缘由。
	catalogRestoreReason = "catalog_restored"
	// catalogRemovalRowReason catalog_removals 行的 reason。
	catalogRemovalRowReason = "user_removed"
)

// cleanCatalogIDs 入参规整：正整数、去重、保序（批量写入口共用单实现）。
func cleanCatalogIDs(ids []int64) []int64 {
	out := []int64{}
	seen := map[int64]bool{}
	for _, id := range ids {
		if id > 0 && !seen[id] {
			seen[id] = true
			out = append(out, id)
		}
	}
	return out
}

func catalogJSON(ids []int64) string {
	b, _ := json.Marshal(ids)
	return string(b)
}

func (h *handler) handleRemove(w http.ResponseWriter, r *http.Request) {
	h.handleCatalogWrite(w, r, h.removeGames)
}

func (h *handler) handleRestore(w http.ResponseWriter, r *http.Request) {
	h.handleCatalogWrite(w, r, h.restoreGames)
}

// handleCatalogWrite 批量写端点共用壳：写链依赖校验 + GameIdsRequest body
// 校验（pydantic 形状：缺 appids / 非 list / 长度 1..500 / 元素整数强转，
// 长度约束先于元素校验短路）。
func (h *handler) handleCatalogWrite(w http.ResponseWriter, r *http.Request, run func(context.Context, []int64) (map[string]any, error)) {
	if h.writes == nil || h.writes.Gate == nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	ids, ok := parseGameIdsBody(w, r)
	if !ok {
		return
	}
	result, err := run(r.Context(), ids)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	writeJSON(w, http.StatusOK, result)
}

func parseGameIdsBody(w http.ResponseWriter, r *http.Request) ([]int64, bool) {
	body, ok := decodeJSONBody(w, r)
	if !ok {
		return nil, false
	}
	raw, present := body["appids"]
	if !present {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []any{
			bodyErr{Type: "missing", Loc: []any{"body", "appids"}, Msg: "Field required", Input: body}}})
		return nil, false
	}
	items, ok := raw.([]any)
	if !ok {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []any{
			bodyErr{Type: "list_type", Loc: []any{"body", "appids"}, Msg: "Input should be a valid list", Input: raw}}})
		return nil, false
	}
	if len(items) < 1 || len(items) > 500 {
		errType, field := "too_short", "min_length"
		limit := 1
		msg := fmt.Sprintf("List should have at least 1 item after validation, not %d", len(items))
		if len(items) > 500 {
			errType, field, limit = "too_long", "max_length", 500
			msg = fmt.Sprintf("List should have at most 500 items after validation, not %d", len(items))
		}
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []any{
			bodyErr{Type: errType, Loc: []any{"body", "appids"}, Msg: msg, Input: raw,
				Ctx: map[string]any{"field_type": "List", field: limit, "actual_length": len(items)}}}})
		return nil, false
	}
	ids := make([]int64, 0, len(items))
	var errs []bodyErr
	for i, item := range items {
		v, ok, errType, msg := pyIntFromBody(item)
		if !ok {
			errs = append(errs, bodyErr{Type: errType, Loc: []any{"body", "appids", i}, Msg: msg, Input: item})
			continue
		}
		ids = append(ids, v)
	}
	if len(errs) > 0 {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": errs})
		return nil, false
	}
	return ids, true
}

// removeGames 批量移出游戏商店：catalog_removals 幂等落行（重复移除保留
// 首次时刻，仅对目录里实际存在的 appid 生效）+ 移出关注语义（脱池、摘
// manual/favorite/board 来源、挂 catalog_removed 监控排除）。
func (h *handler) removeGames(ctx context.Context, appids []int64) (map[string]any, error) {
	clean := cleanCatalogIDs(appids)
	if len(clean) == 0 {
		return map[string]any{"removed": 0, "missing": 0}, nil
	}
	d := h.writes
	known := map[int64]bool{}
	now := time.Now().In(beijingZone).Format("2006-01-02 15:04:05.000000")
	err := d.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		rows, e := conn.QueryContext(ctx,
			`SELECT DISTINCT appid FROM games WHERE appid IN (SELECT value FROM json_each(?))`,
			catalogJSON(clean))
		if e != nil {
			return e
		}
		for rows.Next() {
			var id int64
			if e := rows.Scan(&id); e != nil {
				rows.Close()
				return e
			}
			known[id] = true
		}
		if e := rows.Err(); e != nil {
			rows.Close()
			return e
		}
		rows.Close()
		targets := []int64{}
		for _, id := range clean {
			if known[id] {
				targets = append(targets, id)
			}
		}
		if len(targets) == 0 {
			return nil
		}
		// 幂等落账本（INSERT OR IGNORE），仅记录事实，恢复即删行回商店。
		// WHERE 1 是 SELECT 形态下 ON CONFLICT 子句的解析消歧（SQLite 语法要求）。
		if _, e = conn.ExecContext(ctx,
			`INSERT INTO catalog_removals(appid, reason, removed_at)
			 SELECT CAST(j.value AS INTEGER), ?, ? FROM json_each(?) j WHERE 1
			 ON CONFLICT(appid) DO NOTHING`,
			catalogRemovalRowReason, now, catalogJSON(targets)); e != nil {
			return e
		}
		// 移出关注语义（remove_pool_items 同径）：wishlist_items 全行脱池 +
		// 摘用户来源 + 挂监控排除，排除同时挡爬取主链与账号同步复活。
		if _, e = conn.ExecContext(ctx,
			`UPDATE wishlist_items SET active=0, excluded=1, manual=0
			 WHERE appid IN (SELECT value FROM json_each(?))`, catalogJSON(targets)); e != nil {
			return e
		}
		on := true
		_, e = d.Monitor.ApplyTx(ctx, conn, "game", targets, monitoring.Change{
			Sources:  map[int64][]string{},
			Managed:  []string{"manual", "favorite", "board"},
			Excluded: &on, Reason: catalogRemovalReason,
		})
		return e
	})
	if err != nil {
		return nil, err
	}
	removed := 0
	for _, id := range clean {
		if known[id] {
			removed++
		}
	}
	return map[string]any{"removed": removed, "missing": len(clean) - removed}, nil
}

// restoreGames 批量恢复：删 catalog_removals 行即回到商店；只解除本链路
// 所挂的排除（无排除或 reason=catalog_removed），用户显式排除保留；对
// 恢复款补一次取价（忙 / 空任务只记日志，恢复已生效）。
func (h *handler) restoreGames(ctx context.Context, appids []int64) (map[string]any, error) {
	clean := cleanCatalogIDs(appids)
	if len(clean) == 0 {
		return map[string]any{"restored": 0, "missing": 0}, nil
	}
	d := h.writes
	var restored int64
	var recrawl []int64
	err := d.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		res, e := conn.ExecContext(ctx,
			`DELETE FROM catalog_removals WHERE appid IN (SELECT value FROM json_each(?))`,
			catalogJSON(clean))
		if e != nil {
			return e
		}
		restored, _ = res.RowsAffected()
		reasons := map[int64]string{}
		rows, e := conn.QueryContext(ctx,
			`SELECT target_id, reason FROM monitor_exclusions
			 WHERE target_type='game' AND active=1 AND target_id IN (SELECT value FROM json_each(?))`,
			catalogJSON(clean))
		if e != nil {
			return e
		}
		for rows.Next() {
			var id int64
			var reason sql.NullString
			if e := rows.Scan(&id, &reason); e != nil {
				rows.Close()
				return e
			}
			reasons[id] = reason.String
		}
		if e := rows.Err(); e != nil {
			rows.Close()
			return e
		}
		rows.Close()
		for _, id := range clean {
			if reason, ok := reasons[id]; !ok || reason == catalogRemovalReason {
				recrawl = append(recrawl, id)
			}
		}
		if len(recrawl) > 0 {
			off := false
			if _, e = d.Monitor.ApplyTx(ctx, conn, "game", recrawl, monitoring.Change{
				Excluded: &off, Reason: catalogRestoreReason,
			}); e != nil {
				return e
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	if len(recrawl) > 0 && d.StartJob != nil {
		if _, e := d.StartJob(ctx, "appids", recrawl, "catalog_restore"); e != nil {
			d.Log("目录恢复后 %d 款未自动取价，留待下一轮刷新 │ 原因=%v", len(recrawl), e)
		}
	}
	return map[string]any{"restored": restored, "missing": len(clean) - int(restored)}, nil
}

// handleRetryRemoved 下架游戏手动复探：清 removed_at/removed_strikes +
// 立即重爬（removed_retry 单点）。404 = 游戏 {appid} 不存在；忙 / 空任务
// 时清标已生效，note 语义不视为失败。
func (h *handler) handleRetryRemoved(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	if h.writes == nil || h.writes.Gate == nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	d := h.writes
	exists := false
	e := d.Gate.WithWrite(r.Context(), store.PriorityInteractive, func(conn *sql.Conn) error {
		var one int
		if e := conn.QueryRowContext(r.Context(), `SELECT 1 FROM games WHERE appid=?`, appid).Scan(&one); e != nil {
			if errors.Is(e, sql.ErrNoRows) {
				return nil
			}
			return e
		}
		exists = true
		_, e := conn.ExecContext(r.Context(),
			`UPDATE games SET removed_at=NULL, removed_strikes=0 WHERE appid=?`, appid)
		return e
	})
	if e != nil {
		writeJSON(w, http.StatusInternalServerError, detailNull())
		return
	}
	if !exists {
		writeJSON(w, http.StatusNotFound, map[string]any{"detail": fmt.Sprintf("游戏 %d 不存在", appid)})
		return
	}
	if d.StartJob == nil {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "jobId": nil, "requeued": false,
			"note": "爬取出口未装配"})
		return
	}
	res, e := d.StartJob(r.Context(), "appids", []int64{appid}, "removed_retry")
	if e == nil {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "jobId": res["id"], "requeued": true})
		return
	}
	// 忙 / 空任务列表：清标已生效，下一轮关注层刷新自然带上，不视为失败。
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "jobId": nil, "requeued": false, "note": e.Error()})
}
