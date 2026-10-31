package proxypool

// ProxyNode 登记链（Python registry.py 对齐）：快照 → Registry 事实源对齐。
// 硬规则：①来源按本次快照指纹集合整体替换；②runtime_name 首次铸造后不重铸；
// ③仅当来源全部流失才向状态机要一次判定（probe_ok=None）。
// 并发边界：串行化由写闸（单 Actor + 跨进程锁）提供；事务归调用方，本文件只执行
// SQL 不提交。

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

// ProxyNode 注册表行。
type ProxyNode struct {
	ID                  int64
	NodeID              string
	RuntimeName         string
	ProxyType           string
	Server              sql.NullString
	NormalizedConfig    string // 库内 JSON 原文；空串 = NULL
	State               string
	FirstSeen           sql.NullString
	SubscriptionID      sql.NullInt64
	OriginalName        sql.NullString
	LastSeen            sql.NullString
	LastSourceSeen      sql.NullString
	LastL0At            sql.NullString
	L0DelayMS           sql.NullInt64
	LastL1At            sql.NullString
	LastL2At            sql.NullString
	ExitIP              sql.NullString
	ConsecutiveFailures int64
	CooldownUntil       sql.NullString
}

// StateChange 一次真实发生的状态迁移（节点、原状态、新状态）。
type StateChange struct {
	NodeID string
	From   string
	To     string
}

// ApplyResult 一次登记的账目摘要（供日志与测试断言，不参与状态决策）。
type ApplyResult struct {
	CreatedNodes     []string
	RefreshedNodes   []string
	RemovedSources   []string
	StateChanges     []StateChange
	SkippedStale     bool
	SkippedIdentical bool
}

// pyTimeText 库内时间文本：naive 北京挂钟、空格分隔、恒 6 位微秒
// （SQLAlchemy SQLite DATETIME 存储形态）。
func pyTimeText(t time.Time) string {
	return t.Format("2006-01-02 15:04:05.000000")
}

// LockSubscriptionMutation 取该订阅 Registry 变更的串行化边界，不改任何数据。
// 同值 UPDATE 只为在本事务里开写事务；Go 侧写闸已保证串行，保留为语义对齐
// （要求「读快照 → 写 Registry」原子的调用方必须在读之前先调它）。
func LockSubscriptionMutation(ctx context.Context, conn *sql.Conn, subscriptionID int64) error {
	_, err := conn.ExecContext(ctx,
		`UPDATE proxy_nodes SET last_source_seen = last_source_seen WHERE subscription_id = ?`,
		subscriptionID)
	return err
}

var nameBaseSuffix = regexp.MustCompile(`#\d+\s*$`)

// nameBase 节点名主干：去掉机场内容版本后缀（…#23）与首尾空白。
func nameBase(name string) string {
	return pyTrimSpace(nameBaseSuffix.ReplaceAllString(name, ""))
}

// desiredNodes 快照 → 指纹表（信息占位节点不进 Registry；同指纹首见者胜）。
func desiredNodes(nodes []map[string]any, tag string) (map[string]map[string]any, error) {
	desired := map[string]map[string]any{}
	for _, node := range nodes {
		if IsInfoPlaceholder(pyStr(node["name"])) {
			continue
		}
		fp, err := NodeFingerprint(node, tag)
		if err != nil {
			return nil, err
		}
		if _, ok := desired[fp]; !ok {
			desired[fp] = node
		}
	}
	return desired, nil
}

