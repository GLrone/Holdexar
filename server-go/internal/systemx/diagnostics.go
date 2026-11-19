package systemx

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/eventstream"
	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/store"
)

// DiagnosticsDeps 诊断面依赖。
type DiagnosticsDeps struct {
	Gate         *store.WriteGate
	Ring         *eventstream.Ring
	ClashRunning func() bool
	ClashSubID   func(ctx context.Context) *int64
	Cycles       func(ctx context.Context, limit int) ([]map[string]any, error)
	JobRuns      func(ctx context.Context, limit int) (map[string]any, error)
}

const writeLockWaitWarnMS = 30000

func RegisterDiagnostics(mux *http.ServeMux, db *sql.DB, deps DiagnosticsDeps) {
	mux.HandleFunc("GET /api/v1/system/write-scheduler", func(w http.ResponseWriter, r *http.Request) {
		if deps.Gate == nil {
			writeJSON(w, http.StatusInternalServerError, detailNull())
			return
		}
		snap := deps.Gate.Diagnostics(r.Context())
		if snap == nil {
			writeJSON(w, http.StatusInternalServerError, detailNull())
			return
		}
		writeJSON(w, http.StatusOK, snap)
	})
	mux.HandleFunc("GET /api/v1/system/logs", func(w http.ResponseWriter, r *http.Request) {
		limit, errs := queryLimit(r, 200, 1, 2000)
		if errs != nil {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []diagValErr{*errs}})
			return
		}
		lines := []string{}
		if deps.Ring != nil {
			lines = deps.Ring.Snapshot(limit)
		}
		writeJSON(w, http.StatusOK, map[string]any{"lines": lines})
	})
	mux.HandleFunc("GET /api/v1/system/diagnostics", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, diagnosticsSnapshot(r.Context(), db, deps))
	})
}

type diagValErr = httpx.ValErr

func queryLimit(r *http.Request, fallback, minValue, maxValue int) (int, *diagValErr) {
	raw := r.URL.Query().Get("limit")
	if raw == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 0, &diagValErr{Type: "int_parsing", Loc: []any{"query", "limit"},
			Msg: "Input should be a valid integer, unable to parse string as an integer", Input: raw}
	}
	if n < minValue {
		return 0, &diagValErr{Type: "greater_than_equal", Loc: []any{"query", "limit"},
			Msg: fmt.Sprintf("Input should be greater than or equal to %d", minValue), Input: raw,
			Ctx: map[string]any{"ge": minValue}}
	}
	if n > maxValue {
		return 0, &diagValErr{Type: "less_than_equal", Loc: []any{"query", "limit"},
			Msg: fmt.Sprintf("Input should be less than or equal to %d", maxValue), Input: raw,
			Ctx: map[string]any{"le": maxValue}}
	}
	return n, nil
}

// diagnosticsSnapshot 聚合五段诊断面，单面失败降级为 {"error": ...}。
func diagnosticsSnapshot(ctx context.Context, db *sql.DB, deps DiagnosticsDeps) map[string]any {
	identity := diagSection(func() (map[string]any, error) { return identitySection(ctx, db, deps) })
	cycles := diagSection(func() (map[string]any, error) { return cyclesSection(ctx, deps) })
	jobRuns := diagSection(func() (map[string]any, error) { return jobRunsSection(ctx, deps) })
	writeLedger := diagSection(func() (map[string]any, error) { return writeLedgerSection(deps), nil })
	backlog := diagSection(func() (map[string]any, error) { return backlogSection(ctx, db) })

	signals := []map[string]any{}
	lastCycle := map[string]any{}
	if raw, ok := cycles["last"].(map[string]any); ok && raw != nil {
		lastCycle = raw
	}
	if text, ok := lastCycle["status"].(string); ok && text == "failed" {
		reason, _ := lastCycle["error"].(string)
		if reason == "" {
			reason = "原因未随轮留痕"
		}
		signals = append(signals, signal("last_cycle_failed", "error",
			"上一轮价格刷新失败："+reason))
	}
	lockWaitMax, hasLock := maxLockWait(writeLedger)
	if hasLock && lockWaitMax > writeLockWaitWarnMS {
		signals = append(signals, signal("write_lock_wait_high", "warning",
			fmt.Sprintf("最近写账等写锁最高 %d 毫秒（预警线 %d），写库可能存在竞争",
				lockWaitMax, writeLockWaitWarnMS)))
	}
	items := jobRunItems(jobRuns)
	bad := 0
	for _, item := range items {
		switch text, _ := item["status"].(string); text {
		case "failed", "interrupted":
			bad++
		}
	}
	if bad > 0 {
		signals = append(signals, signal("job_runs_failed", "warning",
			fmt.Sprintf("最近 %d 次生产作业 %d 次失败/中断（作业台账面）", len(items), bad)))
	}
	if missingPending, ok := backlog["missing_pending"].(bool); ok && missingPending {
		signals = append(signals, signal("missing_backlog", "info",
			"存在待补抓欠账（补抓通道按冷却自动消化）"))
	}
	if _, failed := identity["error"]; !failed {
		hijacked, _ := identity["network_hijacked"].(bool)
		if hijacked {
			signals = append(signals, signal("network_hijacked", "error",
				netenv.TakeoverReason+"（节点体检与价格刷新已让路）"))
		}
		strategy, _ := identity["strategy"].(string)
		exits, _ := identity["pool_available_exits"].(int)
		if (strategy == "proxy_first" || strategy == "proxy_only") && exits == 0 {
			signals = append(signals, signal("proxy_pool_unavailable", "error",
				"当前策略需要代理池但无可用出口，价格轮会被拒绝（到网络页确认订阅与节点）"))
		}
	}
	return map[string]any{
		"generated_at": time.Now().Format("2006-01-02T15:04:05"),
		"identity":     identity,
		"cycles":       cycles,
		"job_runs":     jobRuns,
		"write_ledger": writeLedger,
		"backlog":      backlog,
		"signals":      signals,
	}
}

