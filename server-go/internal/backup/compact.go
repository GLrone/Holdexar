package backup

import (
	"context"
	"fmt"
	"os"
	"time"

	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/store"
)

type CompactResult struct {
	OK          bool  `json:"ok"`
	FreedBytes  int64 `json:"freedBytes"`
	BeforeBytes int64 `json:"beforeBytes"`
	AfterBytes  int64 `json:"afterBytes"`
}

// footprint 统计主库及 WAL/shm 总占用，避免 VACUUM 合并 WAL 后释放量失真。
func footprint(dbPath string) int64 {
	var total int64
	for _, suffix := range []string{"", "-wal", "-shm"} {
		if info, err := os.Stat(dbPath + suffix); err == nil {
			total += info.Size()
		}
	}
	return total
}

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
	// 维护窗口内写闸会拒新写，直接执行 wal_checkpoint(TRUNCATE) 避免自锁。
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
