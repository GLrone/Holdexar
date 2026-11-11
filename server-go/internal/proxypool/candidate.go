package proxypool

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/store"
)

type CandidateCheck struct {
	DB         *sql.DB
	Gate       *store.WriteGate
	DataDir    string
	ExePath    string
	BundledExe string
	Now        func() time.Time
	Business   BusinessProbeOptions
	ExitIP     ExitProbeOptions

	mu sync.Mutex
}

type CandidateReport struct {
	Promoted []int64
	Failures map[int64]string
}

// Check runs first checks for explicit candidates, or all auto-refresh candidates when ids is nil.
func (c *CandidateCheck) Check(ctx context.Context, ids []int64) (CandidateReport, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	report := CandidateReport{Failures: map[int64]string{}}
	if ids != nil && len(ids) == 0 {
		return report, nil
	}
	targets, err := c.candidateIDs(ctx, ids)
	if err != nil {
		return report, err
	}
	for _, id := range targets {
		if err := ctx.Err(); err != nil {
			return report, err
		}
		promoted, err := c.checkOne(ctx, id, ids == nil)
		if promoted {
			report.Promoted = append(report.Promoted, id)
		}
		if err != nil {
			report.Failures[id] = err.Error()
		}
	}
	return report, ctx.Err()
}

func (c *CandidateCheck) candidateIDs(ctx context.Context, ids []int64) ([]int64, error) {
	query := "SELECT id FROM proxy_subscriptions WHERE kind = 'clash' AND admission_status = ?"
	args := []any{AdmissionCandidate}
	if ids == nil {
		query += " AND auto_refresh = 1"
	} else {
		query += " AND id IN (" + placeholders(len(ids)) + ")"
		for _, id := range ids {
			args = append(args, id)
		}
	}
	rows, err := c.DB.QueryContext(ctx, query+" ORDER BY id", args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var found []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		found = append(found, id)
	}
	return found, rows.Err()
}

func (c *CandidateCheck) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

func (c *CandidateCheck) candidateSnapshot(ctx context.Context, conn *sql.Conn, id int64, automatic bool) (*Snapshot, error) {
	var sealed string
	var autoRefresh bool
	var stats, lastSuccess sql.NullString
	err := conn.QueryRowContext(ctx, `SELECT url, auto_refresh, last_stats, CAST(last_success_at AS TEXT) FROM proxy_subscriptions
		WHERE id = ? AND kind = 'clash' AND admission_status = ?`, id, AdmissionCandidate).Scan(&sealed, &autoRefresh, &stats, &lastSuccess)
	if err == sql.ErrNoRows || err == nil && (!lastSuccess.Valid || automatic && !autoRefresh) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	url := OpenURL(c.DataDir, sealed)
	if url == "" {
		return nil, errors.New(UnreadableMessage)
	}
	snap, err := RestoreLatestSnapshot(ctx, conn, c.DataDir, id, url)
	if err != nil {
		return nil, errors.New("订阅快照不可恢复")
	}
	if snap == nil {
		return nil, nil
	}
	traffic := stats.String
	if snap.Userinfo != "" {
		traffic = pyDumpJSONText(map[string]any{"traffic": snap.Userinfo})
	}
	if reason := SubscriptionBlockReason(traffic, c.now()); reason != "" {
		return nil, errors.New(reason)
	}
	return snap, nil
}

func (c *CandidateCheck) checkOne(ctx context.Context, id int64, automatic bool) (bool, error) {
	conn, err := c.DB.Conn(ctx)
	if err != nil {
		return false, err
	}
	snap, err := c.candidateSnapshot(ctx, conn, id, automatic)
	conn.Close()
	if err != nil || snap == nil {
		return false, err
	}

	started := c.now()
	runID, err := StartHealthRun(ctx, c.Gate, DefaultIngestChannel, &id, started)
	if err != nil {
		return false, err
	}
	rows, nodeIDs, err := c.probeSnapshot(ctx, id, snap)
	summary := HealthRunSummary{Total: int64(len(nodeIDs))}
	exits := map[string]bool{}
	var probed int
	for _, row := range rows {
		if !row.Probed {
			continue
		}
		probed++
		if row.Alive {
			summary.SteamOK++
			if row.ExitIP != "" {
				exits[row.ExitIP] = true
			}
		} else {
			summary.Failed++
		}
	}
	summary.IPKnown = int64(len(exits))

	var promoted bool
	if err == nil {
		if probed == 0 {
			err = errors.New("未取得可归属订阅节点的检测结果")
		} else {
			promoted, err = c.commitCandidate(ctx, id, automatic, snap, rows, nodeIDs, runID, summary)
		}
	}
	if err != nil {
		summary.EndReason = err.Error()
	}
	finished := c.now()
	finishCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	finishErr := FinishHealthRun(finishCtx, c.Gate, runID, summary, finished, finished.Sub(started).Milliseconds())
	return promoted, errors.Join(err, finishErr)
}

