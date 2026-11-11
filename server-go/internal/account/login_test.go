package account

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

const loginFixtureSID = "76561198000000001"

type loginFixture struct {
	service       *Service
	key           *rsa.PrivateKey
	guards        []int
	accept        atomic.Bool
	wrong         atomic.Bool
	rotated       atomic.Bool
	begin         atomic.Int32
	final         atomic.Int32
	logs          bytes.Buffer
	logMu         sync.Mutex
	noTimestamp   atomic.Bool
	reject        atomic.Bool
	finalFailures atomic.Int32
	wrongOwner    atomic.Bool
	deviceTrusted atomic.Bool
	expectedGuard int
	beginEntered  chan struct{}
	finalEntered  chan struct{}
}

func newLoginFixture(t *testing.T, guards []int) *loginFixture {
	t.Helper()
	key, e := rsa.GenerateKey(rand.Reader, 2048)
	if e != nil {
		t.Fatal(e)
	}
	f := &loginFixture{key: key, guards: guards, expectedGuard: 3}
	f.service, _ = serviceFixture(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("x-eresult", "1")
		var fields []loginPBField
		if strings.HasPrefix(r.URL.Path, "/auth/") {
			if r.Header.Get("Origin") != "https://store.steampowered.com" || r.Header.Get("Referer") != "https://store.steampowered.com/login/" || r.Header.Get("User-Agent") != loginUserAgent {
				t.Error("browser auth headers missing")
			}
			var value string
			if r.Method == "GET" {
				value = r.URL.Query().Get("input_protobuf_encoded")
			} else if r.ParseMultipartForm(1<<20) == nil {
				value = r.FormValue("input_protobuf_encoded")
				defer r.MultipartForm.RemoveAll()
			}
			raw, e := base64.StdEncoding.DecodeString(value)
			if e != nil {
				t.Error(e)
			}
			fields, e = readLoginPB(raw)
			if e != nil {
				t.Error(e)
			}
		}
		switch r.URL.Path {
		case "/auth/GetPasswordRSAPublicKey/v1":
			if string(pbData(fields, 1, 2)) != "fixture-user" {
				t.Error("wrong account name")
			}
			body := pbBytes(nil, 1, []byte(key.N.Text(16)))
			body = pbBytes(body, 2, []byte(fmt.Sprintf("%x", key.E)))
			if !f.noTimestamp.Load() {
				body = pbVarint(body, 3, 1000)
			}
			w.Write(body)
		case "/auth/BeginAuthSessionViaCredentials/v1":
			if f.begin.Add(1) == 1 && f.beginEntered != nil {
				close(f.beginEntered)
				<-r.Context().Done()
				return
			}
			cipher, e := base64.StdEncoding.DecodeString(string(pbData(fields, 3, 2)))
			if e != nil {
				t.Error(e)
			}
			plain, e := rsa.DecryptPKCS1v15(rand.Reader, key, cipher)
			if e != nil || string(plain) != "fixture-password" || pbNumber(fields, 4) != 1000 || pbNumber(fields, 5) != 1 || pbNumber(fields, 7) != 1 || string(pbData(fields, 8, 2)) != "Store" || pbNumber(fields, 11) != 6 {
				t.Errorf("auth request shape invalid: %v", e)
			}
			if f.wrong.Load() {
				w.Header().Set("x-eresult", "5")
				return
			}
			body := pbBytes(pbVarint(nil, 1, 7), 2, []byte("request-one"))
			body = binary.AppendUvarint(body, 3<<3|5)
			body = binary.LittleEndian.AppendUint32(body, math.Float32bits(1))
			for _, guard := range f.guards {
				body = pbBytes(body, 4, pbVarint(nil, 1, uint64(guard)))
			}
			w.Write(pbVarint(body, 5, 76561198000000001))
		case "/checkdevice/" + loginFixtureSID:
			json.NewEncoder(w).Encode(map[string]bool{"success": f.deviceTrusted.Load()})
		case "/auth/UpdateAuthSessionWithSteamGuardCode/v1":
			if pbNumber(fields, 1) != 7 || len(pbData(fields, 2, 1)) != 8 || binary.LittleEndian.Uint64(pbData(fields, 2, 1)) != 76561198000000001 || pbNumber(fields, 4) != uint64(f.expectedGuard) {
				t.Error("Steam Guard request shape invalid")
			}
			if string(pbData(fields, 3, 2)) != "RIGHT" {
				w.Header().Set("x-eresult", "88")
				if f.expectedGuard == 2 {
					w.Header().Set("x-eresult", "65")
				}
				return
			}
			f.rotated.Store(true)
			f.accept.Store(true)
			w.Write(pbBytes(pbVarint(nil, 5, 9), 6, []byte("request-two")))
		case "/auth/PollAuthSessionStatus/v1":
			if f.reject.Load() {
				w.Header().Set("x-eresult", "27")
				return
			}
			if f.rotated.Load() && (pbNumber(fields, 1) != 9 || string(pbData(fields, 2, 2)) != "request-two") {
				t.Error("poll ignored auth session rotation")
			}
			if f.accept.Load() {
				w.Write(pbBytes(nil, 3, []byte("refresh-fixture")))
			}
		case "/finalize":
			count := f.final.Add(1)
			if f.finalEntered != nil {
				io.Copy(io.Discard, r.Body)
				close(f.finalEntered)
				<-r.Context().Done()
				return
			}
			if count <= f.finalFailures.Load() {
				w.WriteHeader(503)
				return
			}
			json.NewEncoder(w).Encode(map[string]any{"transfer_info": []any{map[string]any{"url": "http://" + r.Host + "/transfer", "params": map[string]string{"nonce": "transfer-fixture"}}}})
		case "/transfer":
			owner := loginFixtureSID
			if f.wrongOwner.Load() {
				owner = "76561198000000002"
			}
			http.SetCookie(w, &http.Cookie{Name: "steamLoginSecure", Value: accessFixture(owner, time.Now().Add(24*time.Hour).Unix())})
			fmt.Fprint(w, `{}`)
		case "/account/":
			fmt.Fprint(w, walletPage(loginFixtureSID, "¥51.50", "¥51.50", 23))
		default:
			fmt.Fprint(w, `{"persona_name":"Login Fixture","online":true}`)
		}
	})
	base := strings.TrimSuffix(f.service.opts.Steam.opts.AccountURL, "/account/")
	f.service.opts.Steam.opts.AuthBaseURL = base + "/auth"
	f.service.opts.Steam.opts.CheckDeviceURL = base + "/checkdevice"
	f.service.opts.Log = func(format string, args ...any) {
		f.logMu.Lock()
		defer f.logMu.Unlock()
		fmt.Fprintf(&f.logs, format+"\n", args...)
	}
	return f
}