// ApplySnapshot 把订阅的来源证据整体对齐到快照的事实。
// tag 是该订阅的 SubscriptionTag(url)，进节点指纹；空串仅测试造数用。
func ApplySnapshot(ctx context.Context, conn *sql.Conn, subscriptionID int64, snap *Snapshot, now time.Time, tag string) (ApplyResult, error) {
	if err := LockSubscriptionMutation(ctx, conn, subscriptionID); err != nil {
		return ApplyResult{}, err
	}
	// 顺序语义：最新成功快照胜，判据与 latest_snapshot 同源（id 最大者）。
	// 不能用 fetched_at：慢请求「先抓取、后落库」会让两套判据互相矛盾。
	var incomingID, newestID sql.NullInt64
	err := conn.QueryRowContext(ctx,
		`SELECT MAX(id) FROM subscription_snapshots WHERE subscription_id = ? AND status = 'OK' AND sha256 = ?`,
		subscriptionID, snap.Sha256).Scan(&incomingID)
	if err != nil {
		return ApplyResult{}, err
	}
	if incomingID.Valid {
		err := conn.QueryRowContext(ctx,
			`SELECT MAX(id) FROM subscription_snapshots WHERE subscription_id = ? AND status = 'OK'`,
			subscriptionID).Scan(&newestID)
		if err != nil {
			return ApplyResult{}, err
		}
		if newestID.Valid && incomingID.Int64 < newestID.Int64 {
			return ApplyResult{SkippedStale: true}, nil
		}
		if newestID.Valid && incomingID.Int64 == newestID.Int64 {
			// 同内容重放：节点集合与指纹集一致且名称/配置逐字段相等 → 跳过。
			same, err := registryMatchesSnapshot(ctx, conn, subscriptionID, snap, tag)
			if err != nil {
				return ApplyResult{}, err
			}
			if same {
				return ApplyResult{SkippedIdentical: true}, nil
			}
		}
	}
	return applyNodes(ctx, conn, subscriptionID, snap.Nodes, now, tag, false)
}

// registryMatchesSnapshot 同内容重放判定：本订阅名下节点集合与快照指纹集
// 一致、且每行名称与归一化配置逐字段相等。
func registryMatchesSnapshot(ctx context.Context, conn *sql.Conn, subscriptionID int64, snap *Snapshot, tag string) (bool, error) {
	desired, err := desiredNodes(snap.Nodes, tag)
	if err != nil {
		return false, err
	}
	rows, err := conn.QueryContext(ctx,
		`SELECT node_id, original_name, CAST(normalized_config AS TEXT) FROM proxy_nodes WHERE subscription_id = ?`,
		subscriptionID)
	if err != nil {
		return false, err
	}
	defer rows.Close()
	seen := map[string]bool{}
	for rows.Next() {
		var nodeID string
		var name, cfg sql.NullString
		if err := rows.Scan(&nodeID, &name, &cfg); err != nil {
			return false, err
		}
		seen[nodeID] = true
		want, ok := desired[nodeID]
		if !ok {
			return false, nil
		}
		if !name.Valid || name.String != pyStr(want["name"]) {
			return false, nil
		}
		var stored map[string]any
		if cfg.Valid && cfg.String != "" {
			if err := json.Unmarshal([]byte(cfg.String), &stored); err != nil {
				return false, nil
			}
		}
		if !pyConfigEqual(stored, want) {
			return false, nil
		}
	}
	if err := rows.Err(); err != nil {
		return false, err
	}
	return len(seen) == len(desired), nil
}

// pyConfigEqual Python 字典相等语义：数值按值比较（443 与 443.0 相等）。
func pyConfigEqual(a, b map[string]any) bool {
	if len(a) != len(b) {
		return false
	}
	for k, va := range a {
		vb, ok := b[k]
		if !ok || !pyValueEqual(va, vb) {
			return false
		}
	}
	return true
}

func pyValueEqual(a, b any) bool {
	if a == nil || b == nil {
		return a == nil && b == nil
	}
	na, aIsNum := pyNumberValue(a)
	nb, bIsNum := pyNumberValue(b)
	if aIsNum && bIsNum {
		return na == nb
	}
	if aIsNum != bIsNum {
		return false
	}
	switch av := a.(type) {
	case bool:
		bv, ok := b.(bool)
		return ok && av == bv
	case string:
		bv, ok := b.(string)
		return ok && av == bv
	case []any:
		bv, ok := b.([]any)
		if !ok || len(av) != len(bv) {
			return false
		}
		for i := range av {
			if !pyValueEqual(av[i], bv[i]) {
				return false
			}
		}
		return true
	case map[string]any:
		bv, ok := b.(map[string]any)
		return ok && pyConfigEqual(av, bv)
	}
	return false
}

// pyNumberValue 数值统一到 float64（Python == 对 int/float 混比较按值）。
func pyNumberValue(v any) (float64, bool) {
	switch x := v.(type) {
	case int:
		return float64(x), true
	case int64:
		return float64(x), true
	case float64:
		return x, true
	case json.Number:
		f, err := parsePyNumber(string(x))
		return f, err == nil
	case string:
		// 库内 JSON 解析出的数字形态已在 Unmarshal 后呈 json.Number，
		// 字符串不是数字
		return 0, false
	}
	return 0, false
}