func (c *CandidateCheck) commitCandidate(ctx context.Context, id int64, automatic bool, snap *Snapshot,
	rows []CheckRow, nodeIDs map[string]string, runID int64, summary HealthRunSummary,
) (bool, error) {
	var promoted bool
	err := c.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		current, err := c.candidateSnapshot(ctx, conn, id, automatic)
		if err != nil {
			return err
		}
		if current == nil || current.URL != snap.URL || current.Sha256 != snap.Sha256 {
			return errors.New("检测期间订阅内容或归属发生变化，结果未应用")
		}
		now := c.now()
		applied, err := ApplySnapshot(ctx, conn, id, current, now, SubscriptionTag(current.URL))
		if err != nil {
			return err
		}
		if applied.SkippedStale {
			return errors.New("检测的订阅快照已过期，结果未应用")
		}
		if _, err := IngestNodeCheck(ctx, conn, id, rows, nodeIDs, now, runID, DefaultIngestChannel); err != nil {
			return err
		}
		patch := map[string]any{"alive": summary.SteamOK, "aliveUnique": summary.IPKnown, "total": summary.Total}
		if current.Userinfo != "" {
			patch["traffic"] = current.Userinfo
		}
		if _, err := conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET last_stats =
			json_patch(CASE WHEN json_valid(last_stats) THEN last_stats ELSE '{}' END, ?) WHERE id = ?`,
			pyDumpJSONText(patch), id); err != nil {
			return err
		}
		if summary.SteamOK >= AutoAdmitMinAlive {
			result, err := AutoAdmitHealthyCandidate(ctx, conn, c.DataDir, id, now)
			if err != nil {
				return err
			}
			if result == nil || !result.Promoted {
				return errors.New("通过体检的订阅未能加入生产池")
			}
			promoted = true
		}
		return ctx.Err()
	})
	return promoted && err == nil, err
}

func candidateBundle(id int64, snap *Snapshot) (clashcfg.Bundle, []string, map[string]string, error) {
	var groups []map[string]any
	if snap.Fmt == FormatYAML || snap.Fmt == FormatProviderYAML {
		doc, err := clashYAMLLoad(string(snap.RawContent))
		if err != nil {
			return clashcfg.Bundle{}, nil, nil, err
		}
		entries, _ := doc["proxy-groups"].([]any)
		for _, entry := range entries {
			if group, ok := entry.(map[string]any); ok {
				groups = append(groups, group)
			}
		}
	}
	names := map[string]string{}
	nodeIDs := map[string]string{}
	seen := map[string]bool{}
	var targets, roots []string
	tag := SubscriptionTag(snap.URL)
	for i, node := range snap.Nodes {
		original := pyStr(node["name"])
		if IsInfoPlaceholder(original) {
			continue
		}
		if _, exists := names[original]; exists {
			return clashcfg.Bundle{}, nil, nil, errors.New("订阅节点重名")
		}
		name := fmt.Sprintf("candidate-%d", i)
		names[original] = name
		fingerprint, err := NodeFingerprint(node, tag)
		if err != nil {
			return clashcfg.Bundle{}, nil, nil, err
		}
		if !seen[fingerprint] && !pyFalsy(node["server"]) && !pyFalsy(node["type"]) {
			seen[fingerprint] = true
			targets = append(targets, name)
			roots = append(roots, original)
			nodeIDs[name] = fingerprint
		}
	}
	bundle, err := clashcfg.RenderNodeBundle(snap.Nodes, names, clashcfg.BundleOptions{
		Groups: groups, Namespace: fmt.Sprint(id), Roots: roots, RootsSet: true,
	})
	return bundle, targets, nodeIDs, err
}

func (c *CandidateCheck) probeSnapshot(ctx context.Context, id int64, snap *Snapshot) ([]CheckRow, map[string]string, error) {
	bundle, names, nodeIDs, err := candidateBundle(id, snap)
	if err != nil {
		return nil, nodeIDs, errors.New("订阅节点连接依赖不可用")
	}
	if len(names) == 0 {
		return nil, nodeIDs, errors.New("订阅没有可供体检的节点")
	}
	exe := c.ExePath
	if exe == "" {
		exe = clashkernel.KernelExe(c.DataDir)
		if _, err := os.Stat(exe); errors.Is(err, os.ErrNotExist) && c.BundledExe != "" {
			exe = c.BundledExe
		}
	}
	if _, err := os.Stat(exe); err != nil {
		return nil, nodeIDs, errors.New("缺少可用于体检的代理内核")
	}
	exe, err = filepath.Abs(exe)
	if err != nil {
		return nil, nodeIDs, err
	}
	dir, err := os.MkdirTemp("", "holdexar-candidate-")
	if err != nil {
		return nil, nodeIDs, errors.New("无法创建临时体检目录")
	}
	defer os.RemoveAll(dir)

	controller, err := clashcfg.FreeLocalPort()
	if err != nil {
		return nil, nodeIDs, err
	}
	secret := clashcfg.NewControllerSecret()
	base := fmt.Sprintf("127.0.0.1:%d", controller)
	doc := clashcfg.NewDoc()
	doc.Set("external-controller", base)
	doc.Set("secret", secret)
	doc.Set("allow-lan", false)
	doc.Set("bind-address", "127.0.0.1")
	doc.Set("mode", "global")
	doc.Set("log-level", "warning")
	proxies := make([]any, 0, len(bundle.Proxies))
	for _, node := range bundle.Proxies {
		proxies = append(proxies, node)
	}
	doc.Set("proxies", proxies)
	if len(bundle.Groups) > 0 {
		groups := make([]any, 0, len(bundle.Groups))
		for _, group := range bundle.Groups {
			groups = append(groups, group)
		}
		doc.Set("proxy-groups", groups)
	}
	doc.Set("rules", []any{"MATCH,DIRECT"})
	text := clashcfg.InjectProbeLanes(clashcfg.DumpYAML(doc), clashcfg.ProbeLaneOptions{
		PortBase: 24400, LaneCount: min(ProbeConcurrency, len(names)),
	})
	configPath := filepath.Join(dir, "config.yaml")
	if err := os.WriteFile(configPath, []byte(text), 0o600); err != nil {
		return nil, nodeIDs, errors.New("无法写入临时体检配置")
	}
	if err := clashkernel.ValidateConfig(exe, configPath, dir); err != nil {
		return nil, nodeIDs, errors.New("临时体检配置未通过内核预检")
	}
	if err := clashkernel.ConfirmConfigText(configPath, text); err != nil {
		return nil, nodeIDs, errors.New("临时体检配置已变化")
	}
	if err := ctx.Err(); err != nil {
		return nil, nodeIDs, err
	}
	proc, err := clashkernel.Spawn(exe, []string{"-d", dir, "-f", configPath}, filepath.Join(dir, "probe.log"))
	if err != nil {
		return nil, nodeIDs, errors.New("临时体检内核未启动")
	}
	defer proc.Stop()
	baseURL := "http://" + base
	if !waitController(ctx, baseURL, secret, DeadProbeStartupWait) ||
		!clashkernel.PortOwnedByProcess(controller, uint32(proc.Pid())) {
		return nil, nodeIDs, errors.New("临时体检控制器未就绪")
	}
	allLanes := PoolProbeLanes(text)
	lanes := allLanes[:0]
	for _, lane := range allLanes {
		if clashkernel.PortOwnedByProcess(lane.Port, uint32(proc.Pid())) {
			lanes = append(lanes, lane)
		}
	}
	if len(lanes) == 0 {
		return nil, nodeIDs, errors.New("临时体检通道未就绪")
	}
	results := make([]CheckRow, len(names))
	for offset := 0; offset < len(names); offset += len(lanes) {
		if err := ctx.Err(); err != nil {
			return nil, nodeIDs, err
		}
		var wg sync.WaitGroup
		for i, name := range names[offset:min(offset+len(lanes), len(names))] {
			wg.Go(func() {
				lane := lanes[i]
				business, exit := c.Business, c.ExitIP
				business.Selector, exit.Selector = lane.Group, lane.Group
				var ip ExitIPResult
				var ipWG sync.WaitGroup
				ipWG.Go(func() { ip = ProbeExitIP(ctx, baseURL, secret, lane.Port, name, exit) })
				biz := ProbeBusiness(ctx, baseURL, secret, lane.Port, name, business)
				ipWG.Wait()
				results[offset+i] = CheckRow{
					Name: name, Alive: biz.OK, MS: max(1, biz.LatencyMS), ExitIP: ip.ExitIP,
					Probed:      !strings.HasPrefix(biz.Detail, NodeProbeFailed),
					SteamStatus: biz.HTTPStatus, HasSteam: biz.HTTPStatus != 0,
				}
			})
		}
		wg.Wait()
	}
	if !proc.Running() {
		return nil, nodeIDs, errors.New("临时体检内核在检测期间退出")
	}
	return results, nodeIDs, ctx.Err()
}
