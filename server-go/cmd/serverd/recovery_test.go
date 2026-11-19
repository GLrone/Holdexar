package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"holdexar/server-go/internal/access"
	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/store"
)

func TestRecoveryRateLimitPerPeer(t *testing.T) {
	access.ResetUnlockState()
	t.Cleanup(access.ResetUnlockState)

	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "holdexar.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	gate := store.NewWriteGate(db, filepath.Join(dir, "gate.lock"))
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	acctSvc := account.NewService(ctx, db, gate, account.ServiceOptions{DataDir: dir})
	sessions := access.New()
	mux := http.NewServeMux()
	registerRecovery(mux, acctSvc, db, sessions, func(string, ...any) {})

	attacker := "198.51.100.25"
	other := "198.51.100.26"
	for range 5 {
		access.RecordUnlockFailureFor(attacker)
	}
	if access.UnlockLockedSeconds(attacker) <= 0 {
		t.Fatal("攻击对端应已锁定")
	}

	call := func(path, body, remoteAddr string) map[string]any {
		rec := httptest.NewRecorder()
		req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
		req.RemoteAddr = remoteAddr
		mux.ServeHTTP(rec, req)
		if rec.Code != http.StatusOK {
			t.Fatalf("%s status=%d", path, rec.Code)
		}
		var out map[string]any
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatalf("%s decode: %v (%s)", path, err, rec.Body.String())
		}
		return out
	}

	for _, ep := range []string{
		"/api/v1/session/recovery/login/start",
		"/api/v1/session/recovery/login/code",
		"/api/v1/session/recovery/complete",
	} {
		blocked := call(ep, `{}`, attacker+":40000")
		if blocked["ok"] != false || !strings.Contains(blocked["error"].(string), "尝试次数过多") {
			t.Fatalf("被锁定对端 %s 应被限速拒绝: %v", ep, blocked)
		}
	}

	otherStart := call("/api/v1/session/recovery/login/start", `{"account":"","password":""}`, other+":40000")
	if errMsg, _ := otherStart["error"].(string); strings.Contains(errMsg, "尝试次数过多") {
		t.Fatalf("未锁定对端被误限速: %v", otherStart)
	}
	loopStart := call("/api/v1/session/recovery/login/start", `{"account":"","password":""}`, "127.0.0.1:50000")
	if errMsg, _ := loopStart["error"].(string); strings.Contains(errMsg, "尝试次数过多") {
		t.Fatalf("回环对端被误限速: %v", loopStart)
	}
	loopComplete := call("/api/v1/session/recovery/complete", `{}`, "127.0.0.1:50000")
	if loopComplete["ok"] != false || loopComplete["error"] != "Steam 登录流程未完成" {
		t.Fatalf("未登录 complete 应报未完成: %v", loopComplete)
	}
}

type recoverySteamMock struct {
	acctSvc *account.Service
	db      *sql.DB
	mux     *http.ServeMux
	steamID atomic.Uint64
	accept  atomic.Bool
	wrongPw atomic.Bool
}

func testPBBytes(dst []byte, field int, value []byte) []byte {
	dst = binary.AppendUvarint(dst, uint64(field<<3|2))
	dst = binary.AppendUvarint(dst, uint64(len(value)))
	return append(dst, value...)
}

func testPBVarint(dst []byte, field int, value uint64) []byte {
	dst = binary.AppendUvarint(dst, uint64(field<<3))
	return binary.AppendUvarint(dst, value)
}

func testAccessToken(sid string, exp int64) string {
	h := base64.RawURLEncoding.EncodeToString([]byte(`{"alg":"none"}`))
	p := base64.RawURLEncoding.EncodeToString([]byte(fmt.Sprintf(`{"sub":%q,"exp":%d}`, sid, exp)))
	return sid + "||" + h + "." + p + ".sig"
}

