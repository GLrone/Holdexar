package bills

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"maps"
	"net/url"
	"sync"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const syncKey = "bills.sync_snapshot"

func snapshotKey(sid string) string { return syncKey + ":" + sid }

var billZone = time.FixedZone("Beijing", 8*60*60)

type SyncOptions struct {
	Accounts *account.Service
	Steam    *account.SteamClient
	Recorder *jobrun.Recorder
	Now      func() time.Time
	Log      func(string, ...any)
	base     *url.URL
	sleep    func(context.Context, time.Duration) error
}

type Service struct {
	db     *sql.DB
	gate   *store.WriteGate
	opts   SyncOptions
	life   context.Context
	cancel context.CancelFunc

	mu       sync.Mutex
	jobs     sync.WaitGroup
	closing  bool
	owners   map[string]*billSyncLock
	bindings map[string]bool // A live worker consumes one pending follow-up.
}

type billSyncLock struct {
	slot chan struct{}
	refs int
}

func NewService(parent context.Context, db *sql.DB, gate *store.WriteGate, opts SyncOptions) *Service {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	if opts.base == nil {
		opts.base, _ = url.Parse(steamStoreURL)
	}
	life, cancel := context.WithCancel(parent)
	return &Service{db: db, gate: gate, opts: opts, life: life, cancel: cancel,
		owners: make(map[string]*billSyncLock), bindings: make(map[string]bool)}
}

func (s *Service) work(parent context.Context) (context.Context, func(), error) {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return nil, nil, context.Canceled
	}
	s.jobs.Add(1)
	s.mu.Unlock()
	ctx, cancel := context.WithCancel(parent)
	stop := context.AfterFunc(s.life, cancel)
	return ctx, func() { stop(); cancel(); s.jobs.Done() }, nil
}

