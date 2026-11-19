package proxypool

import (
	"context"
	"database/sql"
	"sync/atomic"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/store"
)

var rebuildPending atomic.Bool

func RequestRebuild() { rebuildPending.Store(true) }

func RebuildPending() bool { return rebuildPending.Load() }

func TakeRebuildPending() bool { return rebuildPending.Swap(false) }

// RunPendingRebuild 先取维护预约再取 pending 位，防重建期间新产生请求被吞；占用即让路。
func RunPendingRebuild(ctx context.Context, rebuild func(ctx context.Context) error) (bool, error) {
	if occupancy.CrawlerBusy() {
		return false, nil
	}
	if err := occupancy.BeginMaintenance(occupancy.MaintenanceRebuild); err != nil {
		return false, nil
	}
	defer occupancy.EndMaintenance()
	if !TakeRebuildPending() {
		return false, nil
	}
	if rebuild == nil {
		return false, nil
	}
	err := rebuild(ctx)
	if err != nil && ctx.Err() == nil {
		RequestRebuild()
	}
	return true, err
}

func ConsumeRebuildAfterCrawl(ctx context.Context, rebuild func(ctx context.Context) error,
	runtime *clashkernel.Runtime) error {
	if !RebuildPending() || runtime == nil || !runtime.Status().Running {
		return nil
	}
	_, err := RunPendingRebuild(ctx, rebuild)
	return err
}

func WireRebuildPipeline(db *sql.DB, gate *store.WriteGate, dataDir, exePath string,
	runtime *clashkernel.Runtime) func() {
	prev := occupancy.OnCrawlEnd
	occupancy.OnCrawlEnd = func() {
		_ = ConsumeRebuildAfterCrawl(context.Background(),
			KernelRebuildFunc(db, gate, dataDir, exePath, runtime), runtime)
	}
	return func() { occupancy.OnCrawlEnd = prev }
}
