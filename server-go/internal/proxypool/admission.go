package proxypool

// 订阅级生产准入（Python admission.py / bootstrap.py 对齐）：
// CANDIDATE 只落快照不进 Registry，ACTIVE 才进；晋升不是只改字段——必须同事务
// 完成「读该订阅当前 URL 最近一次成功快照 → apply_snapshot → 置 ACTIVE」，
// 否则出现「库说 ACTIVE、Registry 里没有来源」的中间事实。没有成功快照就
// 不允许晋升——不重抓、不用失败快照、不用旧缓存、不猜数据。
// deprecated 与本角色完全独立：不互相转换、不互相跟随。
// 并发边界：串行化由写闸提供；事务归调用方，本文件只执行 SQL 不提交。

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sort"
	"time"
)

const (
	AdmissionActive    = "ACTIVE"
	AdmissionCandidate = "CANDIDATE"

	// 健康自动转正门槛：订阅最近一次体检有可用节点即视为健康。准入的判断依据
	// 是体检事实，不是人工点头——用户导入订阅即意图投产。
	AutoAdmitMinAlive = 1

	// 订阅自动重拉最短间隔（小时）：距上次成功拉取不足该时长的订阅，30 分钟拍
	// 整条跳过（同内容重拉无收益；节点活性由池健康层守护，手动重拉不受限）。
	AutoRepullMinIntervalHours = 2
)

// PromotionError 晋升不成立：订阅不存在 / 非 clash / 没有成功快照 / 快照不可恢复。
type PromotionError struct{ Detail string }

func (e *PromotionError) Error() string { return e.Detail }

// PromotionResult 一次晋升的账目（Promoted=false = 本来就是 ACTIVE，幂等空转）。
// AppliedNodes 是本次晋升按快照对齐的去重指纹数——它包含「给已有节点新增一个
// 来源」的情形，拿 created/refreshed 计数会误读成 0。
type PromotionResult struct {
	SubscriptionID int64
	Promoted       bool
	Detail         string
	SnapshotSha256 string
	AppliedNodes   int
}

// ExitResult 一次退出生产池的账目（Exited=false = 本来就不在池里，幂等空转）。
type ExitResult struct {
	SubscriptionID int64
	Exited         bool
	Detail         string
	RemovedSources int
	DegradedNodes  int
}

// subscriptionAdmissionRow 准入判定所需的订阅行投影。
type subscriptionAdmissionRow struct {
	ID              int64
	Kind            string
	URL             string // 密文原文，开封在消费点
	AdmissionStatus sql.NullString
}

func loadSubscriptionAdmission(ctx context.Context, conn *sql.Conn, subscriptionID int64) (*subscriptionAdmissionRow, error) {
	var row subscriptionAdmissionRow
	err := conn.QueryRowContext(ctx,
		`SELECT id, kind, url, admission_status FROM proxy_subscriptions WHERE id = ?`,
		subscriptionID).Scan(&row.ID, &row.Kind, &row.URL, &row.AdmissionStatus)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	return &row, nil
}

// IsAdmitted 生产准入判定：NULL（遗留行读不到值）一律按 ACTIVE——兼容旧数据，
// 绝不因准入切片把历史订阅降级成候选。
func IsAdmitted(admissionStatus sql.NullString) bool {
	status := AdmissionActive
	if admissionStatus.Valid && admissionStatus.String != "" {
		status = admissionStatus.String
	}
	return status == AdmissionActive
}

