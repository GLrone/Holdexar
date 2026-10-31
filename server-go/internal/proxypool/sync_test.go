package proxypool

import (
	"context"
	"database/sql"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

// newSyncer 带桩 Fetch 的编排器（桩记录并发峰值，可按 URL 定制结果）。
func newSyncer(t *testing.T, db *sql.DB, gate *store.WriteGate, dataDir string, fetch func(url string) (*FetchResult, error)) (*Syncer, *concurrencyProbe) {
	t.Helper()
	probe := &concurrencyProbe{}
	s := &Syncer{
		DB: db, Gate: gate, DataDir: dataDir,
		Channels: []Channel{{Name: ChannelDirect, EgressID: ChannelDirect}},
		Now: func() time.Time {
			return time.Date(2026, 10, 31, 12, 0, 0, 0, time.Local)
		},
		Fetch: func(ctx context.Context, url string, channels []Channel, opts FetchOptions) (*FetchResult, error) {
			return probe.enter(func() (*FetchResult, error) { return fetch(url) })
		},
	}
	return s, probe
}

type concurrencyProbe struct {
	mu      sync.Mutex
	current int
	peak    int
}

func (p *concurrencyProbe) enter(fn func() (*FetchResult, error)) (*FetchResult, error) {
	p.mu.Lock()
	p.current++
	if p.current > p.peak {
		p.peak = p.current
	}
	p.mu.Unlock()
	defer func() {
		p.mu.Lock()
		p.current--
		p.mu.Unlock()
	}()
	return fn()
}

func stubResult(url string) *FetchResult {
	raw := []byte(validSubscriptionBody)
	return &FetchResult{
		Raw: raw, HTTPStatus: 200, ContentType: "text/yaml",
		Channel: ChannelDirect, Fmt: FormatYAML,
		EgressID: ChannelDirect, DurationMS: 100,
		Attempts: []FetchAttempt{{Channel: ChannelDirect, OK: true, Fmt: FormatYAML, Stage: "complete", EgressID: ChannelDirect}},
	}
}

func addSubscription(t *testing.T, db *sql.DB, dataDir, url string) int64 {
	t.Helper()
	sealed, err := SealURL(dataDir, url)
	if err != nil {
		t.Fatal(err)
	}
	res, err := db.Exec(`INSERT INTO proxy_subscriptions (url) VALUES (?)`, sealed)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := res.LastInsertId()
	return id
}

func TestSyncAppliesSubscription(t *testing.T) {
	db, gate, dataDir := newTestDB(t)
	ctx := context.Background()
	subID := addSubscription(t, db, dataDir, "https://sub.example.com/a")
	s, probe := newSyncer(t, db, gate, dataDir, func(url string) (*FetchResult, error) {
		return stubResult(url), nil
	})
	res, err := s.SyncSubscriptions(ctx)
	if err != nil {
		t.Fatalf("同步失败：%v", err)
	}
	if probe.peak > MaxConcurrentSubscriptionFetches {
		t.Fatalf("跨订阅并发峰值 %d 超上限", probe.peak)
	}
	if len(res.Failures) != 0 || res.NodeCount[subID] != 1 {
		t.Fatalf("同步账目不符：%+v", res)
	}
	// Registry 对齐
	var nodeCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM proxy_nodes`).Scan(&nodeCount); err != nil {
		t.Fatal(err)
	}
	if nodeCount != 1 {
		t.Fatalf("Registry 应有 1 节点，got %d", nodeCount)
	}
	// 快照账本：OK 行 + 原始字节落盘
	var status, rawPath string
	var nodeCountSnap int
	if err := db.QueryRow(
		`SELECT status, raw_path, node_count FROM subscription_snapshots WHERE subscription_id = ?`,
		subID).Scan(&status, &rawPath, &nodeCountSnap); err != nil {
		t.Fatal(err)
	}
	if status != "OK" || nodeCountSnap != 1 {
		t.Fatalf("快照行不符：status=%s nodes=%d", status, nodeCountSnap)
	}
	raw, err := os.ReadFile(rawPath)
	if err != nil {
		t.Fatalf("原始字节应落盘：%v", err)
	}
	if string(raw) != validSubscriptionBody {
		t.Fatalf("落盘字节应与下载内容一致")
	}
	// 订阅行投影
	var fetchStatus string
	var lastError sql.NullString
	if err := db.QueryRow(
		`SELECT last_fetch_status, last_error FROM proxy_subscriptions WHERE id = ?`,
		subID).Scan(&fetchStatus, &lastError); err != nil {
		t.Fatal(err)
	}
	if fetchStatus != "OK" || lastError.Valid {
		t.Fatalf("订阅行应记 OK：%s %v", fetchStatus, lastError)
	}
}

func TestSyncFailureKeepsLastGoodSnapshot(t *testing.T) {
	db, gate, dataDir := newTestDB(t)
	ctx := context.Background()
	subID := addSubscription(t, db, dataDir, "https://sub.example.com/a")
	calls := 0
	s, _ := newSyncer(t, db, gate, dataDir, func(url string) (*FetchResult, error) {
		calls++
		if calls == 1 {
			return stubResult(url), nil
		}
		return nil, &FetchFailedError{
			URL: url,
			Attempts: []FetchAttempt{
				{Channel: ChannelDirect, Fmt: FormatUnknown, Error: "request_timeout", Stage: "timeout"},
			},
			DurationMS: 24000,
		}
	})
	if _, err := s.SyncSubscriptions(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := s.SyncSubscriptions(ctx); err != nil {
		t.Fatal(err)
	}
	// 成功快照不被失败覆盖：OK 行恰 1 条（sha 仍指向第一轮内容）
	var okCount int
	if err := db.QueryRow(
		`SELECT COUNT(*) FROM subscription_snapshots WHERE subscription_id = ? AND status = 'OK'`,
		subID).Scan(&okCount); err != nil {
		t.Fatal(err)
	}
	if okCount != 1 {
		t.Fatalf("失败不得新增 OK 快照行，got %d", okCount)
	}
	// 失败留痕行在场
	var failedCount int
	if err := db.QueryRow(
		`SELECT COUNT(*) FROM subscription_snapshots WHERE subscription_id = ? AND status = 'FAILED'`,
		subID).Scan(&failedCount); err != nil {
		t.Fatal(err)
	}
	if failedCount != 1 {
		t.Fatalf("失败应留痕 1 行，got %d", failedCount)
	}
	var lastError string
	if err := db.QueryRow(`SELECT last_error FROM proxy_subscriptions WHERE id = ?`, subID).Scan(&lastError); err != nil {
		t.Fatal(err)
	}
	if lastError == "" {
		t.Fatal("订阅行应带失败原因")
	}
	// 失败不触碰 Registry
	var nodeCount int
	if err := db.QueryRow(`SELECT COUNT(*) FROM proxy_nodes`).Scan(&nodeCount); err != nil {
		t.Fatal(err)
	}
	if nodeCount != 1 {
		t.Fatalf("失败轮不得改 Registry，节点数 %d", nodeCount)
	}
}

func TestSyncURLChangedGuard(t *testing.T) {
	db, gate, dataDir := newTestDB(t)
	ctx := context.Background()
	subID := addSubscription(t, db, dataDir, "https://sub.example.com/a")
	s, _ := newSyncer(t, db, gate, dataDir, func(url string) (*FetchResult, error) {
		// 下载期间订阅换了地址（模拟另一会话改绑）：闸内现读应拦下
		sealed, err := SealURL(dataDir, "https://sub.example.com/ROTATED")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := db.Exec(`UPDATE proxy_subscriptions SET url = ? WHERE id = ?`, sealed, subID); err != nil {
			t.Fatal(err)
		}
		return stubResult(url), nil
	})
	res, err := s.SyncSubscriptions(ctx)
	if err != nil {
		t.Fatalf("编排不得因 URL 变化整体失败：%v", err)
	}
	if res.Failures[subID] != messageURLChanged {
		t.Fatalf("应按 URL 变化记账：%v", res.Failures)
	}
	var okCount int
	if err := db.QueryRow(
		`SELECT COUNT(*) FROM subscription_snapshots WHERE status = 'OK'`).Scan(&okCount); err != nil {
		t.Fatal(err)
	}
	if okCount != 0 {
		t.Fatalf("换址结果不得落 OK 快照，got %d", okCount)
	}
}

func TestSyncSnapshotRestoreRoundTrip(t *testing.T) {
	db, gate, dataDir := newTestDB(t)
	ctx := context.Background()
	subID := addSubscription(t, db, dataDir, "https://sub.example.com/a")
	s, _ := newSyncer(t, db, gate, dataDir, func(url string) (*FetchResult, error) {
		return stubResult(url), nil
	})
	if _, err := s.SyncSubscriptions(ctx); err != nil {
		t.Fatal(err)
	}
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		snap, err := RestoreLatestSnapshot(ctx, conn, dataDir, subID, "https://sub.example.com/a")
		if err != nil {
			return err
		}
		if snap == nil {
			t.Fatal("应恢复出快照")
		}
		if snap.URL != "https://sub.example.com/a" || snap.NodeCount != 1 || len(snap.Nodes) != 1 {
			t.Fatalf("恢复元数据不符：%+v", snap)
		}
		if snap.Nodes[0]["name"] != "测试节点" {
			t.Fatalf("恢复节点不符：%v", snap.Nodes[0])
		}
		if len(snap.Attempts) != 1 || !snap.Attempts[0].OK {
			t.Fatalf("恢复尝试留痕不符：%+v", snap.Attempts)
		}
		// 换链接后旧快照不得被选中
		none, err := RestoreLatestSnapshot(ctx, conn, dataDir, subID, "https://other.example.com/x")
		if err != nil || none != nil {
			t.Fatalf("异 URL 查询应返回空：%v %v", none, err)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("恢复链失败：%v", err)
	}
}

func TestSyncDeletedSubscriptionDuringFetch(t *testing.T) {
	db, gate, dataDir := newTestDB(t)
	ctx := context.Background()
	subID := addSubscription(t, db, dataDir, "https://sub.example.com/a")
	s, _ := newSyncer(t, db, gate, dataDir, func(url string) (*FetchResult, error) {
		if _, err := db.Exec(`DELETE FROM proxy_subscriptions WHERE id = ?`, subID); err != nil {
			t.Fatal(err)
		}
		return stubResult(url), nil
	})
	res, err := s.SyncSubscriptions(ctx)
	if err != nil {
		t.Fatalf("编排不得整体失败：%v", err)
	}
	if res.Failures[subID] != "订阅在刷新期间已删除，本次结果未应用" {
		t.Fatalf("删除订阅应记账：%v", res.Failures)
	}
}

func TestUnreadableURLPath(t *testing.T) {
	db, gate, dataDir := newTestDB(t)
	ctx := context.Background()
	// 写入一段伪密文（IsEncrypted 命中但解密必败）
	res, err := db.Exec(`INSERT INTO proxy_subscriptions (url) VALUES (?)`, "enc1:not-a-real-token")
	if err != nil {
		t.Fatal(err)
	}
	subID, _ := res.LastInsertId()
	s, probe := newSyncer(t, db, gate, dataDir, func(url string) (*FetchResult, error) {
		t.Fatal("解不开的链接不得发起下载")
		return nil, nil
	})
	if _, err := s.SyncSubscriptions(ctx); err != nil {
		t.Fatal(err)
	}
	if probe.peak != 0 {
		t.Fatal("不可读链接不应进入下载段")
	}
	var lastError string
	if err := db.QueryRow(`SELECT last_error FROM proxy_subscriptions WHERE id = ?`, subID).Scan(&lastError); err != nil {
		t.Fatal(err)
	}
	if lastError != UnreadableMessage {
		t.Fatalf("应记用户语言提示：%q", lastError)
	}
}

func TestSyncRawOrphanOnRollbackKeepsFileIdempotent(t *testing.T) {
	// 内容寻址落盘幂等：同一内容二次写不重复落文件
	dataDir := t.TempDir()
	path, err := writeRaw(dataDir, 3, "abc", []byte("hello"))
	if err != nil {
		t.Fatal(err)
	}
	path2, err := writeRaw(dataDir, 3, "abc", []byte("hello"))
	if err != nil {
		t.Fatal(err)
	}
	if path != path2 {
		t.Fatalf("同内容应同路径：%s vs %s", path, path2)
	}
	if filepath.Base(path) != "abc.bin" {
		t.Fatalf("内容寻址命名不符：%s", path)
	}
}
