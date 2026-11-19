package proxypool

// 事件边界：进度发布只进 events 总线（progress 可丢面），不是生产依赖。

import (
	"context"
	"database/sql"
	"strings"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
)

const MaintenanceHealthKind = "health"

var MaintenanceHealthActive = func() bool { return false }

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

type HealthRunSummary struct {
	Total     int64
	SteamOK   int64
	IPKnown   int64
	Failed    int64
	EndReason string
}

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

type MaintenanceRunView struct {
	RunID     int64
	Channel   string
	StartedAt string
	ProbedL1  int64
	ProbedL2  int64
	TotalL1   int
	TotalL2   int
}

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
