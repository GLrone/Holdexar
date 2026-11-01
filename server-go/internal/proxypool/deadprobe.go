package proxypool

// 出池节点恢复探测（Python dead_probe.py 对齐）：DEAD/RETIRED 节点按「最久没被
// 探过」轮转取批，临时内核加载同订阅连接依赖、每目标一个独立 listener，仅探测
// 待恢复目标。归因铁律：只有 NODE 属于节点失败证据，其余（dependency/config/
// kernel/controller/infra）都是「这一探没测到节点」——不构成健康证据。
// 恢复结果记入既有健康观测，仅真实探测结论推进节点状态。

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/store"
)

const (
	// 短命内核使用独立端口段，避免与其他 Runtime 的监听端口冲突。
	ProbePortBase = 23200
	// 每批探测的节点数上限：一个进程内每个节点占一个 listener，批次越大启动越慢。
	DeadProbeBatchSize = 24
	// 启动后等控制器就绪的上限。
	DeadProbeStartupWait = 12 * time.Second
	// 单节点探测超时（毫秒，传给内核的 /delay 接口）。
	DeadProbeNodeTimeoutMS = 5000
)

// 恢复探测结论的归因：只有 CauseNode 属于节点失败证据。
const (
	CauseNode       = "node"
	CauseDependency = "dependency"
	CauseConfig     = "config"
	CauseKernel     = "kernel"
	CauseController = "controller"
	CauseInfra      = "infra"
)

// RecoveredNode 一个待恢复节点的探测输入（只带渲染配置需要的字段）。
type RecoveredNode struct {
	NodeID         string
	RuntimeName    string
	OriginalName   string
	Config         map[string]any
	SubscriptionID sql.NullInt64
}

// DeadProbeOutcome 一次恢复探测的结论（含归因）。
type DeadProbeOutcome struct {
	NodeID      string
	RuntimeName string
	OK          bool
	DelayMS     int64
	Detail      string
	NodeFailure bool
	Cause       string
}

// freeProbePort 优先用 preferred，被占则向上顺延；整段（8 个）占满返回 0。
func freeProbePort(preferred int) int {
	for port := preferred; port < preferred+8; port++ {
		if !clashkernel.PortListening(port) {
			return port
		}
	}
	return 0
}

// loadRecoverableNodes 取一批待恢复节点（DEAD/RETIRED），按「最久没被探过」轮转。
func loadRecoverableNodes(ctx context.Context, q queryable, batch int) ([]RecoveredNode, error) {
	if batch <= 0 {
		return nil, nil
	}
	rows, err := q.QueryContext(ctx, `
		SELECT n.node_id, n.runtime_name, n.original_name, n.subscription_id, CAST(n.normalized_config AS TEXT)
		FROM proxy_nodes n
		LEFT JOIN (
			SELECT node_id AS lid, MAX(observed_at) AS last_at
			FROM health_observations GROUP BY node_id
		) p ON p.lid = n.node_id
		WHERE n.state IN (?, ?)
		ORDER BY p.last_at ASC, n.node_id ASC
		LIMIT ?`, NodeDead, NodeRetired, batch)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RecoveredNode
	for rows.Next() {
		var (
			nodeID string
			name   string
			orig   sql.NullString
			sid    sql.NullInt64
			cfg    sql.NullString
		)
		if err := rows.Scan(&nodeID, &name, &orig, &sid, &cfg); err != nil {
			return nil, err
		}
		config := map[string]any{}
		if cfg.Valid && cfg.String != "" {
			if jsonUnmarshalUseNumber([]byte(cfg.String), &config) != nil {
				config = map[string]any{}
			}
			config = normalizeJSONNumbers(config).(map[string]any)
		}
		original := orig.String
		if original == "" {
			original = name
		}
		out = append(out, RecoveredNode{
			NodeID: nodeID, RuntimeName: name, OriginalName: original,
			Config: config, SubscriptionID: sid,
		})
	}
	return out, rows.Err()
}

