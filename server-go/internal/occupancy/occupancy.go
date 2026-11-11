// 生产爬取占用与维护包络（Python app/crawler/occupancy.py 对齐）：占用门禁落
// 执行入口，手动/队列、bundles 链尾直调、CLI 调试三路共用同一互斥语义。
// 爬取占用与维护预约（体检/重建）共用一把锁互斥：任一在位，另一方即被拒。
// 占用是进程内状态不落库：回答"此刻有没有爬取在执行"，不是历史。
package occupancy

import (
	"errors"
	"fmt"
	"sync"
)

// 维护预约种类；种类 → 用户拒因文案。
const (
	MaintenanceHealth  = "health"
	MaintenanceRebuild = "rebuild"
	MaintenanceStorage = "storage"
)

var maintenanceReasons = map[string]string{
	MaintenanceHealth:  "代理体检进行中",
	MaintenanceRebuild: "代理通道正在重建",
	MaintenanceStorage: "数据库正在整理",
}

// ErrCrawlerBusy 占用冲突哨兵；具体拒因在错误值文案里，errors.Is 判别。
var ErrCrawlerBusy = errors.New("已有爬取任务在执行（或维护预约在位）")

// BusyError 占用冲突，文案即用户可见拒因。
type BusyError struct{ Reason string }

func (e *BusyError) Error() string { return e.Reason }
func (e *BusyError) Unwrap() error { return ErrCrawlerBusy }

var (
	mu              sync.Mutex
	holder          string // 当前爬取占用者 tag；空 = 无
	maintenanceKind string // 当前维护预约种类；空 = 无
)

// CrawlerBusy 爬取占用或维护预约任一在位即为忙（体检/重建全程持位）。
func CrawlerBusy() bool {
	mu.Lock()
	defer mu.Unlock()
	return holder != "" || maintenanceKind != ""
}

// MaintenanceActive 维护预约在位。
func MaintenanceActive() bool {
	mu.Lock()
	defer mu.Unlock()
	return maintenanceKind != ""
}

// CurrentHolder 当前爬取占用者 tag；空 = 无。
func CurrentHolder() string {
	mu.Lock()
	defer mu.Unlock()
	return holder
}

// MaintenanceKind 当前维护预约种类；空 = 无。
func MaintenanceKind() string {
	mu.Lock()
	defer mu.Unlock()
	return maintenanceKind
}

// MaintenanceReason 维护预约的用户拒因；无预约返回空。
func MaintenanceReason() string {
	mu.Lock()
	defer mu.Unlock()
	return maintenanceReasonOf(maintenanceKind)
}

// BeginCrawl 取得爬取占用：维护预约在位即拒（拒因对齐维护种类）；同 tag 重入
// 幂等（执行入口预取占用后同名复用）；其它占用者在位即拒并带占用者 tag。
// tag 恒为非空：空 tag 不产生占用（调用面不存在此形态）。
func BeginCrawl(tag string) error {
	mu.Lock()
	defer mu.Unlock()
	if maintenanceKind != "" {
		return &BusyError{Reason: maintenanceReasonOf(maintenanceKind)}
	}
	if holder == tag {
		return nil
	}
	if holder != "" {
		return &BusyError{Reason: fmt.Sprintf("已有爬取任务在执行（%s）", holder)}
	}
	holder = tag
	return nil
}

// OnCrawlEnd 爬取占用释放后的钩子（接线方注入；nil = 无）。消费待重建归
// proxypool 接线；钩子在占用已释放后异步执行（Python create_task 同款），
// 自身故障不影响占用释放。
var OnCrawlEnd func()

// EndCrawl 释放爬取占用；与成功的 BeginCrawl 成对，异常路径由调用方 defer 配对。
// 释放即触发重建消费钩子（Python end_crawl → _kick_rebuild_consume 同款）。
func EndCrawl() {
	mu.Lock()
	holder = ""
	mu.Unlock()
	if OnCrawlEnd != nil {
		go OnCrawlEnd()
	}
}

// BeginMaintenance 取得维护预约：已有预约或爬取占用在位即拒（不幂等，预约
// 重入同样拒）。
func BeginMaintenance(kind string) error {
	mu.Lock()
	defer mu.Unlock()
	if maintenanceKind != "" {
		return &BusyError{Reason: maintenanceReasonOf(maintenanceKind)}
	}
	if holder != "" {
		return &BusyError{Reason: fmt.Sprintf("已有爬取任务在执行（%s）", holder)}
	}
	maintenanceKind = kind
	return nil
}

// EndMaintenance 清掉维护预约；与 BeginMaintenance 成对（finally 配对），无预约
// 时调用为幂等空操作。
func EndMaintenance() {
	mu.Lock()
	maintenanceKind = ""
	mu.Unlock()
}

// maintenanceReasonOf 种类 → 用户拒因；种类表外回退原值，不 panic。
func maintenanceReasonOf(kind string) string {
	if reason, ok := maintenanceReasons[kind]; ok {
		return reason
	}
	return kind
}
