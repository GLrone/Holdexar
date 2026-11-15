// 订阅节点检测链：会话快照（手动检测/首检/定时体检同源）+ 探测 lane 逐节点探测 +
// 池账本落账（冷却/退役/废弃）+ selector 自愈。全程持 TestLock 串行，会话读写走
// testMu；出网（业务探针/出口 IP/控制器/lane 端口预检）全部可注入，测试绝不真出网。
package clashservice

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"regexp"
	"sort"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

// FirstCheckReuseMinutes 启动/切换触发的首检复用窗口：窗口内账本已有结论就沿用，不重测。
const FirstCheckReuseMinutes = 5

const testSteamTimeout = 12 * time.Second
const testExitIPTimeout = 10 * time.Second
const testCtlTimeout = 8 * time.Second

var secretLineRe = regexp.MustCompile(`(?m)^secret\s*:\s*(\S+)\s*$`)

// ─── 会话快照（进度怎么记、谁读）─────────────────────────────

// testNodeRow 单节点检测行（camelCase，对齐 client.ts ClashNodeTestItem）。
type testNodeRow struct {
	Name        string  `json:"name"`
	Alive       bool    `json:"alive"`
	SteamOK     bool    `json:"steamOk"`
	SteamStatus *int    `json:"steamStatus"`
	ExitIP      *string `json:"exitIp"`
	MS          *int64  `json:"ms"`
	Duplicate   bool    `json:"duplicate"`
	Probed      bool    `json:"probed"`
	Cooling     bool    `json:"cooling,omitempty"`
	SourceCode  *string `json:"sourceCode,omitempty"`
	PoolState   *string `json:"poolState,omitempty"`
}

// testSessionSnapshot 一次检测会话（phase=queued/running/done/failed；终态保留）。
type testSessionSnapshot struct {
	Phase           string        `json:"phase"`
	Total           *int          `json:"total"`
	ToProbe         *int          `json:"toProbe"`
	Probed          int           `json:"probed"`
	CooldownSkipped int           `json:"cooldownSkipped"`
	Alive           int           `json:"alive"`
	AliveUnique     *int          `json:"aliveUnique"`
	Selector        *string       `json:"selector"`
	SubscriptionID  *int64        `json:"subscriptionId"`
	Deprecated      *bool         `json:"deprecated"`
	Nodes           []testNodeRow `json:"nodes"`
	StartedAt       string        `json:"startedAt"`
	FinishedAt      *string       `json:"finishedAt"`
	Error           *string       `json:"error"`
}

func newTestSession(now string) *testSessionSnapshot {
	return &testSessionSnapshot{
		Phase:     "queued",
		Nodes:     []testNodeRow{},
		StartedAt: now,
	}
}

func (snap *testSessionSnapshot) clone() *testSessionSnapshot {
	out := *snap
	out.Nodes = make([]testNodeRow, len(snap.Nodes))
	copy(out.Nodes, snap.Nodes)
	return &out
}

func (s *Service) sessionNow() string {
	return s.now().In(sched.DefaultLoc).Format("2006-01-02T15:04:05.000000")
}

// sessionMutate 会话字段更新（无会话时静默——调用方都可能发生在 reset 之前）。
func (s *Service) sessionMutate(fn func(*testSessionSnapshot)) {
	s.testMu.Lock()
	defer s.testMu.Unlock()
	if s.testSession != nil {
		fn(s.testSession)
	}
}

func (s *Service) sessionAppendNode(row testNodeRow) {
	s.sessionMutate(func(snap *testSessionSnapshot) {
		snap.Nodes = append(snap.Nodes, row)
		snap.Probed++
		if row.Alive {
			snap.Alive++
		}
	})
}

// TestProgress 当前/最近一次检测会话快照；无会话 {"phase":"idle"}，终态保留。
func (s *Service) TestProgress(_ context.Context) any {
	s.testMu.Lock()
	defer s.testMu.Unlock()
	if s.testSession == nil {
		return map[string]any{"phase": "idle"}
	}
	return s.testSession.clone()
}

// TestStart 手动检测入口：已有进行中的会话（queued/running）复用直接返回快照
// （手动/首检/体检同源单飞）；否则落 queued 会话并拉起后台任务，立即返回
// 快照（不等探测，对齐 asyncio.create_task）。
func (s *Service) TestStart(_ context.Context) any {
	s.testMu.Lock()
	if s.testSession != nil && (s.testSession.Phase == "queued" || s.testSession.Phase == "running") {
		snap := s.testSession.clone()
		s.testMu.Unlock()
		return snap
	}
	s.testSession = newTestSession(s.sessionNow())
	snap := s.testSession.clone()
	s.testMu.Unlock()

	go func() {
		// 手动检测 = 要全量结论；失败原因（用户语言）已落会话，由进度端点带回。
		if _, err := s.testClashNodes(context.Background(), nil, true); err != nil {
			if _, isStatus := err.(*StatusError); !isStatus {
				s.logf("Clash 节点检测后台会话执行失败：%v", err)
			}
		}
	}()
	return snap
}

// ─── 检测链入口 ──────────────────────────────────────────────

// testResult impl 的返回面（wrapper 据此收口会话 done）。
type testResult struct {
	Total          int
	Probed         int
	Alive          int
	AliveUnique    int
	Selector       string
	SubscriptionID int64
	Deprecated     bool
	Nodes          []testNodeRow
}

// testClashNodes 检测会话入口：探测实现在 impl，进度实时写会话快照，成功/失败
// 都收口到会话（done/failed）。全程持 TestLock（手动/首检/体检三源互斥）。
func (s *Service) testClashNodes(ctx context.Context, subID *int64, probeAll bool) (testResult, error) {
	// 体检/首检直调路径：等锁期间即有 queued 可看。
	s.testMu.Lock()
	if s.testSession == nil {
		s.testSession = newTestSession(s.sessionNow())
	}
	s.testMu.Unlock()

	s.TestLock.Lock()
	defer s.TestLock.Unlock()

	result, err := s.testClashNodesImpl(ctx, subID, probeAll)

	s.testMu.Lock()
	if s.testSession != nil {
		if err != nil {
			msg := err.Error()
			if se, ok := err.(*StatusError); ok {
				msg = se.Detail
			}
			if msg == "" {
				msg = "节点检测失败"
			}
			s.testSession.Phase = "failed"
			s.testSession.Error = &msg
			fin := s.sessionNow()
			s.testSession.FinishedAt = &fin
		} else {
			total, probed := result.Total, result.Probed
			alive, aliveUnique := result.Alive, result.AliveUnique
			selector, subIDOut := result.Selector, result.SubscriptionID
			deprecated := result.Deprecated
			fin := s.sessionNow()
			s.testSession.Phase = "done"
			s.testSession.Total = &total
			s.testSession.Probed = probed
			s.testSession.Alive = alive
			s.testSession.AliveUnique = &aliveUnique
			s.testSession.Selector = &selector
			s.testSession.SubscriptionID = &subIDOut
			s.testSession.Deprecated = &deprecated
			s.testSession.Nodes = result.Nodes
			s.testSession.FinishedAt = &fin
		}
	}
	s.testMu.Unlock()

	if err != nil {
		return testResult{}, err
	}
	return result, nil
}

