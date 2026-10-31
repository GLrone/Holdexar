package proxypool

// 订阅快照账本（Python subscription.py 快照面）：一次成功抓取产出一份不可变
// 记录，失败也留痕（不改变 Registry、不覆盖最近成功快照）。原始字节按 sha
// 内容寻址落盘（订阅动辄数十 KB 不进库），行内只存路径。

import (
	"context"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/secretbox"
)

// SecretPurposeSubscription 订阅链接静态加密用途串。
const SecretPurposeSubscription = "proxy-subscription"

// UnreadableMessage 链接解密失败的用户语言提示。
const UnreadableMessage = "订阅链接无法在本机解密，请删除该订阅后重新添加"

// SealURL 订阅链接落库前封装（空串原样返回）。
func SealURL(dataDir, url string) (string, error) {
	if url == "" {
		return "", nil
	}
	return secretbox.EncryptSecretIn(dataDir, url, SecretPurposeSubscription)
}

// OpenURL 密文 → 明文。存量明文原样返回；解密失败返回空串（密钥属主机绑定）。
func OpenURL(dataDir, value string) string {
	if value == "" || !secretbox.IsEncrypted(value) {
		return value
	}
	text, err := secretbox.DecryptSecretIn(dataDir, value, SecretPurposeSubscription)
	if err != nil {
		return ""
	}
	return text
}

// URLMatches 快照 provenance 比对：存量值解密后与当前 URL 严格相等。
func URLMatches(dataDir, stored, wanted string) bool {
	return wanted != "" && OpenURL(dataDir, stored) == wanted
}

// FetchAttempt 一次通道尝试的留痕（失败报告的证物，事后改写即失去意义）。
type FetchAttempt struct {
	Channel    string
	OK         bool
	HTTPStatus *int
	Fmt        string
	Error      string
	Stage      string
	ElapsedMS  int
	EgressID   string
}

// Snapshot 一次成功抓取的不可变产物（构造后调用方不得改写 Nodes）。
type Snapshot struct {
	SubscriptionID     int64
	URL                string
	FetchedAt          time.Time
	HTTPStatus         int
	ContentType        string
	Sha256             string
	RawContent         []byte
	Fmt                string
	NodeCount          int
	SourceChannel      string
	Nodes              []map[string]any
	Attempts           []FetchAttempt
	SourceEgress       string
	ProfileTitle       string
	ContentDisposition string
	Userinfo           string
	DurationMS         int
	ProviderNodes      map[string][]map[string]any
}

