package proxypool

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/clashcfg"
)

const PoolFilename = "crawl-pool.yaml"

// poolRequiredConfigKeys 缺任一必选键即不入池，防产生内核拒载的静默差额。
var poolRequiredConfigKeys = []string{"type", "server", "port"}

var builtinProxyNames = map[string]bool{
	"DIRECT": true, "REJECT": true, "REJECT-DROP": true, "GLOBAL": true,
	"COMPATIBLE": true, "PASS": true, "PASS-RULE": true,
}

func PoolPath(dataDir string) string {
	return filepath.Join(dataDir, "proxypool", PoolFilename)
}

func PoolFileNames(dataDir string) []string {
	raw, err := os.ReadFile(PoolPath(dataDir))
	if err != nil {
		return nil
	}
	doc, err := clashcfg.LoadYAML(string(raw))
	if err != nil {
		return nil
	}
	return poolProxyNames(doc)
}

func PoolAuxiliaryNames(dataDir string) []string {
	raw, err := os.ReadFile(PoolPath(dataDir))
	if err != nil {
		return nil
	}
	doc, err := clashcfg.LoadYAML(string(raw))
	if err != nil {
		return nil
	}
	groups, _ := doc["proxy-groups"].([]any)
	var out []string
	for _, g := range groups {
		entry, ok := g.(map[string]any)
		if !ok {
			continue
		}
		out = append(out, pyStr(entry["name"]))
	}
	return out
}

func poolProxyNames(doc map[string]any) []string {
	entries, _ := doc["proxies"].([]any)
	out := make([]string, 0, len(entries))
	for _, e := range entries {
		entry, ok := e.(map[string]any)
		if !ok {
			continue
		}
		out = append(out, pyStr(entry["name"]))
	}
	return out
}

type PoolBuildError struct{ Detail string }

func (e *PoolBuildError) Error() string { return e.Detail }

type PoolBuildResult struct {
	Path           string
	ExpectedCount  int
	WrittenCount   int
	RuntimeNames   []string
	AuxiliaryNames []string
}

type poolNodeRow struct {
	ID             int64
	NodeID         string
	RuntimeName    string
	OriginalName   string
	SubscriptionID sql.NullInt64
	Config         map[string]any
	State          string
	ExitIP         sql.NullString
	L0DelayMS      sql.NullInt64
	LastL1At       sql.NullString
	LastL2At       sql.NullString
}

type poolSubscriptionRow struct {
	ID          int64
	Admission   sql.NullString
	LastStats   sql.NullString
	OriginalURL string
}

func SubscriptionBlockReason(lastStatsJSON string, now time.Time) string {
	if lastStatsJSON == "" {
		return ""
	}
	var stats map[string]any
	if json.Unmarshal([]byte(lastStatsJSON), &stats) != nil {
		return ""
	}
	traffic := pyStr(stats["traffic"])
	values := map[string]int64{}
	for _, part := range strings.Split(traffic, ";") {
		key, value, found := strings.Cut(strings.TrimSpace(part), "=")
		if !found {
			continue
		}
		if n, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64); err == nil {
			values[key] = n
		}
	}
	if expiry := values["expire"]; expiry > 0 && expiry <= now.Unix() {
		return "订阅已到期"
	}
	if total := values["total"]; total > 0 && values["upload"]+values["download"] >= total {
		return "订阅流量已用尽"
	}
	return ""
}

func EligibleNodes(ctx context.Context, conn *sql.Conn, now time.Time) ([]poolNodeRow, error) {
	return eligibleNodes(ctx, conn, now)
}