func parsePyNumber(s string) (float64, error) {
	return strconv.ParseFloat(s, 64)
}

// applyNodes 登记节点身份；restore 只补缺口，不刷新健康或替换来源集合。
func applyNodes(ctx context.Context, conn *sql.Conn, subscriptionID int64, nodes []map[string]any, now time.Time, tag string, restoreOnly bool) (ApplyResult, error) {
	desired, err := desiredNodes(nodes, tag)
	if err != nil {
		return ApplyResult{}, err
	}
	result := ApplyResult{}
	touched := map[string]bool{}

	fps := make([]string, 0, len(desired))
	for fp := range desired {
		fps = append(fps, fp)
	}
	sort.Strings(fps)

	byID, existing, err := loadSubscriptionRows(ctx, conn, subscriptionID, fps)
	if err != nil {
		return ApplyResult{}, err
	}
	taken, err := mintedNames(ctx, conn, subscriptionID)
	if err != nil {
		return ApplyResult{}, err
	}
	nowText := pyTimeText(now)
	var inherit *inheritIndexes

	for _, fp := range fps {
		node := desired[fp]
		originalName := pyStr(node["name"])
		row, ok := byID[fp]
		if ok {
			if restoreOnly {
				continue
			}
			if row.SubscriptionID.Valid && row.SubscriptionID.Int64 != subscriptionID {
				return result, fmt.Errorf("节点身份已归属其他订阅，未覆盖来源")
			}
			if _, err := conn.ExecContext(ctx,
				`UPDATE proxy_nodes SET subscription_id = ?, original_name = ?, normalized_config = ?, last_source_seen = ? WHERE id = ?`,
				subscriptionID, originalName, pyDumpJSONText(node), nowText, row.ID); err != nil {
				return result, err
			}
			result.RefreshedNodes = append(result.RefreshedNodes, fp)
			touched[fp] = true
			continue
		}
		runtimeName := MakeRuntimeName(subscriptionID, originalName, taken)
		taken[runtimeName] = true
		if inherit == nil {
			if inherit, err = loadInheritIndexes(ctx, conn); err != nil {
				return result, err
			}
		}
		sourceSeen := sql.NullString{}
		if !restoreOnly {
			sourceSeen = sql.NullString{String: nowText, Valid: true}
		}
		server := pyNullableText(node["server"])
		var exitIP any
		if ip := inherit.inheritableExitIP(node); ip != "" {
			exitIP = ip
		}
		if _, err := conn.ExecContext(ctx, `INSERT INTO proxy_nodes
			(node_id, runtime_name, proxy_type, server, normalized_config, state,
			 first_seen, subscription_id, original_name, last_seen, last_source_seen,
			 consecutive_failures, exit_ip)
			VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)`,
			fp, runtimeName, pyStr(node["type"]), server, pyDumpJSONText(node), NodeNew,
			nowText, subscriptionID, originalName, nowText, sourceSeen, exitIP); err != nil {
			return result, err
		}
		result.CreatedNodes = append(result.CreatedNodes, fp)
		touched[fp] = true
	}

	if !restoreOnly {
		var removed []string
		for fp := range existing {
			if _, still := desired[fp]; !still {
				removed = append(removed, fp)
			}
		}
		sort.Strings(removed)
		for _, fp := range removed {
			if _, err := conn.ExecContext(ctx,
				`UPDATE proxy_nodes SET subscription_id = NULL WHERE id = ?`, byID[fp].ID); err != nil {
				return result, err
			}
			touched[fp] = true
		}
		result.RemovedSources = removed
		changes, err := SettleNodesAfterSourceChange(ctx, conn, touched, now)
		if err != nil {
			return result, err
		}
		result.StateChanges = changes
	}
	return result, nil
}

