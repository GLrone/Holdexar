package jobrun

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

func TestPruneKeepsTaskReplayAndCascadesOldScheduledEvents(t *testing.T) {
	ctx := context.Background()
	path := filepath.Join(t.TempDir(), "retention.db")
	db, err := store.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if err = store.InitializeFreshDatabase(ctx, db); err != nil {
		t.Fatal(err)
	}
	now := func() time.Time { return time.Date(2026, 11, 6, 4, 36, 0, 0, time.FixedZone("CST", 8*3600)) }
	r := New(db, store.NewWriteGate(db, path), now, t.Logf)
	if _, err = db.Exec(`INSERT INTO agent_sessions(id) VALUES ('runtime');
		INSERT INTO agent_runs(id,session_id,runner,"trigger",status,created_at) VALUES
		('old1','runtime','job','scheduled','done','2026-10-01 04:36:00.000000'),
		('old2','runtime','job','scheduled','failed','2026-10-02 04:36:00.000000'),
		('boundary','runtime','job','scheduled','done','2026-10-07 04:36:00.000000'),
		('task','runtime','task','manual','done','2026-10-01 04:36:00.000000'),
		('fake','runtime','fake','manual','done','2026-10-01 04:36:00.000000');
		INSERT INTO agent_events(run_id,seq,event_type) SELECT id,1,'run.finished' FROM agent_runs`); err != nil {
		t.Fatal(err)
	}
	if count, more, err := r.prune(ctx, 1, 1); count != 1 || !more || err != nil {
		t.Fatalf("first pass deleted=%d capped=%v err=%v", count, more, err)
	}
	if count, more, err := r.prune(ctx, 2, 20); count != 1 || more || err != nil {
		t.Fatalf("next pass deleted=%d capped=%v err=%v", count, more, err)
	}
	var runs, events string
	if err := db.QueryRow(`SELECT (SELECT group_concat(id, ',' ORDER BY id) FROM agent_runs), (SELECT group_concat(run_id, ',' ORDER BY run_id) FROM agent_events)`).Scan(&runs, &events); err != nil || runs != "boundary,fake,task" || events != runs {
		t.Fatalf("runs=%s events=%s err=%v", runs, events, err)
	}
}