func (s *Service) readKernelConfig(configPath string) string {
	if configPath == "" {
		return ""
	}
	raw, err := os.ReadFile(configPath)
	if err != nil {
		return ""
	}
	return string(raw)
}

// testClashNodesImpl 检测主链：身份链 → 探测 → 账本 → 摘要 → 自愈。
// 调用方必须已持 TestLock。
func (s *Service) testClashNodesImpl(ctx context.Context, subID *int64, probeAll bool) (testResult, error) {
	s.sessionMutate(func(snap *testSessionSnapshot) { *snap = *newTestSession(s.sessionNow()) })

	status := s.Kernel.Status()
	if !status.Running {
		return testResult{}, statusErr(400, "Clash 未运行：请先在 Clash 接入选择订阅并启动")
	}
	runningURL := status.SubscriptionURL
	runningText := s.readKernelConfig(status.ConfigPath)
	if runningURL == "" || runningText == "" {
		return testResult{}, statusErr(400, "内核运行配置或订阅归属不可确认，请重新启动订阅")
	}
	resolvedID, nodeNames, nodeIDs, registry, err := s.runtimeCheckNodes(ctx, status, runningURL, runningText, subID)
	if err != nil {
		return testResult{}, err
	}

	ctl := s.clashTestCtl(status.ControllerURL, controllerSecretOf(runningText))
	allProxies, err := ctl.AllProxies()
	if err != nil {
		return testResult{}, err
	}

	now := s.now().In(sched.DefaultLoc)
	var toProbe []string
	if probeAll {
		toProbe = append(toProbe, nodeNames...)
	} else {
		for _, name := range nodeNames {
			if !ledgerInCooldown(registry[name], now) {
				toProbe = append(toProbe, name)
			}
		}
	}
	selector := pickRouteSelector(allProxies)
	s.sessionMutate(func(snap *testSessionSnapshot) {
		snap.Phase = "running"
		total := len(nodeNames)
		toProbeCount := len(toProbe)
		snap.Total = &total
		snap.ToProbe = &toProbeCount
		snap.CooldownSkipped = len(nodeNames) - len(toProbe)
		snap.SubscriptionID = &resolvedID
		snap.Selector = &selector
	})

	// 事实源是运行配置不是磁盘历史——lane listener 端口在听才可用，绑不上
	// 端口的 lane 会把「通道不通」误判成「节点不通」，探测前先剔除。
	parsedLanes, _ := clashcfg.ParseProbeLanes(runningText)
	var lanes []clashcfg.ProbeLane
	for _, lane := range parsedLanes {
		if s.portListening(lane.Port) {
			lanes = append(lanes, lane)
		}
	}
	var results []testNodeRow
	if len(lanes) > 0 {
		results = s.probeNodesViaLanes(ctx, ctl, lanes, toProbe)
	} else {
		// selector 回退路径的探测流量走混合端口：入口没在听时全部节点会误判
		// 失败污染账本（并连带触发订阅误废弃），如实报错代替。
		if !s.Kernel.Status().EntryReady {
			return testResult{}, statusErr(400, "内核入口不可用（混合端口未监听）且探测 lane 缺席，无法检测；请重启内核后重试")
		}
		results = s.probeNodesViaSelector(ctx, ctl, status.Port, toProbe, selector)
	}

	order := make(map[string]int, len(nodeNames))
	for i, name := range nodeNames {
		order[name] = i
	}
	sort.SliceStable(results, func(i, j int) bool {
		return order[results[i].Name] < order[results[j].Name]
	})
	markSameExit(results, order)

	if _, err := s.commitTestLedger(ctx, resolvedID, runningURL, runningText, status.ConfigPath, results, nodeIDs, now); err != nil {
		return testResult{}, err
	}
	byID, err := s.loadLedgerNodes(ctx, s.DB, ledgerIDsOf(nodeIDs))
	if err != nil {
		return testResult{}, err
	}
	registry = map[string]*ledgerNode{}
	for name, fp := range nodeIDs {
		if node := byID[fp]; node != nil {
			registry[name] = node
		}
	}
	aliveCount, uniqueAlive, deprecated, err := s.summarizeNodeResults(ctx, resolvedID, results, nodeNames, now, registry)
	if err != nil {
		return testResult{}, err
	}

	code := fmt.Sprintf("sd%d", resolvedID)
	for i := range results {
		if !results[i].Probed {
			continue
		}
		sc, ps := code, ""
		if node := registry[results[i].Name]; node != nil {
			ps = node.State
		}
		results[i].SourceCode, results[i].PoolState = &sc, &ps
	}

	s.selfHealSelector(ctx, ctl, selector, results, registry, allProxies, now)

	nodes := make([]testNodeRow, 0, len(results)+len(nodeNames))
	nodes = append(nodes, results...)
	nodes = append(nodes, cooldownNodesPayload(nodeNames, probedNames(results), registry)...)
	return testResult{
		Total: len(nodeNames), Probed: len(results), Alive: aliveCount,
		AliveUnique: uniqueAlive, Selector: selector, SubscriptionID: resolvedID,
		Deprecated: deprecated, Nodes: nodes,
	}, nil
}

// ─── 身份链（以运行正文及 URL 确定节点身份，缺少归属证据不猜订阅）──

