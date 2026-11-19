package updateguard

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/server-go/internal/config"
)

func resetGuardState(t *testing.T) {
	t.Helper()
	t.Cleanup(func() {
		current.protected.Store(false)
		current.pageReady.Store(false)
		current.attemptID.Store("")
		current.token.Store("")
		current.dataDirPath.Store("")
	})
	current.protected.Store(false)
	current.pageReady.Store(false)
	current.attemptID.Store("")
	current.token.Store("")
	current.dataDirPath.Store("")
}

func clearUpdateEnv(t *testing.T) {
	t.Helper()
	t.Setenv(config.EnvPrefix+"UPDATE_ATTEMPT_ID", "")
	t.Setenv(config.EnvPrefix+"INSTALL_ROOT", "")
	t.Setenv(config.EnvPrefix+"UPDATE_DATA_TOKEN", "")
}

func writeGuardJSON(t *testing.T, path string, value any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", path, err)
	}
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("marshal %s: %v", path, err)
	}
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatalf("write %s: %v", path, err)
	}
}

func TestHandleUpdateReadyValidationAndStateTransitions(t *testing.T) {
	resetGuardState(t)

	cases := []struct {
		name       string
		body       string
		nilBody    bool
		active     bool
		attemptID  string
		wantStatus int
		wantDetail string
		wantReady  bool
	}{
		{name: "nil body", nilBody: true, wantStatus: http.StatusBadRequest, wantDetail: "请求体无效"},
		{name: "empty body", body: "", wantStatus: http.StatusBadRequest, wantDetail: "请求体无效"},
		{name: "invalid json", body: "{", wantStatus: http.StatusBadRequest, wantDetail: "请求体无效"},
		{name: "json null", body: "null", wantStatus: http.StatusBadRequest, wantDetail: "请求体无效"},
		{name: "json array", body: `["a"]`, wantStatus: http.StatusBadRequest, wantDetail: "请求体无效"},
		{name: "json string", body: `"att-1"`, wantStatus: http.StatusBadRequest, wantDetail: "请求体无效"},
		{name: "trailing json", body: `{"attemptId":"att-1"} {}`, wantStatus: http.StatusBadRequest, wantDetail: "请求体无效"},
		{name: "inactive guard", body: `{"attemptId":"att-1"}`, active: false, wantStatus: http.StatusConflict, wantDetail: "当前不在更新验收期间"},
		{name: "missing attemptId when active", body: `{}`, active: true, attemptID: "att-1", wantStatus: http.StatusConflict, wantDetail: "当前不在更新验收期间"},
		{name: "mismatched attemptId", body: `{"attemptId":"wrong"}`, active: true, attemptID: "att-1", wantStatus: http.StatusConflict, wantDetail: "当前不在更新验收期间"},
		{name: "matching attemptId", body: `{"attemptId":"att-1"}`, active: true, attemptID: "att-1", wantStatus: http.StatusOK, wantReady: true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			current.protected.Store(tc.active)
			current.attemptID.Store(tc.attemptID)
			current.pageReady.Store(false)

			var req *http.Request
			if tc.nilBody {
				req = httptest.NewRequest(http.MethodPost, "/api/v1/system/update-ready", nil)
				req.Body = nil
			} else {
				req = httptest.NewRequest(http.MethodPost, "/api/v1/system/update-ready", strings.NewReader(tc.body))
			}
			req.RemoteAddr = "127.0.0.1:1234"
			rec := httptest.NewRecorder()
			HandleUpdateReady(rec, req)

			if rec.Code != tc.wantStatus {
				t.Fatalf("status = %d, want %d, body = %s", rec.Code, tc.wantStatus, rec.Body.String())
			}
			var payload map[string]any
			if err := json.Unmarshal(rec.Body.Bytes(), &payload); err != nil {
				t.Fatalf("unmarshal response: %v", err)
			}
			if tc.wantDetail != "" && payload["detail"] != tc.wantDetail {
				t.Fatalf("detail = %v, want %q", payload["detail"], tc.wantDetail)
			}
			if tc.wantReady {
				if payload["ready"] != true || !PageReady() {
					t.Fatalf("expected page ready, payload=%v pageReady=%v", payload, PageReady())
				}
			} else if PageReady() {
				t.Fatalf("expected PageReady() == false")
			}
		})
	}

	t.Run("non-loopback rejected with 403", func(t *testing.T) {
		current.protected.Store(true)
		current.attemptID.Store("att-1")
		current.pageReady.Store(false)
		for _, remote := range []string{"192.168.1.25:51234", "192.0.2.1:1234", ""} {
			req := httptest.NewRequest(http.MethodPost, "/api/v1/system/update-ready", strings.NewReader(`{"attemptId":"att-1"}`))
			req.RemoteAddr = remote
			rec := httptest.NewRecorder()
			HandleUpdateReady(rec, req)
			if rec.Code != http.StatusForbidden {
				t.Fatalf("remote=%q status = %d, want 403, body = %s", remote, rec.Code, rec.Body.String())
			}
			if PageReady() {
				t.Fatalf("remote=%q must not flip PageReady()", remote)
			}
		}
	})
}

