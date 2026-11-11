package crawl

import (
	"context"
	"reflect"
	"testing"
)

func TestPruneJobsSettlesBeforeRemovingEvidence(t *testing.T) {
	r, db := readFixture(t)
	ctx := context.Background()
	readCycle(t, db, 10, `[]`)
	readExec(t, db, `UPDATE price_cycles SET status='cancelled' WHERE id=10`)
	readExec(t, db, `INSERT INTO crawl_jobs(id,kind,status,stats_json,finished_at,cycle_id) VALUES
		(1,'manual','failed','{"targetStartedAt":"2026-07-01T00:00:00.000000","targets":{"appids":[101,102],"regions":["CN","US"]},"processed":1}','2026-07-01 00:01:00',NULL),
		(2,'manual','done','{"targetStartedAt":"2026-07-02T00:00:00.000000","skipUnits":["101:CN","102:US"]}','2026-07-02 00:01:00',NULL),
		(3,'manual','interrupted','{"targetStartedAt":"2026-07-01T00:00:00.000000","targets":{"tasks":[{"region":"CN","appids":[103]}]}}','2026-07-01 00:01:00',NULL),
		(4,'manual','failed','{"targets":{"appids":[104],"regions":["CN"]}}','2026-07-01 00:01:00',NULL),
		(5,'manual','failed','{"targetStartedAt":"2026-07-01T00:00:00.000000","targets":{"appids":[105],"regions":["CN"]}}','2026-07-01 00:01:00',10),
		(6,'manual','done','{}','2026-07-01 00:01:00',NULL),
		(7,'manual','done','{}','2026-07-01 00:01:00',NULL),
		(8,'manual','running','{}','2026-07-01 00:01:00',NULL),
		(9,'manual','done','{}','2026-11-01 00:01:00',NULL),
		(11,'manual','done','{bad json','2026-07-01 00:01:00',NULL),
		(12,'manual','done','{}',NULL,NULL),
		(13,'manual','failed','{"targetStartedAt":"2026-11-01T00:00:00.000000","targets":{"appids":[106,107],"regions":["CN"]}}','2026-11-01 00:01:00',NULL)`)
	readCurrent(t, db, 103, "CN", "locked", "success", "locked", "2026-07-02 00:00:00.000000")
	readCurrent(t, db, 106, "CN", "locked", "success", "locked", "2026-11-02 00:00:00.000000")
	readExec(t, db, `INSERT INTO price_events(id,cycle_id,appid,event_type) VALUES (1,-6,101,'PRICE_DROP')`)
	readExec(t, db, `INSERT INTO notification_candidates(event_id,cycle_id,appid,event_type,category,recipient,status,attempts)
		VALUES (1,-7,101,'PRICE_DROP','price','fixture@example.invalid','pending',0)`)

	if count, more, err := r.pruneJobs(ctx, 2, 1); count != 2 || !more || err != nil {
		t.Fatalf("first pass deleted=%d capped=%v err=%v", count, more, err)
	}
	if count, more, err := r.pruneJobs(ctx, 2, 20); count != 2 || more || err != nil {
		t.Fatalf("next pass deleted=%d capped=%v err=%v", count, more, err)
	}
	ids, err := r.planner.planIDs(ctx, `SELECT id FROM crawl_jobs ORDER BY id`)
	if err != nil || !reflect.DeepEqual(ids, []int64{1, 4, 6, 7, 8, 9, 12, 13}) {
		t.Fatalf("survivors=%v err=%v", ids, err)
	}
	var targets, recent string
	var processed int
	if err := db.QueryRow(`SELECT json_extract(stats_json,'$.targets'),json_extract(stats_json,'$.processed') FROM crawl_jobs WHERE id=1`).Scan(&targets, &processed); err != nil || processed != 1 || targets != `{"tasks":[{"region":"CN","appids":[102]},{"region":"US","appids":[101]}]}` {
		t.Fatalf("partial targets=%s processed=%d err=%v", targets, processed, err)
	}
	if err := db.QueryRow(`SELECT json_extract(stats_json,'$.targets') FROM crawl_jobs WHERE id=13`).Scan(&recent); err != nil || recent != `{"appids":[107],"regions":["CN"]}` {
		t.Fatalf("recent targets=%s err=%v", recent, err)
	}
	missing, err := r.planner.Missing(ctx, 0)
	if err != nil || !reflect.DeepEqual(missing, []TargetBatch{{AppIDs: []int64{102, 107}, Region: "cn"}, {AppIDs: []int64{101}, Region: "us"}}) {
		t.Fatalf("remaining work=%v err=%v", missing, err)
	}
}