// loadSubscriptionRows 一次取出本订阅全部行与命中指纹的行
// （对齐 Python 的 OR 查询：本订阅行参与移除判定，命中指纹行参与刷新判定）。
func loadSubscriptionRows(ctx context.Context, conn *sql.Conn, subscriptionID int64, fps []string) (byID map[string]*ProxyNode, existing map[string]bool, err error) {
	byID = map[string]*ProxyNode{}
	existing = map[string]bool{}
	query := `SELECT id, node_id, subscription_id FROM proxy_nodes WHERE subscription_id = ?`
	args := []any{subscriptionID}
	if len(fps) > 0 {
		query += ` OR node_id IN (` + strings.TrimSuffix(strings.Repeat("?,", len(fps)), ",") + `)`
		for _, fp := range fps {
			args = append(args, fp)
		}
	}
	rows, err := conn.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var row ProxyNode
		if err := rows.Scan(&row.ID, &row.NodeID, &row.SubscriptionID); err != nil {
			return nil, nil, err
		}
		byID[row.NodeID] = &row
		if row.SubscriptionID.Valid && row.SubscriptionID.Int64 == subscriptionID {
			existing[row.NodeID] = true
		}
	}
	return byID, existing, rows.Err()
}

// mintedNames 本订阅铸过的名字（已不再提供但行仍在的节点同样占用）。
func mintedNames(ctx context.Context, conn *sql.Conn, subscriptionID int64) (map[string]bool, error) {
	rows, err := conn.QueryContext(ctx,
		`SELECT runtime_name FROM proxy_nodes WHERE runtime_name LIKE ?`,
		fmt.Sprintf("%d|%%", subscriptionID))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	taken := map[string]bool{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		taken[name] = true
	}
	return taken, rows.Err()
}

// inheritIndexes (名字主干→出口IP) 与 (端点键→出口IP) 索引：机场改名单/换
// 内容版本号时把已知出口身份带过内容更替，避免每轮更新后裸探一轮。
type inheritIndexes struct {
	byBase     map[string]string
	byEndpoint map[string]string
}

