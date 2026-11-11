package crawl

import (
	"context"
	"errors"
	"testing"
	"time"

	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/sched"
)

func TestCycleNotificationRunsAfterTerminalAndOutsideCrawlOccupancy(t *testing.T) {
	db, gate := runtimeFixture(t)
	called := 0
	r := New(db, gate, events.NewBus(), Options{AfterCycle: func(ctx context.Context, id int64) error {
		called++
		if occupancy.CrawlerBusy() {
			t.Error("SMTP held crawl occupancy")
		}
		var status string
		if e := db.QueryRowContext(ctx, `SELECT status FROM price_cycles WHERE id=?`, id).Scan(&status); e != nil || status != "completed" {
			t.Errorf("%s %v", status, e)
		}
		return errors.New("mail failed")
	}})
	if _, e := r.Run(context.Background(), "manual", nil); e != nil {
		t.Fatal("mail changed price result", e)
	}
	if called != 1 {
		t.Fatal(called)
	}
}
func TestRepairStillConsumesNotificationsWhenAutomaticPricesDisabled(t *testing.T) {
	db, gate := runtimeFixture(t)
	if _, e := db.Exec(`INSERT INTO app_settings(key,value_json) VALUES('crawl.auto_price','false')`); e != nil {
		t.Fatal(e)
	}
	called := 0
	r := New(db, gate, nil, Options{AfterRepair: func(context.Context) error { called++; return nil }})
	scheduler := sched.NewScheduler()
	now := time.Now()
	if e := r.RegisterSchedules(context.Background(), scheduler); e != nil {
		t.Fatal(e)
	}
	scheduler.StartSim(now)
	scheduler.RunSim(now.Add(11 * time.Second))
	if called != 1 {
		t.Fatal(called)
	}
}
