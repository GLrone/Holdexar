package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"maps"
	"math"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const (
	maintenanceIntervalKey  = "proxy.pool_maintenance_interval_hours"
	maintenanceSignatureKey = "proxy.pool_maintenance_last_sig"
	defaultMaintenanceHours = 2
	globalSelectTimeout     = 10 * time.Second
)

// Cycle runs the health phases around the caller's pending runtime rebuild.
type Cycle struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	DataDir string
	Now     func() time.Time
	Bus     *events.Bus

	ProbeL0URL string
	ProbeL1URL string
	ProbeL2URL string
	ProbeBatch int
}

func (c *Cycle) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

// RunL0 can run during a crawl. Recovery uses a separate temporary kernel.
func (c *Cycle) RunL0(ctx context.Context, checker *HealthChecker) (map[string]any, error) {
	now := c.now()
	before, beforeErr := c.eligible(ctx, now, true)
	var outcomes []ProbeOutcome
	var l0Err error
	if checker != nil {
		outcomes, l0Err = checker.L0CheckPool(ctx, PoolFileNames(c.DataDir), ProbeOptions{URL: c.ProbeL0URL})
	}
	ok := 0
	for _, outcome := range outcomes {
		if outcome.OK {
			ok++
		}
	}
	prober := DeadProbeRunner{DB: c.DB, Gate: c.Gate, DataDir: c.DataDir, Now: c.Now}
	recovery, recoveryErr := prober.RecoverDeadNodes(ctx, c.ProbeL0URL, c.ProbeBatch)
	recovered := 0
	if recoveryErr == nil {
		for _, outcome := range recovery {
			if outcome.OK {
				recovered++
			}
		}
	}
	after, afterErr := c.eligible(ctx, now, true)
	if beforeErr == nil && afterErr == nil && before.changed(after) {
		RequestRebuild()
	}
	return map[string]any{
		"l0_total": len(outcomes), "l0_ok": ok,
		"dead_probe_total": len(recovery), "dead_probe_recovered": recovered,
	}, errors.Join(beforeErr, l0Err, recoveryErr, afterErr)
}

// RunMaintenance checks the saved throttle and reserves the live runtime before probing.
func (c *Cycle) RunMaintenance(ctx context.Context, checker *HealthChecker) (map[string]any, error) {
	if occupancy.CrawlerBusy() {
		return map[string]any{"busy": true, "maintenance": "busy"}, nil
	}
	if checker == nil {
		return map[string]any{"maintenance": "runtime_unavailable"}, nil
	}
	started := c.now()
	if due, err := c.maintenanceDue(ctx, started); err != nil {
		slog.Warn("读取代理体检间隔失败，本轮执行体检", "error", err)
	} else if !due {
		return map[string]any{"maintenance": "throttled"}, nil
	}
	if err := occupancy.BeginMaintenance(occupancy.MaintenanceHealth); err != nil {
		return map[string]any{"busy": true, "maintenance": "busy"}, nil
	}
	defer occupancy.EndMaintenance()
	startedMonotonic := time.Now()

	names := PoolFileNames(c.DataDir)
	before, err := c.eligible(ctx, started, true)
	if err != nil {
		return nil, err
	}
	previous := currentCycleSelection(ctx, checker)
	runID, err := StartHealthRun(ctx, c.Gate, DefaultScheduleChannel, nil, started)
	if err != nil {
		return nil, err
	}
	c.publish("maintenance.started", events.KV{Key: "totals", Val: []events.KV{
		{Key: "l1", Val: len(names)}, {Key: "l2", Val: len(names)},
	}})
	l1, l1Err := checker.ExitIPCheckPool(ctx, names, ExitProbeOptions{URL: c.ProbeL1URL, RunID: runID})
	l2, l2Err := checker.BusinessCheckPool(ctx, names, BusinessProbeOptions{URL: c.ProbeL2URL, RunID: runID})
	var reasons []string
	if l1Err != nil {
		l1 = nil
		reasons = append(reasons, "l1_error")
	}
	if l2Err != nil {
		l2 = nil
		reasons = append(reasons, "l2_error")
	}
	if ctx.Err() != nil {
		reasons = append(reasons, "cancelled")
	}
	summary := HealthRunSummary{Total: int64(len(l2)), EndReason: strings.Join(reasons, ",")}
	for _, outcome := range l1 {
		if outcome.OK && outcome.ExitIP != "" {
			summary.IPKnown++
		}
	}
	for _, outcome := range l2 {
		if outcome.OK {
			summary.SteamOK++
		}
	}
	summary.Failed = summary.Total - summary.SteamOK
	result := map[string]any{
		"maintenance": "ran", "health_run_id": runID, "health_total": summary.Total,
		"health_ok": summary.SteamOK, "health_ip_known": summary.IPKnown, "health_failed": summary.Failed,
	}
	if summary.EndReason != "" {
		result["health_end_reason"] = summary.EndReason
	}
	defer func() {
		report := []events.KV{{Key: "durationMs", Val: time.Since(startedMonotonic).Milliseconds()}}
		if ctx.Err() == nil {
			report = append(report, events.KV{Key: "total", Val: summary.Total})
		}
		if ctx.Err() == nil && l2Err == nil && (summary.Total == 0 || slices.ContainsFunc(l2, func(outcome BusinessOutcome) bool {
			return outcome.OK || !strings.HasPrefix(outcome.Detail, NodeProbeFailed)
		})) {
			report = append(report, events.KV{Key: "ok", Val: summary.SteamOK}, events.KV{Key: "failed", Val: summary.Failed})
		}
		c.publish("maintenance.ended", report...)
	}()

	finished := c.now()
	cleanupCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 2*globalSelectTimeout)
	defer cancel()
	finishErr := FinishHealthRun(cleanupCtx, c.Gate, runID, summary, finished, finished.Sub(started).Milliseconds())
	after, readErr := c.eligible(cleanupCtx, finished, true)
	selection := previous
	if readErr == nil {
		if before.changed(after) {
			RequestRebuild()
		}
		if !slices.Contains(after.names, previous) {
			selection = ""
			if len(after.names) > 0 {
				selection = after.names[0]
			}
		}
	}
	var restoreErr error
	if selection != "" {
		if detail := selectSelector(cleanupCtx, checker.ControllerURL, checker.Secret, "GLOBAL", selection, globalSelectTimeout); detail != "" {
			restoreErr = fmt.Errorf("恢复 GLOBAL 失败: %s", detail)
		}
	}
	if finishErr == nil && readErr == nil && restoreErr == nil {
		if err := settings.SetValues(cleanupCtx, c.Gate, settings.Options{Bus: c.Bus}, map[string]any{maintenanceSignatureKey: after.signature()}); err != nil {
			slog.Warn("保存代理体检签名失败", "error", err)
		}
	}
	return result, errors.Join(l1Err, l2Err, finishErr, readErr, restoreErr, ctx.Err())
}

