package proxypool

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"
)

func TestLiveCandidateFirstCheck(t *testing.T) {
	exe := liveKernelExe(t)
	db, gate, dataDir := newAdmissionDB(t)
	conn := mustConn(t, db)
	ctx := context.Background()
	target := newLiveProbeTarget(t)
	socksPort := minimalSocks5Server(t)
	relayPort := minimalSocks5Server(t)
	plain := "https://provider.example.test/first-check"
	sub := seedSubscription(t, db, dataDir, plain, AdmissionCandidate, "clash", `{"traffic":"total=100; download=12","custom":"preserved"}`)
	source := fmt.Sprintf(`proxies:
  - name: chained
    type: socks5
    server: 127.0.0.1
    port: %d
    dialer-proxy: local relay
  - name: origin
    type: socks5
    server: 127.0.0.1
    port: %d
proxy-groups:
  - name: local relay
    type: select
    proxies: [origin]
rules:
  - GEOIP,CN,DIRECT
  - MATCH,DIRECT
`, socksPort, relayPort)
	snapshotIntoDBRaw(t, conn, dataDir, sub, plain, source, nowFixed())
	if _, err := db.Exec("UPDATE proxy_subscriptions SET last_fetch_status = 'FAILED' WHERE id = ?", sub); err != nil {
		t.Fatal(err)
	}
	check := &CandidateCheck{DB: db, Gate: gate, DataDir: dataDir, ExePath: exe,
		Now: nowFixed, Business: BusinessProbeOptions{URL: target.server.URL + "/generate_204"},
		ExitIP: ExitProbeOptions{URL: target.server.URL + "/ip"},
	}
	report, err := check.Check(ctx, nil)
	if err != nil || len(report.Promoted) != 0 || len(report.Failures) != 0 || target.hits.Load() != 0 {
		t.Fatalf("old snapshot without current successful fetch was probed: report=%+v err=%v hits=%d", report, err, target.hits.Load())
	}
	if _, err := db.Exec("UPDATE proxy_subscriptions SET last_success_at=? WHERE id=?", pyTimeText(nowFixed()), sub); err != nil {
		t.Fatal(err)
	}
	report, err = check.Check(ctx, nil)
	if err != nil || len(report.Promoted) != 0 || mustStatus(t, db, sub) != AdmissionCandidate || target.hits.Load() < 1 {
		t.Fatalf("transport and exit IP are insufficient for production: report=%+v err=%v hits=%d", report, err, target.hits.Load())
	}
	var l2 int
	if err := db.QueryRow("SELECT count(*) FROM health_observations WHERE level = 'L2' AND ok = 1").Scan(&l2); err != nil || l2 != 0 {
		t.Fatalf("false L2 success = %d, %v", l2, err)
	}

	check.Business.URL = target.server.URL + "/IStoreBrowseService/GetItems/v1/"
	report, err = check.Check(ctx, []int64{sub})
	if err != nil || !slices.Equal(report.Promoted, []int64{sub}) || mustStatus(t, db, sub) != AdmissionActive {
		t.Fatalf("business success did not promote: report=%+v err=%v", report, err)
	}
	eligible, err := EligibleNodes(ctx, conn, time.Now())
	if err != nil || len(eligible) != 2 {
		t.Fatalf("eligible nodes=%v err=%v", eligible, err)
	}
	if err := db.QueryRow(`SELECT count(*) FROM health_observations h JOIN proxy_nodes n USING (node_id)
		WHERE h.level = 'L2' AND h.ok = 1 AND n.state = 'ACTIVE' AND n.last_l2_at IS NOT NULL AND n.exit_ip = '127.0.0.1'
		AND CAST(n.last_l1_at AS TEXT)=?`, pyTimeText(nowFixed())).Scan(&l2); err != nil || l2 != 2 {
		t.Fatalf("verified node L2 evidence = %d, %v", l2, err)
	}
	var stats string
	if err := db.QueryRow("SELECT last_stats FROM proxy_subscriptions WHERE id = ?", sub).Scan(&stats); err != nil ||
		lastStatsAlive(stats) != 2 || !strings.Contains(stats, "preserved") || !strings.Contains(stats, "download=12") {
		t.Fatalf("stats=%s err=%v", stats, err)
	}
	report, err = check.Check(ctx, nil)
	if err != nil || len(report.Promoted) != 0 || len(report.Failures) != 0 {
		t.Fatalf("admitted candidates should be skipped: report=%+v err=%v", report, err)
	}
}