// deadProbeBundle 一次恢复探测合并的节点与依赖组。
type deadProbeBundle struct {
	proxies []any
	groups  []any
}

// deadProbeBundles 按订阅分组渲染恢复目标与连接依赖；渲染失败的节点归因
// dependency（节点参数或其依赖不完整，不是节点失败证据）。
func deadProbeBundles(targets []RecoveredNode, dependencies []RecoveredNode,
	sourceDocuments map[int64]map[string]any, sourceErrors map[int64]string,
) ([]RecoveredNode, deadProbeBundle, []DeadProbeOutcome) {
	// 依赖与目标常是同一批行（涉事订阅名下全体节点）：按 node_id 去重后再入池
	// （Python available 字典语义），否则渲染层按重名拒绝。
	bySub := map[sql.NullInt64][]RecoveredNode{}
	seen := map[string]bool{}
	appendNode := func(n RecoveredNode) {
		if seen[n.NodeID] {
			return
		}
		seen[n.NodeID] = true
		bySub[n.SubscriptionID] = append(bySub[n.SubscriptionID], n)
	}
	for _, n := range dependencies {
		appendNode(n)
	}
	for _, n := range targets {
		appendNode(n)
	}
	var ready []RecoveredNode
	var bundle deadProbeBundle
	var rejected []DeadProbeOutcome
	for _, node := range targets {
		peers := bySub[node.SubscriptionID]
		payloads := make([]map[string]any, len(peers))
		names := map[string]string{}
		for i, peer := range peers {
			payload := map[string]any{}
			for k, v := range peer.Config {
				payload[k] = v
			}
			payload["name"] = peer.OriginalName
			payloads[i] = payload
			names[peer.OriginalName] = peer.RuntimeName
		}
		renderErr := func() error {
			if pyFalsy(node.Config["server"]) || pyFalsy(node.Config["type"]) {
				return fmt.Errorf("节点连接配置不完整")
			}
			var sourceGroups []map[string]any
			if doc := sourceDocuments[node.SubscriptionID.Int64]; doc != nil {
				if raw, ok := doc["proxy-groups"].([]any); ok {
					for _, g := range raw {
						if gm, ok := g.(map[string]any); ok {
							sourceGroups = append(sourceGroups, gm)
						}
					}
				}
			}
			rendered, err := clashcfg.RenderNodeBundle(payloads, names, clashcfg.BundleOptions{
				Groups:    sourceGroups,
				Namespace: nodeSubNamespace(node),
				Roots:     []string{node.OriginalName}, RootsSet: true,
			})
			if err != nil {
				return err
			}
			for _, proxy := range rendered.Proxies {
				ptype := pyStr(proxy["type"])
				if pyFalsy(ptype) || (ptype != "direct" && ptype != "reject" && pyFalsy(proxy["server"])) {
					return fmt.Errorf("节点连接配置不完整：%s", pyStr(proxy["name"]))
				}
			}
			for _, proxy := range rendered.Proxies {
				bundle.proxies = append(bundle.proxies, proxy)
			}
			for _, group := range rendered.Groups {
				bundle.groups = append(bundle.groups, group)
			}
			return nil
		}()
		if renderErr != nil {
			detail := "节点依赖不可用：" + renderErr.Error()
			if sourceError, ok := sourceErrors[node.SubscriptionID.Int64]; ok {
				detail = detail + "；" + sourceError
			}
			rejected = append(rejected, DeadProbeOutcome{
				NodeID: node.NodeID, RuntimeName: node.RuntimeName,
				Detail: detail, NodeFailure: false, Cause: CauseDependency,
			})
			continue
		}
		ready = append(ready, node)
	}
	return ready, bundle, rejected
}

// pyFalsy Python not v 的标量语义（dead_probe.py 的完整性判断用真值而非
// pyStr——pyStr(nil) 是 "None"，判不了缺键）。
func pyFalsy(v any) bool {
	switch x := v.(type) {
	case nil:
		return true
	case string:
		return x == ""
	case bool:
		return !x
	case int:
		return x == 0
	case int64:
		return x == 0
	case float64:
		return x == 0
	case json.Number:
		f, err := x.Float64()
		return err == nil && f == 0
	}
	return false
}

