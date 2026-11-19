package crawl

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/crawler"
	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/occupancy"
)

const freshnessMaxAppids = 200

func decodeImportBody(w http.ResponseWriter, req *http.Request) ([]any, bool) {
	respond := func(errs ...httpx.ValErr) {
		httpx.WriteValidationError(w, errs...)
	}
	raw, e := io.ReadAll(http.MaxBytesReader(w, req.Body, 1<<20))
	if e != nil {
		respond(httpx.ValErr{Type: "json_invalid", Loc: []any{"body"},
			Msg: "JSON decode error", Input: map[string]any{}})
		return nil, false
	}
	if len(strings.TrimSpace(string(raw))) == 0 {
		respond(httpx.ValErr{Type: "missing", Loc: []any{"body"}, Msg: "Field required", Input: nil})
		return nil, false
	}
	dec := json.NewDecoder(strings.NewReader(string(raw)))
	dec.UseNumber()
	var top any
	if e := dec.Decode(&top); e != nil {
		respond(httpx.ValErr{Type: "json_invalid", Loc: []any{"body"},
			Msg: "JSON decode error", Input: map[string]any{}})
		return nil, false
	}
	fields, ok := top.(map[string]any)
	if !ok {
		respond(httpx.ValErr{Type: "model_attributes_type", Loc: []any{"body"},
			Msg: "Input should be a valid dictionary or object to extract fields from", Input: top})
		return nil, false
	}
	rawItems, present := fields["appids"]
	if !present || rawItems == nil {
		respond(httpx.ValErr{Type: "missing", Loc: []any{"body", "appids"}, Msg: "Field required", Input: fields})
		return nil, false
	}
	items, ok := rawItems.([]any)
	if !ok {
		respond(httpx.ValErr{Type: "list_type", Loc: []any{"body", "appids"},
			Msg: "Input should be a valid list", Input: rawItems})
		return nil, false
	}
	if len(items) < 1 {
		respond(httpx.ValErr{Type: "too_short", Loc: []any{"body", "appids"},
			Msg: "List should have at least 1 item after validation, not 0", Input: items,
			Ctx: map[string]any{"field_type": "List", "min_length": 1, "actual_length": len(items)}})
		return nil, false
	}
	if len(items) > 100 {
		respond(httpx.ValErr{Type: "too_long", Loc: []any{"body", "appids"},
			Msg: fmt.Sprintf("List should have at most 100 items after validation, not %d", len(items)), Input: items,
			Ctx: map[string]any{"field_type": "List", "max_length": 100, "actual_length": len(items)}})
		return nil, false
	}
	var errs []httpx.ValErr
	out := []any{}
	for i, item := range items {
		n, e := pyIntLax(item)
		if e != nil {
			errs = append(errs, httpx.ValErr{Type: e.errType, Loc: []any{"body", "appids", i},
				Msg: e.msg, Input: item})
			continue
		}
		out = append(out, n)
	}
	if len(errs) > 0 {
		respond(errs...)
		return nil, false
	}
	return out, true
}

// pyIntLax 宽松强转整数：bool→0/1，整值浮点与整串收下，带小数报 int_from_float。
func pyIntLax(v any) (int64, *struct {
	errType string
	msg     string
}) {
	fail := func(errType, msg string) (int64, *struct {
		errType string
		msg     string
	}) {
		return 0, &struct {
			errType string
			msg     string
		}{errType, msg}
	}
	switch x := v.(type) {
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case json.Number:
		if i, e := strconv.ParseInt(x.String(), 10, 64); e == nil {
			return i, nil
		}
		if f, e := strconv.ParseFloat(x.String(), 64); e == nil && f == float64(int64(f)) {
			return int64(f), nil
		}
		return fail("int_from_float", "Input should be a valid integer, got a number with a fractional part")
	case string:
		trimmed := strings.TrimSpace(x)
		if i, e := strconv.ParseInt(trimmed, 10, 64); e == nil {
			return i, nil
		}
		return fail("int_parsing", "Input should be a valid integer, unable to parse string as an integer")
	case float64:
		if x == float64(int64(x)) {
			return int64(x), nil
		}
		return fail("int_from_float", "Input should be a valid integer, got a number with a fractional part")
	case int64:
		return x, nil
	case int:
		return int64(x), nil
	}
	return fail("int_parsing", "Input should be a valid integer, unable to parse string as an integer")
}

func response(w http.ResponseWriter, status int, v any) {
	httpx.WriteJSON(w, status, v)
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
	mux.HandleFunc("GET /api/v1/crawl/freshness", func(w http.ResponseWriter, req *http.Request) {
		raws := req.URL.Query()["appid"]
		ids := []int64{}
		seen := map[int64]bool{}
		var errs []httpx.ValErr
		for i, raw := range raws {
			n, e := strconv.ParseInt(raw, 10, 64)
			if e != nil {
				errs = append(errs, httpx.ValErr{
					Type: "int_parsing", Loc: []any{"query", "appid", i},
					Msg: httpx.IntParseMsg, Input: raw,
				})
				continue
			}
			if !seen[n] {
				seen[n] = true
				ids = append(ids, n)
			}
		}
		if len(errs) > 0 {
			httpx.WriteValidationError(w, errs...)
			return
		}
		if len(ids) == 0 {
			response(w, 400, map[string]any{"detail": "缺少 appid 参数"})
			return
		}
		if len(ids) > freshnessMaxAppids {
			response(w, 400, map[string]any{"detail": fmt.Sprintf("一次最多查询 %d 个 appid", freshnessMaxAppids)})
			return
		}
		items, e := r.AppIDFreshness(req.Context(), ids)
		if e != nil {
			responseError(w, e)
			return
		}
		response(w, 200, map[string]any{"items": items})
	})
	mux.HandleFunc("POST /api/v1/crawl/import", func(w http.ResponseWriter, req *http.Request) {
		body, ok := decodeImportBody(w, req)
		if !ok {
			return
		}
		if r.ImportAppids == nil {
			response(w, 503, map[string]any{"detail": "目录导入未装配"})
			return
		}
		result, e := r.ImportAppids(req.Context(), body)
		if e != nil {
			response(w, 400, map[string]any{"detail": e.Error()})
			return
		}
		response(w, 200, result)
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
