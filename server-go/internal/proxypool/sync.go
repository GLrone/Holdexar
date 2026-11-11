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
	"encoding/json"
	"errors"
	"strings"
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
	ID     int64
	URL    string
	Repull SubRepullInfo
}

// SyncResult 一轮同步的账目。
type SyncResult struct {
	StrategySwitched  bool
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

var syncSlot = make(chan struct{}, 1)

// ListSubscriptions 读订阅清单（闸外只读）。
func (s *Syncer) ListSubscriptions(ctx context.Context) ([]Subscription, error) {
	return listSyncSources(ctx, s.DB, 0)
}

// SyncSubscriptions 全量同步：并发抓取（闸外）→ 闸内一次提交。
func (s *Syncer) SyncSubscriptions(ctx context.Context) (*SyncResult, error) {
	return s.syncSubscriptions(ctx, false, 0)
}

func (s *Syncer) SyncAutomatic(ctx context.Context) (*SyncResult, error) {
	return s.syncSubscriptions(ctx, true, 0)
}

func (s *Syncer) SyncOne(ctx context.Context, id int64) (*SyncResult, error) {
	if id <= 0 {
		return nil, errors.New("订阅编号无效")
	}
	return s.syncSubscriptions(ctx, false, id)
}

func (s *Syncer) syncSubscriptions(ctx context.Context, automatic bool, onlyID int64) (*SyncResult, error) {
	var before Subscription
	if onlyID != 0 {
		subs, err := listSyncSources(ctx, s.DB, onlyID)
		if err != nil {
			return nil, err
		}
		if len(subs) == 1 {
			before = subs[0]
		}
	}
	select {
	case syncSlot <- struct{}{}:
		defer func() { <-syncSlot }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	s.fill()
	subs, err := s.ListSubscriptions(ctx)
	if err != nil {
		return nil, err
	}
	selected := subs[:0]
	now := s.Now()
	for _, sub := range subs {
		if onlyID != 0 && sub.ID != onlyID {
			continue
		}
		if automatic {
			targets, _ := FilterRepullTargets([]SubRepullInfo{sub.Repull}, now, false)
			if len(targets) == 0 {
				continue
			}
		}
		selected = append(selected, sub)
	}
	subs = selected
	result := &SyncResult{
		SubscriptionCount: len(subs),
		SnapshotSha256:    map[int64]string{},
		NodeCount:         map[int64]int{},
		Failures:          map[int64]string{},
		Snapshots:         map[int64]*Snapshot{},
	}
	if len(subs) == 0 {
		return result, nil
	}
	if onlyID != 0 && before.ID == onlyID && before.URL == subs[0].URL && subs[0].Repull.LastFetchStatus == "OK" {
		current := subs[0].Repull.LastFetchAt
		old := before.Repull.LastFetchAt
		if current != nil && (old == nil || !current.Equal(*old)) {
			conn, err := s.DB.Conn(ctx)
			if err != nil {
				return result, err
			}
			snap, restoreErr := RestoreLatestSnapshot(ctx, conn, s.DataDir, onlyID, OpenURL(s.DataDir, before.URL))
			conn.Close()
			if restoreErr == nil && snap != nil {
				result.SnapshotSha256[onlyID], result.NodeCount[onlyID], result.Snapshots[onlyID] = snap.Sha256, snap.NodeCount, snap
				return result, nil
			}
		}
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
			select {
			case fetchSlots <- struct{}{}:
			case <-ctx.Done():
				return
			}
			defer func() { <-fetchSlots }()
			res, err := s.Fetch(ctx, subURL, s.Channels, FetchOptions{})
			fetchedMu.Lock()
			fetched[subID] = FetchedOutcome{Result: res, Err: err, URL: subURL}
			fetchedMu.Unlock()
		}(sub.ID, url)
	}
	wg.Wait()
	if err := ctx.Err(); err != nil {
		return result, err
	}

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
			if sub.URL != live.URL {
				result.Failures[sub.ID] = messageURLChanged
				continue
			}
			if automatic && !live.Repull.AutoRefresh {
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
		if _, err := conn.ExecContext(ctx, "RELEASE sp_subscription_apply"); err != nil {
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
	if liveURL != subURL {
		workRollback()
		rollback()
		result.Failures[subID] = messageURLChanged
		return nil
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
	if err := projectAcceptedSubscription(ctx, conn, subID, snap, now); err != nil {
		workRollback()
		return fail(errMessage(err))
	}
	if _, err := conn.ExecContext(ctx, "RELEASE sp_subscription_work"); err != nil {
		return err
	}
	return finishOK(snap.Sha256, len(nodes), snap)
}

func (s *Syncer) fill() {
	if len(s.Channels) == 0 {
		s.Channels = BuildChannels("", nil)
	}
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
	return listSyncSources(ctx, conn, 0)
}

func listSyncSources(ctx context.Context, q queryable, id int64) ([]Subscription, error) {
	rows, err := q.QueryContext(ctx, `SELECT id, url, COALESCE(auto_refresh,1),
		COALESCE(last_fetch_status,''), CAST(last_fetch_at AS TEXT)
		FROM proxy_subscriptions WHERE kind='clash' AND (?=0 OR id=?) ORDER BY id`, id, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []Subscription
	for rows.Next() {
		var sub Subscription
		var fetched sql.NullString
		if err := rows.Scan(&sub.ID, &sub.URL, &sub.Repull.AutoRefresh, &sub.Repull.LastFetchStatus, &fetched); err != nil {
			return nil, err
		}
		sub.Repull.ID = sub.ID
		if at, err := parsePyDatetime(fetched.String); err == nil {
			sub.Repull.LastFetchAt = &at
		}
		out = append(out, sub)
	}
	return out, rows.Err()
}

func projectAcceptedSubscription(ctx context.Context, conn *sql.Conn, id int64, snap *Snapshot, now time.Time) error {
	var stored, label, deprecatedReason sql.NullString
	if err := conn.QueryRowContext(ctx, `SELECT CAST(last_stats AS TEXT),label,deprecated_reason FROM proxy_subscriptions WHERE id=?`, id).
		Scan(&stored, &label, &deprecatedReason); err != nil {
		return err
	}
	stats := map[string]any{}
	_ = json.Unmarshal([]byte(stored.String), &stats)
	if stats == nil {
		stats = map[string]any{}
	}
	stats["fetched"] = snap.NodeCount
	if snap.Userinfo != "" {
		stats["traffic"] = snap.Userinfo
	}
	title := truncateRunes(strings.TrimSpace(snap.ProfileTitle), 100)
	source := stats["labelSource"]
	if title != "" && source != "user" && (strings.TrimSpace(label.String) == "" || source == "panel" || label.String == title) {
		label = sql.NullString{String: title, Valid: true}
		stats["labelSource"] = "panel"
	}
	raw, err := json.Marshal(stats)
	if err != nil {
		return err
	}
	reason := deprecatedReason.String
	renewed := snap.Userinfo != "" && SubscriptionBlockReason(string(raw), now) == "" &&
		(strings.Contains(reason, "到期") || strings.Contains(reason, "流量已用尽"))
	_, err = conn.ExecContext(ctx, `UPDATE proxy_subscriptions SET last_stats=?, label=?, last_imported_at=?,
		deprecated=CASE WHEN ? THEN 0 ELSE deprecated END,
		deprecated_at=CASE WHEN ? THEN NULL ELSE deprecated_at END,
		deprecated_reason=CASE WHEN ? THEN NULL ELSE deprecated_reason END WHERE id=?`,
		string(raw), label, pyTimeText(now), renewed, renewed, renewed, id)
	return err
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