func signal(code, severity, text string) map[string]any {
	return map[string]any{"code": code, "severity": severity, "text": text}
}

func diagSection(fn func() (map[string]any, error)) map[string]any {
	out, err := fn()
	if err != nil {
		return map[string]any{"error": clipDiag(err.Error())}
	}
	return out
}

func identitySection(ctx context.Context, db *sql.DB, deps DiagnosticsDeps) (map[string]any, error) {
	if db == nil {
		return nil, errors.New("数据库未就绪")
	}
	strategy := kvStrategy(db)
	var explicit bool
	var raw sql.NullString
	if err := db.QueryRowContext(ctx,
		`SELECT value_json FROM app_settings WHERE key='proxy.strategy_explicit'`).Scan(&raw); err == nil && raw.Valid {
		var value any
		if json.Unmarshal([]byte(raw.String), &value) == nil {
			if flag, ok := value.(bool); ok {
				explicit = flag
			}
		}
	}
	clashRunning := deps.ClashRunning != nil && deps.ClashRunning()
	systemProxy, _ := netenv.ResolveSystemProxy(ctx)
	dataDir := resolveDataDir()
	identity := map[string]any{
		"app_version":          config.Version(),
		"schema_version":       store.CurrentSchemaVersion,
		"data_dir":             dataDir,
		"strategy":             strategy,
		"strategy_explicit":    explicit,
		"clash_running":        clashRunning,
		"system_proxy":         nil,
		"network_hijacked":     netenv.NetworkTakenOver(ctx),
		"pool_available_exits": poolAvailableExits(ctx, db, deps),
	}
	if systemProxy != "" {
		identity["system_proxy"] = systemProxy
	}
	return identity, nil
}

// kvStrategy 读取 proxy.strategy（pinned/clash 归一为 proxy_first）。
func kvStrategy(db *sql.DB) string {
	var raw sql.NullString
	if err := db.QueryRow(
		`SELECT value_json FROM app_settings WHERE key='proxy.strategy'`).Scan(&raw); err != nil || !raw.Valid {
		return "direct_only"
	}
	var value any
	if json.Unmarshal([]byte(raw.String), &value) != nil {
		return "direct_only"
	}
	text, _ := value.(string)
	switch text {
	case "pinned", "clash":
		return "proxy_first"
	case "":
		return "direct_only"
	}
	return text
}