var egressIDPattern = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,100}$`)

// safeEgressID 非法形态一律按 NULL 处理。
func safeEgressID(v string) any {
	if v == "" || !egressIDPattern.MatchString(v) {
		return nil
	}
	return v
}

// AttemptsJSON 通道尝试留痕落库文本（ensure_ascii=True 紧凑 JSON）。
func AttemptsJSON(attempts []FetchAttempt) string {
	items := make([]any, 0, len(attempts))
	for _, a := range attempts {
		var httpStatus any
		if a.HTTPStatus != nil {
			httpStatus = *a.HTTPStatus
		}
		errorText := a.Error
		if len(errorText) > 100 {
			errorText = truncateRunes(errorText, 100)
		}
		items = append(items, map[string]any{
			"channel":     truncateRunes(a.Channel, 32),
			"egress_id":   nilIfEmpty(truncateRunes(a.EgressID, 100)),
			"ok":          a.OK,
			"http_status": httpStatus,
			"format":      truncateRunes(a.Fmt, 16),
			"stage":       truncateRunes(a.Stage, 32),
			"elapsed_ms":  maxInt(0, a.ElapsedMS),
			"error":       errorText,
		})
	}
	// Python json.dumps 不 sort_keys：按字面量键序输出
	text, _ := pyJSONEncodeOrderedASCII(items, []string{
		"channel", "egress_id", "ok", "http_status", "format", "stage", "elapsed_ms", "error",
	})
	return text
}

// pyJSONEncodeOrderedASCII 按固定键序输出对象列表（对齐 Python dict 字面量序）。
func pyJSONEncodeOrderedASCII(items []any, keyOrder []string) (string, error) {
	var sb stringsBuilder
	sb.WriteByte('[')
	for i, item := range items {
		if i > 0 {
			sb.WriteByte(',')
		}
		m, ok := item.(map[string]any)
		if !ok {
			return "", fmt.Errorf("快照留痕序列化只支持对象列表")
		}
		sb.WriteByte('{')
		first := true
		for _, k := range keyOrder {
			v, present := m[k]
			if !present {
				continue
			}
			if !first {
				sb.WriteByte(',')
			}
			first = false
			pyEncodeString(&sb, k)
			sb.WriteByte(':')
			if err := pyEncodeOrderedValue(&sb, v, keyOrder); err != nil {
				return "", err
			}
		}
		sb.WriteByte('}')
	}
	sb.WriteByte(']')
	return asciiEscapeLate(sb.String()), nil
}

func pyEncodeOrderedValue(sb *stringsBuilder, v any, keyOrder []string) error {
	switch x := v.(type) {
	case nil:
		sb.WriteString("null")
	case bool:
		if x {
			sb.WriteString("true")
		} else {
			sb.WriteString("false")
		}
	case string:
		pyEncodeString(sb, x)
	case int:
		sb.WriteString(strconv.Itoa(x))
	case int64:
		sb.WriteString(strconv.FormatInt(x, 10))
	case []any:
		sb.WriteByte('[')
		for i, item := range x {
			if i > 0 {
				sb.WriteByte(',')
			}
			if err := pyEncodeOrderedValue(sb, item, keyOrder); err != nil {
				return err
			}
		}
		sb.WriteByte(']')
	case map[string]any:
		sb.WriteByte('{')
		first := true
		for _, k := range keyOrder {
			val, present := x[k]
			if !present {
				continue
			}
			if !first {
				sb.WriteByte(',')
			}
			first = false
			pyEncodeString(sb, k)
			sb.WriteByte(':')
			if err := pyEncodeOrderedValue(sb, val, keyOrder); err != nil {
				return err
			}
		}
		sb.WriteByte('}')
	default:
		return fmt.Errorf("快照留痕序列化不支持值类型 %T", v)
	}
	return nil
}

// asciiEscapeLate ensure_ascii=True 后处理：非 ASCII 码点转 \uXXXX。
func asciiEscapeLate(s string) string {
	if isASCII(s) {
		return s
	}
	var sb strings.Builder
	for _, r := range s {
		if r <= 0x7e {
			sb.WriteRune(r)
		} else if r > 0xffff {
			r1, r2 := utf16Pair(r)
			fmt.Fprintf(&sb, `\u%04x\u%04x`, r1, r2)
		} else {
			fmt.Fprintf(&sb, `\u%04x`, r)
		}
	}
	return sb.String()
}

func isASCII(s string) bool {
	for i := 0; i < len(s); i++ {
		if s[i] > 0x7e {
			return false
		}
	}
	return true
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func truncateRunes(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n])
}

func maxInt(a, b int) int {
	if a > b {
		return a
	}
	return b
}

// SnapshotDir 原始字节落盘目录。
func snapshotDir(dataDir string) string {
	return filepath.Join(dataDir, "proxypool", "snapshots")
}

// writeRaw 内容寻址落盘：相同内容只存一份；先 .tmp 再替换（避免半截文件）。
func writeRaw(dataDir string, subscriptionID int64, sha string, raw []byte) (string, error) {
	dir := filepath.Join(snapshotDir(dataDir), strconv.FormatInt(subscriptionID, 10))
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return "", err
	}
	path := filepath.Join(dir, sha+".bin")
	if _, err := os.Stat(path); err == nil {
		return path, nil
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, raw, 0o644); err != nil {
		return "", err
	}
	if err := os.Rename(tmp, path); err != nil {
		_ = os.Remove(tmp)
		return "", err
	}
	return path, nil
}

// PersistSnapshot 快照落盘 + 写行。调用方负责事务边界：本函数不提交。
// 落盘排在写入之前：文件是幂等的内容寻址写入，事务回滚后留孤儿文件远好过
// 「事务提交了但原始字节没落盘」——后者会让恢复永远拿不回基线。
func PersistSnapshot(ctx context.Context, conn *sql.Conn, dataDir string, snap *Snapshot) (int64, error) {
	path, err := writeRaw(dataDir, snap.SubscriptionID, snap.Sha256, snap.RawContent)
	if err != nil {
		return 0, err
	}
	sealed, err := SealURL(dataDir, snap.URL)
	if err != nil {
		return 0, err
	}
	profileTitle := nilIfEmptyStr(truncateRunes(snap.ProfileTitle, 100))
	contentDisposition := nilIfEmptyStr(truncateRunes(snap.ContentDisposition, 255))
	userinfo := nilIfEmptyStr(truncateRunes(snap.Userinfo, 500))
	var providerJSON any
	if len(snap.ProviderNodes) > 0 {
		providerJSON = pyDumpProviderJSON(snap.ProviderNodes)
	}
	res, err := conn.ExecContext(ctx, `INSERT INTO subscription_snapshots
		(subscription_id, sha256, format, raw_path, url, node_count, status,
		 http_status, content_type, source_channel, source_egress, profile_title,
		 content_disposition, userinfo, duration_ms, attempts_json, provider_nodes_json, fetched_at)
		VALUES (?, ?, ?, ?, ?, ?, 'OK', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		snap.SubscriptionID, snap.Sha256, snap.Fmt, path, sealed, snap.NodeCount,
		snap.HTTPStatus, nilIfEmptyStr(snap.ContentType), nilIfEmptyStr(snap.SourceChannel),
		safeEgressID(snap.SourceEgress), profileTitle, contentDisposition, userinfo,
		maxInt(0, snap.DurationMS), AttemptsJSON(snap.Attempts), providerJSON,
		pyTimeText(snap.FetchedAt))
	if err != nil {
		return 0, err
	}
	return res.LastInsertId()
}

