package alerts

import (
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func TestManualHealthAlertUsesOriginalCooldownAndFact(t *testing.T) {
	ctx := t.Context()
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err := store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	s := New(db, store.NewWriteGate(db, path), Options{})
	now := time.Now()
	if created, err := s.RecordProxyHealth(ctx, false, nil, now); err != nil || created {
		t.Fatalf("no channels: created=%v err=%v", created, err)
	}
	if _, err := db.Exec(`INSERT INTO proxies(id,scheme,host,port,enabled,status,consecutive_failures) VALUES(1,'http','localhost',1,0,'failed',5);
		INSERT INTO app_settings(key,value_json) VALUES ('notifications.prefs','{"enabled":true}'),('smtp.to_addr','"local@example.test"'),
		('alerts.sys_alert_at','{"backup":10}');
		CREATE TRIGGER block_health_fact BEFORE INSERT ON notification_candidates BEGIN SELECT RAISE(ABORT,'fixture rejection'); END`); err != nil {
		t.Fatal(err)
	}
	if created, err := s.RecordProxyHealth(ctx, false, nil, now); err == nil || created {
		t.Fatalf("injected candidate failure: created=%v err=%v", created, err)
	}
	var facts int
	if err := db.QueryRow(`SELECT COUNT(*) FROM fact_notices`).Scan(&facts); err != nil || facts != 0 {
		t.Fatalf("fact after rollback=%d err=%v", facts, err)
	}
	if _, err := db.Exec(`DROP TRIGGER block_health_fact`); err != nil {
		t.Fatal(err)
	}
	for i, at := range []time.Time{now, now.Add(11 * time.Hour), now.Add(12 * time.Hour)} {
		want := i != 1
		if created, err := s.RecordProxyHealth(ctx, false, nil, at); err != nil || created != want {
			t.Fatalf("run=%d created=%v want=%v err=%v", i, created, want, err)
		}
	}
	var candidates, backup int
	if err := db.QueryRow(`SELECT (SELECT count(*) FROM notification_candidates WHERE source_kind='fact_notice' AND event_type='PROXY_ALERT'),
		json_extract(value_json,'$.backup') FROM app_settings WHERE key='alerts.sys_alert_at'`).Scan(&candidates, &backup); err != nil || candidates != 2 || backup != 10 {
		t.Fatalf("candidates=%d backup=%d err=%v", candidates, backup, err)
	}
}