// nodeSubNamespace 恢复目标的订阅命名空间（无归属节点用 0 占位，仅渲染隔离用）。
func nodeSubNamespace(node RecoveredNode) string {
	if !node.SubscriptionID.Valid {
		return "0"
	}
	return strconv.FormatInt(node.SubscriptionID.Int64, 10)
}

// renderDeadProbeConfig 临时内核配置：依赖仅用于连接，listener 只分配给恢复目标。
func renderDeadProbeConfig(targets []RecoveredNode, controllerPort int, secret string,
	listenerBase int, bundle deadProbeBundle) (string, error) {
	listeners := make([]any, len(targets))
	for i, node := range targets {
		listeners[i] = map[string]any{
			"name":   fmt.Sprintf("probe-%d", i),
			"type":   "mixed",
			"port":   int64(listenerBase + i),
			"listen": "127.0.0.1",
			"proxy":  node.RuntimeName,
		}
	}
	doc := clashcfg.NewDoc()
	doc.Set("mode", "global")
	doc.Set("log-level", "warning")
	doc.Set("allow-lan", false)
	doc.Set("bind-address", "127.0.0.1")
	doc.Set("external-controller", fmt.Sprintf("127.0.0.1:%d", controllerPort))
	doc.Set("secret", secret)
	doc.Set("proxies", bundle.proxies)
	if len(bundle.groups) > 0 {
		doc.Set("proxy-groups", bundle.groups)
	}
	doc.Set("listeners", listeners)
	doc.Set("rules", []any{"MATCH,DIRECT"})
	text := clashcfg.DumpYAML(doc)
	if err := clashcfg.ValidateExecutionConfigText(text); err != nil {
		return "", err
	}
	return text, nil
}

// waitController 等临时内核控制器就绪（超时返回 false）。
func waitController(ctx context.Context, baseURL, secret string, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	client := probeHTTPClient(2 * time.Second)
	for time.Now().Before(deadline) {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, baseURL+"/version", nil)
		if err == nil {
			if secret != "" {
				req.Header.Set("Authorization", "Bearer "+secret)
			}
			if resp, err := client.Do(req); err == nil {
				resp.Body.Close()
				if resp.StatusCode == http.StatusOK {
					return true
				}
			}
		}
		select {
		case <-ctx.Done():
			return false
		case <-time.After(200 * time.Millisecond):
		}
	}
	return false
}

// DeadProbeOptions 一次出池探测的输入。
type DeadProbeOptions struct {
	ExePath         string
	WorkDir         string
	URL             string
	Nodes           []RecoveredNode
	Dependencies    []RecoveredNode
	SourceDocuments map[int64]map[string]any
	SourceErrors    map[int64]string
}

// ProbeOutOfPoolNodes 临时内核并发探测；依赖错误与基础设施失败不作为节点失败证据。
// 返回（rejected + 每个就绪目标的结论），顺序与 rejected/目标输入序一致。
func ProbeOutOfPoolNodes(ctx context.Context, opts DeadProbeOptions) []DeadProbeOutcome {
	if len(opts.Nodes) == 0 {
		return nil
	}
	var out []DeadProbeOutcome
	func() {
		defer func() {
			if recovered := recover(); recovered != nil {
				out = appendUnavailable(out, opts.Nodes,
					fmt.Sprintf("探测异常：%T", recovered), CauseInfra)
			}
		}()
		out = probeOutOfPoolRun(ctx, opts)
	}()
	return out
}

func appendUnavailable(base []DeadProbeOutcome, nodes []RecoveredNode, detail, cause string) []DeadProbeOutcome {
	for _, n := range nodes {
		base = append(base, DeadProbeOutcome{
			NodeID: n.NodeID, RuntimeName: n.RuntimeName,
			Detail: detail, NodeFailure: false, Cause: cause,
		})
	}
	return base
}

