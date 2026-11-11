package crawl

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"time"

	"holdexar/server-go/internal/crawler"
	"holdexar/server-go/internal/occupancy"
)

func response(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}
func responseError(w http.ResponseWriter, err error) {
	status := http.StatusInternalServerError
	if errors.Is(err, ErrBusy) || errors.Is(err, occupancy.ErrCrawlerBusy) {
		status = http.StatusConflict
	}
	response(w, status, map[string]any{"detail": err.Error()})
}
func queryInt(req *http.Request, key string, fallback, minValue, maxValue int) (int, error) {
	s := req.URL.Query().Get(key)
	if s == "" {
		return fallback, nil
	}
	n, e := strconv.Atoi(s)
	if e != nil || n < minValue || n > maxValue {
		return 0, fmt.Errorf("参数 %s 超出允许范围", key)
	}
	return n, nil
}

func (r *Runtime) Register(mux *http.ServeMux, lifetime context.Context) {
	mux.HandleFunc("GET /api/v1/crawl/jobs", func(w http.ResponseWriter, req *http.Request) {
		limit, e := queryInt(req, "limit", 20, 1, 100)
		if e != nil {
			response(w, 422, map[string]any{"detail": e.Error()})
			return
		}
		v, e := r.Jobs(req.Context(), limit)
		if e != nil {
			responseError(w, e)
			return
		}
		response(w, 200, v)
	})
	mux.HandleFunc("GET /api/v1/crawl/cycles", func(w http.ResponseWriter, req *http.Request) {
		limit, e := queryInt(req, "limit", 10, 1, 50)
		if e != nil {
			response(w, 422, map[string]any{"detail": e.Error()})
			return
		}
		v, e := r.Cycles(req.Context(), limit)
		if e != nil {
			responseError(w, e)
			return
		}
		response(w, 200, v)
	})
	mux.HandleFunc("GET /api/v1/crawl/cycles/{cycleID}/coverage", func(w http.ResponseWriter, req *http.Request) {
		id, e := strconv.ParseInt(req.PathValue("cycleID"), 10, 64)
		if e != nil {
			response(w, 422, map[string]any{"detail": "周期编号无效"})
			return
		}
		v, e := r.Coverage(req.Context(), id)
		if e != nil {
			responseError(w, e)
			return
		}
		if v == nil {
			response(w, 404, map[string]any{"detail": "周期不存在"})
			return
		}
		response(w, 200, v)
	})
	mux.HandleFunc("GET /api/v1/crawl/price-events", func(w http.ResponseWriter, req *http.Request) {
		limit, e := queryInt(req, "limit", 100, 1, 500)
		if e != nil {
			response(w, 422, map[string]any{"detail": e.Error()})
			return
		}
		cycle, e := queryInt(req, "cycle_id", 0, -int(^uint(0)>>1), int(^uint(0)>>1))
		if e != nil {
			response(w, 422, map[string]any{"detail": e.Error()})
			return
		}
		appid, e := queryInt(req, "appid", 0, 1, int(^uint(0)>>1))
		if e != nil {
			response(w, 422, map[string]any{"detail": e.Error()})
			return
		}
		v, e := r.PriceEventsFiltered(req.Context(), int64(cycle), int64(appid), req.URL.Query().Get("event_type"), limit)
		if e != nil {
			responseError(w, e)
			return
		}
		response(w, 200, v)
	})
	mux.HandleFunc("GET /api/v1/crawl/freshness/latest", func(w http.ResponseWriter, req *http.Request) {
		v, e := r.LatestFreshness(req.Context())
		if e != nil {
			responseError(w, e)
			return
		}
		response(w, 200, v)
	})
	mux.HandleFunc("GET /api/v1/crawl/active", func(w http.ResponseWriter, req *http.Request) {
		r.mu.Lock()
		id := nullableID(r.jobID)
		r.mu.Unlock()
		kind := occupancy.MaintenanceKind()
		var maintenance any
		if kind != "" {
			maintenance = map[string]any{"kind": kind, "reason": occupancy.MaintenanceReason()}
		}
		response(w, 200, map[string]any{"activeJobId": id, "busy": id != nil || occupancy.CurrentHolder() != "", "maintenance": kind == occupancy.MaintenanceHealth, "maintenanceInfo": maintenance, "throttled": crawler.DefaultRateLimiter.Waiting()})
	})
	mux.HandleFunc("GET /api/v1/crawl/queue-specs", func(w http.ResponseWriter, req *http.Request) {
		specs, e := r.planner.DefaultSpecs(req.Context(), false)
		if e != nil {
			responseError(w, e)
			return
		}
		items := []map[string]any{}
		for _, s := range specs {
			name := s.Scope
			if name == "" {
				name = s.Kind
			}
			items = append(items, map[string]any{"name": name, "kind": nullable(s.Kind)})
		}
		response(w, 200, map[string]any{"specs": items})
	})
	mux.HandleFunc("POST /api/v1/crawl/stop", func(w http.ResponseWriter, req *http.Request) {
		id, e := queryInt(req, "jobId", 0, 1, int(^uint(0)>>1))
		if e != nil {
			response(w, 422, map[string]any{"detail": e.Error()})
			return
		}
		chain := true
		if s := req.URL.Query().Get("chain"); s != "" {
			chain, e = strconv.ParseBool(s)
			if e != nil {
				response(w, 422, map[string]any{"detail": "停止范围无效"})
				return
			}
		}
		response(w, 200, map[string]any{"stopped": r.StopJob(int64(id), chain)})
	})
	mux.HandleFunc("POST /api/v1/crawl/run", func(w http.ResponseWriter, req *http.Request) {
		var body struct {
			Scope    string   `json:"scope"`
			AppIDs   []int64  `json:"appids"`
			Regions  []string `json:"regions"`
			Kind     string   `json:"kind"`
			Cooldown *int     `json:"cooldown"`
		}
		body.Scope = "appids"
		body.Kind = "manual"
		decoder := json.NewDecoder(http.MaxBytesReader(w, req.Body, 1<<20))
		if e := decoder.Decode(&body); e != nil {
			response(w, 422, map[string]any{"detail": "任务参数无效"})
			return
		}
		var extra any
		if e := decoder.Decode(&extra); e != io.EOF {
			response(w, 422, map[string]any{"detail": "任务参数无效"})
			return
		}
		for _, id := range body.AppIDs {
			if id <= 0 {
				response(w, 400, map[string]any{"detail": "游戏编号无效"})
				return
			}
		}
		var v map[string]any
		var e error
		if body.Scope == "all" {
			v, e = r.StartCycle(lifetime)
		} else {
			var cooldown *time.Duration
			if body.Cooldown != nil {
				d := time.Duration(*body.Cooldown) * time.Minute
				cooldown = &d
			}
			v, e = r.StartJob(lifetime, body.Scope, body.AppIDs, body.Regions, body.Kind, cooldown)
		}
		if e != nil {
			if errors.Is(e, ErrBusy) || errors.Is(e, occupancy.ErrCrawlerBusy) || errors.Is(e, errProxyNotReady) {
				response(w, 409, map[string]any{"detail": e.Error()})
			} else {
				response(w, 400, map[string]any{"detail": e.Error()})
			}
			return
		}
		response(w, 200, v)
	})
}