func eligibleNodes(ctx context.Context, conn queryable, now time.Time) ([]poolNodeRow, error) {
	subs := map[int64]poolSubscriptionRow{}
	rows, err := conn.QueryContext(ctx,
		`SELECT id, admission_status, CAST(last_stats AS TEXT) FROM proxy_subscriptions`)
	if err != nil {
		return nil, err
	}
	for rows.Next() {
		var r poolSubscriptionRow
		if err := rows.Scan(&r.ID, &r.Admission, &r.LastStats); err != nil {
			rows.Close()
			return nil, err
		}
		subs[r.ID] = r
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()

	nodeRows, err := conn.QueryContext(ctx,
		`SELECT id, node_id, runtime_name, original_name, subscription_id, CAST(normalized_config AS TEXT),
		 state, exit_ip, l0_delay_ms, CAST(last_l1_at AS TEXT), CAST(last_l2_at AS TEXT)
		 FROM proxy_nodes
		 WHERE state IN (?, ?, ?) AND subscription_id IS NOT NULL
		 ORDER BY id`, NodeNew, NodeActive, NodeStale)
	if err != nil {
		return nil, err
	}
	defer nodeRows.Close()
	var out []poolNodeRow
	for nodeRows.Next() {
		var node poolNodeRow
		var orig, cfg sql.NullString
		if err := nodeRows.Scan(&node.ID, &node.NodeID, &node.RuntimeName, &orig, &node.SubscriptionID, &cfg,
			&node.State, &node.ExitIP, &node.L0DelayMS, &node.LastL1At, &node.LastL2At); err != nil {
			return nil, err
		}
		sub, ok := subs[node.SubscriptionID.Int64]
		if !ok || !IsAdmitted(sub.Admission) {
			continue
		}
		if SubscriptionBlockReason(sub.LastStats.String, now) != "" {
			continue
		}
		config := map[string]any{}
		if cfg.Valid && cfg.String != "" {
			if jsonUnmarshalUseNumber([]byte(cfg.String), &config) != nil {
				continue
			}
			config = normalizeJSONNumbers(config).(map[string]any)
		}
		if !poolConfigComplete(config) || IsInfoPlaceholder(node.RuntimeName) {
			continue
		}
		node.OriginalName = orig.String
		node.Config = config
		out = append(out, node)
	}
	return out, nodeRows.Err()
}

func poolConfigComplete(config map[string]any) bool {
	if len(config) == 0 {
		return false
	}
	for _, key := range poolRequiredConfigKeys {
		v, ok := config[key]
		if !ok || v == nil || pyStr(v) == "" {
			return false
		}
	}
	return true
}

// 指纹与 YAML 共用：整数归一化为 int64（超范围回退 float64），防加引号或 .0 拒载。
func normalizeJSONNumbers(v any) any {
	switch x := v.(type) {
	case map[string]any:
		for k, item := range x {
			x[k] = normalizeJSONNumbers(item)
		}
		return x
	case []any:
		for i, item := range x {
			x[i] = normalizeJSONNumbers(item)
		}
		return x
	case json.Number:
		text := string(x)
		if !strings.ContainsAny(text, ".eE") {
			if n, err := strconv.ParseInt(text, 10, 64); err == nil {
				return n
			}
		}
		if f, err := x.Float64(); err == nil {
			return f
		}
		return text
	default:
		return v
	}
}

func loadPoolSourceDocuments(ctx context.Context, conn *sql.Conn, dataDir string, nodes []poolNodeRow, errors map[int64]string) map[int64]map[string]any {
	originals := map[int64]map[string]bool{}
	needs := map[int64]bool{}
	for _, node := range nodes {
		original := node.OriginalName
		if original == "" {
			original = pyStr(node.Config["name"])
		}
		if originals[node.SubscriptionID.Int64] == nil {
			originals[node.SubscriptionID.Int64] = map[string]bool{}
		}
		originals[node.SubscriptionID.Int64][original] = true
	}
	for _, node := range nodes {
		sid := node.SubscriptionID.Int64
		dp := pyStr(node.Config["dialer-proxy"])
		if dp == "" || builtinProxyNames[dp] || originals[sid][dp] {
			continue
		}
		needs[sid] = true
	}
	if len(needs) == 0 {
		return nil
	}
	docs := map[int64]map[string]any{}
	for sid := range needs {
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

func loadSubscriptionProxyGroups(ctx context.Context, conn *sql.Conn, dataDir string, sid int64) (map[string]any, error) {
	var sealed sql.NullString
	if err := conn.QueryRowContext(ctx,
		`SELECT url FROM proxy_subscriptions WHERE id = ?`, sid).Scan(&sealed); err != nil {
		return nil, fmt.Errorf("订阅不存在：%d", sid)
	}
	url := OpenURL(dataDir, sealed.String)
	if url == "" {
		url = sealed.String
	}
	snap, err := RestoreLatestSnapshot(ctx, conn, dataDir, sid, url)
	if err != nil {
		return nil, fmt.Errorf("订阅快照不可恢复：%s", err.Error())
	}
	if snap == nil {
		return map[string]any{}, nil
	}
	if snap.Fmt != FormatYAML && snap.Fmt != FormatProviderYAML {
		return map[string]any{}, nil
	}
	doc, err := clashcfg.LoadYAML(string(snap.RawContent))
	if err != nil {
		return nil, fmt.Errorf("订阅快照不是合法 YAML：%s", err.Error())
	}
	return doc, nil
}

func renderPoolNodes(nodes []poolNodeRow, sourceDocuments map[int64]map[string]any) (string, []string, error) {
	names := make([]string, len(nodes))
	seen := map[string]bool{}
	var duplicates []string
	for i, node := range nodes {
		names[i] = node.RuntimeName
		if seen[node.RuntimeName] {
			duplicates = append(duplicates, node.RuntimeName)
		}
		seen[node.RuntimeName] = true
	}
	if len(duplicates) > 0 {
		uniq := map[string]bool{}
		for _, d := range duplicates {
			uniq[d] = true
		}
		shown := make([]string, 0, len(uniq))
		for d := range uniq {
			shown = append(shown, d)
		}
		slices.Sort(shown)
		shownList := make([]string, len(shown))
		for i, s := range shown {
			shownList[i] = pyReprValue(s)
		}
		return "", nil, &PoolBuildError{Detail: fmt.Sprintf(
			"池内 runtime_name 重复：%s（写盘前即拒绝：宁可不生成，也不生成一个静默少节点的池）",
			"["+strings.Join(shownList, ", ")+"]")}
	}

	grouped := map[int64][]int{}
	var order []int64
	for i, node := range nodes {
		sid := node.SubscriptionID.Int64
		if _, ok := grouped[sid]; !ok {
			order = append(order, sid)
		}
		grouped[sid] = append(grouped[sid], i)
	}
	proxies := make([]any, len(nodes))
	var groups []any
	for _, sid := range order {
		indexes := grouped[sid]
		namesByOriginal := map[string]string{}
		ambiguous := map[string]bool{}
		payloads := make([]map[string]any, len(indexes))
		sourceNames := make([]string, len(indexes))
		for j, idx := range indexes {
			node := nodes[idx]
			payload := map[string]any{}
			for k, v := range node.Config {
				payload[k] = v
			}
			payload["name"] = node.RuntimeName
			original := node.OriginalName
			if original == "" {
				original = pyStr(node.Config["name"])
			}
			if prev, ok := namesByOriginal[original]; ok && prev != node.RuntimeName {
				ambiguous[original] = true
			}
			namesByOriginal[original] = node.RuntimeName
			payloads[j] = payload
			sourceNames[j] = original
		}
		for name := range ambiguous {
			delete(namesByOriginal, name)
		}
		source := sourceDocuments[sid]
		var sourceGroups []map[string]any
		if source != nil {
			raw, _ := source["proxy-groups"].([]any)
			for _, g := range raw {
				if gm, ok := g.(map[string]any); ok {
					sourceGroups = append(sourceGroups, gm)
				}
			}
		}
		bundle, err := clashcfg.RenderNodeBundle(payloads, namesByOriginal, clashcfg.BundleOptions{
			Groups: sourceGroups, Namespace: strconv.FormatInt(sid, 10),
			SourceNames: sourceNames, SourceNamesSet: true,
		})
		if err != nil {
			return "", nil, &PoolBuildError{Detail: fmt.Sprintf("节点连接依赖不完整：%s", err.Error())}
		}
		for j, idx := range indexes {
			proxies[idx] = bundle.Proxies[j]
		}
		for _, g := range bundle.Groups {
			groups = append(groups, g)
		}
	}
	doc := clashcfg.NewDoc()
	doc.Set("proxies", proxies)
	if len(groups) > 0 {
		doc.Set("proxy-groups", groups)
	}
	return clashcfg.DumpYAML(doc), names, nil
}

func PoolSignature(ctx context.Context, conn *sql.Conn, dataDir string, now time.Time) ([]string, error) {
	nodes, err := EligibleNodes(ctx, conn, now)
	if err != nil {
		return nil, err
	}
	signatures := make([]string, 0, len(nodes)+1)
	for _, node := range nodes {
		payload := map[string]any{}
		for k, v := range node.Config {
			payload[k] = v
		}
		payload["name"] = node.RuntimeName
		blob, err := pyJSONEncode(payload)
		if err != nil {
			return nil, err
		}
		sum := sha256.Sum256([]byte(blob))
		signatures = append(signatures,
			node.RuntimeName+":"+hex.EncodeToString(sum[:]))
	}
	for _, slot := range selectCrawlSlots(nodes, nil) {
		signatures = append(signatures, "exit:"+slot.ExitIP+"|"+slot.Node)
	}
	sourceErrors := map[int64]string{}
	sources := loadPoolSourceDocuments(ctx, conn, dataDir, nodes, sourceErrors)
	var dependencies any
	text, _, renderErr := renderPoolNodes(nodes, sources)
	if renderErr != nil {
		dependencies = map[string]any{"invalid": renderErr.Error()}
	} else {
		parsed, err := clashcfg.LoadYAML(text)
		if err != nil {
			dependencies = map[string]any{"invalid": err.Error()}
		} else {
			dependencies = parsed["proxy-groups"]
		}
	}
	groups, _ := dependencies.([]any)
	// len(groups)>0 或非 nil 失败标记视为有依赖，nil 才算空。
	hasDependencies := len(groups) > 0 || (dependencies != nil && groups == nil)
	if hasDependencies || len(sourceErrors) > 0 {
		errMap := map[string]any{}
		for sid, msg := range sourceErrors {
			errMap[strconv.FormatInt(sid, 10)] = msg
		}
		blob, err := pyJSONEncode(map[string]any{"groups": dependencies, "errors": errMap})
		if err != nil {
			return nil, err
		}
		sum := sha256.Sum256([]byte(blob))
		signatures = append(signatures, "dependencies:"+hex.EncodeToString(sum[:]))
	}
	return signatures, nil
}

func PoolSignatureChanged(before, after []string) bool {
	if len(before) != len(after) {
		return true
	}
	for i := range before {
		if before[i] != after[i] {
			return true
		}
	}
	return false
}

var RequestPoolRebuild = RequestRebuild

func MaybeRequestPoolRebuild(before, after []string) {
	if PoolSignatureChanged(before, after) {
		RequestPoolRebuild()
	}
}

func BuildPool(ctx context.Context, conn *sql.Conn, dataDir string, now time.Time) (PoolBuildResult, error) {
	eligible, err := EligibleNodes(ctx, conn, now)
	if err != nil {
		return PoolBuildResult{}, err
	}
	sourceErrors := map[int64]string{}
	sources := loadPoolSourceDocuments(ctx, conn, dataDir, eligible, sourceErrors)
	text, names, err := renderPoolNodes(eligible, sources)
	if err != nil {
		return PoolBuildResult{}, err
	}
	parsed, err := clashcfg.LoadYAML(text)
	if err != nil {
		return PoolBuildResult{}, &PoolBuildError{Detail: "生成的池不是 {proxies: [...]} 形态：" + err.Error()}
	}
	entries, ok := parsed["proxies"].([]any)
	if !ok {
		return PoolBuildResult{}, &PoolBuildError{Detail: "生成的池不是 {proxies: [...]} 形态"}
	}
	if len(entries) != len(names) {
		return PoolBuildResult{}, &PoolBuildError{Detail: fmt.Sprintf(
			"写入数量 %d ≠ 预期 %d（存在被静默丢掉的节点）", len(entries), len(names))}
	}

	path := PoolPath(dataDir)
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return PoolBuildResult{}, err
	}
	tmp := strings.TrimSuffix(path, ".yaml") + ".tmp"
	if err := os.WriteFile(tmp, []byte(text), 0o644); err != nil {
		return PoolBuildResult{}, err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return PoolBuildResult{}, err
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		return PoolBuildResult{}, err
	}
	replayed, err := clashcfg.LoadYAML(string(raw))
	if err != nil {
		return PoolBuildResult{}, &PoolBuildError{Detail: "磁盘复读的名字集合与预期不一致：" + err.Error()}
	}
	diskNames := poolProxyNames(replayed)
	if len(diskNames) != len(names) {
		return PoolBuildResult{}, &PoolBuildError{Detail: "磁盘复读的名字集合与预期不一致"}
	}
	diskSet := map[string]bool{}
	for _, n := range diskNames {
		diskSet[n] = true
	}
	for _, n := range names {
		if !diskSet[n] {
			return PoolBuildResult{}, &PoolBuildError{Detail: "磁盘复读的名字集合与预期不一致"}
		}
	}
	var auxiliary []string
	if rawGroups, ok := parsed["proxy-groups"].([]any); ok {
		for _, g := range rawGroups {
			if gm, ok := g.(map[string]any); ok {
				auxiliary = append(auxiliary, pyStr(gm["name"]))
			}
		}
	}
	return PoolBuildResult{
		Path: path, ExpectedCount: len(names), WrittenCount: len(entries),
		RuntimeNames: names, AuxiliaryNames: auxiliary,
	}, nil
}