func (s *Service) runtimeCheckNodes(ctx context.Context, status clashkernel.Status, runningURL, runningText string, subID *int64) (int64, []string, map[string]string, map[string]*ledgerNode, error) {
	runningID, hasRunningID := SubscriptionIDOf(status.ConfigPath)
	if subID != nil && hasRunningID && *subID != runningID {
		return 0, nil, nil, nil, statusErr(400, "检测订阅与内核实际运行订阅不一致")
	}
	var matches []int64
	rows, err := s.DB.QueryContext(ctx, `SELECT id, url FROM proxy_subscriptions WHERE kind='clash' ORDER BY id`)
	if err != nil {
		return 0, nil, nil, nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id int64
		var stored string
		if err := rows.Scan(&id, &stored); err != nil {
			return 0, nil, nil, nil, err
		}
		if proxypool.OpenURL(s.DataDir, stored) == runningURL &&
			(subID == nil || id == *subID) &&
			(!hasRunningID || id == runningID) {
			matches = append(matches, id)
		}
	}
	if err := rows.Err(); err != nil {
		return 0, nil, nil, nil, err
	}
	if len(matches) != 1 {
		return 0, nil, nil, nil, statusErr(400, "检测订阅与内核实际运行订阅不一致")
	}
	resolved := matches[0]

	// 节点身份从订阅原文缓存取（与同步期同一解析器、同一指纹来源）。运行配置
	// 是渲染产物，节点段与原文一致——proxypool 的严格 YAML 解析器不认渲染产物
	// 的顶层零缩进序列，原文是唯一稳定可解析的节点事实源。
	rawText := s.readKernelConfig(SubscriptionConfigPath(s.DataDir, resolved))
	if rawText == "" {
		return 0, nil, nil, nil, statusErr(400, "订阅缓存不可读，节点身份不可确认；请刷新订阅后重试")
	}
	format, err := proxypool.DetectFormat([]byte(rawText))
	if err != nil {
		return 0, nil, nil, nil, statusErr(400, "订阅缓存不可解析，体检未开始；请刷新订阅后重试")
	}
	parsed, err := proxypool.ParseNodes([]byte(rawText), format)
	if err != nil {
		return 0, nil, nil, nil, statusErr(400, "订阅缓存没有可检测的节点，体检未开始；请刷新订阅后重试")
	}
	tag := proxypool.SubscriptionTag(runningURL)
	names := []string{}
	nodeIDs := map[string]string{}
	seen := map[string]bool{}
	for _, node := range parsed {
		name := anyStr(node["name"])
		if name == "" || seen[name] {
			continue
		}
		seen[name] = true
		fp, ferr := proxypool.NodeFingerprint(node, tag)
		if ferr != nil {
			return 0, nil, nil, nil, statusErr(400, "运行节点身份不可计算，体检未开始；请刷新订阅后重试")
		}
		names = append(names, name)
		nodeIDs[name] = fp
	}
	byID, err := s.loadLedgerNodes(ctx, s.DB, ledgerIDsOf(nodeIDs))
	if err != nil {
		return 0, nil, nil, nil, err
	}
	registry := map[string]*ledgerNode{}
	for name, fp := range nodeIDs {
		node := byID[fp]
		if node == nil || (node.SubscriptionID.Valid && node.SubscriptionID.Int64 != resolved) {
			return 0, nil, nil, nil, statusErr(400, "运行节点身份未登记，体检未开始；请刷新订阅后重试")
		}
		registry[name] = node
	}
	return resolved, names, nodeIDs, registry, nil
}

// ─── 探测通道 ────────────────────────────────────────────────

func (s *Service) portListening(port int) bool {
	if s.PortListeningFn != nil {
		return s.PortListeningFn(port)
	}
	return clashkernel.PortListening(port)
}

func (s *Service) steamProbe(ctx context.Context, port int) (bool, int) {
	if s.SteamProbe != nil {
		return s.SteamProbe(ctx, port)
	}
	return s.steamProbeDefault(ctx, port)
}

func (s *Service) exitIPProbe(ctx context.Context, port int) string {
	if s.ExitIPProbe != nil {
		return s.ExitIPProbe(ctx, port)
	}
	return s.exitIPProbeDefault(ctx, port)
}

// probeNodesViaLanes 经探测 lane 并发探测（并发上限 = lane 数，20）：每条 lane
// 一个独立 listener + include-all select 组，组选择互不干扰。节点多于 lane 数
// 分波：每波先切组再整波并发，波内即进度（逐行进会话）。
func (s *Service) probeNodesViaLanes(ctx context.Context, ctl clashTestController, lanes []clashcfg.ProbeLane, names []string) []testNodeRow {
	results := []testNodeRow{}
	for start := 0; start < len(names); start += len(lanes) {
		end := start + len(lanes)
		if end > len(names) {
			end = len(names)
		}
		wave := names[start:end]
		rows := make([]testNodeRow, len(wave))
		var wg sync.WaitGroup
		for i, name := range wave {
			wg.Add(1)
			go func(i int, name string) {
				defer wg.Done()
				rows[i] = s.probeNodeViaLane(ctx, ctl, lanes[i], name)
			}(i, name)
		}
		wg.Wait()
		results = append(results, rows...)
		for _, row := range rows {
			s.sessionAppendNode(row)
		}
	}
	return results
}

// probeNodeViaLane 单节点经单条探测 lane：切本 lane 组 → 组选择回读核验
// now==name → 并发跑 Steam 存活与出口 IP。回读不对齐（选择没生效）按探不到
// 处理——不产生该节点的健康证据。
func (s *Service) probeNodeViaLane(ctx context.Context, ctl clashTestController, lane clashcfg.ProbeLane, name string) testNodeRow {
	blank := testNodeRow{Name: name}
	if err := ctl.PutProxy(lane.Group, name); err != nil {
		return blank
	}
	now, err := ctl.ProxyNow(lane.Group)
	if err != nil || now != name {
		return blank
	}
	started := time.Now()
	var (
		wg          sync.WaitGroup
		steamOK     bool
		steamStatus int
		exitIP      string
	)
	wg.Add(2)
	go func() { defer wg.Done(); steamOK, steamStatus = s.steamProbe(ctx, lane.Port) }()
	go func() { defer wg.Done(); exitIP = s.exitIPProbe(ctx, lane.Port) }()
	wg.Wait()
	if ctx.Err() != nil {
		return testNodeRow{Name: name, Probed: true}
	}
	ms := time.Since(started).Milliseconds()
	return testNodeRow{
		Name: name, Alive: steamOK, SteamOK: steamOK,
		SteamStatus: intPtrNonZero(steamStatus), ExitIP: strPtr(exitIP), MS: &ms, Probed: true,
	}
}

// probeNodesViaSelector 回退路径：内核配置无探测 lane 时逐节点切路由 selector
// 经混合端口探测。selector 切换是全局状态，只能串行；rule 模式下流量按规则组
// 走（不经过 GLOBAL），检测期间临时切 global 模式让 GLOBAL 成为总闸，测完恢复。
func (s *Service) probeNodesViaSelector(ctx context.Context, ctl clashTestController, mixedPort int, names []string, selector string) []testNodeRow {
	prevMode := ""
	if mode, err := ctl.CurrentMode(); err == nil {
		prevMode = mode
		if prevMode != "" && prevMode != "global" {
			if err := ctl.PatchMode("global"); err != nil {
				prevMode = "" // 模式切换失败则按当前模式尽力检测
			}
		}
	}
	results := []testNodeRow{}
	for _, name := range names {
		row := testNodeRow{Name: name}
		if err := ctl.PutProxy(selector, name); err == nil {
			started := time.Now()
			var (
				wg          sync.WaitGroup
				steamOK     bool
				steamStatus int
				exitIP      string
			)
			wg.Add(2)
			go func() { defer wg.Done(); steamOK, steamStatus = s.steamProbe(ctx, mixedPort) }()
			go func() { defer wg.Done(); exitIP = s.exitIPProbe(ctx, mixedPort) }()
			wg.Wait()
			if ctx.Err() == nil {
				ms := time.Since(started).Milliseconds()
				row = testNodeRow{
					Name: name, Alive: steamOK, SteamOK: steamOK,
					SteamStatus: intPtrNonZero(steamStatus), ExitIP: strPtr(exitIP), MS: &ms, Probed: true,
				}
			} else {
				row.Probed = true
			}
		}
		results = append(results, row)
		s.sessionAppendNode(row)
	}
	if prevMode != "" && prevMode != "global" {
		if err := ctl.PatchMode(prevMode); err != nil {
			s.logf("检测后恢复内核路由模式失败，内核可能仍处于 global 模式（重启内核可恢复）│ 原模式=%s", prevMode)
		}
	}
	return results
}

