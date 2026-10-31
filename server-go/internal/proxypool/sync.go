package proxypool

// 订阅同步编排（Python bootstrap.sync_subscriptions 本批范围）：
// 订阅 → 快照 → Registry。Runtime 层不参与，也拿不到订阅 URL。
// 铁律：并发下载（纯网络）在写闸外完成；闸内只允许落库 I/O（含快照原始
// 字节的内容寻址落盘，属本轮流库伴生）与收尾提交——网络等待进闸会横持
// 写者位，饿死全体写。闸内先重读订阅状态再应用；提交在闸内完成，闸界与
// SQLite 文件锁界对齐。

import (
	"context"
	"database/sql"
	"errors"
	"sync"
	"time"

	"holdexar/server-go/internal/store"
)

// MaxConcurrentSubscriptionFetches 跨订阅并发上限（有界并发下载，落库仍串行）。
const MaxConcurrentSubscriptionFetches = 5

// 订阅行 URL 在闸内变化时的拒因。
const messageURLChanged = "订阅地址在刷新期间发生变化，本次结果未应用"

// Subscription 同步所需的最小订阅行投影（URL 密文，开封在消费点）。
type Subscription struct {
	ID  int64
	URL string
}

// SyncResult 一轮同步的账目。
type SyncResult struct {
	SubscriptionCount int
	SnapshotSha256    map[int64]string
	NodeCount         map[int64]int
	Failures          map[int64]string
	SkippedCandidates []int64
	Snapshots         map[int64]*Snapshot
}

// FetchedOutcome 单订阅的闸外抓取产物（URL = 下载时的明文地址）。
type FetchedOutcome struct {
	Result *FetchResult
	Err    error
	URL    string
}

// Syncer 编排器。
type Syncer struct {
	DB                *sql.DB
	Gate              *store.WriteGate
	DataDir           string
	Channels          []Channel
	MaxConcurrentSubs int
	Now               func() time.Time
	Fetch             func(ctx context.Context, url string, channels []Channel, opts FetchOptions) (*FetchResult, error)
}

var syncMutex sync.Mutex