func loadInheritIndexes(ctx context.Context, conn *sql.Conn) (*inheritIndexes, error) {
	// 一次按 last_seen 降序扫描，setdefault 保留首个 = 最近 seen 的化身
	rows, err := conn.QueryContext(ctx,
		`SELECT node_id, proxy_type, server, exit_ip, CAST(normalized_config AS TEXT), CAST(last_seen AS TEXT)
		 FROM proxy_nodes WHERE exit_ip IS NOT NULL ORDER BY last_seen DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	idx := &inheritIndexes{byBase: map[string]string{}, byEndpoint: map[string]string{}}
	for rows.Next() {
		var nodeID, proxyType string
		var server, exitIP, cfg sql.NullString
		if err := rows.Scan(&nodeID, &proxyType, &server, &exitIP, &cfg, new(sql.NullString)); err != nil {
			return nil, err
		}
		var config map[string]any
		if cfg.Valid && cfg.String != "" {
			_ = json.Unmarshal([]byte(cfg.String), &config)
		}
		if base := nameBase(pyStr(config["name"])); base != "" {
			idx.setBase(base, exitIP.String)
		}
		endpointKey := endpointKey(proxyType, server.String, pyStr(config["port"]))
		if endpointKey != "" {
			idx.setEndpoint(endpointKey, exitIP.String)
		}
	}
	return idx, rows.Err()
}

func (x *inheritIndexes) setBase(base, ip string) {
	if _, ok := x.byBase[base]; !ok {
		x.byBase[base] = ip
	}
}

func (x *inheritIndexes) setEndpoint(key, ip string) {
	if _, ok := x.byEndpoint[key]; !ok {
		x.byEndpoint[key] = ip
	}
}

func endpointKey(proxyType, server, port string) string {
	if proxyType == "" || server == "" || port == "" {
		return ""
	}
	return proxyType + "\x00" + server + "\x00" + port
}

// inheritableExitIP 只认最近 seen 的同主干化身；都没有已知出口返回空。
func (x *inheritIndexes) inheritableExitIP(node map[string]any) string {
	base := nameBase(pyStr(node["name"]))
	serverText := pyStr(node["server"])
	endpoint := endpointKey(pyStr(node["type"]), serverText, pyStr(node["port"]))
	if base == "" && endpoint == "" {
		return ""
	}
	if base != "" {
		if ip, ok := x.byBase[base]; ok {
			return ip
		}
	}
	if endpoint != "" {
		if ip, ok := x.byEndpoint[endpoint]; ok {
			return ip
		}
	}
	return ""
}

// SettleNodesAfterSourceChange 来源变动后的节点收尾（唯一实现）：
// 仍归属 → 推进 last_source_seen；归属被解除 → 向状态机要一次判定。
func SettleNodesAfterSourceChange(ctx context.Context, conn *sql.Conn, nodeIDs map[string]bool, now time.Time) ([]StateChange, error) {
	var changes []StateChange
	ids := make([]string, 0, len(nodeIDs))
	for id := range nodeIDs {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	nowText := pyTimeText(now)
	for _, nodeID := range ids {
		var row ProxyNode
		err := conn.QueryRowContext(ctx,
			`SELECT id, state, subscription_id, consecutive_failures FROM proxy_nodes WHERE node_id = ?`,
			nodeID).Scan(&row.ID, &row.State, &row.SubscriptionID, &row.ConsecutiveFailures)
		if err != nil {
			return changes, err
		}
		if row.SubscriptionID.Valid {
			if _, err := conn.ExecContext(ctx,
				`UPDATE proxy_nodes SET last_source_seen = ? WHERE id = ?`, nowText, row.ID); err != nil {
				return changes, err
			}
			// 来源仍在提供却停在 DEAD 的节点放回 STALE 重参选：订阅每轮刷新
			// 都在提供它，状态却永远翻不回来（404 坐实 DEAD 的死锁）。
			if row.State == NodeDead {
				target, err := ApplyTransition(NodeDead, NodeStale)
				if err != nil {
					return changes, err
				}
				if _, err := conn.ExecContext(ctx,
					`UPDATE proxy_nodes SET state = ?, consecutive_failures = 0 WHERE id = ?`,
					target, row.ID); err != nil {
					return changes, err
				}
				changes = append(changes, StateChange{NodeID: nodeID, From: NodeDead, To: target})
			}
			continue
		}
		target, err := EvaluateNodeState(row.State, EvaluateNodeStateInput{
			SourceSeen:          false,
			ProbeOK:             nil,
			ConsecutiveFailures: int(row.ConsecutiveFailures),
		})
		if err != nil {
			return changes, err
		}
		if target != row.State {
			if _, err := conn.ExecContext(ctx,
				`UPDATE proxy_nodes SET state = ? WHERE id = ?`, target, row.ID); err != nil {
				return changes, err
			}
			changes = append(changes, StateChange{NodeID: nodeID, From: row.State, To: target})
		}
	}
	return changes, nil
}

// pyNullableText 标量 → 可空文本（None → NULL；其余 pyStr 形态）。
func pyNullableText(v any) any {
	if v == nil {
		return nil
	}
	return pyStr(v)
}

// pyDumpJSONText normalized_config 落库文本：库内形态经 Python json.dumps
// 默认序列化（带默认分隔符），读取侧一律重新解析后规范化，此格式仅为兼容。
func pyDumpJSONText(node map[string]any) string {
	keys := make([]string, 0, len(node))
	for k := range node {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var sb strings.Builder
	sb.WriteByte('{')
	for i, k := range keys {
		if i > 0 {
			sb.WriteString(", ")
		}
		sb.WriteString(pythonQuoted(k))
		sb.WriteString(": ")
		sb.WriteString(pyValueSQLText(node[k]))
	}
	sb.WriteByte('}')
	return sb.String()
}

func pythonQuoted(s string) string {
	var sb strings.Builder
	pyEncodeString(&sb, s)
	return sb.String()
}

func pyValueSQLText(v any) string {
	switch x := v.(type) {
	case nil:
		return "null"
	case bool:
		if x {
			return "true"
		}
		return "false"
	case string:
		return pythonQuoted(x)
	case json.Number:
		return string(x)
	case int:
		return fmt.Sprintf("%d", x)
	case int64:
		return fmt.Sprintf("%d", x)
	case float64:
		return formatPyFloat(x)
	case []any:
		var sb strings.Builder
		sb.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				sb.WriteString(", ")
			}
			sb.WriteString(pyValueSQLText(item))
		}
		sb.WriteByte(']')
		return sb.String()
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		var sb strings.Builder
		sb.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				sb.WriteString(", ")
			}
			sb.WriteString(pythonQuoted(k))
			sb.WriteString(": ")
			sb.WriteString(pyValueSQLText(x[k]))
		}
		sb.WriteByte('}')
		return sb.String()
	}
	return "null"
}