func (f *loginFixture) logText() string {
	f.logMu.Lock()
	defer f.logMu.Unlock()
	return f.logs.String()
}

func waitLoginState(t *testing.T, s *Service, wanted string) LoginState {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		state := s.LoginStatus()
		if state.State == wanted {
			return state
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("login wanted=%s actual=%+v", wanted, s.LoginStatus())
	return LoginState{}
}

func TestLoginGuardRotationCompletesRealBindingWithoutLeakingPassword(t *testing.T) {
	f := newLoginFixture(t, []int{2, 3, 4})
	s := f.service
	ctx := context.Background()
	result, e := s.StartLogin(ctx, " fixture-user ", "fixture-password")
	if e != nil || result["ok"] != true || s.LoginStatus().State != "awaiting_confirmation" || !s.LoginStatus().CodeAvailable {
		t.Fatalf("start=%v error=%v", result, e)
	}
	if result, e = s.StartLogin(ctx, "fixture-user", "fixture-password"); e != nil || result["busy"] != true {
		t.Fatalf("busy=%v error=%v", result, e)
	}
	result, e = s.SubmitLoginCode(ctx, "WRONG")
	if e != nil || result["ok"] != false || result["error"] != "验证码不正确，请重试" || s.LoginStatus().State != "awaiting_confirmation" {
		t.Fatalf("bad code=%v error=%v", result, e)
	}
	result, e = s.SubmitLoginCode(ctx, " RIGHT ")
	if e != nil || result["ok"] != true {
		t.Fatalf("good code=%v error=%v", result, e)
	}
	state := waitLoginState(t, s, "done")
	status, e := s.Status(ctx)
	if e != nil || status["cookie_steam_id"] != loginFixtureSID || status["wallet"].(map[string]any)["balance"] != 51.5 || f.begin.Load() != 1 || f.final.Load() != 1 {
		t.Fatalf("bound=%v error=%v begin=%d final=%d", status, e, f.begin.Load(), f.final.Load())
	}
	public, _ := json.Marshal(state)
	if strings.Contains(string(public), "request-two") || strings.Contains(string(public), "refresh-fixture") || strings.Contains(f.logText(), "fixture-password") {
		t.Fatal("credentials leaked in public state or logs")
	}
	var stored string
	s.db.QueryRow(`SELECT cookies FROM steam_accounts`).Scan(&stored)
	if !strings.HasPrefix(stored, "enc1:") || strings.Contains(stored, "fixture-password") {
		t.Fatal("login credential not encrypted")
	}
}

func TestLoginWrongPasswordTimeoutAndCancelKeepAccountsUnbound(t *testing.T) {
	f := newLoginFixture(t, []int{3})
	s := f.service
	ctx := context.Background()
	f.wrong.Store(true)
	result, e := s.StartLogin(ctx, "fixture-user", "fixture-password")
	if e != nil || result["ok"] != false || s.LoginStatus().Error != "账号或密码不正确" {
		t.Fatalf("wrong password=%v error=%v", result, e)
	}
	f.wrong.Store(false)
	s.opts.LoginTimeout = 100 * time.Millisecond
	if _, e = s.StartLogin(ctx, "fixture-user", "fixture-password"); e != nil {
		t.Fatal(e)
	}
	if state := waitLoginState(t, s, "failed"); state.Error != "登录等待超时，请重试" {
		t.Fatalf("timeout=%+v", state)
	}
	s.opts.LoginTimeout = 5 * time.Minute
	if _, e = s.StartLogin(ctx, "fixture-user", "fixture-password"); e != nil {
		t.Fatal(e)
	}
	s.CancelLogin()
	if state := s.LoginStatus(); state.State != "idle" {
		t.Fatal(state)
	}
	result, e = s.SubmitLoginCode(ctx, "RIGHT")
	if e != nil || result["no_session"] != true {
		t.Fatalf("after cancel=%v %v", result, e)
	}
	status, e := s.Status(ctx)
	if e != nil || status["has_cookie"] != false || f.final.Load() != 0 {
		t.Fatalf("unexpected binding=%v %v", status, e)
	}
}

func TestLoginMissingTimestampDoesNotSubmitPassword(t *testing.T) {
	f := newLoginFixture(t, nil)
	f.noTimestamp.Store(true)
	result, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password")
	if e != nil || result["ok"] != false || f.begin.Load() != 0 || f.service.LoginStatus().Error != loginResponseMessage {
		t.Fatalf("malformed key response result=%v error=%v begin=%d", result, e, f.begin.Load())
	}
}

func TestLoginCancelDuringSigningDoesNotOverwriteNewAttempt(t *testing.T) {
	f := newLoginFixture(t, []int{3})
	f.beginEntered = make(chan struct{})
	done := make(chan error, 1)
	go func() {
		_, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password")
		done <- e
	}()
	select {
	case <-f.beginEntered:
	case <-time.After(3 * time.Second):
		t.Fatal("credential request did not start")
	}
	f.service.CancelLogin()
	if result, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password"); e != nil || result["ok"] != true {
		t.Fatalf("replacement attempt=%v %v", result, e)
	}
	select {
	case e := <-done:
		if e != nil {
			t.Fatal(e)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("cancel did not interrupt credential request")
	}
	if state := f.service.LoginStatus(); state.State != "awaiting_code" {
		t.Fatalf("old attempt overwrote new state: %+v", state)
	}
}

func TestLoginCancelDuringFinalizeAndShutdownCannotBindLater(t *testing.T) {
	f := newLoginFixture(t, []int{4})
	f.finalEntered = make(chan struct{})
	f.accept.Store(true)
	if result, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password"); e != nil || result["ok"] != true {
		t.Fatalf("start=%v %v", result, e)
	}
	select {
	case <-f.finalEntered:
	case <-time.After(3 * time.Second):
		t.Fatal("finalize did not start")
	}
	f.service.CancelLogin()
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if e := f.service.Close(ctx); e != nil {
		t.Fatal("login still running after close", e)
	}
	var count int
	if e := f.service.db.QueryRow(`SELECT count(*) FROM steam_accounts`).Scan(&count); e != nil || count != 0 || f.service.LoginStatus().State != "idle" {
		t.Fatalf("cancelled login bound account count=%d error=%v", count, e)
	}
}