// ─── 同出口去重（代表行择优，其余标 duplicate）─────────────────

func sameExitKeyLess(a, b testNodeRow, order map[string]int) bool {
	key := func(r testNodeRow) (int, bool, int64, int) {
		ms := int64(-1)
		if r.MS != nil {
			ms = *r.MS
		}
		return boolToInt(!r.Alive), r.Cooling, ms, order[r.Name]
	}
	aAlive, aCool, aMS, aOrder := key(a)
	bAlive, bCool, bMS, bOrder := key(b)
	if aAlive != bAlive {
		return aAlive < bAlive
	}
	if aCool != bCool {
		return aCool
	}
	if aMS != bMS {
		if aMS < 0 || bMS < 0 {
			return bMS < 0 // 无延迟排后（inf 语义）
		}
		return aMS < bMS
	}
	return aOrder < bOrder
}

func boolToInt(b bool) int {
	if b {
		return 1
	}
	return 0
}

func markSameExit(results []testNodeRow, order map[string]int) {
	groups := map[string][]int{}
	for i, row := range results {
		if row.ExitIP != nil && *row.ExitIP != "" {
			groups[*row.ExitIP] = append(groups[*row.ExitIP], i)
		}
	}
	for _, idxs := range groups {
		primary := idxs[0]
		for _, i := range idxs[1:] {
			if sameExitKeyLess(results[i], results[primary], order) {
				primary = i
			}
		}
		for _, i := range idxs {
			results[i].Duplicate = i != primary
		}
	}
}

// ─── 账本落账（健康、观测与冷却同一事务）──────────────────────

// commitTestLedger 归属守卫 → 建体检运行行 → ingest 逐节点对齐 → 收口运行行，
// 全部在一个写事务内（落账失败不能继续写一份冷却结果）。
func (s *Service) commitTestLedger(ctx context.Context, subID int64, runningURL, runningText, runningConfigPath string, results []testNodeRow, nodeIDs map[string]string, now time.Time) (proxypool.IngestionResult, error) {
	checkRows := make([]proxypool.CheckRow, 0, len(results))
	for _, row := range results {
		cr := proxypool.CheckRow{Name: row.Name, Alive: row.Alive, Probed: row.Probed}
		if row.ExitIP != nil {
			cr.ExitIP = *row.ExitIP
		}
		if row.MS != nil {
			cr.MS = *row.MS
		}
		if row.SteamStatus != nil {
			cr.SteamStatus = *row.SteamStatus
			cr.HasSteam = true
		}
		checkRows = append(checkRows, cr)
	}
	var outcome proxypool.IngestionResult
	err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var storedURL string
		err := conn.QueryRowContext(ctx, `SELECT url FROM proxy_subscriptions WHERE id=?`, subID).Scan(&storedURL)
		if err != nil || proxypool.OpenURL(s.DataDir, storedURL) != runningURL ||
			s.runtimeIdentityChanged(runningURL, runningText, runningConfigPath) {
			return statusErr(400, "体检期间运行配置或订阅归属已变化，结果未落账")
		}
		if err := proxypool.EnsureHealthSchema(ctx, conn); err != nil {
			return err
		}
		res, err := conn.ExecContext(ctx,
			`INSERT INTO health_runs (channel, subscription_id, started_at, total, steam_ok, ip_known, failed)
			 VALUES ('manual', ?, ?, 0, 0, 0, 0)`, subID, pyTextTime(now))
		if err != nil {
			return err
		}
		runID, err := res.LastInsertId()
		if err != nil {
			return err
		}
		outcome, err = proxypool.IngestNodeCheck(ctx, conn, subID, checkRows, nodeIDs, now, runID, "manual")
		if err != nil {
			return err
		}
		finished := s.now()
		steamOK, ipKnown := 0, map[string]bool{}
		for _, row := range results {
			if row.Alive {
				steamOK++
				if row.ExitIP != nil && *row.ExitIP != "" {
					ipKnown[*row.ExitIP] = true
				}
			}
		}
		if _, err := conn.ExecContext(ctx,
			`UPDATE health_runs SET total=?, steam_ok=?, ip_known=?, failed=?, finished_at=?, duration_ms=? WHERE id=?`,
			len(results), steamOK, len(ipKnown), len(results)-steamOK,
			pyTextTime(finished), finished.Sub(now).Milliseconds(), runID); err != nil {
			return err
		}
		return nil
	})
	if err != nil {
		return proxypool.IngestionResult{}, err
	}
	if outcome.Changed() {
		proxypool.RequestRebuild()
	}
	s.logf("体检结果对齐池账本：本轮命中 %d 个 │ 命中=%d 复活=%d 新增出口=%d",
		outcome.Matched, outcome.Matched, outcome.Activated, outcome.ExitIPsAdded)
	return outcome, nil
}

// runtimeIdentityChanged 检测期间内核的订阅归属/配置正文是否漂移
// （Python 逐项对照 process 身份；Go 进程身份经由 Status 的订阅 URL 与
// 配置路径判定——Stop/切换都会清掉或换掉这两者）。
func (s *Service) runtimeIdentityChanged(runningURL, runningText, runningConfigPath string) bool {
	cur := s.Kernel.Status()
	if !cur.Running || cur.SubscriptionURL != runningURL || cur.ConfigPath != runningConfigPath {
		return true
	}
	return s.readKernelConfig(cur.ConfigPath) != runningText
}

// ─── 摘要（存活统计 / 订阅废弃判定 / 走线日志）─────────────────

// ledgerNode 检测链视角的权威节点行（proxy_nodes）。
type ledgerNode struct {
	ID                  int64
	NodeID              string
	State               string
	SubscriptionID      sql.NullInt64
	ConsecutiveFailures int64
	ExitIP              sql.NullString
	CooldownUntil       sql.NullString
	LastL2At            sql.NullString
}

func ledgerIDsOf(nodeIDs map[string]string) []string {
	seen := map[string]bool{}
	ids := []string{}
	for _, id := range nodeIDs {
		if id != "" && !seen[id] {
			seen[id] = true
			ids = append(ids, id)
		}
	}
	return ids
}

