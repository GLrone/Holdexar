package systemx

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

func dataTestEnv(t *testing.T) (*access.Gate, http.Handler, string) {
	t.Helper()
	access.ResetUnlockState()
	access.SetLANEnabled(false)
	access.SetExtraHosts(nil)
	secretbox.ResetState()
	t.Cleanup(func() {
		access.ResetUnlockState()
		access.SetLANEnabled(false)
		access.SetExtraHosts(nil)
		secretbox.ResetState()
	})

	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "holdexar.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	gate := access.New()
	writeGate := store.NewWriteGate(db, filepath.Join(dir, "gate.lock"))
	mux := http.NewServeMux()
	RegisterData(mux, db, writeGate, gate, dir, nil, func() {})
	return gate, gate.Middleware(mux), dir
}

func TestSecurityLockRotatesAndIssuesFreshSession(t *testing.T) {
	gate, h, dir := dataTestEnv(t)
	master := make([]byte, 32)
	recFile, err := secretbox.WrapPassphrase(master, "pass-123456")
	if err != nil {
		t.Fatal(err)
	}
	if err := secretbox.WriteRecord(dir, recFile); err != nil {
		t.Fatal(err)
	}
	secretbox.LoadUnlockedKey(master)

	issue := func() *http.Cookie {
		w := httptest.NewRecorder()
		gate.Issue(w)
		return w.Result().Cookies()[0]
	}
	c1 := issue()
	c2 := issue()

	lockRec := httptest.NewRecorder()
	lockReq := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:28765/api/v1/system/security/lock", nil)
	lockReq.RemoteAddr = "127.0.0.1:51234"
	lockReq.AddCookie(c1)
	h.ServeHTTP(lockRec, lockReq)
	if lockRec.Code != http.StatusOK {
		t.Fatalf("security/lock 状态码=%d body=%s", lockRec.Code, lockRec.Body.String())
	}
	cookies := lockRec.Result().Cookies()
	if len(cookies) != 1 || cookies[0].Name != access.SessionCookie {
		t.Fatalf("security/lock 应重签新会话 Cookie: %+v", cookies)
	}
	cNew := cookies[0]
	if cNew.Value == c1.Value || cNew.Value == c2.Value {
		t.Fatalf("security/lock 下发的 Cookie 未轮换")
	}

	for name, tc := range map[string]struct {
		cookie *http.Cookie
		want   int
	}{
		"old-1": {c1, http.StatusUnauthorized},
		"old-2": {c2, http.StatusUnauthorized},
		"fresh": {cNew, http.StatusOK},
	} {
		r := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodGet, "http://127.0.0.1:28765/api/v1/system/security", nil)
		req.RemoteAddr = "127.0.0.1:51234"
		req.AddCookie(tc.cookie)
		h.ServeHTTP(r, req)
		if r.Code != tc.want {
			t.Fatalf("%s 状态码=%d，应为 %d", name, r.Code, tc.want)
		}
	}

	if locked, err := secretbox.IsLocked(dir); err != nil || !locked {
		t.Fatalf("lock 后主密钥应处于锁定态: locked=%v err=%v", locked, err)
	}
}

func TestImportLegacyLoopbackOnly(t *testing.T) {
	gate, h, dir := dataTestEnv(t)
	access.SetLANEnabled(true)
	access.SetExtraHosts([]string{"192.168.1.5"})

	w := httptest.NewRecorder()
	gate.Issue(w)
	cookie := w.Result().Cookies()[0]
	missingPath := filepath.Join(dir, "missing.db")
	payloadBytes, _ := json.Marshal(map[string]string{"path": missingPath})

	remoteRec := httptest.NewRecorder()
	remoteReq := httptest.NewRequest(http.MethodPost, "http://192.168.1.5:28765/api/v1/import-legacy", strings.NewReader(string(payloadBytes)))
	remoteReq.Host = "192.168.1.5:28765"
	remoteReq.RemoteAddr = "192.168.1.25:51234"
	remoteReq.AddCookie(cookie)
	h.ServeHTTP(remoteRec, remoteReq)
	if remoteRec.Code != http.StatusForbidden {
		t.Fatalf("局域网对端 import-legacy 状态码=%d，应为 403; body=%s", remoteRec.Code, remoteRec.Body.String())
	}

	localRec := httptest.NewRecorder()
	localReq := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:28765/api/v1/import-legacy", strings.NewReader(string(payloadBytes)))
	localReq.Host = "127.0.0.1:28765"
	localReq.RemoteAddr = "127.0.0.1:51234"
	localReq.AddCookie(cookie)
	h.ServeHTTP(localRec, localReq)
	if localRec.Code != http.StatusNotFound {
		t.Fatalf("回环对端应通过回环闸到达文件检查(404)，实际=%d body=%s", localRec.Code, localRec.Body.String())
	}
	if strings.Contains(localRec.Body.String(), missingPath) || strings.Contains(localRec.Body.String(), "missing.db") {
		t.Fatalf("404 错误体不得回显原始文件系统路径: %s", localRec.Body.String())
	}
}

