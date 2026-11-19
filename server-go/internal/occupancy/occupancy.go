// 进程内爬取占用与维护预约（体检/重建/整理）单锁互斥状态（不落库）。
package occupancy

import (
	"errors"
	"fmt"
	"sync"
)

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

var ErrCrawlerBusy = errors.New("已有爬取任务在执行（或维护预约在位）")

type BusyError struct{ Reason string }

func (e *BusyError) Error() string { return e.Reason }
func (e *BusyError) Unwrap() error { return ErrCrawlerBusy }

var (
	mu              sync.Mutex
	holder          string
	maintenanceKind string
)

func CrawlerBusy() bool {
	mu.Lock()
	defer mu.Unlock()
	return holder != "" || maintenanceKind != ""
}

func MaintenanceActive() bool {
	mu.Lock()
	defer mu.Unlock()
	return maintenanceKind != ""
}

func CurrentHolder() string {
	mu.Lock()
	defer mu.Unlock()
	return holder
}

func MaintenanceKind() string {
	mu.Lock()
	defer mu.Unlock()
	return maintenanceKind
}

func MaintenanceReason() string {
	mu.Lock()
	defer mu.Unlock()
	return maintenanceReasonOf(maintenanceKind)
}

// BeginCrawl 取得爬取占用（维护在位或他者占用时拒；同 tag 重入幂等，空 tag 不占位）。
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

// OnCrawlEnd 在 EndCrawl 释放占用后异步执行（nil 则跳过），不阻塞释放。
var OnCrawlEnd func()

// EndCrawl 释放爬取占用并异步触发 OnCrawlEnd。
func EndCrawl() {
	mu.Lock()
	holder = ""
	mu.Unlock()
	if OnCrawlEnd != nil {
		go OnCrawlEnd()
	}
}

// BeginMaintenance 取得维护预约（不幂等，已有预约或爬取占用时均拒）。
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

func EndMaintenance() {
	mu.Lock()
	maintenanceKind = ""
	mu.Unlock()
}

func maintenanceReasonOf(kind string) string {
	if reason, ok := maintenanceReasons[kind]; ok {
		return reason
	}
	return kind
}