type rowQuerier interface {
	QueryContext(ctx context.Context, query string, args ...any) (*sql.Rows, error)
}

func (s *Service) loadLedgerNodes(ctx context.Context, q rowQuerier, ids []string) (map[string]*ledgerNode, error) {
	out := map[string]*ledgerNode{}
	if len(ids) == 0 {
		return out, nil
	}
	placeholders := strings.TrimSuffix(strings.Repeat("?,", len(ids)), ",")
	args := make([]any, len(ids))
	for i, id := range ids {
		args[i] = id
	}
	query := `SELECT id, node_id, state, subscription_id, consecutive_failures, exit_ip,
		CAST(cooldown_until AS TEXT), CAST(last_l2_at AS TEXT)
		FROM proxy_nodes WHERE node_id IN (` + placeholders + `)`
	rows, err := q.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		node := &ledgerNode{}
		if err := rows.Scan(&node.ID, &node.NodeID, &node.State, &node.SubscriptionID,
			&node.ConsecutiveFailures, &node.ExitIP, &node.CooldownUntil, &node.LastL2At); err != nil {
			return nil, err
		}
		out[node.NodeID] = node
	}
	return out, rows.Err()
}

func pyTextTime(t time.Time) string {
	return t.Format("2006-01-02 15:04:05.000000")
}

// parsePyTime 库内时间文本（pyTimeText 的北京挂钟 naive）→ time.Time。
// 按写入时区（DefaultLoc）还原时刻，保证与 s.now() 的时刻比较不受机器时区影响。
func parsePyTime(text string) (time.Time, bool) {
	for _, layout := range []string{
		"2006-01-02 15:04:05.000000", "2006-01-02 15:04:05",
		"2006-01-02T15:04:05.000000", "2006-01-02T15:04:05",
	} {
		if t, err := time.ParseInLocation(layout, text, sched.DefaultLoc); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

// ledgerInCooldown 节点是否处于冷却期（冷却到点 = 该测了）。
func ledgerInCooldown(node *ledgerNode, now time.Time) bool {
	if node == nil || !node.CooldownUntil.Valid || node.CooldownUntil.String == "" {
		return false
	}
	until, ok := parsePyTime(node.CooldownUntil.String)
	return ok && until.After(now)
}

// ledgerAliveByLedger 冷却跳过/自愈口径的账本存活：ACTIVE/STALE 且有体检证据。
func ledgerAliveByLedger(node *ledgerNode) bool {
	return node != nil && (node.State == proxypool.NodeActive || node.State == proxypool.NodeStale) &&
		node.LastL2At.Valid && node.LastL2At.String != ""
}

// summarizeNodeResults 摘要消费账本健康，落订阅统计和走线日志。
// 冷却跳过节点的存活口径读权威账本（与投影期 ok 判定同语义）。
func (s *Service) summarizeNodeResults(ctx context.Context, subID int64, results []testNodeRow, nodeNames []string, now time.Time, registry map[string]*ledgerNode) (int, int, bool, error) {
	statsJSON, subDeprecated, blockedByResources := s.readSubscriptionStats(ctx, subID)
	if len(nodeNames) > 0 && !anyProbed(results) {
		// 没有任何实探结论（全冷却跳过/全没探到）：沿用上次统计口径。
		var stats map[string]any
		_ = json.Unmarshal([]byte(statsJSON), &stats)
		alive := minInt(pyIntOr(stats["alive"]), len(nodeNames))
		deprecated := subDeprecated
		if blockedByResources {
			dep, err := s.evaluateSubscriptionDeprecation(ctx, subID, alive, len(nodeNames))
			if err != nil {
				return 0, 0, false, err
			}
			deprecated = dep
		}
		return alive, pyIntOr(stats["aliveUnique"]), deprecated, nil
	}

	aliveCount := 0
	uniqueIPs := map[string]bool{}
	for _, row := range results {
		if row.Probed && row.Alive {
			aliveCount++
			if row.ExitIP != nil && *row.ExitIP != "" {
				uniqueIPs[*row.ExitIP] = true
			}
		}
	}
	probedSet := probedNames(results)
	for _, name := range nodeNames {
		if probedSet[name] {
			continue
		}
		node := registry[name]
		if !ledgerAliveByLedger(node) {
			continue
		}
		aliveCount++
		if node.ExitIP.Valid && node.ExitIP.String != "" {
			uniqueIPs[node.ExitIP.String] = true
		}
	}
	uniqueAlive := len(uniqueIPs)

	checked := make([]testNodeRow, 0, len(results))
	for _, row := range results {
		if row.Probed {
			checked = append(checked, row)
		}
	}
	targetResponseOnly := len(checked) > 0
	var deprecated bool
	if targetResponseOnly {
		for _, row := range checked {
			if row.Alive {
				targetResponseOnly = false
				break
			}
		}
	}
	if targetResponseOnly {
		for _, row := range checked {
			hasEvidence := (row.SteamStatus != nil) || (row.ExitIP != nil && *row.ExitIP != "")
			if !hasEvidence {
				targetResponseOnly = false
				break
			}
		}
	}
	if targetResponseOnly {
		deprecated = subDeprecated
	} else {
		dep, err := s.evaluateSubscriptionDeprecation(ctx, subID, aliveCount, len(nodeNames))
		if err != nil {
			return 0, 0, false, err
		}
		deprecated = dep
	}
	if !targetResponseOnly {
		if err := s.mergeLastStats(ctx, subID, map[string]any{
			"alive": aliveCount, "total": len(nodeNames),
		}); err != nil {
			return aliveCount, uniqueAlive, deprecated, err
		}
	}
	errText := ""
	if aliveCount == 0 && !targetResponseOnly {
		errText = "全部节点不可用"
	}
	s.recordEventWithError(ctx, "clash", "node-test", fmt.Sprintf("clash:%d", subID), errText)
	if deprecated && !targetResponseOnly {
		s.maybeRevertToDirect(ctx)
	}
	return aliveCount, uniqueAlive, deprecated, nil
}

func anyProbed(results []testNodeRow) bool {
	for _, row := range results {
		if row.Probed {
			return true
		}
	}
	return false
}

func probedNames(results []testNodeRow) map[string]bool {
	out := map[string]bool{}
	for _, row := range results {
		if row.Probed {
			out[row.Name] = true
		}
	}
	return out
}

// readSubscriptionStats 订阅行投影（last_stats 原文 / deprecated / 资源封锁判定）。
func (s *Service) readSubscriptionStats(ctx context.Context, subID int64) (statsJSON string, deprecated bool, blocked bool) {
	var raw sql.NullString
	var dep bool
	err := s.DB.QueryRowContext(ctx,
		`SELECT CAST(last_stats AS TEXT), COALESCE(deprecated,0) FROM proxy_subscriptions WHERE id=?`,
		subID).Scan(&raw, &dep)
	if err != nil {
		return "", false, false
	}
	statsJSON = raw.String
	deprecated = dep
	blocked = proxypool.SubscriptionBlockReason(statsJSON, s.now()) != ""
	return statsJSON, deprecated, blocked
}

// evaluateSubscriptionDeprecation 资源失效或没有可用节点时停用，恢复后自动解除。
func (s *Service) evaluateSubscriptionDeprecation(ctx context.Context, subID int64, aliveCount, total int) (bool, error) {
	if total <= 0 {
		return false, nil
	}
	statsJSON, deprecated, _ := s.readSubscriptionStats(ctx, subID)
	reason := proxypool.SubscriptionBlockReason(statsJSON, s.now())
	if reason == "" && aliveCount <= 0 {
		reason = fmt.Sprintf("没有可用节点（可用 0/%d）", total)
	}
	if reason != "" {
		if !deprecated {
			now := pyTextTime(s.now().In(sched.DefaultLoc))
			if err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
				_, err := conn.ExecContext(ctx,
					`UPDATE proxy_subscriptions SET deprecated=1, deprecated_at=?, deprecated_reason=? WHERE id=?`,
					now, reason, subID)
				return err
			}); err != nil {
				return false, err
			}
			s.logf("订阅 %d 暂不可用：%s │ 可用=%d 总数=%d", subID, reason, aliveCount, total)
		}
		return true, nil
	}
	if deprecated {
		if err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			_, err := conn.ExecContext(ctx,
				`UPDATE proxy_subscriptions SET deprecated=0, deprecated_at=NULL, deprecated_reason=NULL WHERE id=?`,
				subID)
			return err
		}); err != nil {
			return false, err
		}
		s.logf("订阅 %d 恢复可用（%d/%d），已解除废弃标记", subID, aliveCount, total)
	}
	return false, nil
}