func TestLoginRejectionAndFinalizationRetry(t *testing.T) {
	f := newLoginFixture(t, []int{4})
	f.reject.Store(true)
	if _, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password"); e != nil {
		t.Fatal(e)
	}
	if state := waitLoginState(t, f.service, "failed"); !strings.Contains(state.Error, "未获确认") {
		t.Fatal(state)
	}
	f.reject.Store(false)
	f.accept.Store(true)
	f.finalFailures.Store(1)
	if _, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password"); e != nil {
		t.Fatal(e)
	}
	waitLoginState(t, f.service, "done")
	if f.begin.Load() != 2 || f.final.Load() != 2 {
		t.Fatalf("repeated user auth instead of finalize retry: begin=%d final=%d", f.begin.Load(), f.final.Load())
	}
}

func TestLoginHTTPStatusValidationAndBusyContract(t *testing.T) {
	f := newLoginFixture(t, []int{3})
	mux := http.NewServeMux()
	f.service.Register(mux)
	for _, test := range []struct {
		method, path, body string
		status             int
	}{
		{"GET", "/status", "", 200},
		{"POST", "/code", `{"code":"RIGHT"}`, 409},
		{"POST", "/start", `{"account_name":"fixture-user"}`, 422},
		{"POST", "/start", `{"account_name":"fixture-user","password":"fixture-password"}`, 200},
		{"POST", "/start", `{"account_name":"fixture-user","password":"fixture-password"}`, 409},
		{"POST", "/code", `{"code":null}`, 422},
		{"POST", "/cancel", "", 200},
	} {
		w := httptest.NewRecorder()
		mux.ServeHTTP(w, httptest.NewRequest(test.method, "/api/v1/account/login"+test.path, strings.NewReader(test.body)))
		if w.Code != test.status || strings.Contains(w.Body.String(), "fixture-password") {
			t.Fatalf("%s %s code=%d body=%s", test.method, test.path, w.Code, w.Body.String())
		}
	}
}

