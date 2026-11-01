package proxypool

// 池构建与重建钩子（Python pool.py / bootstrap.py 对齐）：
// EligibleNodes 读当前合格集（只读不落盘）；renderPoolNodes 纯渲染（重名即拒绝）；
// BuildPool 三门禁后原子落盘；PoolSignature 按完整节点参数与连接依赖组判定
// 运行集变化——判定"池变了"用运行集签名而不是快照 SHA：快照整体变了但合格
// 运行集没变时，重启内核毫无意义。合格集变化 → RequestPoolRebuild 请求重建。
// 并发边界：只执行 SQL 不提交，事务归调用方（经写闸）。

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/clashcfg"
)

const PoolFilename = "crawl-pool.yaml"

// 内核能装载一条代理所需的最小键：缺任何一个都不该进池（写进去只会被内核
// 拒绝，比不写更糟——那会变成「池里有它、内核里没有」的静默差额）。
var poolRequiredConfigKeys = []string{"type", "server", "port"}

// builtinProxyNames 内建代理名（Python BUILTIN_PROXY_NAMES 同表）：这些目标
// 不需要跨订阅来源解释，不算「连接依赖来源不可用」。
var builtinProxyNames = map[string]bool{
	"DIRECT": true, "REJECT": true, "REJECT-DROP": true, "GLOBAL": true,
	"COMPATIBLE": true, "PASS": true, "PASS-RULE": true,
}

// PoolPath 池文件落盘路径。
func PoolPath(dataDir string) string {
	return filepath.Join(dataDir, "proxypool", PoolFilename)
}

// PoolFileNames 池文件当前写入的节点名（没有文件返回空）。这是落盘产物的读数，
// 与 EligibleNodes（合格集）区分：判断池文件有没有跟上合格集要比集合，比数量
// 会在「换了一个节点但数量相同」时漏判。
func PoolFileNames(dataDir string) []string {
	raw, err := os.ReadFile(PoolPath(dataDir))
	if err != nil {
		return nil
	}
	doc, err := clashYAMLLoad(string(raw))
	if err != nil {
		return nil
	}
	return poolProxyNames(doc)
}

// PoolAuxiliaryNames 池产物中的连接依赖组名称。
func PoolAuxiliaryNames(dataDir string) []string {
	raw, err := os.ReadFile(PoolPath(dataDir))
	if err != nil {
		return nil
	}
	doc, err := clashYAMLLoad(string(raw))
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

// PoolBuildError 门禁未通过。写盘前抛出，调用方可以确信旧池未被破坏。
type PoolBuildError struct{ Detail string }

func (e *PoolBuildError) Error() string { return e.Detail }

// PoolBuildResult 一次池生成的账目（数量是门禁的观测值，供调用方记录/断言）。
type PoolBuildResult struct {
	Path           string
	ExpectedCount  int
	WrittenCount   int
	RuntimeNames   []string
	AuxiliaryNames []string
}

// poolNodeRow 合格节点行投影（Config 为解析后的归一化配置）。
type poolNodeRow struct {
	ID             int64
	NodeID         string
	RuntimeName    string
	OriginalName   string
	SubscriptionID sql.NullInt64
	Config         map[string]any
}

// poolSubscriptionRow 准入判定所需的订阅行投影。
type poolSubscriptionRow struct {
	ID          int64
	Admission   sql.NullString
	LastStats   sql.NullString
	OriginalURL string
}

// SubscriptionBlockReason 资源封锁判定（Python ProxySubscription.resource_block_reason
// 对齐）：last_stats.traffic 的 expire 已过 → 订阅已到期；total>0 且已用尽 →
// 订阅流量已用尽。返回空串 = 不封锁。
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

// EligibleNodes 当前合格节点（池的输入集）。只读、不落盘。
// 合格 = 状态合格（NEW/ACTIVE/STALE）+ 配置完整 + 仍归属订阅 + 订阅在生产准入
// 且未资源封锁；信息占位节点（机场公告位）永不合格。
func EligibleNodes(ctx context.Context, conn *sql.Conn, now time.Time) ([]poolNodeRow, error) {
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
		`SELECT id, node_id, runtime_name, original_name, subscription_id, CAST(normalized_config AS TEXT)
		 FROM proxy_nodes
		 WHERE state IN (?, ?, ?) AND subscription_id IS NOT NULL
		 ORDER BY id`, NodeNew, NodeActive, NodeStale)
	if err != nil {
		return nil, err
	}
	defer nodeRows.Close()
	var out []poolNodeRow
	for nodeRows.Next() {
		var (
			id     int64
			nodeID string
			name   string
			orig   sql.NullString
			sid    sql.NullInt64
			cfg    sql.NullString
		)
		if err := nodeRows.Scan(&id, &nodeID, &name, &orig, &sid, &cfg); err != nil {
			return nil, err
		}
		sub, ok := subs[sid.Int64]
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
		if !poolConfigComplete(config) || IsInfoPlaceholder(name) {
			continue
		}
		out = append(out, poolNodeRow{
			ID: id, NodeID: nodeID, RuntimeName: name,
			OriginalName:   orig.String,
			SubscriptionID: sid, Config: config,
		})
	}
	return out, nodeRows.Err()
}