func probeOutOfPoolRun(ctx context.Context, opts DeadProbeOptions) []DeadProbeOutcome {
	ready, bundle, rejected := deadProbeBundles(
		opts.Nodes, opts.Dependencies, opts.SourceDocuments, opts.SourceErrors)
	unavailable := func(detail, cause string) []DeadProbeOutcome {
		return appendUnavailable(rejected, ready, detail, cause)
	}
	if len(ready) == 0 {
		return rejected
	}
	controllerPort := freeProbePort(ProbePortBase)
	if controllerPort == 0 {
		return unavailable("没有可用的探测端口", CauseInfra)
	}
	listenerBase := controllerPort + 20
	secret := fmt.Sprintf("dead-probe-%d", controllerPort)
	baseURL := fmt.Sprintf("http://127.0.0.1:%d", controllerPort)
	text, err := renderDeadProbeConfig(ready, controllerPort, secret, listenerBase, bundle)
	if err != nil {
		return unavailable("恢复探测配置未通过准入："+err.Error(), CauseConfig)
	}
	if err := os.MkdirAll(opts.WorkDir, 0o755); err != nil {
		return unavailable(fmt.Sprintf("探测异常：%T", err), CauseInfra)
	}
	configPath := filepath.Join(opts.WorkDir, "config.yaml")
	if err := os.WriteFile(configPath, []byte(text), 0o644); err != nil {
		return unavailable(fmt.Sprintf("探测异常：%T", err), CauseInfra)
	}
	precheckErr := clashkernel.ValidateConfig(opts.ExePath, configPath, opts.WorkDir)
	if precheckErr == nil {
		precheckErr = clashkernel.ConfirmConfigText(configPath, text)
	}
	if precheckErr != nil {
		cause := CauseKernel
		if pe, ok := precheckErr.(*clashkernel.PrecheckError); ok && pe.Kind == clashkernel.PrecheckAdmission {
			cause = CauseConfig
		}
		kind := ""
		if pe, ok := precheckErr.(*clashkernel.PrecheckError); ok {
			kind = string(pe.Kind)
		}
		return unavailable(fmt.Sprintf("临时内核预检未通过（%s）：%s", kind, precheckErr.Error()), cause)
	}
	proc, err := clashkernel.Spawn(opts.ExePath,
		[]string{"-d", opts.WorkDir, "-f", configPath},
		filepath.Join(opts.WorkDir, "probe.log"))
	if err != nil {
		return unavailable(fmt.Sprintf("临时内核无法启动：%T", err), CauseKernel)
	}
	defer proc.Stop()
	if !waitController(ctx, baseURL, secret, DeadProbeStartupWait) {
		return unavailable("临时内核控制器未就绪", CauseController)
	}
	results := make([]DeadProbeResult, len(ready))
	sem := make(chan struct{}, ProbeConcurrency)
	var wg sync.WaitGroup
	for i, node := range ready {
		wg.Add(1)
		go func(i int, node RecoveredNode) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			results[i] = probeOutcomeFromResult(node,
				ProbeNode(ctx, baseURL, secret, node.RuntimeName, ProbeOptions{
					URL:           opts.URL,
					TimeoutMS:     DeadProbeNodeTimeoutMS,
					ClientTimeout: 20 * time.Second,
				}))
		}(i, node)
	}
	wg.Wait()
	out := append([]DeadProbeOutcome{}, rejected...)
	for _, r := range results {
		out = append(out, r.Outcome)
	}
	return out
}

// DeadProbeResult 单节点探测的原始结果与结论。
type DeadProbeResult struct {
	Result  ProbeResult
	Outcome DeadProbeOutcome
}

func probeOutcomeFromResult(node RecoveredNode, result ProbeResult) DeadProbeResult {
	cause := CauseController
	if result.Valid {
		cause = CauseNode
	}
	return DeadProbeResult{Result: result, Outcome: DeadProbeOutcome{
		NodeID: node.NodeID, RuntimeName: node.RuntimeName,
		OK: result.OK, DelayMS: result.DelayMS, Detail: result.Detail,
		NodeFailure: result.Valid, Cause: cause,
	}}
}