func (c *Cycle) publish(name string, details ...events.KV) {
	if c.Bus != nil {
		c.Bus.Publish(name, append([]events.KV{{Key: "kind", Val: occupancy.MaintenanceHealth}}, details...)...)
	}
}

type cyclePoolState struct {
	names []string
	exits map[string]string
	slots []crawlSlot
}

func (s cyclePoolState) signature() string {
	return sha256Hex([]byte(strings.Join(s.names, "\n")))
}

func (s cyclePoolState) changed(other cyclePoolState) bool {
	return !slices.Equal(s.names, other.names) || !maps.Equal(s.exits, other.exits) ||
		!slices.EqualFunc(s.slots, other.slots, func(a, b crawlSlot) bool {
			return a.ExitIP == b.ExitIP && a.Node == b.Node
		})
}

func (c *Cycle) eligible(ctx context.Context, now time.Time, withExits bool) (cyclePoolState, error) {
	tx, err := c.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return cyclePoolState{}, err
	}
	defer tx.Rollback()
	nodes, err := eligibleNodes(ctx, tx, now)
	if err != nil {
		return cyclePoolState{}, err
	}
	state := cyclePoolState{names: make([]string, 0, len(nodes))}
	if withExits {
		state.exits = make(map[string]string, len(nodes))
		state.slots = selectCrawlSlots(nodes, nil)
	}
	for _, node := range nodes {
		state.names = append(state.names, node.RuntimeName)
		if withExits && node.ExitIP.String != "" {
			state.exits[node.RuntimeName] = node.ExitIP.String
		}
	}
	return state, nil
}

func (c *Cycle) maintenanceDue(ctx context.Context, now time.Time) (bool, error) {
	interval, err := c.readSetting(ctx, maintenanceIntervalKey)
	if err != nil {
		return true, err
	}
	hours := maintenanceHours(interval)
	if hours <= 0 {
		return true, nil
	}
	var startedAt string
	err = c.DB.QueryRowContext(ctx, `SELECT CAST(started_at AS TEXT) FROM health_runs
		WHERE channel = ? AND finished_at IS NOT NULL AND end_reason IS NULL AND total > 0 AND steam_ok > 0
		ORDER BY started_at DESC LIMIT 1`, DefaultScheduleChannel).Scan(&startedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return true, nil
	}
	if err != nil {
		return true, err
	}
	started, err := parsePyDatetime(startedAt)
	if err != nil {
		return true, err
	}
	localNow, err := parsePyDatetime(pyTimeText(now))
	if err != nil || localNow.Sub(started).Hours() >= hours {
		return true, err
	}
	state, err := c.eligible(ctx, now, false)
	if err != nil {
		return true, err
	}
	signature, err := c.readSetting(ctx, maintenanceSignatureKey)
	return signature != state.signature(), err
}

func (c *Cycle) readSetting(ctx context.Context, key string) (any, error) {
	var raw sql.NullString
	err := c.DB.QueryRowContext(ctx, "SELECT CAST(value_json AS TEXT) FROM app_settings WHERE key = ?", key).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil || !raw.Valid {
		return nil, err
	}
	var value any
	err = json.Unmarshal([]byte(raw.String), &value)
	return value, err
}

func maintenanceHours(value any) float64 {
	switch hours := value.(type) {
	case float64:
		return hours
	case string:
		if n, err := strconv.ParseFloat(strings.TrimSpace(hours), 64); err == nil && !math.IsNaN(n) && !math.IsInf(n, 0) {
			return n
		}
	case bool:
		if hours {
			return 1
		}
		return 0
	}
	return defaultMaintenanceHours
}

func currentCycleSelection(ctx context.Context, checker *HealthChecker) string {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(checker.ControllerURL, "/")+"/proxies/GLOBAL", nil)
	if err != nil {
		return ""
	}
	if checker.Secret != "" {
		request.Header.Set("Authorization", "Bearer "+checker.Secret)
	}
	client := probeHTTPClient(globalSelectTimeout)
	defer client.CloseIdleConnections()
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	response, err := client.Do(request)
	if err != nil {
		return ""
	}
	defer response.Body.Close()
	var selected struct{ Now string }
	if response.StatusCode != http.StatusOK || json.NewDecoder(response.Body).Decode(&selected) != nil {
		return ""
	}
	return selected.Now
}