func (r *Runtime) StartCycle(parent context.Context) (map[string]any, error) {
	ctx, release, e := r.begin(parent)
	if e != nil {
		return nil, e
	}
	specs, e := r.planner.DefaultSpecs(ctx, true)
	if e != nil {
		release()
		return nil, e
	}
	segments := []map[string]any{}
	total := 0
	for i := range specs {
		s := &specs[i]
		if s.Scope == "" {
			continue
		}
		ids, e := r.planner.ResolveScope(ctx, s.Scope, s.AppIDs)
		if e != nil {
			release()
			return nil, e
		}
		s.ResolvedAppIDs = ids
		total += len(ids)
		segments = append(segments, map[string]any{"name": s.Scope, "count": len(ids)})
	}
	go func() {
		defer release()
		if _, e := r.runCycle(ctx, "manual", specs); e != nil {
			r.opts.Log("手动价格更新未完成：%v", e)
		}
	}()
	return map[string]any{"queued": true, "scope": "all", "total": total, "segments": segments}, nil
}

func (r *Runtime) StartJob(parent context.Context, scope string, ids []int64, requested []string, kind string, cooldown *time.Duration) (map[string]any, error) {
	return r.startJob(parent, scope, ids, requested, kind, "manual", cooldown)
}

func (r *Runtime) StartFamilyWishlist(parent context.Context, ids []int64) error {
	_, err := r.startJob(parent, "appids", ids, nil, "family_wishlist", "automatic", nil)
	return err
}

func (r *Runtime) startJob(parent context.Context, scope string, ids []int64, requested []string, kind, trigger string, cooldown *time.Duration) (map[string]any, error) {
	ctx, release, e := r.begin(parent)
	if e != nil {
		return nil, e
	}
	if scope == "owned" && requested == nil {
		v, e := r.setting(ctx, "crawl.owned_regions", nil)
		if e != nil {
			release()
			return nil, e
		}
		if list, ok := v.([]any); ok {
			for _, v := range list {
				if code, ok := v.(string); ok {
					requested = append(requested, code)
				}
			}
		}
	}
	regions, e := r.planner.Regions(ctx, requested)
	if e != nil {
		release()
		return nil, e
	}
	var tasks []TargetBatch
	if kind == "repair" || kind == "missing" {
		delay := 24 * time.Hour
		if kind == "repair" {
			delay = 4 * time.Minute
		}
		if cooldown != nil {
			delay = *cooldown
		}
		tasks, e = r.planner.Missing(ctx, delay)
	} else {
		ids, e = r.planner.ResolveScope(ctx, scope, ids)
		if e == nil {
			tasks = makeBatches(ids, regions)
		}
	}
	if e != nil || len(tasks) == 0 {
		release()
		if e == nil {
			e = fmt.Errorf("任务列表为空")
		}
		return nil, e
	}
	type accepted struct {
		data map[string]any
		err  error
	}
	ready := make(chan accepted, 1)
	go func() {
		defer release()
		start := r.now()
		jobScope := scope
		count := len(ids)
		if kind == "missing" || kind == "repair" {
			jobScope = ""
			count = 0
			for _, task := range tasks {
				count += len(task.AppIDs)
			}
		}
		id, status, err := r.runJob(ctx, 0, kind, jobScope, trigger, tasks, regions, func(id int64) {
			ready <- accepted{data: map[string]any{"id": id, "scope": scope, "count": count, "regions": regionCodes(regions)}}
		})
		if id == 0 {
			ready <- accepted{err: err}
			return
		}
		if err != nil {
			r.opts.Log("手动抓取未完成：%v", err)
		}
		if ctx.Err() == nil && status != "stopped" {
			r.finishStandalone(ctx, id, tasks, start)
		}
	}()
	result := <-ready
	return result.data, result.err
}
