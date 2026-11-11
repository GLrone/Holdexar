package proxypool

import (
	"context"
	"database/sql"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"holdexar/server-go/internal/store"
)

const subscriptionRoutesPath = "/api/v1/proxies/subscriptions"

func subscriptionRouteDB(t *testing.T) (*sql.DB, *store.WriteGate, string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "fixture.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	if err := store.InitializeFreshDatabase(t.Context(), db); err != nil {
		t.Fatal(err)
	}
	return db, store.NewWriteGate(db, path), dir
}

func subscriptionRouteRequest(t *testing.T, server *httptest.Server, method, suffix, input string, status int) string {
	t.Helper()
	req, err := http.NewRequestWithContext(t.Context(), method, server.URL+subscriptionRoutesPath+suffix, strings.NewReader(input))
	if err != nil {
		t.Fatal(err)
	}
	resp, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if resp.StatusCode != status {
		t.Fatalf("%s %s status = %d, want %d: %s", method, suffix, resp.StatusCode, status, raw)
	}
	return string(raw)
}

func TestSubscriptionRoutesSaveRefreshAndReveal(t *testing.T) {
	db, gate, dir := subscriptionRouteDB(t)
	var fetches atomic.Int32
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		fetches.Add(1)
		w.Header().Set("Subscription-Userinfo", "upload=1; download=2; total=30")
		_, _ = io.WriteString(w, validSubscriptionBody)
	}))
	t.Cleanup(provider.Close)
	syncer := Syncer{DB: db, Gate: gate, DataDir: dir}
	mux := http.NewServeMux()
	RegisterSubscriptionRoutes(mux, db, gate, dir, func(ctx context.Context, _ int64) (*SyncResult, error) {
		return syncer.SyncSubscriptions(ctx)
	}, nil)
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)

	const credential = "fixture-private-segment"
	url := provider.URL + "/" + credential + "?token=fixture-query"
	input, _ := json.Marshal(map[string]string{"kind": "clash", "url": url, "label": " Test subscription "})
	added := subscriptionRouteRequest(t, server, "POST", "", string(input), http.StatusOK)
	if !strings.Contains(added, `"nodes":1`) || strings.Contains(added, credential) || fetches.Load() != 1 {
		t.Fatalf("add did not run the shared fetch or exposed its URL: %s, fetches=%d", added, fetches.Load())
	}
	listed := subscriptionRouteRequest(t, server, "GET", "?kind=clash", "", http.StatusOK)
	if !strings.Contains(listed, provider.URL+"/***") || !strings.Contains(listed, `"label":"Test subscription"`) || strings.Contains(listed, credential) {
		t.Fatalf("list did not mask the URL or project the persisted label: %s", listed)
	}
	if plain := subscriptionRouteRequest(t, server, "GET", "?kind=plain", "", http.StatusOK); !strings.Contains(plain, `"items":[]`) {
		t.Fatalf("kind filter did not return an empty list: %s", plain)
	}
	revealed := subscriptionRouteRequest(t, server, "GET", "/1/url", "", http.StatusOK)
	if !strings.Contains(revealed, url) {
		t.Fatalf("explicit reveal did not open the stored URL: %s", revealed)
	}
	var sealed string
	if err := db.QueryRow(`SELECT url FROM proxy_subscriptions WHERE id=1`).Scan(&sealed); err != nil {
		t.Fatal(err)
	}
	if !strings.HasPrefix(sealed, "enc1:") || strings.Contains(sealed, credential) {
		t.Fatal("subscription credential was persisted without sealing")
	}
	refreshed := subscriptionRouteRequest(t, server, "POST", "/1/refresh", "", http.StatusOK)
	if !strings.Contains(refreshed, `"traffic":"upload=1; download=2; total=30"`) || fetches.Load() != 2 {
		t.Fatalf("traffic endpoint did not return the same refresh result: %s, fetches=%d", refreshed, fetches.Load())
	}
	full := subscriptionRouteRequest(t, server, "POST", "/1/sync", "", http.StatusOK)
	if !strings.Contains(full, `"nodes":1`) || fetches.Load() != 3 {
		t.Fatalf("manual sync did not return the shared refresh result: %s, fetches=%d", full, fetches.Load())
	}
}

func TestSubscriptionRoutesReplaceURLAndRetainUserInputWhenFetchFails(t *testing.T) {
	db, gate, dir := subscriptionRouteDB(t)
	provider := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, "fixture failure", http.StatusServiceUnavailable)
	}))
	t.Cleanup(provider.Close)
	if _, err := db.Exec(`INSERT INTO proxy_subscriptions (id,kind,url,label,deprecated,admission_status,last_stats,last_imported_at,cache_url_identity)
		VALUES (1,'clash','https://fixture.invalid/old','Provider label',1,'ACTIVE','{"labelSource":"panel","alive":2,"traffic":"expire=1"}','2026-11-08 10:11:12.000000','old')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`INSERT INTO proxy_nodes (node_id,runtime_name,proxy_type,state,subscription_id,consecutive_failures)
		VALUES ('old-node','old-node','ss','ACTIVE',1,0)`); err != nil {
		t.Fatal(err)
	}
	syncer := Syncer{DB: db, Gate: gate, DataDir: dir}
	mux := http.NewServeMux()
	var invalidations atomic.Int32
	RegisterSubscriptionRoutes(mux, db, gate, dir, func(ctx context.Context, _ int64) (*SyncResult, error) {
		return syncer.SyncSubscriptions(ctx)
	}, func() { invalidations.Add(1) })
	server := httptest.NewServer(mux)
	t.Cleanup(server.Close)
	newURL := provider.URL + "/new-fixture-credential"
	input, _ := json.Marshal(map[string]string{"url": newURL})
	updated := subscriptionRouteRequest(t, server, "PUT", "/1", string(input), http.StatusOK)
	if !strings.Contains(updated, `"warning":`) || strings.Contains(updated, newURL) || invalidations.Load() != 1 {
		t.Fatalf("changed subscription did not safely report its failed refresh: %s", updated)
	}
	var url, admission, fetchStatus string
	var stats, imported, identity, label sql.NullString
	var deprecated bool
	if err := db.QueryRow(`SELECT url,admission_status,last_fetch_status,last_stats,last_imported_at,cache_url_identity,label,deprecated FROM proxy_subscriptions WHERE id=1`).Scan(
		&url, &admission, &fetchStatus, &stats, &imported, &identity, &label, &deprecated,
	); err != nil {
		t.Fatal(err)
	}
	if OpenURL(dir, url) != newURL || admission != AdmissionCandidate || fetchStatus != "FAILED" || stats.Valid || imported.Valid || identity.Valid || label.Valid || deprecated {
		t.Fatalf("URL change preserved old facts or lost user input: admission=%s status=%s stats=%v imported=%v identity=%v label=%v deprecated=%v",
			admission, fetchStatus, stats, imported, identity, label, deprecated)
	}
	var oldOwners int
	if err := db.QueryRow(`SELECT COUNT(*) FROM proxy_nodes WHERE subscription_id=1`).Scan(&oldOwners); err != nil || oldOwners != 0 {
		t.Fatalf("old URL's nodes still belong to the new subscription: %d, %v", oldOwners, err)
	}
}