// poolConfigComplete 资格之外的「配置完整」：池里每一条都必须是内核装载得了的。
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

// normalizeJSONNumbers 节点配置树内数字形态归一（Python json.loads 语义）：
// json.Number 整数字面量 → int64，带小数/指数 → float64，其余原样。渲染与
// 指纹两侧共用：YAML dump 对 int64 原样输出，json.Number 会被按字符串加引号、
// float64 整值会带 .0——两者真内核都拒载。超出 int64 的整数字面量按 float64
// 近似（Python 任意精度整数在节点配置里不可达）。
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

// loadPoolSourceDocuments 只为无法由同订阅节点解释的连接目标读取快照原文。
// 格式不是 YAML / provider YAML 的快照按空文档处理；读取失败记入 errors。
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

// loadSubscriptionProxyGroups 读该订阅当前 URL 最近一次成功快照的 proxy-groups。
// 没有可恢复快照按空文档（调用方以 errors 区分「读了但空」与「读失败」）。
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
	doc, err := clashYAMLLoad(string(snap.RawContent))
	if err != nil {
		return nil, fmt.Errorf("订阅快照不是合法 YAML：%s", err.Error())
	}
	return doc, nil
}

// renderPoolNodes 纯渲染层：不碰数据库、不碰磁盘，只做重复名门禁并产出 YAML 文本。
// 单独暴露是为了让「重名即拒绝」这条规则不依赖数据库唯一约束兜底也测得到。
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
		sort.Strings(shown)
		shownList := make([]string, len(shown))
		for i, s := range shown {
			shownList[i] = pyReprValue(s)
		}
		return "", nil, &PoolBuildError{Detail: fmt.Sprintf(
			"池内 runtime_name 重复：%s（写盘前即拒绝：宁可不生成，也不生成一个静默少节点的池）",
			"["+strings.Join(shownList, ", ")+"]")}
	}

	grouped := map[int64][]int{} // 订阅 → 节点下标（保序）
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

// PoolSignature 按完整节点参数及必要连接依赖组判定运行集变化。
// 每个节点一条 `runtime_name:sha256(完整参数)`；连接依赖组与来源错误合成一条
// `dependencies:sha256(...)`。返回的切片有序（按节点主键）。
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
	sourceErrors := map[int64]string{}
	sources := loadPoolSourceDocuments(ctx, conn, dataDir, nodes, sourceErrors)
	var dependencies any
	text, _, renderErr := renderPoolNodes(nodes, sources)
	if renderErr != nil {
		dependencies = map[string]any{"invalid": renderErr.Error()}
	} else {
		parsed, err := clashYAMLLoad(text)
		if err != nil {
			dependencies = map[string]any{"invalid": err.Error()}
		} else {
			dependencies = parsed["proxy-groups"]
		}
	}
	groups, _ := dependencies.([]any)
	// Python 真值语义：依赖组列表非空，或渲染失败落下的 {"invalid": ...} 标记
	// （dict 恒真值），都算「依赖集有内容」；nil（无依赖组）才算空。
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

// PoolSignatureChanged 两个运行集签名是否不同（顺序敏感：签名按节点主键有序）。
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

// RequestPoolRebuild 合格集变化 → 请求重建的注入点。默认 no-op：重建编排批次
// 接线时替换为「置重建待办位」，由空闲期消费者对接 clashcfg.PrepareRuntimeConfig
// 与 clashkernel 热重载完成收敛。退出条件：重建编排落地后本变量由其接管，
// 调用方一律经 MaybeRequestPoolRebuild，不再直调本变量。
var RequestPoolRebuild = func() {}

// MaybeRequestPoolRebuild 签名变化时请求重建（幂等：连续置位只算一次的语义
// 由待办位持有方保证）。
func MaybeRequestPoolRebuild(before, after []string) {
	if PoolSignatureChanged(before, after) {
		RequestPoolRebuild()
	}
}

// BuildPool 读 Registry → 渲染 → 三门禁 → 落盘 → 复读。事务与提交归调用方
// （经写闸）；任何门禁失败时旧池保持原样（不靠回滚补）。
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
	// 门禁一/二：生成的文本自己必须解析得回来，且条目数不能缩水。
	parsed, err := clashYAMLLoad(text)
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

	// 门禁三：写完复读，磁盘上的名字集合必须与预期一致（名字是池与内核之间
	// 唯一的对账钥匙）。
	raw, err := os.ReadFile(path)
	if err != nil {
		return PoolBuildResult{}, err
	}
	replayed, err := clashYAMLLoad(string(raw))
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