func TestMiddlewareBlocksWritesWhileActive(t *testing.T) {
	resetGuardState(t)

	nextCalled := 0
	handler := Middleware(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		nextCalled++
		w.WriteHeader(http.StatusNoContent)
	}))

	current.protected.Store(true)

	for _, tc := range []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/api/v1/health"},
		{http.MethodHead, "/api/v1/health"},
		{http.MethodOptions, "/api/v1/settings"},
		{http.MethodPost, "/api/v1/system/update-ready"},
		{http.MethodPost, "/non-api/action"},
	} {
		req := httptest.NewRequest(tc.method, tc.path, nil)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusNoContent {
			t.Fatalf("%s %s code = %d, want 204", tc.method, tc.path, rec.Code)
		}
	}

	for _, method := range []string{http.MethodPost, http.MethodPut, http.MethodPatch, http.MethodDelete} {
		req := httptest.NewRequest(method, "/api/v1/settings", nil)
		rec := httptest.NewRecorder()
		handler.ServeHTTP(rec, req)
		if rec.Code != http.StatusServiceUnavailable {
			t.Fatalf("%s /api/v1/settings code = %d, want 503", method, rec.Code)
		}
	}

	ClearProtection()
	req := httptest.NewRequest(http.MethodPost, "/api/v1/settings", nil)
	rec := httptest.NewRecorder()
	handler.ServeHTTP(rec, req)
	if rec.Code != http.StatusNoContent {
		t.Fatalf("after ClearProtection code = %d, want 204", rec.Code)
	}
	if nextCalled != 6 {
		t.Fatalf("nextCalled = %d, want 6", nextCalled)
	}
}

func TestActivateNormalAndProtectedLifecycle(t *testing.T) {
	resetGuardState(t)
	clearUpdateEnv(t)

	dataDir := filepath.Join(t.TempDir(), "data")
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}

	tok, err := Activate(dataDir)
	if err != nil || tok != "" || Active() {
		t.Fatalf("clean Activate = (%q, %v), Active=%v", tok, err, Active())
	}

	installRoot := filepath.Join(t.TempDir(), "root")
	writeGuardJSON(t, MarkerPath(dataDir), map[string]any{
		"purpose":     "update",
		"attemptId":   "att-1",
		"token":       "tok-1",
		"installRoot": filepath.Clean(installRoot),
		"dataDir":     filepath.Clean(dataDir),
		"pid":         os.Getpid(),
	})
	if _, err := Activate(dataDir); err == nil {
		t.Fatalf("expected error when env vars missing with update marker")
	}

	writeGuardJSON(t, filepath.Join(installRoot, "state", "transaction.json"), map[string]any{
		"attemptId":                     "att-1",
		"dataToken":                     "tok-1",
		"dataDir":                       filepath.Clean(dataDir),
		"dataDbPath":                    dbPathOf(dataDir),
		"dataPrepared":                  true,
		"dataAccepted":                  false,
		"dataRecoveryPhase":             "prepared",
		"dataProbeRegistrationRequired": true,
		"launchPending":                 false,
		"children": []any{
			map[string]any{"pid": os.Getpid(), "stopped": false},
		},
	})
	t.Setenv(config.EnvPrefix+"UPDATE_ATTEMPT_ID", "att-1")
	t.Setenv(config.EnvPrefix+"INSTALL_ROOT", installRoot)
	t.Setenv(config.EnvPrefix+"UPDATE_DATA_TOKEN", "tok-1")

	tok, err = Activate(dataDir)
	if err != nil {
		t.Fatalf("valid Activate failed: %v", err)
	}
	if tok != "tok-1" || !Active() || AttemptID() != "att-1" || Token() != "tok-1" || DataDir() != filepath.Clean(dataDir) {
		t.Fatalf("unexpected state tok=%q active=%v attempt=%q token=%q dataDir=%q",
			tok, Active(), AttemptID(), Token(), DataDir())
	}

	resetGuardState(t)
	t.Setenv(config.EnvPrefix+"UPDATE_DATA_TOKEN", "wrong-tok")
	if _, err := Activate(dataDir); err == nil {
		t.Fatalf("expected error on token mismatch")
	}
}

func TestProcessAlive(t *testing.T) {
	if !processAlive(os.Getpid()) {
		t.Fatalf("expected current PID %d to be alive", os.Getpid())
	}
	if processAlive(0) {
		t.Fatalf("expected PID 0 not alive")
	}
}
