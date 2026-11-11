package crawl

import (
	"context"
	"encoding/json"
)

func (r *Runtime) RunAccountDiscovery(parent context.Context, wished, owned []int64) (bool, error) {
	return r.accountDiscovery(parent, wished, owned, false)
}
func (r *Runtime) StartAccountDiscovery(parent context.Context, wished, owned []int64) (bool, error) {
	return r.accountDiscovery(parent, wished, owned, true)
}
func (r *Runtime) accountDiscovery(parent context.Context, wished, owned []int64, background bool) (bool, error) {
	ctx, release, e := r.begin(parent)
	if e != nil {
		return false, e
	}
	transferred := false
	defer func() {
		if !transferred {
			release()
		}
	}()
	active, e := r.PoolAppIDs(ctx)
	if e != nil {
		return false, e
	}
	allowed := map[int64]bool{}
	for _, id := range active {
		allowed[id] = true
	}
	seen := map[int64]bool{}
	filter := func(ids []int64) []int64 {
		out := []int64{}
		for _, id := range ids {
			if allowed[id] && !seen[id] {
				out = append(out, id)
				seen[id] = true
			}
		}
		return out
	}
	wished, owned = filter(wished), filter(owned)
	all := append(append([]int64{}, wished...), owned...)
	raw, _ := json.Marshal(all)
	rows, e := r.db.QueryContext(ctx, `SELECT appid FROM wishlist_items WHERE active=1 AND wishlisted=1 AND appid IN(SELECT value FROM json_each(?)) GROUP BY appid`, string(raw))
	if e != nil {
		return false, e
	}
	globalWish := map[int64]bool{}
	for rows.Next() {
		var id int64
		if e = rows.Scan(&id); e != nil {
			rows.Close()
			return false, e
		}
		globalWish[id] = true
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return false, e
	}
	ownedOnly := []int64{}
	for _, id := range owned {
		if globalWish[id] {
			wished = append(wished, id)
		} else {
			ownedOnly = append(ownedOnly, id)
		}
	}
	owned = ownedOnly
	value, e := r.setting(ctx, "crawl.owned_regions", nil)
	if e != nil {
		return false, e
	}
	requested := []string{}
	if list, ok := value.([]any); ok {
		for _, v := range list {
			if code, ok := v.(string); ok {
				requested = append(requested, code)
			}
		}
	}
	if len(requested) == 0 {
		wished = append(wished, owned...)
		owned = nil
	}
	type plan struct {
		tasks   []TargetBatch
		regions []Region
	}
	plans := []plan{}
	for _, part := range []struct {
		ids   []int64
		codes []string
	}{{wished, nil}, {owned, requested}} {
		if len(part.ids) == 0 {
			continue
		}
		regions, e := r.planner.Regions(ctx, part.codes)
		if e != nil {
			return false, e
		}
		tasks := makeBatches(part.ids, regions)
		if len(tasks) == 0 {
			continue
		}
		plans = append(plans, plan{tasks, regions})
	}
	if len(plans) == 0 {
		return false, nil
	}
	run := func() (bool, error) {
		started := false
		for _, part := range plans {
			tasks, regions := part.tasks, part.regions
			start := r.now()
			id, status, e := r.runJob(ctx, 0, "wishlist_sync", "appids", "scheduled", tasks, regions, nil)
			started = started || id > 0
			if id > 0 && status != "stopped" && ctx.Err() == nil {
				r.finishStandalone(ctx, id, tasks, start)
			}
			if e != nil {
				return started, e
			}
			if ctx.Err() != nil {
				return started, ctx.Err()
			}
		}
		return started, nil
	}
	if background {
		transferred = true
		go func() {
			defer release()
			if _, e := run(); e != nil && ctx.Err() == nil {
				r.opts.Log("Steam 新增游戏价格更新未完成：%v", e)
			}
		}()
		return true, nil
	}
	return run()
}
