package derived

// 派生列日切换全库重算（对齐 Python games/derived.py maintain_derived）：
// 输入版本 = 北京日 + 系列覆盖表。先只读比对版本（一致零重算直接返回），
// 命中才开写闸——hl_flag=1 全量重算（带历史）、评测零行全量重算（不含历史）
// + 成员捆绑联动、系列覆盖变化全量重算系列、保存版本提交。

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"holdexar/server-go/internal/store"
)

// maintainBeijing 北京挂钟时区：UTC+8 固定偏移（现行无夏令时）。
var maintainBeijing = time.FixedZone("UTC+8", 8*3600)

// versionMatches 已存版本与当前输入一致（day + series 全量比对，对齐
// Python json.loads(saved) == version）；缺行/坏 JSON = 不一致。
func versionMatches(saved *string, day string, overrides map[string][]int64) bool {
	if saved == nil || *saved == "" {
		return false
	}
	var doc map[string]any
	if err := json.Unmarshal([]byte(*saved), &doc); err != nil {
		return false
	}
	if d, _ := doc["day"].(string); d != day {
		return false
	}
	return seriesVersionEqual(saved, overrides)
}

// MaintainDerived 日切换维护入口。db 用于闸外只读版本比对，命中才开写闸。
// now 为挂钟基准（day 判定与派生 era 截点）；overrides/order 由宿主闸外
// 装填（LoadSeriesOverrides）。返回 nil = 无需重算或重算成功。
func MaintainDerived(ctx context.Context, db *sql.DB, gate *store.WriteGate, now time.Time,
	overrides map[string][]int64, order []string) error {
	now = now.In(maintainBeijing)
	day := now.Format("2006-01-02")
	var saved sql.NullString
	err := db.QueryRowContext(ctx,
		"SELECT value_json FROM app_settings WHERE key=?", savedVersionKey).Scan(&saved)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return fmt.Errorf("派生输入版本读取失败: %w", err)
	}
	if saved.Valid && versionMatches(&saved.String, day, overrides) {
		return nil // 版本相同零重算
	}
	return gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		return maintainDerivedTx(ctx, conn, now, day, overrides, order)
	})
}

// maintainDerivedTx 写侧核心（闸内事务连接）：闸内复核版本防并发重复重算，
// 再按 Python maintain_derived 的段落顺序执行。
func maintainDerivedTx(ctx context.Context, conn *sql.Conn, now time.Time, day string,
	overrides map[string][]int64, order []string) error {
	saved, err := readSavedVersion(ctx, conn)
	if err != nil {
		return err
	}
	if versionMatches(saved, day, overrides) {
		return nil
	}
	old := map[string]any{}
	if saved != nil && *saved != "" {
		if err := json.Unmarshal([]byte(*saved), &old); err != nil {
			return fmt.Errorf("派生输入版本解析失败: %w", err)
		}
	}
	oldDay, _ := old["day"].(string)
	if oldDay != day {
		// 史低标记全量重算（hl_flag=1 行，带历史）
		lowIDs, err := queryIDs(ctx, conn, "hl_flag = 1", nil)
		if err != nil {
			return err
		}
		if len(lowIDs) > 0 {
			if _, err := UpdatePriceDerived(ctx, conn, lowIDs, Options{
				Now: now, History: true, Overrides: overrides, OverrideOrder: order,
			}); err != nil {
				return err
			}
		}
		// 评测零行全量重算（smart_score 时机因子日切换）+ 成员捆绑联动
		scoreIDs, err := queryIDs(ctx, conn, "COALESCE(review_count,0) = 0", nil)
		if err != nil {
			return err
		}
		if len(scoreIDs) > 0 {
			if _, err := UpdatePriceDerived(ctx, conn, scoreIDs, Options{
				Now: now, History: false, Overrides: overrides, OverrideOrder: order,
			}); err != nil {
				return err
			}
		}
		if err := UpdateMemberBundles(ctx, conn, scoreIDs); err != nil {
			return err
		}
	}
	// 系列覆盖变化（含首跑）→ 全量重算系列归属
	if !seriesVersionEqual(saved, overrides) {
		if _, err := RefreshSeries(ctx, conn, nil, overrides, order); err != nil {
			return err
		}
	}
	return SaveInputVersion(ctx, conn, now, overrides)
}
