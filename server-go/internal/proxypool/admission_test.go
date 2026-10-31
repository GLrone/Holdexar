package proxypool

// 订阅准入晋升状态机单测：晋升全迁移路径、换链接守卫、stale 兜底、快照恢复
// fail-closed、退出生产、健康自动转正、2h 重拉门与容量钳制。

import (
	"context"
	"database/sql"
	"os"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

// newAdmissionDB 隔离库 + 完整订阅行 DDL + 全部账本表。
func newAdmissionDB(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	db, gate, dir := newTestDB(t)
	conn := mustConn(t, db)
	if err := EnsureSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	if err := EnsureHealthSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`DROP TABLE proxy_subscriptions`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TABLE proxy_subscriptions (
		id INTEGER PRIMARY KEY AUTOINCREMENT,
		url TEXT NOT NULL,
		kind VARCHAR(10) NOT NULL DEFAULT 'clash',
		admission_status VARCHAR(16) NOT NULL DEFAULT 'CANDIDATE',
		last_stats JSON,
		last_fetch_at DATETIME, last_fetch_status VARCHAR(32),
		last_success_at DATETIME, last_error VARCHAR(500), cache_url_identity VARCHAR(64),
		auto_refresh BOOLEAN NOT NULL DEFAULT 1)`); err != nil {
		t.Fatal(err)
	}
	return db, gate, dir
}

func seedSubscription(t *testing.T, db *sql.DB, dataDir, plainURL, status, kind, lastStatsJSON string) int64 {
	t.Helper()
	sealed, err := SealURL(dataDir, plainURL)
	if err != nil {
		t.Fatal(err)
	}
	res, err := db.Exec(`INSERT INTO proxy_subscriptions (url, kind, admission_status, last_stats) VALUES (?, ?, ?, ?)`,
		sealed, kind, status, nullTextArg(lastStatsJSON))
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

func mustSeal(t *testing.T, dataDir, plainURL string) string {
	t.Helper()
	sealed, err := SealURL(dataDir, plainURL)
	if err != nil {
		t.Fatal(err)
	}
	return sealed
}

func mustStatus(t *testing.T, db *sql.DB, id int64) string {
	t.Helper()
	var status string
	if err := db.QueryRow(`SELECT admission_status FROM proxy_subscriptions WHERE id = ?`, id).Scan(&status); err != nil {
		t.Fatal(err)
	}
	return status
}

func nowFixed() time.Time { return time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local) }

const admissionURL = "https://provider.example.com/sub/a"

// admissionYAML 两节点订阅正文（RestoreLatestSnapshot 会把原始字节重解析回节点，
// 快照夹具必须是真 YAML，不能用占位字节）。
const admissionYAML = "proxies:\n" +
	"  - name: 香港 01\n    type: ss\n    server: hk1.example.com\n    port: 8443\n" +
	"    cipher: aes-256-gcm\n    password: p1\n" +
	"  - name: 美国 02\n    type: vmess\n    server: us2.example.com\n    port: 443\n" +
	"    uuid: aaaa-bbbb\n    tls: true\n"

// snapshotIntoDB 持久化一份成功快照（不登记节点），返回快照。
func snapshotIntoDB(t *testing.T, conn *sql.Conn, dataDir string, subID int64, plainURL string, now time.Time) *Snapshot {
	t.Helper()
	return snapshotIntoDBRaw(t, conn, dataDir, subID, plainURL, admissionYAML, now)
}

// snapshotIntoDBRaw 用指定正文持久化快照（不同 URL 的快照须不同正文，
// 同 sha 的两行会让 stale 守卫永远不触发）。
func snapshotIntoDBRaw(t *testing.T, conn *sql.Conn, dataDir string, subID int64, plainURL, yamlText string, now time.Time) *Snapshot {
	t.Helper()
	raw := []byte(yamlText)
	snap := &Snapshot{
		SubscriptionID: subID, URL: plainURL, FetchedAt: now, HTTPStatus: 200,
		ContentType: "text/yaml", Sha256: sha256Hex(raw), RawContent: raw,
		Fmt: FormatYAML, NodeCount: 2, SourceChannel: ChannelDirect,
	}
	if _, err := PersistSnapshot(context.Background(), conn, dataDir, snap); err != nil {
		t.Fatal(err)
	}
	return snap
}

func TestPromoteToActiveFullFlow(t *testing.T) {
	db, _, dir := newAdmissionDB(t)
	conn := mustConn(t, db)
	subID := seedSubscription(t, db, dir, admissionURL, AdmissionCandidate, "clash", "")
	now := nowFixed()

	// 完全没抓成功过：拒绝晋升，不重抓、不用失败快照
	if _, err := PromoteToActive(context.Background(), conn, dir, subID, now); err == nil {
		t.Fatalf("无快照必须拒绝晋升")
	} else if pe, ok := err.(*PromotionError); !ok || pe.Detail != "没有成功快照，拒绝晋升（不重新抓取、不用失败快照或旧缓存）" {
		t.Fatalf("拒因不符：%v", err)
	}

	// 快照字节丢失：fail-closed 拒绝晋升
	snapshotIntoDB(t, conn, dir, subID, admissionURL, now)
	var rawPath string
	if err := db.QueryRow(`SELECT raw_path FROM subscription_snapshots WHERE subscription_id = ?`, subID).Scan(&rawPath); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(rawPath); err != nil {
		t.Fatal(err)
	}
	if _, err := PromoteToActive(context.Background(), conn, dir, subID, now); err == nil {
		t.Fatalf("快照不可恢复必须拒绝晋升")
	} else if pe, ok := err.(*PromotionError); !ok || !contains(pe.Detail, "最近一次成功快照不可恢复") {
		t.Fatalf("拒因不符：%v", err)
	}

	// 正常晋升：状态置 ACTIVE、节点进 Registry
	snapshotIntoDB(t, conn, dir, subID, admissionURL, now)
	result, err := PromoteToActive(context.Background(), conn, dir, subID, now)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Promoted || result.AppliedNodes != 2 || result.SnapshotSha256 == "" {
		t.Fatalf("晋升账目不符：%+v", result)
	}
	if status := mustStatus(t, db, subID); status != AdmissionActive {
		t.Fatalf("晋升后应为 ACTIVE，实际 %s", status)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE subscription_id = ?`, subID); n != 2 {
		t.Fatalf("Registry 应有 2 个来源，实际 %d", n)
	}

	// 已是 ACTIVE：幂等空转
	result, err = PromoteToActive(context.Background(), conn, dir, subID, now)
	if err != nil {
		t.Fatal(err)
	}
	if result.Promoted {
		t.Fatalf("已是 ACTIVE 应幂等空转：%+v", result)
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (s == sub || len(sub) == 0 || indexOf(s, sub) >= 0)
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func TestPromoteToActiveURLChangeGuard(t *testing.T) {
	db, _, dir := newAdmissionDB(t)
	conn := mustConn(t, db)
	now := nowFixed()
	// 订阅先有旧链接快照，换链接后旧快照不得顶替
	oldID := seedSubscription(t, db, dir, "https://provider.example.com/sub/old", AdmissionCandidate, "clash", "")
	snapshotIntoDB(t, conn, dir, oldID, "https://provider.example.com/sub/old", now)
	if _, err := db.Exec(`UPDATE proxy_subscriptions SET url = ? WHERE id = ?`,
		mustSeal(t, dir, "https://provider.example.com/sub/new"), oldID); err != nil {
		t.Fatal(err)
	}
	if _, err := PromoteToActive(context.Background(), conn, dir, oldID, now); err == nil {
		t.Fatalf("换链接后无当前 URL 快照必须拒绝晋升")
	} else if pe, ok := err.(*PromotionError); !ok ||
		pe.Detail != "当前订阅 URL 还没有成功快照（换链接后必须重新成功抓取），拒绝晋升" {
		t.Fatalf("拒因不符：%v", err)
	}
	if status := mustStatus(t, db, oldID); status != AdmissionCandidate {
		t.Fatalf("拒绝晋升必须保持 CANDIDATE，实际 %s", status)
	}
}

// stale 兜底：apply 时库里有更新的成功快照（换链接后的 id 更大行）→
// 「最新成功快照胜」守卫跳过 → 拒绝晋升保持 CANDIDATE——绝不允许
// ACTIVE + Registry 无来源的中间事实。
func TestPromoteToActiveStaleRefusal(t *testing.T) {
	db, _, dir := newAdmissionDB(t)
	conn := mustConn(t, db)
	now := nowFixed()
	subID := seedSubscription(t, db, dir, admissionURL, AdmissionCandidate, "clash", "")
	// 当前 URL 的快照先落（id 小），换链接前的旧链接快照后落（id 大）——
	// 读「当前 URL 最新成功快照」拿到旧行，apply 即被判 stale
	snapshotIntoDB(t, conn, dir, subID, admissionURL, now)
	snapshotIntoDBRaw(t, conn, dir, subID, "https://provider.example.com/sub/old",
		strings.Replace(admissionYAML, "p1", "old-pw", 1), now)
	if _, err := PromoteToActive(context.Background(), conn, dir, subID, now); err == nil {
		t.Fatalf("stale 快照必须拒绝晋升")
	} else if pe, ok := err.(*PromotionError); !ok ||
		pe.Detail != "最近一次成功快照在晋升过程中已被更新的成功快照取代，拒绝晋升（保持 CANDIDATE）" {
		t.Fatalf("拒因不符：%v", err)
	}
	if status := mustStatus(t, db, subID); status != AdmissionCandidate {
		t.Fatalf("stale 拒绝必须保持 CANDIDATE，实际 %s", status)
	}
	// Registry 里不得有来源（stale 路径不写节点）
	if n := countRows(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE subscription_id = ?`, subID); n != 0 {
		t.Fatalf("stale 拒绝不得留来源行，实际 %d", n)
	}
}

func TestPromoteToActiveGuards(t *testing.T) {
	db, _, dir := newAdmissionDB(t)
	conn := mustConn(t, db)
	now := nowFixed()
	// 订阅不存在
	if _, err := PromoteToActive(context.Background(), conn, dir, 999, now); err == nil {
		t.Fatalf("订阅不存在必须报错")
	}
	// 非 clash 订阅没有生产准入语义
	crawlID := seedSubscription(t, db, dir, admissionURL, AdmissionCandidate, "crawl", "")
	if _, err := PromoteToActive(context.Background(), conn, dir, crawlID, now); err == nil {
		t.Fatalf("非 clash 订阅必须拒绝晋升")
	}
}

func TestExitFromProduction(t *testing.T) {
	db, _, dir := newAdmissionDB(t)
	conn := mustConn(t, db)
	now := nowFixed()
	subID := seedSubscription(t, db, dir, admissionURL, AdmissionCandidate, "clash", "")
	snapshotIntoDB(t, conn, dir, subID, admissionURL, now)
	if _, err := PromoteToActive(context.Background(), conn, dir, subID, now); err != nil {
		t.Fatal(err)
	}
	var someNodeID string
	if err := db.QueryRow(`SELECT node_id FROM proxy_nodes WHERE subscription_id = ? LIMIT 1`, subID).Scan(&someNodeID); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO health_observations (node_id, level, ok) VALUES (?, 'L2', 1)`, someNodeID); err != nil {
		t.Fatal(err)
	}

	result, err := ExitFromProduction(context.Background(), conn, subID)
	if err != nil {
		t.Fatal(err)
	}
	if !result.Exited || result.RemovedSources != 2 {
		t.Fatalf("退出账目不符：%+v", result)
	}
	if status := mustStatus(t, db, subID); status != AdmissionCandidate {
		t.Fatalf("退出后应回 CANDIDATE，实际 %s", status)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM proxy_nodes WHERE subscription_id = ?`, subID); n != 0 {
		t.Fatalf("节点行应随订阅整体离场，实际剩 %d", n)
	}
	if n := countRows(t, db, `SELECT COUNT(*) FROM health_observations WHERE node_id = ?`, someNodeID); n != 0 {
		t.Fatalf("健康观测应一并清理，实际剩 %d", n)
	}
	// 幂等：已不在池里再退出是空转
	result, err = ExitFromProduction(context.Background(), conn, subID)
	if err != nil {
		t.Fatal(err)
	}
	if result.Exited {
		t.Fatalf("已退出再退应是空转：%+v", result)
	}
}

func TestAutoAdmitHealthyCandidate(t *testing.T) {
	db, _, dir := newAdmissionDB(t)
	conn := mustConn(t, db)
	now := nowFixed()

	// 体检无可用节点：保持候选
	weakID := seedSubscription(t, db, dir, admissionURL, AdmissionCandidate, "clash", `{"alive": 0}`)
	if result, err := AutoAdmitHealthyCandidate(context.Background(), conn, dir, weakID, now); err != nil || result != nil {
		t.Fatalf("无可用节点不得转正：%+v %v", result, err)
	}
	if status := mustStatus(t, db, weakID); status != AdmissionCandidate {
		t.Fatalf("应保持候选，实际 %s", status)
	}

	// 健康但没有成功快照：转正不成保持候选，不半截污染
	healthyNoSnap := seedSubscription(t, db, dir, "https://provider.example.com/sub/b", AdmissionCandidate, "clash", `{"alive": 3}`)
	if result, err := AutoAdmitHealthyCandidate(context.Background(), conn, dir, healthyNoSnap, now); err != nil || result != nil {
		t.Fatalf("快照未就绪应保持候选：%+v %v", result, err)
	}
	if status := mustStatus(t, db, healthyNoSnap); status != AdmissionCandidate {
		t.Fatalf("转正失败不得改状态，实际 %s", status)
	}

	// 健康且快照就绪：直接晋升
	okID := seedSubscription(t, db, dir, "https://provider.example.com/sub/c", AdmissionCandidate, "clash", `{"alive": 2}`)
	snapshotIntoDB(t, conn, dir, okID, "https://provider.example.com/sub/c", now)
	result, err := AutoAdmitHealthyCandidate(context.Background(), conn, dir, okID, now)
	if err != nil {
		t.Fatal(err)
	}
	if result == nil || !result.Promoted {
		t.Fatalf("健康候选应转正：%+v", result)
	}
	if status := mustStatus(t, db, okID); status != AdmissionActive {
		t.Fatalf("转正后应为 ACTIVE，实际 %s", status)
	}

	// 已是 ACTIVE：不再处理
	if result, err := AutoAdmitHealthyCandidate(context.Background(), conn, dir, okID, now); err != nil || result != nil {
		t.Fatalf("已准入订阅不得重复处理：%+v %v", result, err)
	}
}

func TestFilterRepullTargets(t *testing.T) {
	now := nowFixed()
	recent := now.Add(-30 * time.Minute)
	old := now.Add(-3 * time.Hour)
	subs := []SubRepullInfo{
		{ID: 1, AutoRefresh: true, LastFetchStatus: "OK", LastFetchAt: &recent},
		{ID: 2, AutoRefresh: true, LastFetchStatus: "OK", LastFetchAt: &old},
		{ID: 3, AutoRefresh: true, LastFetchStatus: "FAILED", LastFetchAt: &recent},
		{ID: 4, AutoRefresh: false, LastFetchStatus: "OK", LastFetchAt: &old},
		{ID: 5, AutoRefresh: true},
	}
	// 手动口径：全量、无门（用户点了重拉就是要最新内容）
	targets, gated := FilterRepullTargets(subs, now, true)
	if len(targets) != 5 || len(gated) != 0 {
		t.Fatalf("手动重拉不受间隔门：targets=%v gated=%v", targets, gated)
	}
	// 自动口径：关闭自动更新的跳过；距上次成功拉取不足 2h 的整条跳过；失败的照常重试
	targets, gated = FilterRepullTargets(subs, now, false)
	if len(targets) != 3 || targets[0] != 2 || targets[1] != 3 || targets[2] != 5 {
		t.Fatalf("自动口径目标集不符：%v", targets)
	}
	if len(gated) != 1 || gated[0] != 1 {
		t.Fatalf("2h 门应拦下订阅 1：%v", gated)
	}
}

func TestCapacityClamps(t *testing.T) {
	// 验证容量 40 × 安全系数 0.8 → 建议生产上限 32
	if got := ProductionSafeCapacity(40); got != 32 {
		t.Fatalf("验证容量 40 的生产上限应为 32，实际 %d", got)
	}
	if got := ProductionSafeCapacity(0); got != 0 {
		t.Fatalf("零验证容量的生产上限应为 0，实际 %d", got)
	}
	if got := ProductionSafeCapacity(10); got != 8 {
		t.Fatalf("验证容量 10 的生产上限应为 8，实际 %d", got)
	}
	if MaxCrawlWorkers != 60 || MaxLanes != 120 {
		t.Fatalf("worker/lane 上限与 Python 常量漂移：%d/%d", MaxCrawlWorkers, MaxLanes)
	}
	laneCases := []struct {
		poolSize, requested, want int
	}{
		{0, -1, 0}, {0, 5, 0}, {50, -1, 50}, {50, 3, 3}, {50, 0, 0},
		{50, 200, 50}, {130, -1, 120}, {200, 500, 120}, {10, -1, 10},
	}
	for _, tc := range laneCases {
		if got := LaneCountFor(tc.poolSize, tc.requested); got != tc.want {
			t.Fatalf("LaneCountFor(%d, %d) = %d，期望 %d", tc.poolSize, tc.requested, got, tc.want)
		}
	}
}
