package migration

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"

	"holdexar/server-go/internal/store"
)

// PrepareDatabaseAuthorized 携带维护授权令牌的准备：更新验收期间恢复保护
// 标记（purpose=update）由令牌匹配放行。
func PrepareDatabaseAuthorized(ctx context.Context, path, maintenanceToken string) (*sql.DB, error) {
	return prepareDatabase(ctx, path, maintenanceToken)
}

func PrepareDatabase(ctx context.Context, path string) (*sql.DB, error) {
	return prepareDatabase(ctx, path, "")
}

func prepareDatabase(ctx context.Context, path, maintenanceToken string) (*sql.DB, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		abs = resolved
	}
	unlock, err := store.LockDatabaseForPreparation(ctx, abs)
	if err != nil {
		return nil, fmt.Errorf("数据库准备未能取得写入保护: %w", err)
	}
	defer unlock()
	// 保护标记按用途分派：update=令牌放行（updateguard 已先校验），restore=阶段
	// 处置（CheckRestoreMarker），maintenance=陈旧清理；update/restore 存在时
	// 不做陈旧维护标记清理——两类保护永不按 pid 年龄回收
	if err := store.CheckRestoreMarker(abs); err != nil {
		return nil, fmt.Errorf("已保留原库并停止启动: %w", err)
	}
	switch {
	case store.IsUpdateMarker(abs):
		if maintenanceToken == "" {
			// purpose=update 的验收保护标记：未持令牌的启动不得触碰
			return nil, fmt.Errorf("已保留原库并停止启动: %w", store.ErrMaintenanceProtected)
		}
	case store.HasRestoreMarker(abs):
		// 阶段处置已由 CheckRestoreMarker 完成
	default:
		if err := store.CheckMaintenance(abs); err != nil {
			return nil, fmt.Errorf("已保留原库并停止启动: %w", err)
		}
	}

	version := 0
	if _, err := os.Stat(abs); err == nil {
		version, err = store.ReadSchemaVersion(abs)
		if err != nil {
			return nil, err
		}
	} else if !os.IsNotExist(err) {
		return nil, err
	}
	if version > store.CurrentSchemaVersion {
		return nil, fmt.Errorf("数据库结构版本 v%d 高于当前程序支持的数据库版本 v%d，拒绝启动", version, store.CurrentSchemaVersion)
	}

	db, err := store.Open(abs)
	if err != nil {
		return nil, err
	}
	prepared := false
	defer func() {
		if !prepared {
			db.Close()
		}
	}()

	if version == 0 {
		var objects int
		if err = db.QueryRowContext(ctx, "SELECT count(*) FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'").Scan(&objects); err == nil {
			if objects == 0 {
				err = store.InitializeFreshDatabase(ctx, db)
			} else {
				var recognized int
				err = db.QueryRowContext(ctx, "SELECT count(*) FROM sqlite_schema WHERE type='table' AND name IN ('games','app_settings')").Scan(&recognized)
				if err == nil && recognized != 2 {
					err = fmt.Errorf("未登记版本的数据库结构无法确认，已保留原库并拒绝启动")
				}
				if err == nil {
					err = snapshotBeforeMigration(ctx, db, abs)
				}
				if err == nil {
					err = upgradeDatabase(ctx, db, version)
				}
			}
		}
	} else {
		if version < store.CurrentSchemaVersion {
			err = snapshotBeforeMigration(ctx, db, abs)
		}
		if err == nil {
			err = upgradeDatabase(ctx, db, version)
		}
	}

	if err != nil {
		return nil, err
	}
	// 恢复后的库已通过迁移与完整性校验：解除恢复保护
	if err := store.ClearRestoreMarker(abs); err != nil {
		db.Close()
		return nil, fmt.Errorf("恢复保护记录未能解除，已停止启动: %w", err)
	}
	prepared = true
	return db, nil
}

func snapshotBeforeMigration(ctx context.Context, db *sql.DB, path string) error {
	file, err := os.CreateTemp(filepath.Dir(path), ".holdexar-migration-*.db")
	if err != nil {
		return fmt.Errorf("迁移前快照未完成，已阻止数据库结构变更: %w", err)
	}
	staging := file.Name()
	file.Close()
	defer os.Remove(staging)
	if err := store.Snapshot(ctx, db, staging); err != nil {
		return fmt.Errorf("迁移前快照未完成，已阻止数据库结构变更: %w", err)
	}
	if result, err := store.QuickCheck(staging); err != nil || result != "ok" {
		return fmt.Errorf("迁移前快照校验失败，已阻止数据库结构变更: %q, %v", result, err)
	}
	file, err = os.OpenFile(staging, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	err = file.Sync()
	closeErr := file.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err := os.Rename(staging, path+".pre-migration.bak"); err != nil {
		return fmt.Errorf("迁移前快照未完成，已阻止数据库结构变更: %w", err)
	}
	return nil
}