func TestLoginRejectsUnknownCAWithoutRetryOrCredentialDelivery(t *testing.T) {
	var requests, retries atomic.Int32
	untrusted := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { requests.Add(1) }))
	t.Cleanup(untrusted.Close)
	s, _ := serviceFixture(t, func(w http.ResponseWriter, r *http.Request) { t.Error("unexpected account request") })
	s.opts.Steam.opts.AuthBaseURL = untrusted.URL
	s.opts.Steam.opts.HTTPClient = &http.Client{Transport: http.DefaultTransport.(*http.Transport).Clone()}
	s.opts.Steam.opts.Sleep = func(context.Context, time.Duration) error { retries.Add(1); return nil }
	result, e := s.StartLogin(context.Background(), "fixture-user", "fixture-password")
	if e != nil || result["ok"] != false || !strings.Contains(s.LoginStatus().Error, "证书校验失败") || requests.Load() != 0 || retries.Load() != 0 {
		t.Fatalf("untrusted TLS result=%v error=%v requests=%d retries=%d", result, e, requests.Load(), retries.Load())
	}
}

type loginRoundTripFunc func(*http.Request) (*http.Response, error)

func (f loginRoundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestLoginConnectionRetryAndWrongOwnerCookieRejection(t *testing.T) {
	f := newLoginFixture(t, []int{4})
	f.accept.Store(true)
	f.wrongOwner.Store(true)
	var rsaCalls, retries atomic.Int32
	f.service.opts.Steam.opts.HTTPClient = &http.Client{Transport: loginRoundTripFunc(func(r *http.Request) (*http.Response, error) {
		if strings.Contains(r.URL.Path, "GetPasswordRSAPublicKey") && rsaCalls.Add(1) < 3 {
			return nil, &net.OpError{Op: "dial", Net: "tcp", Err: fmt.Errorf("fixture transient failure")}
		}
		return http.DefaultTransport.RoundTrip(r)
	})}
	f.service.opts.Steam.opts.Sleep = func(ctx context.Context, d time.Duration) error {
		if d == 800*time.Millisecond || d == 1600*time.Millisecond {
			retries.Add(1)
			return ctx.Err()
		}
		timer := time.NewTimer(d)
		defer timer.Stop()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C:
			return nil
		}
	}
	if result, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password"); e != nil || result["ok"] != true {
		t.Fatalf("retry start=%v %v", result, e)
	}
	waitLoginState(t, f.service, "failed")
	status, e := f.service.Status(context.Background())
	if e != nil || status["has_cookie"] != false || f.begin.Load() != 1 || f.final.Load() != 1 || rsaCalls.Load() != 3 || retries.Load() != 2 {
		t.Fatalf("wrong-owner cookie/retry result=%v %v begin=%d final=%d rsa=%d retries=%d", status, e, f.begin.Load(), f.final.Load(), rsaCalls.Load(), retries.Load())
	}
}