// poolAvailableExits 统计可用出口数（手动池 enabled&ok + 当前订阅 ACTIVE/STALE 且有 L2 节点按出口 IP 去重）。
func poolAvailableExits(ctx context.Context, db *sql.DB, deps DiagnosticsDeps) int {
	var poolOK int
	if err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM proxies WHERE enabled=1 AND status='ok'`).Scan(&poolOK); err != nil {
		return 0
	}
	if deps.ClashSubID == nil {
		return poolOK
	}
	subID := deps.ClashSubID(ctx)
	if subID == nil || *subID <= 0 {
		return poolOK
	}
	var subscriptionReady bool
	if err := db.QueryRowContext(ctx,
		`SELECT EXISTS(SELECT 1 FROM proxy_subscriptions WHERE id=? AND COALESCE(deprecated,0)=0)`,
		*subID).Scan(&subscriptionReady); err != nil || !subscriptionReady {
		return poolOK
	}
	rows, err := db.QueryContext(ctx,
		`SELECT COALESCE(exit_ip,'') FROM proxy_nodes WHERE subscription_id=? AND state IN ('ACTIVE','STALE')
		 AND last_l2_at IS NOT NULL`, *subID)
	if err != nil {
		return poolOK
	}
	defer rows.Close()
	okIdents := map[string]bool{}
	for rows.Next() {
		var exitIP string
		if err := rows.Scan(&exitIP); err != nil {
			break
		}
		okIdents[exitIP] = true
	}
	return poolOK + len(okIdents)
}

func cyclesSection(ctx context.Context, deps DiagnosticsDeps) (map[string]any, error) {
	if deps.Cycles == nil {
		return nil, errors.New("未装配")
	}
	cycles, err := deps.Cycles(ctx, 5)
	if err != nil {
		return nil, err
	}
	var last any
	if len(cycles) > 0 {
		last = cycles[0]
	}
	return map[string]any{"last": last, "recent": cycles}, nil
}

func jobRunsSection(ctx context.Context, deps DiagnosticsDeps) (map[string]any, error) {
	if deps.JobRuns == nil {
		return nil, errors.New("未装配")
	}
	out, err := deps.JobRuns(ctx, 10)
	if err != nil {
		return nil, err
	}
	if out == nil {
		return map[string]any{}, nil
	}
	return out, nil
}

type writeLedgerSample struct {
	Time       string `json:"time"`
	Message    string `json:"message"`
	Entries    *int64 `json:"entries"`
	TotalMS    *int64 `json:"total_ms"`
	LockwaitMS *int64 `json:"lockwait_ms"`
}

func writeLedgerSection(deps DiagnosticsDeps) map[string]any {
	samples := []writeLedgerSample{}
	if deps.Ring != nil {
		samples = parseWriteLedgerLines(deps.Ring.Snapshot(2000), 8)
	}
	return map[string]any{"samples": samples}
}

// parseWriteLedgerLines 从新到旧扫描环形日志，提取最近 limit 条写账样本。
func parseWriteLedgerLines(lines []string, limit int) []writeLedgerSample {
	out := []writeLedgerSample{}
	for i := len(lines) - 1; i >= 0 && len(out) < limit; i-- {
		line := lines[i]
		if !strings.Contains(line, "写库分段完成") {
			continue
		}
		entry := writeLedgerSample{}
		head, tail, hasTail := strings.Cut(line, " │ ")
		parts := strings.SplitN(head, " · ", 2)
		if len(parts) == 2 {
			entry.Message = parts[1]
		}
		if hasTail {
			for _, pair := range strings.Fields(tail) {
				key, value, _ := strings.Cut(pair, "=")
				digits := digitsOnly(value)
				if digits == "" {
					continue
				}
				n, err := strconv.ParseInt(digits, 10, 64)
				if err != nil {
					continue
				}
				number := n
				switch key {
				case "款数":
					entry.Entries = &number
				case "总计":
					entry.TotalMS = &number
				case "等写锁":
					entry.LockwaitMS = &number
				}
			}
		}
		if len(line) >= 8 {
			entry.Time = line[:8]
		}
		out = append(out, entry)
	}
	return out
}

func digitsOnly(value string) string {
	var sb strings.Builder
	for _, r := range value {
		if r >= '0' && r <= '9' {
			sb.WriteRune(r)
		}
	}
	return sb.String()
}

func maxLockWait(section map[string]any) (int64, bool) {
	raw, ok := section["samples"].([]writeLedgerSample)
	if !ok {
		return 0, false
	}
	best, found := int64(0), false
	for _, sample := range raw {
		if sample.LockwaitMS != nil && (!found || *sample.LockwaitMS > best) {
			best, found = *sample.LockwaitMS, true
		}
	}
	return best, found
}

func jobRunItems(section map[string]any) []map[string]any {
	if typed, ok := section["items"].([]map[string]any); ok {
		return typed
	}
	raw, _ := section["items"].([]any)
	out := make([]map[string]any, 0, len(raw))
	for _, item := range raw {
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out
}

// backlogSection 检查非空区且距上次更新超 4 分钟的 missing 价格行数。
func backlogSection(ctx context.Context, db *sql.DB) (map[string]any, error) {
	if db == nil {
		return nil, errors.New("数据库未就绪")
	}
	cutoff := time.Now().UTC().Add(8 * time.Hour).Add(-4 * time.Minute).Format("2006-01-02 15:04:05")
	var exists bool
	if err := db.QueryRowContext(ctx,
		`SELECT EXISTS(SELECT 1 FROM game_current_prices
		 WHERE price_status='missing' AND region_code!='' AND updated_at < ?)`,
		cutoff).Scan(&exists); err != nil {
		return nil, err
	}
	return map[string]any{"missing_pending": exists}, nil
}

func clipDiag(text string) string {
	runes := []rune(text)
	if len(runes) > 200 {
		return string(runes[:200])
	}
	return text
}