// loadDeadProbeSourceDocuments 读涉事订阅当前快照的原文文档（proxy-groups 渲染
// 依赖）。读取失败记入 errors（恢复探测对来源失败降级：依赖不可用归因）。
// 与 Python 的差异：不做「尚无快照时回落原始订阅缓存文件」——Go 侧快照落盘即
// 事实源，缓存文件概念不存在。
func loadDeadProbeSourceDocuments(ctx context.Context, conn *sql.Conn, dataDir string,
	subIDs []int64, errors map[int64]string) map[int64]map[string]any {
	if len(subIDs) == 0 {
		return nil
	}
	docs := map[int64]map[string]any{}
	for _, sid := range subIDs {
		doc, err := loadSubscriptionProxyGroups(ctx, conn, dataDir, sid)
		if err != nil {
			if errors != nil {
				errors[sid] = err.Error()
			}
			docs[sid] = map[string]any{}
			continue
		}
		docs[sid] = doc
	}
	return docs
}

// DeadProbeRunner 恢复探测编排器：持有库、写闸与数据目录。
type DeadProbeRunner struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	DataDir string
	Now     func() time.Time
}

func (r *DeadProbeRunner) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

// RecoverDeadNodes 自建内核恢复探测整链：取批 → 定位同订阅依赖与来源文档 →
// 临时内核探测 → 恢复结果记入既有健康观测（仅真实节点结论推进状态）。
// probeURL 为空时按 L2 业务探针目标；无待恢复节点或内核缺失返回 nil。
func (r *DeadProbeRunner) RecoverDeadNodes(ctx context.Context, probeURL string, batch int) ([]DeadProbeOutcome, error) {
	if probeURL == "" {
		probeURL = BusinessProbeURL(DefaultBusinessAppID, BusinessCC)
	}
	now := r.now()
	conn, err := r.DB.Conn(ctx)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	nodes, err := loadRecoverableNodes(ctx, conn, batch)
	if err != nil {
		return nil, err
	}
	if len(nodes) == 0 {
		return nil, nil
	}
	exe := filepath.Join(r.DataDir, "clash", clashkernel.KernelFilename())
	if _, err := os.Stat(exe); err != nil {
		return nil, nil
	}
	subIDSet := map[int64]bool{}
	for _, node := range nodes {
		if node.SubscriptionID.Valid {
			subIDSet[node.SubscriptionID.Int64] = true
		}
	}
	subIDs := make([]int64, 0, len(subIDSet))
	for sid := range subIDSet {
		subIDs = append(subIDs, sid)
	}
	peers, err := loadRecoveredPeers(ctx, conn, subIDs)
	if err != nil {
		return nil, err
	}
	sourceErrors := map[int64]string{}
	documents := loadDeadProbeSourceDocuments(ctx, conn, r.DataDir, subIDs, sourceErrors)

	var targets []RecoveredNode
	var sourceFailures []DeadProbeOutcome
	for _, node := range nodes {
		if node.SubscriptionID.Valid {
			targets = append(targets, node)
			continue
		}
		sourceFailures = append(sourceFailures, DeadProbeOutcome{
			NodeID: node.NodeID, RuntimeName: node.RuntimeName,
			Detail: "节点来源不存在", NodeFailure: false, Cause: CauseDependency,
		})
	}
	workDir := filepath.Join(r.DataDir, "proxypool", "_dead_probe",
		fmt.Sprintf("run-%d", now.UnixMilli()))
	outcomes := ProbeOutOfPoolNodes(ctx, DeadProbeOptions{
		ExePath: exe, WorkDir: workDir, URL: probeURL,
		Nodes: targets, Dependencies: peers,
		SourceDocuments: documents, SourceErrors: sourceErrors,
	})
	_ = os.RemoveAll(workDir)
	byNode := map[string]DeadProbeOutcome{}
	for _, o := range append(sourceFailures, outcomes...) {
		byNode[o.NodeID] = o
	}
	ordered := make([]DeadProbeOutcome, 0, len(nodes))
	for _, node := range nodes {
		if o, ok := byNode[node.NodeID]; ok {
			ordered = append(ordered, o)
		}
	}
	if len(ordered) == 0 {
		return nil, nil
	}
	if err := r.commitRecovery(ctx, ordered, now, probeURL); err != nil {
		return ordered, err
	}
	return ordered, nil
}