func newRecoverySteamMock(t *testing.T) *recoverySteamMock {
	t.Helper()
	access.ResetUnlockState()
	t.Cleanup(access.ResetUnlockState)

	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	m := &recoverySteamMock{}
	m.steamID.Store(76561198000000001)

	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("x-eresult", "1")
		sidNum := m.steamID.Load()
		sidStr := strconv.FormatUint(sidNum, 10)
		switch {
		case r.URL.Path == "/auth/GetPasswordRSAPublicKey/v1":
			body := testPBBytes(nil, 1, []byte(key.N.Text(16)))
			body = testPBBytes(body, 2, []byte(fmt.Sprintf("%x", key.E)))
			body = testPBVarint(body, 3, 1000)
			_, _ = w.Write(body)
		case r.URL.Path == "/auth/BeginAuthSessionViaCredentials/v1":
			if m.wrongPw.Load() {
				w.Header().Set("x-eresult", "5")
				return
			}
			body := testPBBytes(testPBVarint(nil, 1, 7), 2, []byte("req-1"))
			body = binary.AppendUvarint(body, 3<<3|5)
			body = binary.LittleEndian.AppendUint32(body, math.Float32bits(1))
			body = testPBBytes(body, 4, testPBVarint(nil, 1, 3))
			_, _ = w.Write(testPBVarint(body, 5, sidNum))
		case strings.HasPrefix(r.URL.Path, "/checkdevice/"):
			_ = json.NewEncoder(w).Encode(map[string]bool{"success": false})
		case r.URL.Path == "/auth/UpdateAuthSessionWithSteamGuardCode/v1":
			_ = r.ParseMultipartForm(1 << 20)
			raw, _ := base64.StdEncoding.DecodeString(r.FormValue("input_protobuf_encoded"))
			if !strings.Contains(string(raw), "RIGHT") {
				w.Header().Set("x-eresult", "88")
				return
			}
			m.accept.Store(true)
			_, _ = w.Write(testPBBytes(testPBVarint(nil, 5, 9), 6, []byte("req-2")))
		case r.URL.Path == "/auth/PollAuthSessionStatus/v1":
			if m.accept.Load() {
				_, _ = w.Write(testPBBytes(nil, 3, []byte("refresh-tok")))
			}
		case r.URL.Path == "/finalize":
			_ = json.NewEncoder(w).Encode(map[string]any{
				"transfer_info": []any{map[string]any{
					"url":    "http://" + r.Host + "/transfer",
					"params": map[string]string{"nonce": "n1"},
				}},
			})
		case r.URL.Path == "/transfer":
			http.SetCookie(w, &http.Cookie{
				Name:  "steamLoginSecure",
				Value: testAccessToken(sidStr, time.Now().Add(24*time.Hour).Unix()),
			})
			_, _ = w.Write([]byte(`{}`))
		case r.URL.Path == "/account/":
			fmt.Fprintf(w, `<div data-steamid="%s"></div><div class="accountData price">¥10.00</div>`, sidStr)
		default:
			_, _ = w.Write([]byte(`{"persona_name":"RecoveryUser","online":true}`))
		}
	}))
	t.Cleanup(srv.Close)

	dir := t.TempDir()
	dbPath := filepath.Join(dir, "holdexar.db")
	db, err := store.Open(dbPath)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	if err := store.InitializeFreshDatabase(context.Background(), db); err != nil {
		t.Fatal(err)
	}
	gate := store.NewWriteGate(db, filepath.Join(dir, "gate.lock"))
	steam := account.NewSteamClient(account.SteamOptions{
		HTTPClient:     srv.Client(),
		AuthBaseURL:    srv.URL + "/auth",
		CheckDeviceURL: srv.URL + "/checkdevice",
		FinalizeURL:    srv.URL + "/finalize",
		AccountURL:     srv.URL + "/account/",
		ProfileURL:     srv.URL + "/miniprofile/%s/json",
		Sleep: func(ctx context.Context, _ time.Duration) error {
			time.Sleep(5 * time.Millisecond)
			return ctx.Err()
		},
	})
	acctSvc := account.NewService(context.Background(), db, gate, account.ServiceOptions{
		DataDir: dir,
		Steam:   steam,
	})
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = acctSvc.Close(ctx)
	})
	mux := http.NewServeMux()
	registerRecovery(mux, acctSvc, db, access.New(), func(string, ...any) {})
	m.acctSvc = acctSvc
	m.db = db
	m.mux = mux
	return m
}

func (m *recoverySteamMock) post(t *testing.T, path, body, remoteAddr string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	req.RemoteAddr = remoteAddr
	m.mux.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("%s status=%d body=%s", path, rec.Code, rec.Body.String())
	}
	var out map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		t.Fatalf("%s json: %v (%s)", path, err, rec.Body.String())
	}
	return rec, out
}

func (m *recoverySteamMock) waitDone(t *testing.T, wanted string) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if m.acctSvc.LoginStatus().State == wanted {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("login wanted=%s got=%+v", wanted, m.acctSvc.LoginStatus())
}

func TestRecoveryNoReplayAndNonRecoveryBypassBlocked(t *testing.T) {
	m := newRecoverySteamMock(t)
	const boundSID = "76561198000000001"
	const peer = "198.51.100.25:40000"

	if _, err := m.acctSvc.BindCookies(context.Background(),
		"sessionid=s1; steamLoginSecure="+testAccessToken(boundSID, time.Now().Add(24*time.Hour).Unix())); err != nil {
		t.Fatal(err)
	}

	m.accept.Store(true)
	res, err := m.acctSvc.StartLogin(context.Background(), "user", "pw")
	if err != nil || res["ok"] != true {
		t.Fatalf("normal StartLogin=%v %v", res, err)
	}
	m.waitDone(t, "done")
	_, nonRecComplete := m.post(t, "/api/v1/session/recovery/complete", `{}`, peer)
	if nonRecComplete["ok"] != false {
		t.Fatalf("normal StartLogin must NOT satisfy /recovery/complete: %v", nonRecComplete)
	}

	m.accept.Store(false)
	_, startOut := m.post(t, "/api/v1/session/recovery/login/start", `{"account":"user","password":"pw"}`, peer)
	if startOut["ok"] != true {
		t.Fatalf("recovery start=%v", startOut)
	}
	_, codeOut := m.post(t, "/api/v1/session/recovery/login/code", `{"code":"RIGHT"}`, peer)
	if codeOut["ok"] != true {
		t.Fatalf("recovery code=%v", codeOut)
	}
	m.waitDone(t, "done")

	rec1, complete1 := m.post(t, "/api/v1/session/recovery/complete", `{}`, peer)
	if complete1["ok"] != true || len(rec1.Result().Cookies()) == 0 {
		t.Fatalf("first /recovery/complete should succeed and issue session cookie: %v", complete1)
	}
	rec2, complete2 := m.post(t, "/api/v1/session/recovery/complete", `{}`, peer)
	if complete2["ok"] != false || len(rec2.Result().Cookies()) != 0 {
		t.Fatalf("second /recovery/complete replay must be rejected: %v", complete2)
	}
}

