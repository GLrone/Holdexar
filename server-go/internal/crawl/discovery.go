package crawl

import (
	"context"
	"fmt"
	"time"

	"holdexar/server-go/internal/priceevents"
)

func (r *Runtime) RunDiscovery(parent context.Context, kind string, ids []int64) (int64, error) {
	return r.runDiscovery(parent, kind, "scheduled", ids)
}

func (r *Runtime) RunHumbleBackfill(parent context.Context, ids []int64) (int64, error) {
	return r.runDiscovery(parent, "hb_backfill", "automatic", ids)
}

func (r *Runtime) runDiscovery(parent context.Context, kind, trigger string, ids []int64) (int64, error) {
	ctx, release, e := r.begin(parent)
	if e != nil {
		return 0, e
	}
	defer release()
	regions, e := r.planner.Regions(ctx, nil)
	if e != nil {
		return 0, e
	}
	ids, e = r.planner.ResolveScope(ctx, "appids", ids)
	if e != nil {
		return 0, e
	}
	tasks := makeBatches(ids, regions)
	if len(tasks) == 0 {
		return 0, nil
	}
	start := r.now()
	id, status, e := r.runJob(ctx, 0, kind, "appids", trigger, tasks, regions, nil)
	if id > 0 && status != "stopped" && ctx.Err() == nil {
		r.finishStandalone(ctx, id, tasks, start)
	}
	return id, e
}

func (r *Runtime) finishStandalone(ctx context.Context, id int64, tasks []TargetBatch, start time.Time) {
	ids := []int64{}
	codes := []string{}
	seen := map[int64]bool{}
	regions := map[string]bool{}
	for _, task := range tasks {
		if !regions[task.Region] {
			regions[task.Region] = true
			codes = append(codes, task.Region)
		}
		for _, appid := range task.AppIDs {
			if !seen[appid] {
				seen[appid] = true
				ids = append(ids, appid)
			}
		}
	}
	if _, e := priceevents.New(r.db, r.gate).DetectWindow(context.Background(), ids, codes, start, r.now(), -id); e != nil {
		r.opts.Log("抓取变化检测失败 │ 任务=%d，原因=%s", id, fmt.Sprint(e))
	}
	r.afterJob(ctx, ids)
}
