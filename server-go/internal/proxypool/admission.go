package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"slices"
	"time"
)

const (
	AdmissionActive    = "ACTIVE"
	AdmissionCandidate = "CANDIDATE"

	AutoAdmitMinAlive = 1

	AutoRepullMinIntervalHours = 2
)

type PromotionError struct{ Detail string }

func (e *PromotionError) Error() string { return e.Detail }

type PromotionResult struct {
	SubscriptionID int64
	Promoted       bool
	Detail         string
	SnapshotSha256 string
	AppliedNodes   int
}

type ExitResult struct {
	SubscriptionID int64
	Exited         bool
	Detail         string
	RemovedSources int
	DegradedNodes  int
}

type subscriptionAdmissionRow struct {
	ID              int64
	Kind            string
	URL             string
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

// IsAdmitted NULL 遗留值一律按 ACTIVE，避免历史订阅被降级。
func IsAdmitted(admissionStatus sql.NullString) bool {
	status := AdmissionActive
	if admissionStatus.Valid && admissionStatus.String != "" {
		status = admissionStatus.String
	}
	return status == AdmissionActive
}

// 先取 LockSubscriptionMutation 再读快照并 apply，最后才写 ACTIVE（SkippedStale 时拒绝晋升）。
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
		return PromotionResult{}, &PromotionError{Detail: fmt.Sprintf("最近一次成功快照不可恢复，拒绝晋升：%s", err.Error())}
	}
	if snap == nil {
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
		// 遇 SkippedStale 拒绝置 ACTIVE，防产生「ACTIVE + Registry 无来源」。
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
		return nil, nil
	}
	return &result, nil
}

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

type SubRepullInfo struct {
	ID              int64
	AutoRefresh     bool
	LastFetchStatus string
	LastFetchAt     *time.Time
}

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
	slices.Sort(targets)
	slices.Sort(gated)
	return targets, gated
}

const CapacitySafetyFactor = 0.8

const MaxCrawlWorkers = 60

const MaxLanes = MaxCrawlWorkers * 2

func ProductionSafeCapacity(verified int) int {
	if verified < 0 {
		return 0
	}
	return int(float64(verified) * CapacitySafetyFactor)
}

// requested < 0 按池规模；池为空或 requested == 0 返回 0（不使用 GLOBAL）。
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
