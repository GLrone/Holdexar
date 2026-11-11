package proxypool

import (
	"context"
	"database/sql"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/store"
)

func cycleFixture(t *testing.T) (*Cycle, *HealthChecker, *stubController) {
	t.Helper()
	h, controller, target, _ := newHealthChecker(t, 0)
	seedNode(t, h.DB, "first", NodeNew, 0, int64(1))
	seedNode(t, h.DB, "second", NodeNew, 0, int64(1))
	if _, err := h.DB.Exec(`
		ALTER TABLE proxy_subscriptions ADD COLUMN admission_status TEXT;
		ALTER TABLE proxy_subscriptions ADD COLUMN last_stats JSON;
		INSERT INTO proxy_subscriptions(id, url) VALUES(1, 'https://fixture.invalid/list');
		UPDATE proxy_nodes SET normalized_config='{"type":"ss","server":"example.test","port":443}';
		CREATE TABLE app_settings (key TEXT PRIMARY KEY, value_json JSON, updated_at DATETIME);
	`); err != nil {
		t.Fatal(err)
	}
	conn := mustConn(t, h.DB)
	if err := EnsureHealthSchema(context.Background(), conn); err != nil {
		t.Fatal(err)
	}
	if err := conn.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(PoolPath(h.DataDir)), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(PoolPath(h.DataDir), []byte("proxies:\n  - name: first\n  - name: second\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	target.browseOK.Store(true)
	controller.now["GLOBAL"] = "first"
	TakeRebuildPending()
	t.Cleanup(func() { TakeRebuildPending() })
	return &Cycle{DB: h.DB, Gate: h.Gate, DataDir: h.DataDir, Now: h.Now,
		ProbeL1URL: target.server.URL + "/ip",
		ProbeL2URL: target.server.URL + "/IStoreBrowseService/GetItems/v1/"}, h, controller
}

func TestCycleRunsL0WhileCrawlOwnsRuntime(t *testing.T) {
	c, checker, controller := cycleFixture(t)
	if err := occupancy.BeginCrawl("cycle-test"); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(occupancy.EndCrawl)
	ctx := context.Background()
	if _, err := c.RunL0(ctx, checker); err != nil {
		t.Fatal(err)
	}
	if got := queryInt(t, c.DB, `SELECT COUNT(*) FROM proxy_nodes WHERE l0_delay_ms=42`); got != 2 {
		t.Fatalf("L0 delay rows = %d, want 2", got)
	}
	result, err := c.RunMaintenance(ctx, checker)
	if err != nil || result["busy"] != true {
		t.Fatalf("busy maintenance = %v, %v", result, err)
	}
	if got := queryInt(t, c.DB, `SELECT COUNT(*) FROM health_runs`); got != 0 {
		t.Fatalf("health runs while occupied = %d, want 0", got)
	}
	controller.mu.Lock()
	defer controller.mu.Unlock()
	if got := controller.now["GLOBAL"]; got != "first" {
		t.Fatalf("GLOBAL while occupied = %q, want first", got)
	}
}

func TestCycleL0RequestsRebuildWhenDelayChangesSelectedRepresentative(t *testing.T) {
	c, checker, _ := cycleFixture(t)
	if _, err := c.DB.Exec(`UPDATE proxy_nodes SET exit_ip='192.0.2.1',
		l0_delay_ms=CASE runtime_name WHEN 'first' THEN 70 ELSE 10 END`); err != nil {
		t.Fatal(err)
	}
	if _, err := c.RunL0(t.Context(), checker); err != nil {
		t.Fatal(err)
	}
	if !TakeRebuildPending() {
		t.Fatal("L0 changed the chosen representative without requesting a rebuild")
	}
}

func TestCycleMaintenanceLinksObservationsAndThrottlesByEligibleNames(t *testing.T) {
	c, checker, controller := cycleFixture(t)
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	c.Bus = events.NewBus()
	subscriber := c.Bus.Subscribe()
	result, err := c.RunMaintenance(ctx, checker)
	if err != nil || result["maintenance"] != "ran" {
		t.Fatalf("maintenance = %v, %v", result, err)
	}
	if got := queryInt(t, c.DB, `SELECT COUNT(*) FROM health_runs WHERE channel='scheduled' AND finished_at IS NOT NULL AND total=2 AND steam_ok=2 AND ip_known=2`); got != 1 {
		t.Fatalf("completed health runs = %d, want 1", got)
	}
	if got := queryInt(t, c.DB, `SELECT COUNT(*) FROM health_observations o JOIN health_runs r ON r.id=o.run_id WHERE o.level IN ('L1','L2')`); got != 4 {
		t.Fatalf("observations linked to scheduled run = %d, want 4", got)
	}
	for _, want := range []struct{ name, data string }{
		{"maintenance.started", `"totals": {"l1": 2, "l2": 2}`},
		{"settings.changed", `proxy.pool_maintenance_last_sig`},
		{"maintenance.ended", `"total": 2, "ok": 2, "failed": 0`},
	} {
		event, err := subscriber.Get(ctx)
		if err != nil || event.Type != want.name || !strings.Contains(event.AsSSE(), want.data) {
			t.Fatalf("event %s = %s, %v", want.name, event.AsSSE(), err)
		}
	}
	if !TakeRebuildPending() {
		t.Fatal("learned exit identities did not request a rebuild")
	}
	controller.mu.Lock()
	selected := controller.now["GLOBAL"]
	controller.mu.Unlock()
	if selected != "first" {
		t.Fatalf("GLOBAL after maintenance = %q, want first", selected)
	}
	result, err = c.RunMaintenance(ctx, checker)
	if err != nil || result["maintenance"] != "throttled" {
		t.Fatalf("maintenance with same signature = %v, %v", result, err)
	}
	if _, err := c.DB.Exec(`UPDATE proxy_nodes SET state='DEAD' WHERE runtime_name='second'`); err != nil {
		t.Fatal(err)
	}
	result, err = c.RunMaintenance(ctx, checker)
	if err != nil || result["maintenance"] != "ran" {
		t.Fatalf("maintenance after membership change = %v, %v", result, err)
	}
	c.Now = func() time.Time { return checker.Now().Add(2 * time.Hour) }
	result, err = c.RunMaintenance(ctx, checker)
	if err != nil || result["maintenance"] != "ran" {
		t.Fatalf("maintenance at interval = %v, %v", result, err)
	}
}

func TestCycleMaintenanceFinishesFailedRunAndReleasesWriteGateForNetwork(t *testing.T) {
	c, checker, controller := cycleFixture(t)
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	if _, err := c.DB.Exec(`CREATE TRIGGER fail_l2 BEFORE INSERT ON health_observations
		WHEN NEW.level='L2' BEGIN SELECT RAISE(ABORT, 'injected failure'); END`); err != nil {
		t.Fatal(err)
	}
	probing := make(chan struct{})
	release := make(chan struct{})
	var started, released sync.Once
	defer released.Do(func() { close(release) })
	blocked := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started.Do(func() { close(probing) })
		select {
		case <-release:
			_, _ = w.Write([]byte("192.0.2.55"))
		case <-r.Context().Done():
		}
	}))
	t.Cleanup(blocked.Close)
	c.ProbeL1URL = blocked.URL
	resultCh := make(chan error, 1)
	go func() {
		_, err := c.RunMaintenance(ctx, checker)
		resultCh <- err
	}()
	select {
	case <-probing:
	case <-ctx.Done():
		t.Fatal("L1 probe did not reach target")
	}
	if occupancy.MaintenanceKind() != occupancy.MaintenanceHealth {
		t.Fatal("maintenance reservation missing during network probe")
	}
	if got := queryInt(t, c.DB, `SELECT COUNT(*) FROM health_runs WHERE finished_at IS NULL`); got != 1 {
		t.Fatalf("in-flight health runs = %d, want 1", got)
	}
	writeCtx, cancelWrite := context.WithTimeout(ctx, 2*time.Second)
	defer cancelWrite()
	if err := c.Gate.WithWrite(writeCtx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(writeCtx, `INSERT INTO app_settings (key, value_json) VALUES ('cycle-test', 'true')`)
		return err
	}); err != nil {
		t.Fatalf("unrelated write during network probe failed: %v", err)
	}
	released.Do(func() { close(release) })
	select {
	case err := <-resultCh:
		if err == nil {
			t.Fatal("expected injected L2 persistence error")
		}
	case <-ctx.Done():
		t.Fatal("maintenance did not finish")
	}
	if got := queryInt(t, c.DB, `SELECT COUNT(*) FROM health_runs WHERE end_reason='l2_error' AND finished_at IS NOT NULL AND steam_ok=0 AND ip_known=2`); got != 1 {
		t.Fatalf("finished failed runs = %d, want 1", got)
	}
	if occupancy.CrawlerBusy() {
		t.Fatal("failed run left a maintenance reservation")
	}
	controller.mu.Lock()
	selected := controller.now["GLOBAL"]
	controller.mu.Unlock()
	if selected != "first" {
		t.Fatalf("GLOBAL after failed maintenance = %q, want first", selected)
	}
	if _, err := c.DB.Exec(`DROP TRIGGER fail_l2`); err != nil {
		t.Fatal(err)
	}
	if result, err := c.RunMaintenance(ctx, checker); err != nil || result["health_ok"] != int64(2) {
		t.Fatalf("retry after failed run = %v, %v", result, err)
	}
}