func (s *Service) Close(ctx context.Context) error {
	s.mu.Lock()
	s.closing = true
	s.cancel()
	s.mu.Unlock()
	done := make(chan struct{})
	go func() { s.jobs.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (s *Service) RequestSync(sid string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closing || s.life.Err() != nil || sid == "" {
		return false
	}
	if _, queued := s.bindings[sid]; queued {
		s.bindings[sid] = true
		return true
	}
	s.bindings[sid] = false
	s.jobs.Add(1)
	go s.drain(sid)
	return true
}

func (s *Service) drain(sid string) {
	defer s.jobs.Done()
	for {
		release, err := s.acquireSync(s.life, sid, true)
		if err == nil {
			s.mu.Lock()
			s.bindings[sid] = false
			s.mu.Unlock()
			_, err = s.syncOwner(s.life, sid, true)
			release()
		}
		if err != nil && s.life.Err() == nil {
			s.opts.Log("绑定后的账单同步未完成 │ SteamID=%s，原因=%v", sid, err)
		}
		s.mu.Lock()
		if !s.bindings[sid] || s.life.Err() != nil {
			delete(s.bindings, sid)
			s.mu.Unlock()
			return
		}
		s.mu.Unlock()
	}
}

func (s *Service) acquireSync(ctx context.Context, sid string, wait bool) (func(), error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	s.mu.Lock()
	lock := s.owners[sid]
	if lock == nil {
		lock = &billSyncLock{slot: make(chan struct{}, 1)}
		s.owners[sid] = lock
	}
	lock.refs++
	s.mu.Unlock()
	unref := func() {
		s.mu.Lock()
		lock.refs--
		if lock.refs == 0 {
			delete(s.owners, sid)
		}
		s.mu.Unlock()
	}
	if wait {
		select {
		case lock.slot <- struct{}{}:
		case <-ctx.Done():
			unref()
			return nil, ctx.Err()
		}
	} else {
		select {
		case lock.slot <- struct{}{}:
		default:
			unref()
			return nil, nil
		}
	}
	return func() { <-lock.slot; unref() }, nil
}

func (s *Service) Status(ctx context.Context) (map[string]any, error) {
	s.mu.Lock()
	active := make(map[string]bool, len(s.owners))
	for sid, lock := range s.owners {
		if len(lock.slot) != 0 {
			active[sid] = true
		}
	}
	s.mu.Unlock()
	rows, err := s.db.QueryContext(ctx, `
SELECT a.steam_id, COALESCE(owned.value_json, legacy.value_json)
FROM steam_accounts a
LEFT JOIN app_settings owned ON owned.key = ? || a.steam_id
LEFT JOIN app_settings legacy ON legacy.key = ?
  AND a.steam_id = CASE WHEN json_valid(legacy.value_json) THEN json_extract(legacy.value_json,'$.steamId') END
ORDER BY a.bound_at, a.steam_id`, snapshotKey(""), syncKey)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var picked map[string]any
	var pickedAt string
	var running, pickedActive bool
	for rows.Next() {
		var sid string
		var value sql.NullString
		if err := rows.Scan(&sid, &value); err != nil {
			return nil, err
		}
		isActive := active[sid]
		running = running || isActive
		var snapshot map[string]any
		if !value.Valid || json.Unmarshal([]byte(value.String), &snapshot) != nil || len(snapshot) == 0 {
			continue
		}
		if owner := snapshot["steamId"]; owner != nil && owner != sid {
			continue
		}
		stamp, _ := snapshot["syncedAt"].(string)
		if stamp == "" {
			stamp, _ = snapshot["since"].(string)
		}
		if picked == nil || (isActive && !pickedActive) || (isActive == pickedActive && stamp > pickedAt) {
			snapshot["steamId"] = sid
			picked, pickedAt, pickedActive = snapshot, stamp, isActive
		}
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if picked == nil {
		picked = map[string]any{}
	}
	picked["running"] = running
	return picked, nil
}

func (s *Service) snapshot(ctx context.Context, session account.StoreSession, value map[string]any) error {
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := session.Check(ctx, conn); err != nil {
			return err
		}
		return settings.WriteValue(ctx, conn, snapshotKey(session.SteamID), value)
	})
}

func (s *Service) latestDate(ctx context.Context, sid string) (string, error) {
	var date string
	err := s.db.QueryRowContext(ctx, `WITH owned AS (SELECT id FROM bill_imports WHERE steam_id=?)
SELECT COALESCE((SELECT MAX(NULLIF(date,'')) FROM bill_game_txs WHERE import_id IN owned),
 (SELECT MAX(NULLIF(date,'')) FROM bill_topup_txs WHERE import_id IN owned),
 (SELECT MAX(NULLIF(date,'')) FROM bill_cdk_games WHERE import_id IN owned),'')`, sid).Scan(&date)
	return date, err
}

func (s *Service) fetch(ctx context.Context, session account.StoreSession, known string, progress func(map[string]any)) (steamReport, bool, error) {
	transport, err := s.opts.Steam.StoreHTTPClient(ctx)
	if err != nil {
		return steamReport{}, false, err
	}
	client, err := newBillSteamClient(s.opts.base, transport, session.Cookie, s.opts.sleep)
	if err != nil {
		return steamReport{}, false, err
	}
	first, unchanged, err := client.probe(ctx, known)
	if err != nil || unchanged {
		return steamReport{}, unchanged, err
	}
	report, err := client.report(ctx, session.SteamID, first, progress)
	return report, false, err
}

func (s *Service) Sync(parent context.Context, sid string, force bool) (map[string]any, error) {
	ctx, done, err := s.work(parent)
	if err != nil {
		return nil, err
	}
	defer done()
	return s.sync(ctx, sid, force)
}

func (s *Service) sync(ctx context.Context, sid string, force bool) (map[string]any, error) {
	explicit := sid != ""
	if !explicit {
		err := s.db.QueryRowContext(ctx, `
SELECT steam_id FROM steam_accounts ORDER BY is_active DESC, bound_at, steam_id LIMIT 1`).Scan(&sid)
		if errors.Is(err, sql.ErrNoRows) {
			return map[string]any{"ok": false, "status": "no_cookie"}, nil
		}
		if err != nil {
			return nil, err
		}
	}
	release, err := s.acquireSync(ctx, sid, explicit)
	if err != nil {
		return nil, err
	}
	if release == nil {
		return map[string]any{"ok": false, "status": "busy"}, nil
	}
	defer release()
	return s.syncOwner(ctx, sid, force)
}

func (s *Service) syncOwner(ctx context.Context, sid string, force bool) (map[string]any, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	session, err := s.opts.Accounts.StoreSession(ctx, sid, false)
	if errors.Is(err, account.ErrAccountNotFound) || errors.Is(err, account.ErrNoStoreCredentials) {
		return map[string]any{"ok": false, "status": "no_cookie"}, nil
	}
	at := s.opts.Now().In(billZone)
	stamp := PyIsoformat(at.Format("2006-01-02 15:04:05.000000"))
	if err == nil && (session.SteamID != sid || len(sid) != 17) {
		err = errors.New("无效的账单账号标识")
	}
	if err != nil {
		if session.SteamID == sid {
			s.recordFailure(ctx, sid, stamp, err)
		}
		return nil, err
	}
	start := map[string]any{"running": true, "steamId": sid, "since": stamp}
	if err = s.snapshot(ctx, session, start); err != nil {
		return nil, err
	}
	result, err := s.syncReport(ctx, session, force, at, stamp, start)
	if err == nil {
		return result, nil
	}
	s.recordFailure(ctx, sid, stamp, err)
	return nil, err
}

func (s *Service) recordFailure(ctx context.Context, sid, stamp string, err error) {
	message := err.Error()
	if ctx.Err() != nil {
		message = "账单同步已中断"
	}
	finish, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
	defer cancel()
	value := map[string]any{"ok": false, "running": false, "error": message, "syncedAt": stamp, "steamId": sid}
	if saveErr := s.gate.WithWrite(finish, store.PriorityBackground, func(conn *sql.Conn) error {
		var bound bool
		if err := conn.QueryRowContext(finish, `SELECT EXISTS(SELECT 1 FROM steam_accounts WHERE steam_id=?)`, sid).Scan(&bound); err != nil {
			return err
		}
		if !bound {
			_, err := conn.ExecContext(finish, `DELETE FROM app_settings WHERE key=? OR
 (key=? AND CASE WHEN json_valid(value_json) THEN json_extract(value_json,'$.steamId') END=?)`, snapshotKey(sid), syncKey, sid)
			return err
		}
		return settings.WriteValue(finish, conn, snapshotKey(sid), value)
	}); saveErr != nil {
		s.opts.Log("账单同步状态未保存 │ SteamID=%s，原因=%v", sid, saveErr)
	}
}

func (s *Service) syncReport(ctx context.Context, session account.StoreSession, force bool, at time.Time, stamp string, start map[string]any) (map[string]any, error) {
	sid := session.SteamID
	known := ""
	if !force {
		var err error
		known, err = s.latestDate(ctx, sid)
		if err != nil {
			return nil, err
		}
	}
	progress := func(info map[string]any) {
		value := maps.Clone(start)
		maps.Copy(value, info)
		if err := s.snapshot(ctx, session, value); err != nil && ctx.Err() == nil {
			s.opts.Log("账单同步进度未保存 │ SteamID=%s，原因=%v", sid, err)
		}
	}
	report, unchanged, err := s.fetch(ctx, session, known, progress)
	if errors.Is(err, errStoreExpired) {
		fresh, renewErr := s.opts.Accounts.StoreSession(ctx, sid, true)
		if renewErr != nil {
			return nil, renewErr
		}
		if fresh.Cookie != session.Cookie {
			session = fresh
			report, unchanged, err = s.fetch(ctx, session, known, progress)
		}
	}
	if err != nil {
		return nil, err
	}
	var parsed parsedReport
	if !unchanged {
		progress(map[string]any{"stage": "import"})
		parsed = parseReport(report)
	}
	var result map[string]any
	err = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := session.Check(ctx, conn); err != nil {
			return err
		}
		if unchanged {
			result = map[string]any{"ok": true, "status": "probed_skip", "steamId": sid, "historyRows": 0, "licenseRows": 0}
			return settings.WriteValue(ctx, conn, snapshotKey(sid), map[string]any{"running": false, "probedSkip": true, "syncedAt": stamp, "steamId": sid})
		}
		var err error
		result, err = importBill(ctx, conn, sid, parsed, at)
		if err != nil {
			return err
		}
		result["ok"], result["historyRows"], result["licenseRows"] = true, len(report.History), len(report.Licenses)
		snapshot := maps.Clone(result)
		snapshot["running"], snapshot["syncedAt"] = false, stamp
		return settings.WriteValue(ctx, conn, snapshotKey(sid), snapshot)
	})
	if err != nil {
		return nil, err
	}
	if !unchanged {
		s.opts.Log("Steam 账单同步完成 │ SteamID=%s，消费=%d，许可=%d", sid, len(report.History), len(report.Licenses))
	}
	return result, nil
}