// mergeLastStats 订阅 last_stats 局部合并（不动其他键）；同值重算不落写。
func (s *Service) mergeLastStats(ctx context.Context, subID int64, fields map[string]any) error {
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var raw sql.NullString
		if err := conn.QueryRowContext(ctx,
			`SELECT CAST(last_stats AS TEXT) FROM proxy_subscriptions WHERE id=?`, subID).Scan(&raw); err != nil {
			return err
		}
		stats := map[string]any{}
		_ = json.Unmarshal([]byte(raw.String), &stats)
		for key, value := range fields {
			if key == "traffic" {
				if text, _ := value.(string); text == "" {
					continue // 流量是单调量：本次没取到保留既有值
				}
			}
			stats[key] = value
		}
		updated, err := json.Marshal(stats)
		if err != nil {
			return err
		}
		if raw.Valid && string(updated) == raw.String {
			return nil
		}
		_, err = conn.ExecContext(ctx,
			`UPDATE proxy_subscriptions SET last_stats=? WHERE id=?`, string(updated), subID)
		return err
	})
}

// recordEventWithError 走线日志（检测链版本带 error 摘要）。
func (s *Service) recordEventWithError(ctx context.Context, kind, target, proxyLabel, errText string) {
	ts := pyTextTime(s.now().In(sched.DefaultLoc))
	err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx,
			`INSERT INTO proxy_events(ts,kind,target,proxy_label,error) VALUES (?,?,?,?,?)`,
			ts, kind, target, proxyLabel, nullStringArg(errText))
		return err
	})
	if err != nil {
		s.logf("记录走线日志失败：%v", err)
	}
}

func nullStringArg(v string) any {
	if v == "" {
		return nil
	}
	return v
}

// maybeRevertToDirect 无可用出口时回退默认直连：只回退依赖代理池的策略
// （proxy_first/proxy_only），用户显式选过的策略不动。
func (s *Service) maybeRevertToDirect(ctx context.Context) bool {
	if s.kvBool(ctx, "proxy.strategy_explicit", false) {
		return false
	}
	strategy := s.kvString(ctx, "proxy.strategy", "direct_only")
	if strategy != "proxy_first" && strategy != "proxy_only" {
		return false
	}
	var liveSubs, enabledPool int
	if err := s.DB.QueryRowContext(ctx,
		`SELECT (SELECT count(*) FROM proxy_subscriptions WHERE COALESCE(deprecated,0)=0),
		 (SELECT count(*) FROM proxies WHERE COALESCE(enabled,0)<>0)`).Scan(&liveSubs, &enabledPool); err != nil {
		return false
	}
	if liveSubs > 0 || enabledPool > 0 {
		return false
	}
	if err := s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, "proxy.strategy", "direct_only")
	}); err != nil {
		return false
	}
	s.logf("无可用代理出口，策略已回退直连")
	return true
}

func (s *Service) kvString(ctx context.Context, key, fallback string) string {
	var raw sql.NullString
	if err := s.DB.QueryRowContext(ctx,
		`SELECT CAST(json_extract(value_json,'$') AS TEXT) FROM app_settings WHERE key=?`, key).Scan(&raw); err != nil || !raw.Valid {
		return fallback
	}
	if strings.TrimSpace(raw.String) == "" {
		return fallback
	}
	return raw.String
}

func (s *Service) kvBool(ctx context.Context, key string, fallback bool) bool {
	var raw sql.NullString
	if err := s.DB.QueryRowContext(ctx,
		`SELECT CAST(json_extract(value_json,'$') AS TEXT) FROM app_settings WHERE key=?`, key).Scan(&raw); err != nil || !raw.Valid {
		return fallback
	}
	var v bool
	if err := json.Unmarshal([]byte(raw.String), &v); err != nil {
		return fallback
	}
	return v
}

// ─── 冷却跳过行的结果载荷（读权威账本状态，标注 cooling）──────

func cooldownNodesPayload(nodeNames []string, probed map[string]bool, registry map[string]*ledgerNode) []testNodeRow {
	payload := []testNodeRow{}
	for _, name := range nodeNames {
		if probed[name] {
			continue
		}
		node := registry[name]
		alive := ledgerAliveByLedger(node)
		row := testNodeRow{Name: name, Alive: alive, SteamOK: alive, Probed: false, Cooling: true}
		if node != nil && node.ExitIP.Valid {
			row.ExitIP = strPtr(node.ExitIP.String)
		}
		payload = append(payload, row)
	}
	return payload
}

// ─── selector 自愈（当前选中已死 → 切最快健康节点）────────────