func TestImportLegacyHardeningUNCInjectionHeaderAndValidDB(t *testing.T) {
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "holdexar.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	gate := access.New()
	writeGate := store.NewWriteGate(db, filepath.Join(dir, "gate.lock"))
	mux := http.NewServeMux()
	RegisterData(mux, db, writeGate, gate, dir, nil, func() {})
	h := gate.Middleware(mux)

	w := httptest.NewRecorder()
	gate.Issue(w)
	cookie := w.Result().Cookies()[0]

	postImport := func(p string) *httptest.ResponseRecorder {
		raw, _ := json.Marshal(map[string]string{"path": p})
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "http://127.0.0.1:28765/api/v1/import-legacy", strings.NewReader(string(raw)))
		req.Host = "127.0.0.1:28765"
		req.RemoteAddr = "127.0.0.1:51234"
		req.AddCookie(cookie)
		h.ServeHTTP(rec, req)
		return rec
	}

	for _, bad := range []string{
		"",
		"relative.db",
		`\\evil\share\a.db`,
		`//evil/share/a.db`,
		`\\.\C:\a.db`,
		`\\?\C:\a.db`,
		filepath.Join(dir, "notes.txt"),
	} {
		rec := postImport(bad)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("bad path %q: status=%d, want 400; body=%s", bad, rec.Code, rec.Body.String())
		}
		if bad != "" && strings.Contains(rec.Body.String(), bad) {
			t.Fatalf("bad path %q echoed in error response: %s", bad, rec.Body.String())
		}
	}

	injectedTarget := filepath.Join(dir, "created-by-rwc.db")
	rec := postImport(injectedTarget + "?mode=rwc")
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("?mode=rwc status=%d, want 400; body=%s", rec.Code, rec.Body.String())
	}
	if _, statErr := os.Stat(injectedTarget); !os.IsNotExist(statErr) {
		t.Fatalf("URI injection must not create %s", injectedTarget)
	}
	sneakyBase := filepath.Join(dir, "sneaky")
	rec = postImport(sneakyBase + "?mode=rwc&pad=.db")
	if rec.Code != http.StatusBadRequest && rec.Code != http.StatusNotFound {
		t.Fatalf("sneaky ?mode=rwc status=%d, want 400/404; body=%s", rec.Code, rec.Body.String())
	}
	if _, statErr := os.Stat(sneakyBase); !os.IsNotExist(statErr) {
		t.Fatalf("sneaky URI injection must not create %s", sneakyBase)
	}
	escapedURI := sqliteReadOnlyURI("/tmp/evil.db?mode=rwc#frag.db")
	if strings.Contains(escapedURI, "?mode=rwc") || !strings.HasSuffix(escapedURI, "?mode=ro") || !strings.Contains(escapedURI, "%3Fmode=rwc%23frag.db") {
		t.Fatalf("sqliteReadOnlyURI failed to escape ?/# injection: %q", escapedURI)
	}

	subDir := filepath.Join(dir, "dir.db")
	if err := os.MkdirAll(subDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if rec := postImport(subDir); rec.Code != http.StatusBadRequest {
		t.Fatalf("directory with .db suffix status=%d, want 400; body=%s", rec.Code, rec.Body.String())
	}
	notSQLite := filepath.Join(dir, "fake.db")
	if err := os.WriteFile(notSQLite, []byte("not an sqlite database at all!"), 0o600); err != nil {
		t.Fatal(err)
	}
	if rec := postImport(notSQLite); rec.Code != http.StatusBadRequest {
		t.Fatalf("non-SQLite regular file status=%d, want 400; body=%s", rec.Code, rec.Body.String())
	}

	legacyPath := filepath.Join(dir, "legacy.SQLITE3")
	legacyDB, err := sql.Open("sqlite", legacyPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacyDB.Exec(`
		CREATE TABLE steam_games (appid INTEGER PRIMARY KEY, name TEXT);
		CREATE TABLE steam_prices (
			appid INTEGER, region_code TEXT, currency TEXT, price REAL, original_price REAL,
			discount_percent INTEGER, sub_id INTEGER, is_gold BOOLEAN, version_suffix TEXT,
			price_status TEXT, crawled_at TEXT
		);
		INSERT INTO steam_games(appid, name) VALUES (620, 'Portal 2');
		INSERT INTO steam_prices(appid, region_code, currency, price, original_price, discount_percent, sub_id, is_gold, version_suffix, price_status, crawled_at)
		VALUES (620, 'cn', 'CNY', 6.8, 68.0, 90, 1234, 0, '', 'ok', '2026-01-01 00:00:00');
	`); err != nil {
		_ = legacyDB.Close()
		t.Fatal(err)
	}
	if err := legacyDB.Close(); err != nil {
		t.Fatal(err)
	}

	okRec := postImport(legacyPath)
	if okRec.Code != http.StatusOK {
		t.Fatalf("valid legacy db import status=%d, want 200; body=%s", okRec.Code, okRec.Body.String())
	}
	var imported map[string]any
	if err := json.Unmarshal(okRec.Body.Bytes(), &imported); err != nil {
		t.Fatal(err)
	}
	if imported["games"] != float64(1) || imported["priceRows"] != float64(1) || imported["currentUpserted"] != float64(1) {
		t.Fatalf("unexpected import counts: %v", imported)
	}
}

func TestSecurityUnlockPerPeerRateLimit(t *testing.T) {
	gate, h, dir := dataTestEnv(t)
	access.SetLANEnabled(true)
	access.SetExtraHosts([]string{"192.168.1.5"})

	master := make([]byte, 32)
	recFile, err := secretbox.WrapPassphrase(master, "right-pass-123")
	if err != nil {
		t.Fatal(err)
	}
	if err := secretbox.WriteRecord(dir, recFile); err != nil {
		t.Fatal(err)
	}

	w := httptest.NewRecorder()
	gate.Issue(w)
	cookie := w.Result().Cookies()[0]

	postUnlockSec := func(host, remoteAddr, pass string) *httptest.ResponseRecorder {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, "http://"+host+"/api/v1/system/security/unlock", strings.NewReader(`{"passphrase":"`+pass+`"}`))
		req.Host = host
		req.RemoteAddr = remoteAddr
		req.AddCookie(cookie)
		h.ServeHTTP(rec, req)
		return rec
	}

	for range 5 {
		if rec := postUnlockSec("192.168.1.5:28765", "192.168.1.25:51234", "wrong"); rec.Code != http.StatusBadRequest {
			t.Fatalf("错口令状态码=%d，应为 400", rec.Code)
		}
	}
	if rec := postUnlockSec("192.168.1.5:28765", "192.168.1.25:51234", "right-pass-123"); rec.Code != http.StatusTooManyRequests {
		t.Fatalf("限速对端应 429，实际=%d body=%s", rec.Code, rec.Body.String())
	}

	if rec := postUnlockSec("127.0.0.1:28765", "127.0.0.1:51234", "right-pass-123"); rec.Code != http.StatusOK {
		t.Fatalf("回环解锁应不受远端限速影响: %d %s", rec.Code, rec.Body.String())
	} else {
		var body map[string]any
		_ = json.Unmarshal(rec.Body.Bytes(), &body)
		if body["ok"] != true {
			t.Fatalf("回环解锁返回体异常: %s", rec.Body.String())
		}
	}
	if access.UnlockLockedSeconds("192.168.1.25") <= 0 {
		t.Fatal("回环解锁成功不应清除远端攻击者的限速窗口")
	}
}