// loadRecoveredPeers 涉事订阅名下全部节点行（连接依赖候选）。
func loadRecoveredPeers(ctx context.Context, conn *sql.Conn, subIDs []int64) ([]RecoveredNode, error) {
	if len(subIDs) == 0 {
		return nil, nil
	}
	query := `SELECT node_id, runtime_name, original_name, subscription_id, CAST(normalized_config AS TEXT)
		FROM proxy_nodes WHERE subscription_id IN (` + placeholders(len(subIDs)) + `)`
	args := make([]any, len(subIDs))
	for i, sid := range subIDs {
		args[i] = sid
	}
	rows, err := conn.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []RecoveredNode
	for rows.Next() {
		var (
			nodeID string
			name   string
			orig   sql.NullString
			sid    sql.NullInt64
			cfg    sql.NullString
		)
		if err := rows.Scan(&nodeID, &name, &orig, &sid, &cfg); err != nil {
			return nil, err
		}
		config := map[string]any{}
		if cfg.Valid && cfg.String != "" {
			if jsonUnmarshalUseNumber([]byte(cfg.String), &config) != nil {
				config = map[string]any{}
			}
			config = normalizeJSONNumbers(config).(map[string]any)
		}
		original := orig.String
		if original == "" {
			original = name
		}
		out = append(out, RecoveredNode{
			NodeID: nodeID, RuntimeName: name, OriginalName: original,
			Config: config, SubscriptionID: sid,
		})
	}
	return out, rows.Err()
}

// commitRecovery 恢复结果落账：闸内按主键重读新鲜行，只有 ok 或节点失败证据
// 推进失败计数与状态；每条结论都留 L2 观测（channel=scheduled，不挂运行行）。
func (r *DeadProbeRunner) commitRecovery(ctx context.Context, outcomes []DeadProbeOutcome, now time.Time, probeURL string) error {
	return r.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureHealthSchema(ctx, conn); err != nil {
			return err
		}
		ids := make([]string, len(outcomes))
		for i, o := range outcomes {
			ids[i] = o.NodeID
		}
		fresh, err := loadNodeRowsByID(ctx, conn, ids)
		if err != nil {
			return err
		}
		for _, o := range outcomes {
			row, ok := fresh[o.NodeID]
			if !ok {
				continue
			}
			if o.OK || o.NodeFailure {
				failures := row.ConsecutiveFailures
				if o.OK {
					failures = 0
				} else {
					failures++
				}
				probeOK := o.OK
				target, err := EvaluateNodeState(row.State, EvaluateNodeStateInput{
					SourceSeen:          row.SubscriptionID.Valid,
					ProbeOK:             &probeOK,
					ConsecutiveFailures: int(failures),
				})
				if err != nil {
					return err
				}
				if _, err := conn.ExecContext(ctx,
					`UPDATE proxy_nodes SET consecutive_failures = ?, state = ?, last_l2_at = ? WHERE id = ?`,
					failures, target, pyTimeText(now), row.ID); err != nil {
					return err
				}
			}
			detail := o.Detail
			if detail == "" {
				detail = DetailRecoveryOK
			}
			if err := insertObservation(ctx, conn, HealthObservationRow{
				NodeID: row.NodeID, Level: ProbeLevelL2, OK: o.OK,
				LatencyMS: o.DelayMS, Detail: detail,
				ObservedAt: now, Channel: DefaultScheduleChannel, Target: probeURL,
			}); err != nil {
				return err
			}
		}
		return nil
	})
}