// ListSubscriptions 读订阅清单（闸外只读）。
func (s *Syncer) ListSubscriptions(ctx context.Context) ([]Subscription, error) {
	rows, err := s.DB.QueryContext(ctx,
		`SELECT id, url FROM proxy_subscriptions ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Subscription
	for rows.Next() {
		var sub Subscription
		if err := rows.Scan(&sub.ID, &sub.URL); err != nil {
			return nil, err
		}
		out = append(out, sub)
	}
	return out, rows.Err()
}

// SyncSubscriptions 全量同步：并发抓取（闸外）→ 闸内一次提交。
func (s *Syncer) SyncSubscriptions(ctx context.Context) (*SyncResult, error) {
	syncMutex.Lock()
	defer syncMutex.Unlock()
	s.fill()
	subs, err := s.ListSubscriptions(ctx)
	if err != nil {
		return nil, err
	}
	result := &SyncResult{
		SubscriptionCount: len(subs),
		SnapshotSha256:    map[int64]string{},
		NodeCount:         map[int64]int{},
		Failures:          map[int64]string{},
		Snapshots:         map[int64]*Snapshot{},
	}

	// 有界并发下载（纯 I/O），落库仍串行（SQLite 单写者）
	fetchSlots := make(chan struct{}, maxInt(s.MaxConcurrentSubs, 1))
	fetched := map[int64]FetchedOutcome{}
	var fetchedMu sync.Mutex
	var wg sync.WaitGroup
	for _, sub := range subs {
		url := OpenURL(s.DataDir, sub.URL)
		if url == "" {
			continue
		}
		wg.Add(1)
		go func(subID int64, subURL string) {
			defer wg.Done()
			fetchSlots <- struct{}{}
			defer func() { <-fetchSlots }()
			res, err := s.Fetch(ctx, subURL, s.Channels, FetchOptions{})
			fetchedMu.Lock()
			fetched[subID] = FetchedOutcome{Result: res, Err: err, URL: subURL}
			fetchedMu.Unlock()
		}(sub.ID, url)
	}
	wg.Wait()

	// 落库段进闸：WithWrite 三合一（闸 + 单连接 + BEGIN IMMEDIATE/COMMIT），
	// 回调内失败整体回滚；单订阅半截写入由 SAVEPOINT 隔离。
	err = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := EnsureSchema(ctx, conn); err != nil {
			return err
		}
		current, err := s.ListSubscriptionsConn(ctx, conn)
		if err != nil {
			return err
		}
		currentByID := map[int64]Subscription{}
		for _, sub := range current {
			currentByID[sub.ID] = sub
		}
		now := s.Now()
		for _, sub := range subs {
			live, still := currentByID[sub.ID]
			if !still {
				result.Failures[sub.ID] = "订阅在刷新期间已删除，本次结果未应用"
				continue
			}
			if err := s.applyOne(ctx, conn, live, fetched[sub.ID], now, result); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return result, err
	}
	return result, nil
}

// applyOne 单订阅闸内应用：SAVEPOINT 隔离，本条半截写入只回滚自己。
func (s *Syncer) applyOne(ctx context.Context, conn *sql.Conn, sub Subscription, item FetchedOutcome, now time.Time, result *SyncResult,
) error {
	if _, err := conn.ExecContext(ctx, "SAVEPOINT sp_subscription_apply"); err != nil {
		return err
	}
	rollback := func() {
		_, _ = conn.ExecContext(ctx, "ROLLBACK TO sp_subscription_apply")
		_, _ = conn.ExecContext(ctx, "RELEASE sp_subscription_apply")
	}
	subID := sub.ID
	// subURL 是**下载时**的地址；闸内现值与它比较才是换址守卫
	subURL := item.URL
	liveURL := OpenURL(s.DataDir, sub.URL)
	finishOK := func(sha string, count int, snap *Snapshot) error {
		identity := SubscriptionURLIdentity(subURL)
		if _, err := conn.ExecContext(ctx,
			`UPDATE proxy_subscriptions SET last_fetch_at = ?, last_fetch_status = 'OK',
			 last_success_at = ?, last_error = NULL, cache_url_identity = ? WHERE id = ?`,
			pyTimeText(now), pyTimeText(now), identity, subID); err != nil {
			return err
		}
		result.SnapshotSha256[subID] = sha
		result.NodeCount[subID] = count
		if snap != nil {
			result.Snapshots[subID] = snap
		}
		return nil
	}
	// 只有下载异常带通道尝试留痕（对齐 Python：getattr(fetch_error, "attempts", [])）
	failureEvidence := func() ([]FetchAttempt, int) {
		if item.Err == nil {
			return nil, 0
		}
		var ffe *FetchFailedError
		var ufe *UnsupportedFormatError
		var ene *EmptyNodeSetError
		var iee *InvalidEncodingError
		switch {
		case errors.As(item.Err, &ffe):
			return ffe.Attempts, ffe.DurationMS
		case errors.As(item.Err, &ufe):
			return ufe.Attempts, ufe.DurationMS
		case errors.As(item.Err, &ene):
			return ene.Attempts, ene.DurationMS
		case errors.As(item.Err, &iee):
			return iee.Attempts, iee.DurationMS
		}
		return nil, 0
	}
	fail := func(message string) error {
		rollback()
		// 失败留痕落在外层事务（savepoint 已回滚半截写入）
		attempts, durationMS := failureEvidence()
		if _, err := conn.ExecContext(ctx, "SAVEPOINT sp_subscription_fail"); err != nil {
			return err
		}
		if item.Err != nil && subURL != "" {
			if err := PersistFetchFailure(ctx, conn, FailureRecord{
				SubscriptionID: subID, DataDir: s.DataDir, URL: subURL,
				Attempts: attempts, Error: message, DurationMS: durationMS, Now: now,
			}); err != nil {
				// 失败明细保存失败不阻断原始失败投影（对齐 Python：仅记日志继续）
				_, _ = conn.ExecContext(ctx, "ROLLBACK TO sp_subscription_fail")
			}
		}
		if _, err := conn.ExecContext(ctx,
			`UPDATE proxy_subscriptions SET last_fetch_at = ?, last_fetch_status = 'FAILED',
			 last_error = ? WHERE id = ?`, pyTimeText(now), truncateRunes(message, 500), subID); err != nil {
			_, _ = conn.ExecContext(ctx, "ROLLBACK TO sp_subscription_fail")
			_, _ = conn.ExecContext(ctx, "RELEASE sp_subscription_fail")
			return err
		}
		result.Failures[subID] = message
		_, _ = conn.ExecContext(ctx, "RELEASE sp_subscription_fail")
		return nil
	}

	if _, err := conn.ExecContext(ctx, "SAVEPOINT sp_subscription_work"); err != nil {
		return err
	}
	workRollback := func() {
		_, _ = conn.ExecContext(ctx, "ROLLBACK TO sp_subscription_work")
		_, _ = conn.ExecContext(ctx, "RELEASE sp_subscription_work")
	}
	if subURL == "" {
		// 链接解不开（密钥属主机绑定）：本轮跳过并留原因，不伪装成抓取失败
		workRollback()
		return fail(UnreadableMessage)
	}
	if item.Result == nil && item.Err == nil {
		// 预取缺失（不可达）：按失败记账，不静默丢
		workRollback()
		return fail("订阅下载未返回结果")
	}
	if item.Err != nil {
		workRollback()
		return fail(errMessage(item.Err))
	}
	// 应用前现读订阅行：抓取可能耗时几十秒，期间地址可能已换——旧链接的
	// 下载结果不得应用到新地址名下（本分支只记账，不动订阅行）
	if liveURL != subURL {
		workRollback()
		result.Failures[subID] = messageURLChanged
		return nil
	}
	res := item.Result
	nodes := res.ResolvedNodes
	if len(nodes) == 0 {
		parsed, perr := ParseNodes(res.Raw, res.Fmt)
		if perr != nil {
			workRollback()
			return fail(errMessage(perr))
		}
		nodes = parsed
	}
	snap := BuildSnapshot(subID, subURL, res, nodes, now)
	if _, err := PersistSnapshot(ctx, conn, s.DataDir, snap); err != nil {
		workRollback()
		return fail(errMessage(err))
	}
	tag := SubscriptionTag(subURL)
	if _, err := ApplySnapshot(ctx, conn, subID, snap, now, tag); err != nil {
		workRollback()
		return fail(errMessage(err))
	}
	if _, err := conn.ExecContext(ctx, "RELEASE sp_subscription_work"); err != nil {
		return err
	}
	return finishOK(snap.Sha256, len(nodes), snap)
}

func (s *Syncer) fill() {
	if s.MaxConcurrentSubs <= 0 {
		s.MaxConcurrentSubs = MaxConcurrentSubscriptionFetches
	}
	if s.Now == nil {
		s.Now = time.Now
	}
	if s.Fetch == nil {
		s.Fetch = func(ctx context.Context, url string, channels []Channel, opts FetchOptions) (*FetchResult, error) {
			return FetchSubscription(ctx, url, channels, opts)
		}
	}
}

// ListSubscriptionsConn 事务内读订阅清单（闸内重读用）。
func (s *Syncer) ListSubscriptionsConn(ctx context.Context, conn *sql.Conn) ([]Subscription, error) {
	rows, err := conn.QueryContext(ctx, `SELECT id, url FROM proxy_subscriptions ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Subscription
	for rows.Next() {
		var sub Subscription
		if err := rows.Scan(&sub.ID, &sub.URL); err != nil {
			return nil, err
		}
		out = append(out, sub)
	}
	return out, rows.Err()
}

// BuildSnapshot 抓取产物 → 不可变快照。
func BuildSnapshot(subscriptionID int64, url string, result *FetchResult, nodes []map[string]any, now time.Time) *Snapshot {
	return &Snapshot{
		SubscriptionID:     subscriptionID,
		URL:                url,
		FetchedAt:          now,
		HTTPStatus:         result.HTTPStatus,
		ContentType:        result.ContentType,
		Sha256:             sha256Hex(result.Raw),
		RawContent:         result.Raw,
		Fmt:                result.Fmt,
		NodeCount:          len(nodes),
		SourceChannel:      result.Channel,
		Nodes:              nodes,
		Attempts:           result.Attempts,
		SourceEgress:       result.EgressID,
		ProfileTitle:       result.ProfileTitle,
		ContentDisposition: result.ContentDisposition,
		Userinfo:           result.Userinfo,
		DurationMS:         result.DurationMS,
	}
}

func errMessage(err error) string {
	msg := truncateRunes(err.Error(), 500)
	if msg == "" {
		return err.Error()
	}
	return msg
}
