package jobrun

import (
	"context"
	"errors"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func TestRecordedJobSuccessFailureAndCancellation(t *testing.T) {
	path := filepath.Join(t.TempDir(), "holdexar.db")
	db, e := store.Open(path)
	if e != nil {
		t.Fatal(e)
	}
	defer db.Close()
	if e = store.InitializeFreshDatabase(context.Background(), db); e != nil {
		t.Fatal(e)
	}
	r := New(db, store.NewWriteGate(db, path), time.Now, func(string, ...any) {})
	if _, e = r.Run(context.Background(), "fx_refresh", func() (map[string]any, error) { return map[string]any{"count": 39}, nil }); e != nil {
		t.Fatal(e)
	}
	if _, e = r.Run(context.Background(), "fx_history_repair", func() (map[string]any, error) { return nil, errors.New("controlled") }); e == nil {
		t.Fatal("error lost")
	}
	if _, e = r.Run(context.Background(), "upstream_timeout", func() (map[string]any, error) { return nil, context.DeadlineExceeded }); !errors.Is(e, context.DeadlineExceeded) {
		t.Fatal(e)
	}
	if _, e = r.Run(context.Background(), "panic", func() (map[string]any, error) { panic("controlled") }); e == nil {
		t.Fatal("panic was not settled")
	}
	ctx, cancel := context.WithCancel(context.Background())
	if _, e = r.Run(ctx, "cancel", func() (map[string]any, error) { cancel(); return nil, ctx.Err() }); !errors.Is(e, context.Canceled) {
		t.Fatal(e)
	}
	for status, want := range map[string]int{"done": 1, "failed": 3, "cancelled": 1} {
		var count int
		_ = db.QueryRow("SELECT count(*) FROM agent_runs WHERE runner='job' AND status=?", status).Scan(&count)
		if count != want {
			t.Fatalf("%s=%d", status, count)
		}
	}
	var events int
	_ = db.QueryRow("SELECT count(*) FROM agent_events WHERE event_type='run.finished'").Scan(&events)
	if events != 5 {
		t.Fatal(events)
	}
	var sessions int
	_ = db.QueryRow("SELECT count(*) FROM agent_sessions WHERE id='runtime'").Scan(&sessions)
	if sessions != 1 {
		t.Fatal(sessions)
	}
	if _, e = db.Exec(`WITH RECURSIVE n(value) AS(SELECT 1 UNION ALL SELECT value+1 FROM n WHERE value<500) INSERT INTO agent_runs(id,session_id,runner,"trigger",status,meta) SELECT printf('%032d',value),'runtime','job','scheduled','running','{}' FROM n`); e != nil {
		t.Fatal(e)
	}
	if e = r.Recover(context.Background()); e != nil {
		t.Fatal(e)
	}
	var recovered int
	_ = db.QueryRow("SELECT count(*) FROM agent_runs WHERE error_code='runtime_stopped' AND status='failed'").Scan(&recovered)
	if recovered != 500 {
		t.Fatalf("recovered=%d", recovered)
	}
}
