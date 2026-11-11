package systemx

import (
	"database/sql"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"holdexar/server-go/internal/config"

	_ "modernc.org/sqlite"
)

// infoKeys / readinessKeys 与 Python 侧响应逐字段对齐的形状契约。
var (
	infoKeys = []string{
		"app", "version", "python", "platform",
		"data_dir", "web_dist_ready", "uptime_seconds", "repo",
	}
	readinessKeys = []string{
		"httpReady", "schedulerReady", "collectionReady", "writeProtected",
		"automaticEnabled", "transportReady", "strategy", "lanes",
		"reason", "activeJobId",
	}
)

func newTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatalf("打开内存库失败：%v", err)
	}
	t.Cleanup(func() { db.Close() })
	return db
}

// stubSources 替换数据目录与前端产物判定，避免测试触碰真实文件系统。
func stubSources(t *testing.T, dataDir string, distReady bool) {
	t.Helper()
	origDataDir, origDist := resolveDataDir, distDirReady
	resolveDataDir = func() string { return dataDir }
	distDirReady = func() bool { return distReady }
	t.Cleanup(func() { resolveDataDir, distDirReady = origDataDir, origDist })
}

func getJSON(t *testing.T, h http.Handler, path string) (int, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, path, nil))
	if rec.Code != http.StatusOK {
		return rec.Code, nil
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("响应不是合法 JSON：%v（%s）", err, rec.Body.String())
	}
	return rec.Code, body
}

func assertKeys(t *testing.T, body map[string]any, want []string) {
	t.Helper()
	if len(body) != len(want) {
		t.Fatalf("字段数不符：得 %d 想要 %d（%v）", len(body), len(want), body)
	}
	for _, key := range want {
		if _, ok := body[key]; !ok {
			t.Fatalf("缺少字段 %s（实际字段集 %v）", key, body)
		}
	}
}

func assertString(t *testing.T, body map[string]any, key, want string) {
	t.Helper()
	got, ok := body[key].(string)
	if !ok {
		t.Fatalf("%s 应为字符串，实际 %v", key, body[key])
	}
	if got != want {
		t.Fatalf("%s = %q，想要 %q", key, got, want)
	}
}

func TestInfoShape(t *testing.T) {
	stubSources(t, `E:\tmp-holdexar-data`, true)
	sys := &System{StartedAt: time.Now().Add(-1500 * time.Millisecond), DataDir: `E:\tmp-holdexar-data`}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/info", sys.handleInfo)

	code, body := getJSON(t, mux, "/api/v1/info")
	if code != http.StatusOK {
		t.Fatalf("状态码 %d，想要 200", code)
	}
	assertKeys(t, body, infoKeys)
	assertString(t, body, "app", config.AppName())
	assertString(t, body, "version", config.Version())
	assertString(t, body, "data_dir", `E:\tmp-holdexar-data`)
	assertString(t, body, "repo", githubRepo)
	if body["web_dist_ready"] != true {
		t.Fatalf("web_dist_ready 应为 true，实际 %v", body["web_dist_ready"])
	}
	uptime, ok := body["uptime_seconds"].(float64)
	if !ok || uptime < 1.0 {
		t.Fatalf("uptime_seconds 应为大于 1 的数值，实际 %v", body["uptime_seconds"])
	}
	if _, ok := body["python"].(string); !ok {
		t.Fatalf("python 应为字符串，实际 %v", body["python"])
	}
	if _, ok := body["platform"].(string); !ok {
		t.Fatalf("platform 应为字符串，实际 %v", body["platform"])
	}
}

func TestReadinessReadyAndNotReady(t *testing.T) {
	db := newTestDB(t)
	sys := &System{DB: db, StartedAt: time.Now()}
	mux := http.NewServeMux()
	mux.HandleFunc("/api/v1/system/readiness", sys.handleReadiness)

	code, body := getJSON(t, mux, "/api/v1/system/readiness")
	if code != http.StatusOK {
		t.Fatalf("就绪态状态码 %d，想要 200", code)
	}
	assertKeys(t, body, readinessKeys)
	if body["httpReady"] != true {
		t.Fatalf("httpReady 应为 true，实际 %v", body["httpReady"])
	}
	for _, key := range []string{"schedulerReady", "collectionReady", "writeProtected", "automaticEnabled", "transportReady"} {
		if body[key] != false {
			t.Fatalf("%s 未接线应显式 false，实际 %v", key, body[key])
		}
	}
	assertString(t, body, "strategy", "proxy_first")
	if body["lanes"] != float64(0) {
		t.Fatalf("lanes 应为 0，实际 %v", body["lanes"])
	}
	assertString(t, body, "reason", "")
	if value, ok := body["activeJobId"]; !ok || value != nil {
		t.Fatalf("activeJobId 空闲应为 null（键在场），实际 %v", body["activeJobId"])
	}

	// 未就绪态：库连接关闭后仍回 200，reason 承载数据库不可达原因。
	if err := db.Close(); err != nil {
		t.Fatalf("关闭内存库失败：%v", err)
	}
	code, body = getJSON(t, mux, "/api/v1/system/readiness")
	if code != http.StatusOK {
		t.Fatalf("未就绪态状态码 %d，想要 200（与 Python 侧一致）", code)
	}
	reason, ok := body["reason"].(string)
	if !ok || reason == "" {
		t.Fatalf("未就绪态 reason 应非空，实际 %v", body["reason"])
	}
}

func TestRegisterRoutes(t *testing.T) {
	stubSources(t, t.TempDir(), false)
	mux := http.NewServeMux()
	Register(mux, newTestDB(t), nil)

	for _, path := range []string{"/api/v1/info", "/api/v1/system/readiness"} {
		code, body := getJSON(t, mux, path)
		if code != http.StatusOK {
			t.Fatalf("GET %s 状态码 %d，想要 200", path, code)
		}
		if len(body) == 0 {
			t.Fatalf("GET %s 响应为空", path)
		}
	}
	// 非读方法按 405 拒绝。
	rec := httptest.NewRecorder()
	mux.ServeHTTP(rec, httptest.NewRequest(http.MethodPost, "/api/v1/info", nil))
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /api/v1/info 状态码 %d，想要 405", rec.Code)
	}
}