func TestRecoveryBadAttemptsLockOutPeerWithoutLockingLoopback(t *testing.T) {
	const peer = "198.51.100.25:40000"
	const peerIP = "198.51.100.25"
	const loopAddr = "127.0.0.1:50000"

	t.Run("repeated bad start locks peer only", func(t *testing.T) {
		m := newRecoverySteamMock(t)
		m.wrongPw.Store(true)
		for range 5 {
			_, out := m.post(t, "/api/v1/session/recovery/login/start", `{"account":"user","password":"bad"}`, peer)
			if out["ok"] != false {
				t.Fatalf("expected failed start: %v", out)
			}
		}
		if access.UnlockLockedSeconds(peerIP) <= 0 {
			t.Fatal("peer should be locked after 5 failed /start calls")
		}
		_, blocked := m.post(t, "/api/v1/session/recovery/login/start", `{"account":"user","password":"bad"}`, peer)
		if !strings.Contains(blocked["error"].(string), "尝试次数过多") {
			t.Fatalf("peer not blocked: %v", blocked)
		}
		_, loopOut := m.post(t, "/api/v1/session/recovery/login/start", `{"account":"","password":""}`, loopAddr)
		if strings.Contains(loopOut["error"].(string), "尝试次数过多") {
			t.Fatalf("loopback must not be locked out: %v", loopOut)
		}
	})

	t.Run("repeated bad code locks peer only", func(t *testing.T) {
		m := newRecoverySteamMock(t)
		_, startOut := m.post(t, "/api/v1/session/recovery/login/start", `{"account":"user","password":"pw"}`, peer)
		if startOut["ok"] != true {
			t.Fatalf("start=%v", startOut)
		}
		for range 5 {
			_, out := m.post(t, "/api/v1/session/recovery/login/code", `{"code":"WRONG"}`, peer)
			if out["ok"] != false {
				t.Fatalf("expected failed code: %v", out)
			}
		}
		if access.UnlockLockedSeconds(peerIP) <= 0 {
			t.Fatal("peer should be locked after 5 failed /code calls")
		}
		_, codeBlocked := m.post(t, "/api/v1/session/recovery/login/code", `{"code":"RIGHT"}`, peer)
		if !strings.Contains(codeBlocked["error"].(string), "尝试次数过多") {
			t.Fatalf("locked peer must be rejected at /code: %v", codeBlocked)
		}
		_, loopOut := m.post(t, "/api/v1/session/recovery/complete", `{}`, loopAddr)
		if strings.Contains(loopOut["error"].(string), "尝试次数过多") {
			t.Fatalf("loopback must not be locked out: %v", loopOut)
		}
	})

	t.Run("repeated bad complete locks peer only", func(t *testing.T) {
		m := newRecoverySteamMock(t)
		for range 5 {
			_, out := m.post(t, "/api/v1/session/recovery/complete", `{}`, peer)
			if out["ok"] != false {
				t.Fatalf("expected failed complete: %v", out)
			}
		}
		if access.UnlockLockedSeconds(peerIP) <= 0 {
			t.Fatal("peer should be locked after 5 failed /complete calls")
		}
		_, loopOut := m.post(t, "/api/v1/session/recovery/complete", `{}`, loopAddr)
		if strings.Contains(loopOut["error"].(string), "尝试次数过多") {
			t.Fatalf("loopback must not be locked out: %v", loopOut)
		}
	})

	t.Run("async unbound account failure records unlock failure", func(t *testing.T) {
		m := newRecoverySteamMock(t)
		m.steamID.Store(76561198000000999)
		for range 5 {
			m.accept.Store(true)
			_, out := m.post(t, "/api/v1/session/recovery/login/start", `{"account":"user","password":"pw"}`, peer)
			if out["ok"] != true {
				t.Fatalf("start=%v", out)
			}
			m.waitDone(t, "failed")
		}
		if access.UnlockLockedSeconds(peerIP) <= 0 {
			t.Fatal("peer should be locked after 5 async unbound recovery login failures")
		}
		_, loopOut := m.post(t, "/api/v1/session/recovery/complete", `{}`, loopAddr)
		if strings.Contains(loopOut["error"].(string), "尝试次数过多") {
			t.Fatalf("loopback must not be locked out: %v", loopOut)
		}
	})
}