// PersistFetchFailure 失败留痕行（不改变可恢复快照：sha256 为空、无 raw_path）。
func PersistFetchFailure(ctx context.Context, conn *sql.Conn, in FailureRecord) error {
	sealed, err := SealURL(in.DataDir, in.URL)
	if err != nil {
		return err
	}
	var httpStatus any
	for i := len(in.Attempts) - 1; i >= 0; i-- {
		if in.Attempts[i].HTTPStatus != nil {
			httpStatus = *in.Attempts[i].HTTPStatus
			break
		}
	}
	errorText := in.Error
	if errorText == "" {
		errorText = "subscription refresh failed"
	}
	errorText = truncateRunes(errorText, 500)
	_, err = conn.ExecContext(ctx, `INSERT INTO subscription_snapshots
		(subscription_id, sha256, format, url, node_count, status,
		 http_status, duration_ms, attempts_json, error, fetched_at)
		VALUES (?, '', ?, ?, 0, 'FAILED', ?, ?, ?, ?, ?)`,
		in.SubscriptionID, FormatUnknown, sealed, httpStatus,
		maxInt(0, in.DurationMS), AttemptsJSON(in.Attempts), errorText,
		pyTimeText(in.Now))
	return err
}

// FailureRecord 失败留痕输入。
type FailureRecord struct {
	SubscriptionID int64
	DataDir        string
	URL            string
	Attempts       []FetchAttempt
	Error          string
	DurationMS     int
	Now            time.Time
}

func nilIfEmptyStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// pyDumpProviderJSON provider 节点元数据落库文本（默认分隔符 JSON）。
func pyDumpProviderJSON(providerNodes map[string][]map[string]any) string {
	names := make([]string, 0, len(providerNodes))
	for name := range providerNodes {
		names = append(names, name)
	}
	sortStrings(names)
	var sb strings.Builder
	sb.WriteByte('{')
	for i, name := range names {
		if i > 0 {
			sb.WriteString(", ")
		}
		sb.WriteString(pythonQuoted(name))
		sb.WriteString(": ")
		sb.WriteString(pyValueSQLText(nodesToAny(providerNodes[name])))
	}
	sb.WriteByte('}')
	return sb.String()
}

func nodesToAny(nodes []map[string]any) []any {
	out := make([]any, 0, len(nodes))
	for _, n := range nodes {
		out = append(out, any(n))
	}
	return out
}

// RestoreLatestSnapshot 从最近一条成功快照行恢复 Snapshot（「上一份」唯一入口）。
// url 非空时只认从该 URL 抓下来的成功快照（换链接后旧链接快照不算事实）。
func RestoreLatestSnapshot(ctx context.Context, conn *sql.Conn, dataDir string, subscriptionID int64, url string) (*Snapshot, error) {
	rows, err := conn.QueryContext(ctx, `SELECT id, sha256, format, raw_path, url, node_count,
		http_status, content_type, source_channel, source_egress, profile_title,
		content_disposition, userinfo, duration_ms, attempts_json, provider_nodes_json,
		CAST(fetched_at AS TEXT)
		FROM subscription_snapshots WHERE subscription_id = ? AND status = 'OK' ORDER BY id DESC`,
		subscriptionID)
	if err != nil {
		return nil, err
	}
	var candidates []snapshotRow
	for rows.Next() {
		var r snapshotRow
		if err := rows.Scan(&r.id, &r.sha, &r.format, &r.rawPath, &r.sealedURL, &r.nodeCount,
			&r.httpStatus, &r.contentType, &r.sourceChannel, &r.sourceEgress, &r.profileTitle,
			&r.contentDisposition, &r.userinfo, &r.durationMS, &r.attemptsJSON, &r.providerNodesJSON,
			&r.fetchedAt); err != nil {
			rows.Close()
			return nil, err
		}
		candidates = append(candidates, r)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()

	var matched *snapshotRow
	if url != "" {
		for i := range candidates {
			if URLMatches(dataDir, candidates[i].sealedURL.String, url) {
				matched = &candidates[i]
				break
			}
		}
	} else if len(candidates) > 0 {
		matched = &candidates[0]
	}
	if matched == nil {
		return nil, nil
	}
	return restoreSnapshotRow(dataDir, matched)
}

// snapshotRow 快照行的只读投影。
type snapshotRow struct {
	id                 int64
	sha, format        sql.NullString
	rawPath            sql.NullString
	sealedURL          sql.NullString
	nodeCount          int64
	httpStatus         sql.NullInt64
	contentType        sql.NullString
	sourceChannel      sql.NullString
	sourceEgress       sql.NullString
	profileTitle       sql.NullString
	contentDisposition sql.NullString
	userinfo           sql.NullString
	durationMS         sql.NullInt64
	attemptsJSON       sql.NullString
	providerNodesJSON  sql.NullString
	fetchedAt          sql.NullString
}

func restoreSnapshotRow(dataDir string, r *snapshotRow) (*Snapshot, error) {
	if !r.rawPath.Valid || r.rawPath.String == "" {
		return nil, &SnapshotRecoveryError{Detail: "快照行没有 raw_path"}
	}
	raw, err := os.ReadFile(r.rawPath.String)
	if err != nil {
		return nil, &SnapshotRecoveryError{Detail: fmt.Sprintf("原始字节读不回：%s", r.rawPath.String)}
	}
	sum := sha256.Sum256(raw)
	sha := hex.EncodeToString(sum[:])
	if sha != r.sha.String {
		return nil, &SnapshotRecoveryError{
			Detail: fmt.Sprintf("原始字节与快照 sha 不符：文件 %s != 记录 %s", sha[:12], r.sha.String[:12]),
		}
	}
	if r.providerNodesJSON.Valid && r.providerNodesJSON.String != "" {
		// provider 订阅的恢复依赖二级来源展开与 override 应用，未随本批迁移；
		// 静默按 inline 解析会丢 provider 节点，fail-closed 报错。
		return nil, &SnapshotRecoveryError{Detail: "provider 订阅快照恢复暂不支持"}
	}
	fmt0 := r.format.String
	if fmt0 == "" {
		fmt0 = FormatUnknown
	}
	nodes, err := ParseNodes(raw, fmt0)
	if err != nil {
		return nil, &SnapshotRecoveryError{Detail: fmt.Sprintf("原始字节已不可解析：%s", err.Error())}
	}
	fetchedAt := time.Now()
	if r.fetchedAt.Valid && r.fetchedAt.String != "" {
		if t, err := parsePyDatetime(r.fetchedAt.String); err == nil {
			fetchedAt = t
		}
	}
	var httpStatus int
	if r.httpStatus.Valid {
		httpStatus = int(r.httpStatus.Int64)
	}
	return &Snapshot{
		URL:                OpenURL(dataDir, r.sealedURL.String),
		FetchedAt:          fetchedAt,
		HTTPStatus:         httpStatus,
		ContentType:        r.contentType.String,
		Sha256:             sha,
		RawContent:         raw,
		Fmt:                fmt0,
		NodeCount:          len(nodes),
		SourceChannel:      r.sourceChannel.String,
		Nodes:              nodes,
		Attempts:           parseStoredAttempts(r.attemptsJSON.String),
		SourceEgress:       stringOrNull(r.sourceEgress),
		ProfileTitle:       stringOrNull(r.profileTitle),
		ContentDisposition: stringOrNull(r.contentDisposition),
		Userinfo:           stringOrNull(r.userinfo),
		DurationMS:         nullIntOr(r.durationMS, 0),
	}, nil
}

// SnapshotRecoveryError 快照恢复失败（fail-closed：静默当成没有上一份会让
// 来源对齐判空）。
type SnapshotRecoveryError struct{ Detail string }

func (e *SnapshotRecoveryError) Error() string {
	return "订阅快照恢复失败：" + e.Detail
}

// parseStoredAttempts 恢复库内通道留痕（字段缺失/类型不符按缺省）。
func parseStoredAttempts(payload string) []FetchAttempt {
	if payload == "" {
		return nil
	}
	var items []map[string]any
	if err := jsonUnmarshalUseNumber([]byte(payload), &items); err != nil {
		return nil
	}
	out := make([]FetchAttempt, 0, len(items))
	for _, item := range items {
		elapsed := int64(0)
		if v, ok := pyNumberToInt64(item["elapsed_ms"]); ok {
			elapsed = v
		}
		a := FetchAttempt{
			Channel:   truncateRunes(pyStr(item["channel"]), 32),
			OK:        pyTruthy(item["ok"]),
			Fmt:       truncateRunes(pyStr(item["format"]), 16),
			Error:     truncateRunes(pyStr(item["error"]), 100),
			Stage:     truncateRunes(pyStr(item["stage"]), 32),
			ElapsedMS: maxInt(0, int(elapsed)),
		}
		if hs, ok := pyNumberToInt64(item["http_status"]); ok {
			v := int(hs)
			a.HTTPStatus = &v
		}
		if e := pyStr(item["egress_id"]); egressIDPattern.MatchString(e) {
			a.EgressID = e
		}
		if a.Error == "" && a.Stage == "" && a.Channel == "" {
			continue
		}
		out = append(out, a)
	}
	return out
}

// IsSnapshotRecovery 判定错误是否为快照恢复失败。
func IsSnapshotRecovery(err error) bool {
	var rec *SnapshotRecoveryError
	return errors.As(err, &rec)
}
