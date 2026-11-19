package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"
)

type ProxyNode struct {
	ID                  int64
	NodeID              string
	RuntimeName         string
	ProxyType           string
	Server              sql.NullString
	NormalizedConfig    string
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

type StateChange struct {
	NodeID string
	From   string
	To     string
}

type ApplyResult struct {
	CreatedNodes     []string
	RefreshedNodes   []string
	RemovedSources   []string
	StateChanges     []StateChange
	SkippedStale     bool
	SkippedIdentical bool
}

func pyTimeText(t time.Time) string {
	return t.Format("2006-01-02 15:04:05.000000")
}

// LockSubscriptionMutation 在事务内先占写锁边界（读快照→写 Registry 调用方须先调）。
func LockSubscriptionMutation(ctx context.Context, conn *sql.Conn, subscriptionID int64) error {
	_, err := conn.ExecContext(ctx,
		`UPDATE proxy_nodes SET last_source_seen = last_source_seen WHERE subscription_id = ?`,
		subscriptionID)
	return err
}

var nameBaseSuffix = regexp.MustCompile(`#\d+\s*$`)

func nameBase(name string) string {
	return pyTrimSpace(nameBaseSuffix.ReplaceAllString(name, ""))
}

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

func ApplySnapshot(ctx context.Context, conn *sql.Conn, subscriptionID int64, snap *Snapshot, now time.Time, tag string) (ApplyResult, error) {
	if err := LockSubscriptionMutation(ctx, conn, subscriptionID); err != nil {
		return ApplyResult{}, err
	}
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
		return 0, false
	}
	return 0, false
}

func parsePyNumber(s string) (float64, error) {
	return strconv.ParseFloat(s, 64)
}

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
	slices.Sort(fps)

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
		slices.Sort(removed)
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

type inheritIndexes struct {
	byBase     map[string]string
	byEndpoint map[string]string
}

func loadInheritIndexes(ctx context.Context, conn *sql.Conn) (*inheritIndexes, error) {
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

func SettleNodesAfterSourceChange(ctx context.Context, conn *sql.Conn, nodeIDs map[string]bool, now time.Time) ([]StateChange, error) {
	var changes []StateChange
	ids := make([]string, 0, len(nodeIDs))
	for id := range nodeIDs {
		ids = append(ids, id)
	}
	slices.Sort(ids)
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

func pyNullableText(v any) any {
	if v == nil {
		return nil
	}
	return pyStr(v)
}

func pyDumpJSONText(node map[string]any) string {
	keys := make([]string, 0, len(node))
	for k := range node {
		keys = append(keys, k)
	}
	slices.Sort(keys)
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
		slices.Sort(keys)
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