func (s *Service) selfHealSelector(ctx context.Context, ctl clashTestController, selector string, results []testNodeRow, registry map[string]*ledgerNode, allProxies map[string]any, now time.Time) {
	healthy := make([]testNodeRow, 0, len(results))
	for _, row := range results {
		if row.Probed && row.Alive && row.MS != nil {
			healthy = append(healthy, row)
		}
	}
	sort.SliceStable(healthy, func(i, j int) bool { return *healthy[i].MS < *healthy[j].MS })

	current := ""
	if info, ok := allProxiesInfo(allProxies, selector); ok {
		current = anyStr(info["now"])
	}
	currentAlive := false
	if current != "" {
		for _, row := range results {
			if row.Name == current && row.Alive {
				currentAlive = true
				break
			}
		}
		if !currentAlive {
			currentAlive = ledgerAliveByLedger(registry[current]) && !ledgerInCooldown(registry[current], now)
		}
	}
	if len(healthy) == 0 || currentAlive {
		return
	}
	target := healthy[0].Name
	if err := ctl.PutProxy(selector, target); err != nil {
		s.logf("切换健康节点失败，不阻塞检测：%v", err)
		return
	}
	s.logf("选中节点 %s 已死，已切换到最快健康节点 %s │ 原节点=%s 新节点=%s", current, target, current, target)
}

// ─── 路由组选择（selector 自愈用；探测 lane 组是检测专用通道）──

func allProxiesInfo(allProxies map[string]any, name string) (map[string]any, bool) {
	proxies, _ := allProxies["proxies"].(map[string]any)
	if proxies == nil {
		return nil, false
	}
	info, _ := proxies[name].(map[string]any)
	return info, info != nil
}

func pickRouteSelector(allProxies map[string]any) string {
	proxies, _ := allProxies["proxies"].(map[string]any)
	type groupInfo struct{ name, typ string }
	var groups []groupInfo
	for name, value := range proxies {
		info, ok := value.(map[string]any)
		if !ok {
			continue
		}
		typ := strings.ToLower(anyStr(info["type"]))
		if typ != "selector" && typ != "fallback" && typ != "urltest" {
			continue
		}
		if strings.HasPrefix(name, clashcfg.ProbeLaneGroupPrefix) {
			continue
		}
		groups = append(groups, groupInfo{name: name, typ: typ})
	}
	score := func(name string) int {
		for i, kw := range []string{"GLOBAL", "PROXY", "节点", "选择", "手动"} {
			if strings.Contains(name, kw) {
				return i
			}
		}
		return 99
	}
	sort.SliceStable(groups, func(i, j int) bool { return score(groups[i].name) < score(groups[j].name) })
	if len(groups) == 0 || groups[0].name == "" {
		return "GLOBAL"
	}
	return groups[0].name
}

// ─── 控制器客户端（PUT/GET 组选择；不走系统代理）──────────────

// clashTestController 检测链对内核 RESTful 控制器的操作面（测试注入桩）。
type clashTestController interface {
	AllProxies() (map[string]any, error)
	PutProxy(group, name string) error
	ProxyNow(group string) (string, error)
	CurrentMode() (string, error)
	PatchMode(mode string) error
}

func (s *Service) clashTestCtl(baseURL, secret string) clashTestController {
	if s.ControllerFactory != nil {
		return s.ControllerFactory(baseURL, secret)
	}
	return &httpClashCtl{
		BaseURL: baseURL, Secret: secret,
		HTTP: &http.Client{Timeout: testCtlTimeout, Transport: &http.Transport{Proxy: nil}},
	}
}

// httpClashCtl 控制器 REST 客户端（Bearer 密钥鉴权，Transport 不读环境代理）。
type httpClashCtl struct {
	BaseURL string
	Secret  string
	HTTP    *http.Client
}

func (c *httpClashCtl) do(method, path, body string) (int, []byte, error) {
	req, err := http.NewRequest(method, strings.TrimRight(c.BaseURL, "/")+path, strings.NewReader(body))
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.Secret != "" {
		req.Header.Set("Authorization", "Bearer "+c.Secret)
	}
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw := make([]byte, 0, 4096)
	buf := make([]byte, 4096)
	for {
		n, rerr := resp.Body.Read(buf)
		raw = append(raw, buf[:n]...)
		if rerr != nil {
			break
		}
		if len(raw) > 8<<20 {
			break
		}
	}
	return resp.StatusCode, raw, nil
}

func (c *httpClashCtl) AllProxies() (map[string]any, error) {
	status, raw, err := c.do(http.MethodGet, "/proxies", "")
	if err != nil {
		return nil, fmt.Errorf("控制器不可达：%w", err)
	}
	if status != 200 {
		return nil, fmt.Errorf("控制器 /proxies 不可用（HTTP %d）", status)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, fmt.Errorf("控制器 /proxies 返回解析失败：%w", err)
	}
	return body, nil
}

func groupPath(group string) string {
	return "/proxies/" + url.PathEscape(group)
}

func (c *httpClashCtl) PutProxy(group, name string) error {
	payload, _ := json.Marshal(map[string]string{"name": name})
	status, _, err := c.do(http.MethodPut, groupPath(group), string(payload))
	if err != nil {
		return fmt.Errorf("控制器不可达：%w", err)
	}
	if status >= 400 {
		return fmt.Errorf("组选择被拒（HTTP %d）", status)
	}
	return nil
}

func (c *httpClashCtl) ProxyNow(group string) (string, error) {
	status, raw, err := c.do(http.MethodGet, groupPath(group), "")
	if err != nil {
		return "", fmt.Errorf("控制器不可达：%w", err)
	}
	if status != 200 {
		return "", fmt.Errorf("组状态不可用（HTTP %d）", status)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		return "", fmt.Errorf("组状态解析失败：%w", err)
	}
	return anyStr(body["now"]), nil
}

func (c *httpClashCtl) CurrentMode() (string, error) {
	status, raw, err := c.do(http.MethodGet, "/configs", "")
	if err != nil {
		return "", fmt.Errorf("控制器不可达：%w", err)
	}
	if status != 200 {
		return "", fmt.Errorf("配置不可用（HTTP %d）", status)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		return "", fmt.Errorf("配置解析失败：%w", err)
	}
	return anyStr(body["mode"]), nil
}

func (c *httpClashCtl) PatchMode(mode string) error {
	payload, _ := json.Marshal(map[string]string{"mode": mode})
	status, _, err := c.do(http.MethodPatch, "/configs", string(payload))
	if err != nil {
		return fmt.Errorf("控制器不可达：%w", err)
	}
	if status >= 400 {
		return fmt.Errorf("模式切换被拒（HTTP %d）", status)
	}
	return nil
}

// controllerSecretOf 运行配置里的控制器密钥（启动文本与命令行同密钥）。
func controllerSecretOf(configText string) string {
	if m := secretLineRe.FindStringSubmatch(configText); m != nil {
		return m[1]
	}
	return ""
}

// ─── 生产探针（经本地代理端口；测试桩覆盖注入面）──────────────