// PromoteToActive 把 CANDIDATE 订阅晋升为 ACTIVE，并把它当前 URL 的最近一次成功
// 快照 apply 进 Registry。只执行 SQL 不提交：状态与来源行必须落在调用方的同一
// 事务里，调用方负责提交或回滚。
// 事务顺序：取写串行化边界 → 读订阅行 → 读当前 URL 的最新成功快照 → apply → 置
// ACTIVE。边界必须在读之前，否则并发 sync 可能在读完快照与 apply 之间落盘更新的
// 成功快照，让 apply 被「最新成功快照胜」守卫跳过后仍写 ACTIVE。
func PromoteToActive(ctx context.Context, conn *sql.Conn, dataDir string, subscriptionID int64, now time.Time) (PromotionResult, error) {
	if err := LockSubscriptionMutation(ctx, conn, subscriptionID); err != nil {
		return PromotionResult{}, err
	}
	sub, err := loadSubscriptionAdmission(ctx, conn, subscriptionID)
	if err != nil {
		return PromotionResult{}, err
	}
	if sub == nil {
		return PromotionResult{}, &PromotionError{Detail: fmt.Sprintf("订阅不存在：%d", subscriptionID)}
	}
	if sub.Kind != "clash" {
		return PromotionResult{}, &PromotionError{Detail: fmt.Sprintf("订阅 %d 不是 clash 订阅，没有生产准入语义", subscriptionID)}
	}
	if IsAdmitted(sub.AdmissionStatus) {
		return PromotionResult{SubscriptionID: subscriptionID, Detail: "已是 ACTIVE"}, nil
	}
	url := OpenURL(dataDir, sub.URL)
	if url == "" {
		return PromotionResult{}, &PromotionError{Detail: UnreadableMessage}
	}
	snap, err := RestoreLatestSnapshot(ctx, conn, dataDir, subscriptionID, url)
	if err != nil {
		// 行在但字节丢失 / sha 对不上 / 解不出来：fail-closed，不晋升
		return PromotionResult{}, &PromotionError{Detail: fmt.Sprintf("最近一次成功快照不可恢复，拒绝晋升：%s", err.Error())}
	}
	if snap == nil {
		// 区分「完全没抓成功过」与「抓成功过但不是当前 URL 的」——换链接后旧
		// 链接的快照不得顶替，那会让 Registry 与订阅 URL 对不上。
		var anyOK int
		if err := conn.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM subscription_snapshots WHERE subscription_id = ? AND status = 'OK'`,
			subscriptionID).Scan(&anyOK); err != nil {
			return PromotionResult{}, err
		}
		if anyOK > 0 {
			return PromotionResult{}, &PromotionError{Detail: "当前订阅 URL 还没有成功快照（换链接后必须重新成功抓取），拒绝晋升"}
		}
		return PromotionResult{}, &PromotionError{Detail: "没有成功快照，拒绝晋升（不重新抓取、不用失败快照或旧缓存）"}
	}
	tag := SubscriptionTag(url)
	result, err := ApplySnapshot(ctx, conn, subscriptionID, snap, now, tag)
	if err != nil {
		return PromotionResult{}, err
	}
	if result.SkippedStale {
		// 兜底：手上有更新的成功快照时绝不允许把 ACTIVE 写下去——那正是
		// 「ACTIVE + Registry 无来源」。保持 CANDIDATE，等下一轮按最新快照来。
		return PromotionResult{}, &PromotionError{Detail: "最近一次成功快照在晋升过程中已被更新的成功快照取代，拒绝晋升（保持 CANDIDATE）"}
	}
	if _, err := conn.ExecContext(ctx,
		`UPDATE proxy_subscriptions SET admission_status = ? WHERE id = ?`,
		AdmissionActive, subscriptionID); err != nil {
		return PromotionResult{}, err
	}
	applied, err := snapshotFingerprintCount(snap.Nodes, tag)
	if err != nil {
		return PromotionResult{}, err
	}
	return PromotionResult{
		SubscriptionID: subscriptionID, Promoted: true,
		Detail: "晋升完成（快照已进 Registry）", SnapshotSha256: snap.Sha256, AppliedNodes: applied,
	}, nil
}

// snapshotFingerprintCount 快照节点的去重指纹数（信息占位节点不入 Registry）。
func snapshotFingerprintCount(nodes []map[string]any, tag string) (int, error) {
	seen := map[string]bool{}
	for _, node := range nodes {
		if IsInfoPlaceholder(pyStr(node["name"])) {
			continue
		}
		fp, err := NodeFingerprint(node, tag)
		if err != nil {
			return 0, err
		}
		seen[fp] = true
	}
	return len(seen), nil
}

// ExitFromProduction 把 ACTIVE 订阅撤出生产池：置回 CANDIDATE，并物理删除它的
// 全部节点行（连同健康观测）。节点跟着订阅走，不留无主台账；别的订阅的节点
// 不受影响。只执行 SQL 不提交，提交归调用方。
func ExitFromProduction(ctx context.Context, conn *sql.Conn, subscriptionID int64) (ExitResult, error) {
	if err := LockSubscriptionMutation(ctx, conn, subscriptionID); err != nil {
		return ExitResult{}, err
	}
	sub, err := loadSubscriptionAdmission(ctx, conn, subscriptionID)
	if err != nil {
		return ExitResult{}, err
	}
	if sub == nil {
		return ExitResult{}, &PromotionError{Detail: fmt.Sprintf("订阅不存在：%d", subscriptionID)}
	}
	if sub.Kind != "clash" {
		return ExitResult{}, &PromotionError{Detail: fmt.Sprintf("订阅 %d 不是 clash 订阅，没有生产准入语义", subscriptionID)}
	}
	if !IsAdmitted(sub.AdmissionStatus) {
		return ExitResult{SubscriptionID: subscriptionID, Detail: "本来就不在池里"}, nil
	}
	rows, err := conn.QueryContext(ctx,
		`SELECT node_id FROM proxy_nodes WHERE subscription_id = ?`, subscriptionID)
	if err != nil {
		return ExitResult{}, err
	}
	var nodeIDs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return ExitResult{}, err
		}
		nodeIDs = append(nodeIDs, id)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return ExitResult{}, err
	}
	rows.Close()
	for _, nodeID := range nodeIDs {
		if _, err := conn.ExecContext(ctx,
			`DELETE FROM health_observations WHERE node_id = ?`, nodeID); err != nil {
			return ExitResult{}, err
		}
	}
	if _, err := conn.ExecContext(ctx,
		`DELETE FROM proxy_nodes WHERE subscription_id = ?`, subscriptionID); err != nil {
		return ExitResult{}, err
	}
	if _, err := conn.ExecContext(ctx,
		`UPDATE proxy_subscriptions SET admission_status = ? WHERE id = ?`,
		AdmissionCandidate, subscriptionID); err != nil {
		return ExitResult{}, err
	}
	return ExitResult{
		SubscriptionID: subscriptionID, Exited: true,
		Detail: "已退出生产池（置回 CANDIDATE）", RemovedSources: len(nodeIDs), DegradedNodes: len(nodeIDs),
	}, nil
}

// AutoAdmitHealthyCandidate 候选订阅的健康自动转正：订阅最近一次体检有可用节点
// （≥ AutoAdmitMinAlive）就直接晋升进池；体检无可用节点（或还没体检过）的订阅
// 保持候选。晋升失败（快照未就绪 / 换链接后当前 URL 还没重抓成功等）时保持候选
// 原样返回 nil——快照与状态不能被半截转正污染，等下一次体检 / 同步再试。
func AutoAdmitHealthyCandidate(ctx context.Context, conn *sql.Conn, dataDir string, subscriptionID int64, now time.Time) (*PromotionResult, error) {
	sub, err := loadSubscriptionAdmission(ctx, conn, subscriptionID)
	if err != nil {
		return nil, err
	}
	if sub == nil || IsAdmitted(sub.AdmissionStatus) {
		return nil, nil
	}
	var lastStats sql.NullString
	if err := conn.QueryRowContext(ctx,
		`SELECT CAST(last_stats AS TEXT) FROM proxy_subscriptions WHERE id = ?`,
		subscriptionID).Scan(&lastStats); err != nil {
		return nil, err
	}
	if lastStatsAlive(lastStats.String) < AutoAdmitMinAlive {
		return nil, nil
	}
	result, err := PromoteToActive(ctx, conn, dataDir, subscriptionID, now)
	if err != nil {
		// 转正不成保持候选原样，等下一轮再试
		return nil, nil
	}
	return &result, nil
}

// lastStatsAlive 从订阅 last_stats JSON 里取 alive 计数（缺席/类型不符按 0，
// Python `int(stats.get("alive") or 0)` 语义）。
func lastStatsAlive(payload string) int {
	if payload == "" {
		return 0
	}
	var stats map[string]any
	if json.Unmarshal([]byte(payload), &stats) != nil {
		return 0
	}
	alive, ok := pyToProbeInt(stats["alive"])
	if !ok {
		return 0
	}
	if alive < 0 {
		return 0
	}
	return int(alive)
}

// ── 重拉间隔门与容量钳制 ─────────────────────────────────────────────────────

// SubRepullInfo 重拉门所需的订阅行投影。
type SubRepullInfo struct {
	ID              int64
	AutoRefresh     bool
	LastFetchStatus string
	LastFetchAt     *time.Time
}

// FilterRepullTargets 一轮同步前筛订阅（bootstrap.sync_subscriptions 对齐）：
// 手动口径无视自动更新开关与间隔门（用户点了重拉就是要最新内容）；自动口径先
// 剔除关闭自动更新的订阅，再把距上次成功拉取不足间隔的整条跳过——不下载、
// 不动节点账本；失败的订阅不受此门（照常按拍重试）。
func FilterRepullTargets(subs []SubRepullInfo, now time.Time, manual bool) (targets []int64, gated []int64) {
	freshCutoff := now.Add(-time.Duration(AutoRepullMinIntervalHours) * time.Hour)
	for _, sub := range subs {
		if manual {
			targets = append(targets, sub.ID)
			continue
		}
		if !sub.AutoRefresh {
			continue
		}
		if sub.LastFetchStatus == "OK" && sub.LastFetchAt != nil && sub.LastFetchAt.After(freshCutoff) {
			gated = append(gated, sub.ID)
			continue
		}
		targets = append(targets, sub.ID)
	}
	sort.Slice(targets, func(i, j int) bool { return targets[i] < targets[j] })
	sort.Slice(gated, func(i, j int) bool { return gated[i] < gated[j] })
	return targets, gated
}

// 容量标定（Python capacity.py / exits.py / runtime.py 对齐）：
// 验证容量来自离线标定工具，建议生产上限 = 验证容量 × 安全系数（40 → 32）。
// lane 数与 worker 数不同源：worker 上限是同时干活的岗位数，lane 数是出口兵源。

// CapacitySafetyFactor 建议生产上限 = 验证容量 × 安全系数。
const CapacitySafetyFactor = 0.8

// MaxCrawlWorkers 出口槽 / worker 上限（exits.py）。
const MaxCrawlWorkers = 60

// MaxLanes 单内核 lane 数上限 = worker 上限 × 2（runtime.py）。
const MaxLanes = MaxCrawlWorkers * 2

// ProductionSafeCapacity 验证容量 → 建议生产上限（int 截断）。
func ProductionSafeCapacity(verified int) int {
	if verified < 0 {
		return 0
	}
	return int(float64(verified) * CapacitySafetyFactor)
}

// LaneCountFor 由池规模决定开几条 lane：一个节点最多占一条工位，上限 MaxLanes。
// requested<0 表示未指定（按池规模）；requested=0 显式要 0 条——池为空或显式 0
// 都返回 0，此时连 GLOBAL 入口都不该被当作可用出口。
func LaneCountFor(poolSize, requested int) int {
	n := poolSize
	if requested >= 0 {
		n = requested
	}
	if n > MaxLanes {
		n = MaxLanes
	}
	if poolSize < n {
		n = poolSize
	}
	if n < 0 {
		n = 0
	}
	return n
}
