package proxypool

// 体检运行行编排（Python health.py HealthRun 段对齐）：scheduled / manual 两条
// 通道共用 health_runs 台账；run 行建轮即提交（探测是分钟级网络段，写事务不得
// 从建轮一路开到首个分块提交）；观测行按 run_id 归属，读账本即得实时进度。
// 事件边界：进度发布只进 events 总线（progress 可丢面），不是生产依赖。

import (
	"context"
	"database/sql"
	"strings"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
)

// MaintenanceHealthKind 体检维护包络的种类值（occupancy.MAINTENANCE_HEALTH）。
const MaintenanceHealthKind = "health"

// MaintenanceHealthActive 体检预约位判定：只在维护预约位为体检时发布进度。
// 接线点：occupancy 维护包络迁移后由其提供实现（Python maintenance_kind()）。
// 缺省恒 false = 不发布——与 Python 未持体检预约位时静默跳过同向，宁缺不误发。
var MaintenanceHealthActive = func() bool { return false }

// PublishProbeProgress 体检维护包络内的探测进度 → events 总线（可丢）。
// 进度的事实源是 health_observations 按 run_id 的累计行数，事件只是让前端少
// 轮询一次；total 是本轮计划的探测总量，给了才随事件下发。
func PublishProbeProgress(bus *events.Bus, level string, probed int, total *int) {
	if bus == nil || !MaintenanceHealthActive() {
		return
	}
	kvs := []events.KV{
		{Key: "kind", Val: MaintenanceHealthKind},
		{Key: "level", Val: level},
		{Key: "probed", Val: probed},
	}
	if total != nil {
		kvs = append(kvs, events.KV{Key: "total", Val: *total})
	}
	bus.Publish("maintenance.progress", kvs...)
}

// HealthRunSummary 体检收尾的节点级结论汇总。
type HealthRunSummary struct {
	Total     int64
	SteamOK   int64
	IPKnown   int64
	Failed    int64
	EndReason string // 多个探针错误逗号连接；空 = 正常收场
}

// StartHealthRun 建一轮体检运行行并即刻提交（run 行先落库，观测归属只依赖
// run.id，不影响一致性）。subscriptionID 供 manual 通道挂订阅归属。
func StartHealthRun(ctx context.Context, gate *store.WriteGate, channel string, subscriptionID *int64, now time.Time) (int64, error) {
	var runID int64
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureHealthSchema(ctx, conn); err != nil {
			return err
		}
		var subArg any
		if subscriptionID != nil {
			subArg = *subscriptionID
		}
		res, err := conn.ExecContext(ctx,
			`INSERT INTO health_runs (channel, subscription_id, started_at, total, steam_ok, ip_known, failed)
			 VALUES (?, ?, ?, 0, 0, 0, 0)`,
			channel, subArg, pyTimeText(now))
		if err != nil {
			return err
		}
		runID, err = res.LastInsertId()
		return err
	})
	if err != nil {
		return 0, err
	}
	return runID, nil
}

// FinishHealthRun 收尾提交：节点级结论汇总、结束时刻与独立计时（Python 按
// finished_at − started_at 的整秒毫秒截断；起止都由调用方时钟给出）。
func FinishHealthRun(ctx context.Context, gate *store.WriteGate, runID int64,
	summary HealthRunSummary, finishedAt time.Time, durationMS int64) error {
	return gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureHealthSchema(ctx, conn); err != nil {
			return err
		}
		_, err := conn.ExecContext(ctx,
			`UPDATE health_runs SET total = ?, steam_ok = ?, ip_known = ?, failed = ?,
			 end_reason = ?, finished_at = ?, duration_ms = ? WHERE id = ?`,
			summary.Total, summary.SteamOK, summary.IPKnown, summary.Failed,
			nullTextArg(summary.EndReason), pyTimeText(finishedAt), durationMS, runID)
		return err
	})
}

// MaintenanceRunView 当前进行中的体检运行读点（health_runs 未收尾行 + 逐层
// 观测计数）。无进行中的 scheduled 运行返回 nil。
type MaintenanceRunView struct {
	RunID     int64
	Channel   string
	StartedAt string // Python isoformat 形态（T 分隔；微秒为零时省略）
	ProbedL1  int64
	ProbedL2  int64
	TotalL1   int
	TotalL2   int
}

// CurrentMaintenanceRun 当前进行中的体检运行（scheduled 通道）。手动节点检测
// 也写 run 行但不持维护预约位；本读点只对 scheduled 运行行负责。
func CurrentMaintenanceRun(ctx context.Context, db *sql.DB, dataDir string) (*MaintenanceRunView, error) {
	var (
		runID     int64
		channel   string
		startedAt sql.NullString
	)
	err := db.QueryRowContext(ctx,
		`SELECT id, channel, CAST(started_at AS TEXT) FROM health_runs
		 WHERE finished_at IS NULL AND channel = ?
		 ORDER BY started_at DESC LIMIT 1`, DefaultScheduleChannel,
	).Scan(&runID, &channel, &startedAt)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	view := &MaintenanceRunView{
		RunID: runID, Channel: channel,
		StartedAt: pyIsoFormat(startedAt.String),
	}
	counts, err := runObservationCounts(ctx, db, runID)
	if err != nil {
		return nil, err
	}
	view.ProbedL1 = counts["l1"]
	view.ProbedL2 = counts["l2"]
	poolTotal := len(PoolFileNames(dataDir))
	view.TotalL1 = poolTotal
	view.TotalL2 = poolTotal
	return view, nil
}

// runObservationCounts 该运行行按层累计的观测行数（level 小写归一）。
func runObservationCounts(ctx context.Context, db *sql.DB, runID int64) (map[string]int64, error) {
	rows, err := db.QueryContext(ctx,
		`SELECT level, COUNT(*) FROM health_observations WHERE run_id = ? GROUP BY level`, runID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]int64{"l1": 0, "l2": 0}
	for rows.Next() {
		var level sql.NullString
		var n int64
		if err := rows.Scan(&level, &n); err != nil {
			return nil, err
		}
		key := strings.ToLower(level.String)
		if _, ok := out[key]; ok {
			out[key] = n
		}
	}
	return out, rows.Err()
}

// pyIsoFormat 库内时间文本 → Python datetime.isoformat() 形态（T 分隔；
// 微秒非零恒 6 位，为零时省略小数节）。
func pyIsoFormat(text string) string {
	t, err := parsePyDatetime(text)
	if err != nil {
		return strings.Replace(text, " ", "T", 1)
	}
	if t.Nanosecond() == 0 {
		return t.Format("2006-01-02T15:04:05")
	}
	return t.Format("2006-01-02T15:04:05.000000")
}
