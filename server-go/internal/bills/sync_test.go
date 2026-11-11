package bills

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"net/url"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

func TestBillSyncBindingScheduleAndAtomicReplacement(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	const a, b = "76561198000000001", "76561198000000002"
	var mode, aPages, bPages atomic.Int32
	blocked, resume := make(chan struct{}, 1), make(chan struct{})
	parallel, advance := make(chan struct{}), make(chan struct{})
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, _ := r.Cookie("steamLoginSecure")
		owner := ""
		if c != nil {
			switch c.Value {
			case a + "||alpha":
				owner = a
			case b + "||beta":
				owner = b
			}
		}
		if owner == "" {
			http.Error(w, "invalid owner", 403)
			return
		}
		switch r.URL.Path {
		case "/account/":
			fmt.Fprint(w, `<a id="account_pulldown">Shared display name</a>`)
		case historyPath:
			current := mode.Load()
			if owner == a {
				aPages.Add(1)
				if current == 1 {
					http.Error(w, "account A upstream restriction", 403)
					return
				}
				if current == 3 {
					select {
					case blocked <- struct{}{}:
					case <-r.Context().Done():
						return
					}
					select {
					case <-resume:
					case <-r.Context().Done():
						return
					}
				}
				if current == 4 {
					select {
					case parallel <- struct{}{}:
					case <-r.Context().Done():
						return
					}
					select {
					case <-advance:
					case <-r.Context().Done():
						return
					}
				}
				fmt.Fprint(w, strings.ReplaceAll(firstBill, "¥12.00", "$2.00"))
				return
			}
			bPages.Add(1)
			page := strings.ReplaceAll(firstBill, "¥12.00", "¥21.00")
			if current >= 1 {
				page = strings.ReplaceAll(page, "1 月 6 日", "1 月 7 日")
			}
			fmt.Fprint(w, page)
		case licensesPath:
			fmt.Fprint(w, firstLicense)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	if _, err = db.ExecContext(ctx, `INSERT INTO bill_imports(id,steam_id,nickname,avatar_base64,source_file,warnings_json) VALUES (10,NULL,'Shared display name','','','[]');
INSERT INTO fx_rate_history(currency_code,rate_date,rate_to_cny,source_kind) VALUES('USD','2026-01-05',7.5,'observed'),('USD','2026-01-06',100,'carried');`); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, path)
	steam := account.NewSteamClient(account.SteamOptions{HTTPClient: srv.Client(), AccountURL: srv.URL + "/account/", ProfileURL: srv.URL + "/miniprofile/%s/json"})
	start := time.Date(2026, 1, 8, 12, 0, 0, 0, time.UTC)
	clockNow := func() time.Time { return start }
	var service *Service
	accounts := account.NewService(ctx, db, gate, account.ServiceOptions{DataDir: filepath.Dir(path), Steam: steam, AfterBind: func(sid string) { service.RequestSync(sid) }})
	base, _ := url.Parse(srv.URL)
	service = NewService(ctx, db, gate, SyncOptions{Accounts: accounts, Steam: steam, Recorder: jobrun.New(db, gate, clockNow, func(string, ...any) {}), Now: clockNow, base: base, sleep: func(context.Context, time.Duration) error { return nil }})
	defer func() {
		finish, stop := context.WithTimeout(context.Background(), 5*time.Second)
		defer stop()
		if err := service.Close(finish); err != nil {
			t.Error(err)
		}
		if err := accounts.Close(finish); err != nil {
			t.Error(err)
		}
	}()
	wait := func(check func() bool) {
		t.Helper()
		for !check() {
			select {
			case <-ctx.Done():
				t.Fatal("automatic bill synchronization did not settle")
			case <-time.After(15 * time.Millisecond):
			}
		}
	}
	count := func(query string, args ...any) int {
		t.Helper()
		var n int
		if err := db.QueryRowContext(ctx, query, args...).Scan(&n); err != nil {
			t.Fatal(err)
		}
		return n
	}
	for _, credential := range []string{a + "||alpha", b + "||beta"} {
		if _, err = accounts.BindCookies(ctx, "steamLoginSecure="+credential); err != nil {
			t.Fatal(err)
		}
	}
	wait(func() bool { return count(`SELECT count(*) FROM bill_imports WHERE steam_id IS NOT NULL`) == 2 })
	const total = `SELECT t.cny_fen FROM bill_game_txs t JOIN bill_imports i ON i.id=t.import_id WHERE i.steam_id=?`
	if count(total, a) != 1500 || count(total, b) != 2100 {
		t.Fatal("binding did not import each account's own amount and historical exchange rate")
	}
	oldLicenseID := count(`SELECT id FROM bill_cdk_games WHERE import_id=(SELECT id FROM bill_imports WHERE steam_id=?)`, b)
	if err = gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE bill_cdk_games SET manual_fen=432 WHERE import_id IN(SELECT id FROM bill_imports WHERE steam_id=?)`, b)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	mode.Store(1)
	clock := sched.NewScheduler()
	if err = service.RegisterSchedules(ctx, clock); err != nil {
		t.Fatal(err)
	}
	clock.StartSim(start)
	defer clock.Stop()
	before := bPages.Load()
	clock.RunSim(start.Add(119 * time.Second))
	if bPages.Load() != before {
		t.Fatal("bills task started before the two minute stagger")
	}
	clock.RunSim(start.Add(2 * time.Minute))
	wait(func() bool {
		return count(`SELECT count(*) FROM agent_runs WHERE status='done' AND json_extract(meta,'$.job')='bills_sync'`) == 1
	})
	if bPages.Load() != before+1 || aPages.Load() < 2 {
		t.Fatalf("schedule did not process both accounts, or repeated B first page: A=%d B=%d", aPages.Load(), bPages.Load())
	}
	if count(`SELECT count(*) FROM bill_game_txs t JOIN bill_imports i ON i.id=t.import_id WHERE i.steam_id=? AND t.date='2026-01-07'`, b) != 1 || count(total, a) != 1500 {
		t.Fatal("A upstream failure interrupted B or changed saved A")
	}
	if count(`SELECT manual_fen FROM bill_cdk_games WHERE import_id=(SELECT id FROM bill_imports WHERE steam_id=?)`, b) != 432 {
		t.Fatal("successful refresh dropped the previous manually entered price")
	}
	if _, err := updateBillCdkPrice(ctx, gate, int64(oldLicenseID), nil); err != sql.ErrNoRows {
		t.Fatalf("a stale price edit reached a replacement row: %v", err)
	}
	if result, err := service.Sync(ctx, b, false); err != nil || result["status"] != "probed_skip" {
		t.Fatalf("equal date first page should skip: %v %v", result, err)
	}
	if err = gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `CREATE TRIGGER fixture_no_snapshot BEFORE UPDATE ON app_settings WHEN NEW.key GLOB 'bills.sync_snapshot*' AND json_extract(NEW.value_json,'$.ok')=1 BEGIN SELECT RAISE(ABORT,'fixture'); END`)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if _, err = service.Sync(ctx, b, true); err == nil || count(total, b) != 2100 {
		t.Fatalf("terminal state failure did not roll back the replacement: %v", err)
	}
	if _, err = db.ExecContext(ctx, `DROP TRIGGER fixture_no_snapshot`); err != nil {
		t.Fatal(err)
	}
	mode.Store(4)
	beforeA, beforeB := aPages.Load(), bPages.Load()
	scheduled := make(chan struct{})
	go func() {
		clock.RunSim(start.Add(32 * time.Minute))
		close(scheduled)
	}()
	waitA := func() {
		t.Helper()
		select {
		case <-parallel:
		case <-ctx.Done():
			t.Fatal("account A did not begin the expected sync")
		}
	}
	advanceA := func() {
		t.Helper()
		select {
		case advance <- struct{}{}:
		case <-ctx.Done():
			t.Fatal("account A stopped before releasing the fixture")
		}
	}
	requestBinding := func() {
		t.Helper()
		for i := 0; i < 3; i++ {
			if !service.RequestSync(a) {
				t.Fatal("binding synchronization was not queued")
			}
		}
	}
	waitA()
	wait(func() bool {
		return bPages.Load() == beforeB+1 && count(`SELECT COUNT(*) FROM app_settings WHERE key=? AND json_extract(value_json,'$.probedSkip')=1`, snapshotKey(b)) == 1
	})
	requestBinding()
	advanceA()
	waitA()
	requestBinding()
	advanceA()
	waitA()
	advanceA()
	wait(func() bool {
		service.mu.Lock()
		defer service.mu.Unlock()
		_, pending := service.bindings[a]
		return !pending
	})
	select {
	case <-scheduled:
	case <-ctx.Done():
		t.Fatal("parallel schedule did not settle")
	}
	if aPages.Load() != beforeA+3 {
		t.Fatalf("repeated binds did not coalesce into one trailing sync: got %d A requests", aPages.Load()-beforeA)
	}
	mode.Store(3)
	done := make(chan error, 1)
	cancelled, cancelSync := context.WithCancel(ctx)
	defer cancelSync()
	go func() { _, err := service.Sync(cancelled, a, true); done <- err }()
	select {
	case <-blocked:
	case <-ctx.Done():
		t.Fatal("missing request to cancel")
	}
	cancelSync()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("wrong cancellation result: %v", err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	if count(`SELECT count(*) FROM app_settings WHERE key=? AND json_extract(value_json,'$.running')=0 AND json_extract(value_json,'$.error')='账单同步已中断'`, snapshotKey(a)) != 1 {
		t.Fatal("cancelled account did not record its own terminal status")
	}
	go func() { _, err := service.Sync(ctx, a, true); done <- err }()
	select {
	case <-blocked:
	case <-ctx.Done():
		t.Fatal("missing in-flight account request")
	}
	if status, err := service.Status(ctx); err != nil || status["running"] != true || status["steamId"] != a {
		t.Fatalf("invalid active snapshot: %v %v", status, err)
	}
	if _, err := accounts.SetActive(ctx, a); err != nil {
		t.Fatal(err)
	}
	if result, err := service.Sync(ctx, "", true); err != nil || result["status"] != "busy" {
		t.Fatalf("unexpected concurrent implicit sync: %v %v", result, err)
	}
	type outcome struct {
		result map[string]any
		err    error
	}
	queued := make(chan outcome, 1)
	go func() {
		result, err := service.Sync(ctx, a, true)
		queued <- outcome{result, err}
	}()
	select {
	case result := <-queued:
		t.Fatalf("explicit same-account request failed to queue: %+v", result)
	case <-time.After(40 * time.Millisecond):
	}
	if _, err := accounts.SetActive(ctx, b); err != nil {
		t.Fatal(err)
	}
	other, stop := context.WithTimeout(ctx, 2*time.Second)
	result, err := service.Sync(other, "", true)
	stop()
	if err != nil || result["ok"] != true || result["steamId"] != b {
		t.Fatalf("blocked A stopped an implicit request for B: %v %v", result, err)
	}
	if status, err := service.Status(ctx); err != nil || status["running"] != true || status["steamId"] != a || status["importId"] != nil {
		t.Fatalf("B's terminal fields leaked into A's active status: %v %v", status, err)
	}
	if _, err := accounts.RemoveAccount(ctx, a); err != nil {
		t.Fatal(err)
	}
	close(resume)
	select {
	case err := <-done:
		if err == nil || !strings.Contains(err.Error(), "已解绑") {
			t.Fatalf("late response not rejected by identity check: %v", err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	select {
	case result := <-queued:
		if result.err != nil || result.result["status"] != "no_cookie" {
			t.Fatalf("queued explicit request did not recheck removed credentials: %+v", result)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	if count(`SELECT count(*) FROM bill_imports WHERE steam_id=?`, a) != 0 || count(total, b) != 2100 || count(`SELECT count(*) FROM bill_imports WHERE steam_id IS NULL`) != 1 {
		t.Fatal("late request changed another account or recreated removed bills")
	}
	if count(`SELECT count(*) FROM app_settings WHERE key=?`, snapshotKey(a)) != 0 {
		t.Fatal("late request left an orphaned status after unbinding")
	}
	if status, err := service.Status(ctx); err != nil || status["steamId"] != b || status["running"] != false || status["ok"] != true {
		t.Fatalf("removed account still owns the visible status: %v %v", status, err)
	}
}

func TestBillSyncStatusLegacyFallbackAndActiveSelection(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	const a, b, orphan = "76561198000000001", "76561198000000002", "76561198000000003"
	exec := func(query string, args ...any) {
		t.Helper()
		if _, err := db.ExecContext(ctx, query, args...); err != nil {
			t.Fatal(err)
		}
	}
	exec(`INSERT INTO steam_accounts(steam_id,cookies,persona_name,avatar_url,is_active,is_online,in_game,wallet_error,wallet_backoff_level,wallet_fail_streak,wallet_frozen)
VALUES (?,'','','',1,0,'','',0,0,0), (?,'','','',0,0,'','',0,0,0)`, a, b)
	service := NewService(ctx, db, store.NewWriteGate(db, path), SyncOptions{})
	defer service.Close(ctx)
	mux := http.NewServeMux()
	service.Register(mux)
	check := func(owner, source string, running bool) {
		t.Helper()
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(http.MethodGet, billViewPrefix+"/sync", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("status request returned %d: %s", w.Code, w.Body)
		}
		var status map[string]any
		if err := json.NewDecoder(w.Body).Decode(&status); err != nil {
			t.Fatal(err)
		}
		if status["running"] != running || owner != "" && status["steamId"] != owner || source != "" && status["source"] != source || owner == "" && len(status) != 1 {
			t.Fatalf("unexpected aggregate status for %q/%q: %v", owner, source, status)
		}
	}
	put := func(key, raw string) {
		t.Helper()
		exec(`INSERT INTO app_settings(key,value_json) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json`, key, raw)
	}
	put(snapshotKey(orphan), `{"source":"orphan","syncedAt":"9999-01-01"}`)
	put(syncKey, `{"source":"unidentified","syncedAt":"9999-01-01"}`)
	check("", "", false)
	put(syncKey, `{"source":"legacy","steamId":"`+a+`","running":true,"syncedAt":"2026-01-03"}`)
	put(snapshotKey(b), `{"source":"owner B","syncedAt":"2026-01-02"}`)
	check(a, "legacy", false)
	put(snapshotKey(a), `{"source":"owner A","since":"2026-01-01"}`)
	check(b, "owner B", false)
	releaseA, err := service.acquireSync(ctx, a, true)
	if err != nil {
		t.Fatal(err)
	}
	defer releaseA()
	check(a, "owner A", true)
	releaseB, err := service.acquireSync(ctx, b, true)
	if err != nil {
		t.Fatal(err)
	}
	defer releaseB()
	check(b, "owner B", true)
	exec(`DELETE FROM steam_accounts WHERE steam_id=?`, b)
	check(a, "owner A", true)
	exec(`DELETE FROM steam_accounts WHERE steam_id=?`, a)
	check("", "", false)
}