func TestLoginEmailCodeAndTrustedDevicePaths(t *testing.T) {
	t.Run("email code", func(t *testing.T) {
		f := newLoginFixture(t, []int{2})
		f.expectedGuard = 2
		if result, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password"); e != nil || result["ok"] != true {
			t.Fatalf("email start=%v %v", result, e)
		}
		if state := f.service.LoginStatus(); state.State != "awaiting_code" || state.CodeHint != "email" || !state.CodeAvailable {
			t.Fatalf("email state=%+v", state)
		}
		if result, e := f.service.SubmitLoginCode(context.Background(), "WRONG"); e != nil || result["ok"] != false || result["error"] != "验证码不正确，请重试" {
			t.Fatalf("email code rejection=%v %v", result, e)
		}
		if result, e := f.service.SubmitLoginCode(context.Background(), "RIGHT"); e != nil || result["ok"] != true {
			t.Fatalf("email code=%v %v", result, e)
		}
		waitLoginState(t, f.service, "done")
	})
	t.Run("trusted device", func(t *testing.T) {
		f := newLoginFixture(t, []int{3})
		f.deviceTrusted.Store(true)
		f.accept.Store(true)
		if result, e := f.service.StartLogin(context.Background(), "fixture-user", "fixture-password"); e != nil || result["ok"] != true {
			t.Fatalf("trusted device start=%v %v", result, e)
		}
		if state := f.service.LoginStatus(); state.State != "awaiting_confirmation" || !strings.Contains(state.Message, "本机已获 Steam 信任") {
			t.Fatalf("trusted device state=%+v", state)
		}
		waitLoginState(t, f.service, "done")
	})
}
