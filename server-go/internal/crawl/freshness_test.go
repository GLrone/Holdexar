package crawl

import (
	"context"
	"math"
	"testing"
)

func TestAppIDFreshnessBucketsAndPending(t *testing.T) {
	r, db := readFixture(t)
	ctx := context.Background()
	exec := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatal(err)
		}
	}
	price := func(appid int64, region, status, outcome, answer, success, updated string) {
		t.Helper()
		exec(`INSERT INTO game_current_prices(appid,region_code,discount_percent,fail_count,price_status,attempt_outcome,steam_answer,last_success_at,updated_at)
		      VALUES(?,?,0,0,?,?,?,?,?)`, appid, region, status, outcome, answer, success, updated)
	}
	price(10, "CN", "ok", "success", "ok", "2026-11-06 10:00:00", "2026-11-06 10:00:00")
	price(10, "US", "ok", "success", "ok", "2026-11-05 20:00:00", "2026-11-05 20:00:00")
	price(30, "CN", "missing", "failed", "", "2026-11-06 07:00:00", "2026-11-06 07:00:00")
	price(40, "CN", "ok", "success", "ok", "2026-11-06 01:00:00", "2026-11-06 01:00:00")
	price(50, "CN", "ok", "success", "ok", "2026-11-05 20:00:00", "2026-11-05 20:00:00")
	price(70, "CN", "ok", "success", "ok", "2026-11-06 09:00:00", "2026-11-06 09:00:00")

	exec(`INSERT INTO price_cycles(id,kind,status,scope,expected_json) VALUES(9,'scheduled','cancelled','pool','{}')`)
	job := func(id int64, status, stats string) {
		t.Helper()
		exec(`INSERT INTO crawl_jobs(id,kind,status,mode,cycle_id,stats_json,started_at) VALUES(?,'manual',?,'app',NULL,?,'2026-11-06 08:00:00')`, id, status, stats)
	}
	price(60, "CN", "ok", "success", "ok", "2026-11-06 07:00:00", "2026-11-06 07:00:00")
	job(1, "failed", `{"targets":{"appids":[60],"regions":["CN"]},"targetStartedAt":"2026-11-06T08:00:00"}`)
	job(2, "failed", `{"skipUnits":["60:CN"],"targetStartedAt":"2026-11-06T09:00:00"}`)
	job(3, "failed", `{"targets":{"appids":[10],"regions":["CN"]},"targetStartedAt":"2026-11-06T09:00:00"}`)
	job(4, "failed", `{"targets":{"appids":[70],"regions":["CN"]},"targetStartedAt":"2026-11-06T10:00:00"}`)
	price(80, "CN", "ok", "success", "ok", "2026-11-06 07:00:00", "2026-11-06 07:00:00")
	job(5, "failed", `{"targets":{"appids":[80],"regions":["CN"]},"targetStartedAt":"2026-11-06T10:00:00"}`)
	exec(`UPDATE crawl_jobs SET cycle_id=9 WHERE id=5`)

	items, err := r.AppIDFreshness(ctx, []int64{10, 20, 30, 40, 50, 60, 70, 80})
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 8 {
		t.Fatalf("条数=%d", len(items))
	}
	type want struct {
		observed  any
		ageHours  float64
		freshness any
	}
	object := map[int64]want{
		10: {"2026-11-06T10:00:00", 2, "fresh"},
		20: {nil, 0, nil},
		30: {"2026-11-06T07:00:00", 5, "fresh"},
		40: {"2026-11-06T01:00:00", 11, "lagging"},
		50: {"2026-11-05T20:00:00", 16, "stale"},
		60: {"2026-11-06T07:00:00", 5, "fresh"},
		70: {"2026-11-06T09:00:00", 3, "fresh"},
		80: {"2026-11-06T07:00:00", 5, "fresh"},
	}
	unitExpect := map[int64]map[string]any{
		10: {"status": "ok", "outcome": "success", "answer": "ok", "lastSuccessAt": "2026-11-06T10:00:00",
			"pending": false, "needsUpdate": false},
		20: {"status": "notAttempted", "outcome": "notAttempted", "answer": nil, "lastSuccessAt": nil,
			"pending": true, "needsUpdate": true},
		30: {"status": "missing", "outcome": "failed", "pending": true, "needsUpdate": true},
		40: {"outcome": "success", "pending": false, "needsUpdate": true},
		50: {"outcome": "success", "pending": false, "needsUpdate": true},
		60: {"outcome": "success", "pending": false, "needsUpdate": false},
		70: {"outcome": "success", "pending": true, "needsUpdate": true},
		80: {"outcome": "success", "pending": false, "needsUpdate": false},
	}
	for _, item := range items {
		appid := item["appid"].(int64)
		w := object[appid]
		if item["observedAt"] != w.observed {
			t.Fatalf("appid %d observedAt=%v want %v", appid, item["observedAt"], w.observed)
		}
		if w.ageHours == 0 && w.observed == nil {
			if item["ageHours"] != nil || item["freshness"] != nil {
				t.Fatalf("appid %d 无观察不应有档位：%v", appid, item)
			}
		} else {
			age, _ := item["ageHours"].(float64)
			if math.Abs(age-w.ageHours) > 1e-9 {
				t.Fatalf("appid %d ageHours=%v want %v", appid, item["ageHours"], w.ageHours)
			}
			if item["freshness"] != w.freshness {
				t.Fatalf("appid %d freshness=%v want %v", appid, item["freshness"], w.freshness)
			}
		}
		regions := item["regions"].(map[string]any)
		if _, ok := regions["US"]; ok && appid == 10 {
			t.Fatalf("appid %d 非启用区不应进明细：%v", appid, regions)
		}
		cn := regions["CN"].(map[string]any)
		for key, expect := range unitExpect[appid] {
			if cn[key] != expect {
				t.Fatalf("appid %d CN.%s=%v want %v", appid, key, cn[key], expect)
			}
		}
	}
}

func TestAppIDFreshnessEmptyInput(t *testing.T) {
	r, _ := readFixture(t)
	items, err := r.AppIDFreshness(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(items) != 0 {
		t.Fatalf("空入参应返回空列表：%v", items)
	}
}
