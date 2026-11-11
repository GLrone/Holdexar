package backup

import (
	"context"
	"fmt"
	"os"
	"time"

	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/store"
)

// CompactResult 原地收缩结果（字节数对齐前端 freedBytes/beforeBytes/afterBytes）。
type CompactResult struct {
	OK          bool  `json:"ok"`
	FreedBytes  int64 `json:"freedBytes"`
	BeforeBytes int64 `json:"beforeBytes"`
	AfterBytes  int64 `json:"afterBytes"`
}

// footprint 主库+WAL+shm 的磁盘占用：VACUUM 会把 WAL 内容并进主库，
// 只看主文件会误判释放量为负。
func footprint(dbPath string) int64 {
	var total int64
	for _, suffix := range []string{"", "-wal", "-shm"} {
		if info, err := os.Stat(dbPath + suffix); err == nil {
			total += info.Size()
		}
	}
	return total
}

// Compact 持维护许可原地回收空闲页：写闸进入维护窗口（新写请求被拒），
// 独占执行 VACUUM 后截断 WAL。期间调度与爬取按既有维护协议让路。
func (s *Service) Compact(ctx context.Context) (CompactResult, error) {
	result := CompactResult{OK: true}
	if _, err := os.Stat(s.dbPath); err != nil {
		return result, err
	}
	if err := store.CheckMaintenance(s.dbPath); err != nil {
		return result, err
	}
	if err := occupancy.BeginMaintenance(occupancy.MaintenanceStorage); err != nil {
		return result, fmt.Errorf("有写入正在进行，请稍后重试：%w", err)
	}
	defer occupancy.EndMaintenance()
	if err := s.pauseWrites(ctx); err != nil {
		return result, fmt.Errorf("有写入正在进行，请稍后重试：%w", err)
	}
	defer s.gate.ResumeWrites()

	before := footprint(s.dbPath)
	var vacuumErr error
	for attempt := 0; attempt < 3; attempt++ {
		if _, vacuumErr = s.db.ExecContext(ctx, "VACUUM"); vacuumErr == nil {
			break
		}
		select {
		case <-ctx.Done():
			return result, ctx.Err()
		case <-time.After(500 * time.Millisecond):
		}
	}
	if vacuumErr != nil {
		return result, fmt.Errorf("空间回收未完成（库仍在写入或被占用）：%w", vacuumErr)
	}
	// 收缩把整库经 WAL 重写一遍：截断只影响文件长度。写闸处于维护窗口，
	// 走闸的检查点会被自己拒绝——此处直接执行同等检查点
	if _, err := s.db.ExecContext(ctx, "PRAGMA wal_checkpoint(TRUNCATE)"); err != nil {
		return result, err
	}
	after := footprint(s.dbPath)
	freed := before - after
	if freed < 0 {
		freed = 0
	}
	result.BeforeBytes, result.AfterBytes, result.FreedBytes = before, after, freed
	return result, nil
}
