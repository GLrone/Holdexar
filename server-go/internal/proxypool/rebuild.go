// 池重建待办位与消费链（Python domains/proxypool/scheduling.py 的
// rebuild_pending 状态机）：池内容变脏只置位，绝不立即动内核；消费点两个——
// 爬取占用释放后立即消费、空闲周期拍。重建全程持维护预约位，与爬取互斥。
package proxypool

import (
	"context"
	"sync/atomic"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/store"
)

// rebuildPending 待重建位：可合并信号（false→true、true→true）。
var rebuildPending atomic.Bool

// RequestRebuild 标记“池内容已脏，需要一次重建”。幂等：连续置位只算一次。
func RequestRebuild() { rebuildPending.Store(true) }

// RebuildPending 待重建位读点。
func RebuildPending() bool { return rebuildPending.Load() }

// TakeRebuildPending 消费语义：取出并清空，返回此前是否待重建。
func TakeRebuildPending() bool { return rebuildPending.Swap(false) }

// RunPendingRebuild 空闲消费待重建：爬取占用或维护预约在位即让路；维护预约
// 取得后才取出待办位——消费发生在动手之前，重建期间新产生的 pending 属下一
// 次重建，不会被这次吞掉。重建本体经注入回调（标准形态 KernelRebuildFunc：
// 闸内建池 + 运行配置重生成 + 内核热重载，空池守卫内置）。返回是否真的执行。
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
	return true, rebuild(ctx)
}

// ConsumeRebuildAfterCrawl 任务收尾（占用刚释放）立即消费待重建，不等周期拍：
// 池 Runtime 在跑才热重建（进程/端口不动，把已标记移除的节点从内核里剔出去）；
// Runtime 不在跑时重建属“拉起内核”恢复路径，仍归周期拍。全程 fail-soft，
// 错误返回不外抛（调用方为占用释放钩子，故障不影响占用释放）。
func ConsumeRebuildAfterCrawl(ctx context.Context, rebuild func(ctx context.Context) error,
	runtime *clashkernel.Runtime) error {
	if !RebuildPending() || runtime == nil || !runtime.Status().Running {
		return nil
	}
	_, err := RunPendingRebuild(ctx, rebuild)
	return err
}

// WireRebuildPipeline 占用释放消费点的一键接线（serverd / live 复用）：爬取
// 释放后异步消费待重建，重建走标准回调。返回撤销函数，测试复位用。
// serverd 周期拍消费与 RequestPoolRebuild 的运行时装配，待内核运行时接线批
// 一并进场（serverd 目前不持内核 Runtime）。
func WireRebuildPipeline(gate *store.WriteGate, dataDir, exePath string,
	runtime *clashkernel.Runtime, lanes int) func() {
	prev := occupancy.OnCrawlEnd
	occupancy.OnCrawlEnd = func() {
		_ = ConsumeRebuildAfterCrawl(context.Background(),
			KernelRebuildFunc(gate, dataDir, exePath, runtime, lanes), runtime)
	}
	return func() { occupancy.OnCrawlEnd = prev }
}