func (s *Service) round(ctx context.Context) (map[string]any, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT steam_id FROM steam_accounts ORDER BY bound_at,steam_id`)
	if err != nil {
		return nil, err
	}
	ids := []string{}
	for rows.Next() {
		var sid string
		if err = rows.Scan(&sid); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, sid)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	if len(ids) == 0 {
		return map[string]any{"accounts": 0, "skipped": "未绑定 Steam Cookie"}, nil
	}
	var jobs sync.WaitGroup
	results := make([]string, len(ids))
	for i, sid := range ids {
		jobs.Go(func() {
			result, err := s.sync(ctx, sid, false)
			switch {
			case err != nil:
				results[i] = "failed"
				if ctx.Err() == nil {
					s.opts.Log("Steam 账单同步未完成，下轮重试 │ SteamID=%s，原因=%v", sid, err)
				}
			case result["status"] == "probed_skip" || result["status"] == "no_cookie":
				results[i] = "skipped"
			default:
				results[i] = "synced"
			}
		})
	}
	jobs.Wait()
	synced, failed, skipped := 0, 0, 0
	for _, status := range results {
		switch status {
		case "failed":
			failed++
		case "skipped":
			skipped++
		default:
			synced++
		}
	}
	return map[string]any{"accounts": len(ids), "synced": synced, "failed": failed, "skipped": skipped}, ctx.Err()
}

func (s *Service) RegisterSchedules(ctx context.Context, clock *sched.Scheduler) error {
	return clock.RegisterInterval("bills_sync", s.opts.Now().Add(2*time.Minute), 30*time.Minute, func(*sched.RunCtx) {
		work, done, err := s.work(ctx)
		if err != nil {
			return
		}
		defer done()
		body := func() (map[string]any, error) { return s.round(work) }
		if s.opts.Recorder != nil {
			_, err = s.opts.Recorder.Run(work, "bills_sync", body)
		} else {
			_, err = body()
		}
		if err != nil && work.Err() == nil {
			s.opts.Log("Steam 账单定时同步异常 │ 原因=%v", err)
		}
	})
}
