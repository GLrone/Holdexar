package derived

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/timeutil"
)

var maintainBeijing = timeutil.ZoneUTC8

// versionMatches 比对已存版本的 day 与 series；缺行或坏 JSON 视为不一致。
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

// MaintainDerived 先闸外只读校验版本，不一致才取写闸全库重算（overrides 由宿主闸外装填）。
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
		return nil
	}
	return gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		return maintainDerivedTx(ctx, conn, now, day, overrides, order)
	})
}

// maintainDerivedTx 闸内复核版本防并发重复重算，按日切换与覆盖变更分段执行。
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
	if !seriesVersionEqual(saved, overrides) {
		if _, err := RefreshSeries(ctx, conn, nil, overrides, order); err != nil {
			return err
		}
	}
	return SaveInputVersion(ctx, conn, now, overrides)
}
