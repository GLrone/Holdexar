package proxypool

import (
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strconv"
	"strings"
	"testing"

	"holdexar/server-go/internal/store"
)

func TestManualHealthChecksThroughProxyAndDisablesFifthFailure(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Host != "fixture.test" {
			t.Errorf("not a proxied request: %s", r.URL)
		}
		fmt.Fprint(w, "<html>blocked</html>")
	}))
	defer server.Close()
	host, portText, _ := net.SplitHostPort(strings.TrimPrefix(server.URL, "http://"))
	port, _ := strconv.Atoi(portText)
	if _, err := db.Exec(`INSERT INTO proxies(id,scheme,host,port,enabled,status,consecutive_failures) VALUES(1,'http',?,?,1,'failed',4)`, host, port); err != nil {
		t.Fatal(err)
	}
	checker := &ManualHealth{DB: db, Gate: store.NewWriteGate(db, path), DataDir: filepath.Dir(path), Target: "http://fixture.test/store"}
	result, err := checker.Check(t.Context(), 0, true)
	if err != nil || len(result) != 1 || !strings.Contains(string(result[0]), `"testError":"非 JSON`) {
		t.Fatalf("result=%s err=%v", result, err)
	}
	var enabled, failures, logs int
	if err := db.QueryRow(`SELECT enabled,consecutive_failures,(SELECT count(*) FROM proxy_events WHERE kind='test') FROM proxies WHERE id=1`).Scan(&enabled, &failures, &logs); err != nil || enabled != 0 || failures != 5 || logs != 1 {
		t.Fatalf("enabled=%d fails=%d events=%d err=%v", enabled, failures, logs, err)
	}
}