func dialViaPort(port int, timeout time.Duration) *http.Client {
	proxyURL, err := url.Parse(fmt.Sprintf("http://127.0.0.1:%d", port))
	if err != nil {
		proxyURL = nil
	}
	return &http.Client{
		Timeout:   timeout,
		Transport: &http.Transport{Proxy: http.ProxyURL(proxyURL)},
	}
}

// steamProbeDefault 生产业务探针：GET StoreBrowse 最小负载；200 且 body 是
// JSON 对象才算活（HTML 拦截页 / 空体 / 非 JSON 一律判否）。
func (s *Service) steamProbeDefault(ctx context.Context, port int) (bool, int) {
	client := dialViaPort(port, testSteamTimeout)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, proxypool.BusinessProbeURL(220, ""), nil)
	if err != nil {
		return false, 0
	}
	resp, err := client.Do(req)
	if err != nil {
		return false, 0
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return false, resp.StatusCode
	}
	body := make([]byte, 0, 4096)
	buf := make([]byte, 4096)
	for {
		n, rerr := resp.Body.Read(buf)
		body = append(body, buf[:n]...)
		if rerr != nil || len(body) > 1<<20 {
			break
		}
	}
	var payload map[string]any
	return json.Unmarshal(body, &payload) == nil && payload != nil, resp.StatusCode
}

// exitIPProbeDefault 出口 IP 探测：ip-api → ip.sb 依次尝试（仅用于去重）。
func (s *Service) exitIPProbeDefault(ctx context.Context, port int) string {
	client := dialViaPort(port, testExitIPTimeout)
	for _, target := range []string{
		"http://ip-api.com/json/?fields=query",
		"https://api.ip.sb/ip",
	} {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
		if err != nil {
			continue
		}
		resp, err := client.Do(req)
		if err != nil {
			continue
		}
		text := make([]byte, 0, 256)
		buf := make([]byte, 1024)
		for {
			n, rerr := resp.Body.Read(buf)
			text = append(text, buf[:n]...)
			if rerr != nil || len(text) > 4096 {
				break
			}
		}
		_ = resp.Body.Close()
		if resp.StatusCode != 200 {
			continue
		}
		trimmed := strings.Trim(strings.TrimSpace(string(text)), `"`)
		if strings.HasPrefix(strings.TrimSpace(string(text)), "{") {
			var payload map[string]any
			if json.Unmarshal(text, &payload) == nil {
				if query := anyStr(payload["query"]); query != "" {
					return query
				}
			}
			continue
		}
		if trimmed != "" {
			return trimmed
		}
	}
	return ""
}

// ─── 首检（启动/切换成功后的后台检测，复用窗口内跳过）─────────

// ClashNodesFresh 该订阅的节点账本是否在复用窗口内有过检测（last_l2_at 最大值；
// 跨重启有效，任何通道的业务证据都推进它）。账本为空或结论过期返回 False。
func (s *Service) ClashNodesFresh(ctx context.Context, subID int64) bool {
	var latest sql.NullString
	if err := s.DB.QueryRowContext(ctx,
		`SELECT CAST(MAX(last_l2_at) AS TEXT) FROM proxy_nodes WHERE subscription_id=?`,
		subID).Scan(&latest); err != nil || !latest.Valid || latest.String == "" {
		return false
	}
	last, ok := parsePyTime(latest.String)
	if !ok {
		return false
	}
	elapsed := s.now().In(sched.DefaultLoc).Sub(last)
	return elapsed < FirstCheckReuseMinutes*time.Minute
}

// SpawnFirstCheck 首检：订阅内容变化（启动/切换/重拉生效）后检测一次写账本
// （订阅废弃判定/selector 自愈都在里面）；缺省全量探测。账本在复用窗口内已有
// 全量结论时跳过（来回切换订阅不重复探测）。失败不影响内核已生效的事实。
func (s *Service) SpawnFirstCheck(subID int64, probeAll bool) {
	go func() {
		ctx := context.Background()
		if s.ClashNodesFresh(ctx, subID) {
			s.logf("Clash 订阅 %d 的检测结论还在 %d 分钟有效期内，沿用上次结果 │ 订阅=%d 复用窗口分钟=%d",
				subID, FirstCheckReuseMinutes, subID, FirstCheckReuseMinutes)
			return
		}
		result, err := s.testClashNodes(ctx, &subID, probeAll)
		if err != nil {
			s.logf("Clash 订阅 %d 首检的节点检测失败，可稍后手动检测 │ 订阅=%d 原因=%v", subID, subID, err)
			return
		}
		s.logf("Clash 订阅 %d 首检完成：共 %d 个节点，%d 个可用 │ 订阅=%d 节点总数=%d 可用节点=%d",
			subID, result.Total, result.Alive, subID, result.Total, result.Alive)
		s.autoPromoteAfterCheck(ctx, subID, result.Alive)
	}()
}

// autoPromoteAfterCheck 首检健康（可用节点 ≥ 1）即转正进池——与同步侧的自动
// 准入同一把尺子（对齐 router._auto_promote_after_check）。快照尚未落库时晋升
// 会失败：账本可用数已写好，下一拍同步落快照时自动转正，这里只记日志不重试。
func (s *Service) autoPromoteAfterCheck(ctx context.Context, subID int64, alive int) {
	if alive < 1 {
		return
	}
	var promotion proxypool.PromotionResult
	err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		var inner error
		promotion, inner = proxypool.PromoteToActive(ctx, conn, s.DataDir, subID, s.now())
		return inner
	})
	if err != nil {
		s.logf("订阅 %d 首检健康（可用 %d 个节点）但暂时无法转正，等同步落快照后自动重试 │ 订阅=%d 原因=%v",
			subID, alive, subID, err)
		return
	}
	if promotion.Promoted {
		s.logf("订阅 %d 首检健康（可用 %d 个节点），已自动转正进池 │ 订阅=%d 可用节点=%d",
			subID, alive, subID, alive)
	}
}

// ─── 小工具 ──────────────────────────────────────────────────

func intPtr(v int) *int { return &v }

// intPtrNonZero 0 = 无 HTTP 响应 → null（对齐 Python steamStatus=None）。
func intPtrNonZero(v int) *int {
	if v == 0 {
		return nil
	}
	return &v
}

func minInt(a, b int) int {
	if a < b {
		return a
	}
	return b
}

func pyIntOr(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case int64:
		return int(n)
	case string:
		var parsed int
		if _, err := fmt.Sscanf(strings.TrimSpace(n), "%d", &parsed); err == nil {
			return parsed
		}
	}
	return 0
}

// anyStr Python str() 投影的窄版：检测链只消费字符串字段。
func anyStr(v any) string {
	switch text := v.(type) {
	case string:
		return text
	case nil:
		return ""
	default:
		raw, err := json.Marshal(text)
		if err != nil {
			return fmt.Sprintf("%v", text)
		}
		return strings.Trim(string(raw), `"`)
	}
}