func TestPrunePriceHistoryKeepsScanReferencesAndStageOrder(t *testing.T) {
	r, db := readFixture(t)
	ctx := context.Background()
	readExec(t, db, `INSERT INTO price_cycles(id,kind,status,scope,started_at,specs_json,notification_scan_json) VALUES
		(1,'scheduled','completed','pool','2026-07-01','[{"scope":"pool","execution":{"state":"deferred"}}]',NULL),
		(2,'scheduled','completed','pool','2026-07-01','[{"scope":"pool","execution":{"state":"done"}}]',NULL),
		(3,'scheduled','partial','pool','2026-07-01','[{"scope":"catalog","execution":{"state":"deferred"}}]',NULL),
		(4,'scheduled','completed','pool','2026-07-01',NULL,'{"state":"scanning"}'),
		(5,'scheduled','completed','pool','2026-07-01',NULL,NULL),
		(6,'scheduled','completed','pool','2026-07-01',NULL,NULL),
		(7,'scheduled','running','pool','2026-07-01',NULL,NULL),
		(8,'scheduled','completed','pool','2026-11-01',NULL,NULL),
		(9,'scheduled','completed','pool','2026-07-01',NULL,NULL),
		(10,'scheduled','completed','pool','2026-07-01',NULL,NULL);
		INSERT INTO price_events(id,cycle_id,appid,event_type,occurred_at) VALUES
		(11,1,100,'PRICE_DROP','2026-07-01'),(14,4,100,'PRICE_DROP','2026-07-01'),
		(15,5,100,'PRICE_DROP','2026-07-01'),(16,-123,100,'PRICE_DROP','2026-07-01'),
		(18,8,100,'PRICE_DROP','2026-11-01');
		INSERT INTO notification_candidates(source_kind,event_id,cycle_id,appid,event_type,category,recipient,status,attempts) VALUES
		('price_event',11,1,100,'PRICE_DROP','price','fixture@example.invalid','pending',0),
		('alert_event',16,10,100,'RULE_PRICE','rules','fixture@example.invalid','pending',0);
		INSERT INTO crawl_jobs(id,kind,status,cycle_id) VALUES (88,'scheduled','interrupted',9)`)

	before, err := r.planner.DefaultSpecs(ctx, true)
	if err != nil || len(before) == 0 || before[0].Scope != "catalog" {
		t.Fatalf("priority before=%v err=%v", before, err)
	}
	first, err := r.prunePriceHistory(ctx, 1, 1)
	if err != nil || first.Events != 1 || first.Cycles != 1 || !first.Truncated {
		t.Fatalf("first result=%+v err=%v", first, err)
	}
	second, err := r.prunePriceHistory(ctx, 1, 20)
	if err != nil || second.Events != 1 || second.Cycles != 1 || second.Truncated {
		t.Fatalf("second result=%+v err=%v", second, err)
	}
	ids, err := r.planner.planIDs(ctx, `SELECT id FROM price_cycles ORDER BY id`)
	if err != nil || !reflect.DeepEqual(ids, []int64{1, 2, 3, 4, 7, 8, 9, 10}) {
		t.Fatalf("cycles=%v err=%v", ids, err)
	}
	events, err := r.planner.planIDs(ctx, `SELECT id FROM price_events ORDER BY id`)
	if err != nil || !reflect.DeepEqual(events, []int64{11, 14, 18}) {
		t.Fatalf("events=%v err=%v", events, err)
	}
	after, err := r.planner.DefaultSpecs(ctx, true)
	if err != nil || !reflect.DeepEqual(before, after) {
		t.Fatalf("priority changed: before=%v after=%v err=%v", before, after, err)
	}

	readExec(t, db, `DELETE FROM notification_candidates WHERE cycle_id=1`)
	last, err := r.prunePriceHistory(ctx, 50, 20)
	if err != nil || last.Events != 1 || last.Cycles != 2 || last.Truncated {
		t.Fatalf("after reference removed=%+v err=%v", last, err)
	}
	after, err = r.planner.DefaultSpecs(ctx, true)
	if err != nil || !reflect.DeepEqual(before, after) {
		t.Fatalf("priority changed after release: before=%v after=%v err=%v", before, after, err)
	}
}
