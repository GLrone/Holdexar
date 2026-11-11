package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/crawler"
	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/priceevents"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/storebrowse"
)

var ErrBusy = errors.New("价格更新正在进行")
var errUserStopped = errors.New("用户已停止")

type Options struct {
	Fetch          func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error)
	Board          func(context.Context, string) ([]int64, error)
	Now            func() time.Time
	EventKey       func(time.Time) *string
	Log            func(string, ...any)
	AfterJob       func(context.Context, []int64) error
	AfterCycle     func(context.Context, int64) error
	AfterRepair    func(context.Context) error
	AfterIdle      func(bool)
	Recorder       *jobrun.Recorder
	ProxyLanes     func(context.Context) ([]proxypool.CrawlLane, error)
	NetworkBlocked func(context.Context) bool
	StoreBrowseURL string
}

type Runtime struct {
	db             *sql.DB
	gate           *store.WriteGate
	bus            *events.Bus
	planner        *Planner
	opts           Options
	mu             sync.Mutex
	busy           bool
	closing        bool
	scheduled      int
	cancel         context.CancelFunc
	interrupt      context.CancelFunc
	jobCancel      context.CancelFunc
	workCancel     context.CancelFunc
	owner          context.Context
	jobID, cycleID int64
	scheduler      *sched.Scheduler
}

func New(db *sql.DB, gate *store.WriteGate, bus *events.Bus, opts Options) *Runtime {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	if bus == nil {
		bus = events.NewBus()
	}
	return &Runtime{db: db, gate: gate, bus: bus, opts: opts, planner: &Planner{DB: db, Gate: gate, Now: opts.Now, Board: opts.Board}}
}

func (r *Runtime) now() time.Time  { return r.opts.Now().In(beijing) }
func timestamp(t time.Time) string { return t.Format("2006-01-02 15:04:05.000000") }

var beijing = func() *time.Location { loc, _ := time.LoadLocation("Asia/Shanghai"); return loc }()

func (r *Runtime) begin(parent context.Context) (context.Context, func(), error) {
	if err := parent.Err(); err != nil {
		return nil, nil, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing {
		return nil, nil, context.Canceled
	}
	if r.busy {
		return nil, nil, ErrBusy
	}
	if err := occupancy.BeginCrawl(fmt.Sprintf("go-price:%p", r)); err != nil {
		return nil, nil, err
	}
	ctx, cancel := context.WithCancelCause(parent)
	r.busy = true
	r.owner = parent
	r.cancel = func() { cancel(errUserStopped) }
	r.interrupt = func() { cancel(context.Canceled) }
	return ctx, func() {
		cancel(nil)
		r.mu.Lock()
		r.busy = false
		r.cancel = nil
		r.interrupt = nil
		r.owner = nil
		r.jobID = 0
		r.cycleID = 0
		r.mu.Unlock()
		occupancy.EndCrawl()
		r.bus.Publish("crawl.idle")
		if r.opts.AfterIdle != nil {
			r.opts.AfterIdle(errors.Is(context.Cause(ctx), errUserStopped))
		}
	}, nil
}

func (r *Runtime) Stop(jobID int64) bool {
	return r.StopJob(jobID, true)
}

func (r *Runtime) StopJob(jobID int64, chain bool) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.busy || (jobID != 0 && jobID != r.jobID) {
		return false
	}
	if !chain {
		if r.jobCancel == nil {
			return false
		}
		r.jobCancel()
	} else {
		r.cancel()
	}
	return true
}

func (r *Runtime) Run(parent context.Context, kind string, specs []Spec) (cycleID int64, err error) {
	ctx, release, err := r.begin(parent)
	if err != nil {
		return 0, err
	}
	shouldNotify := false
	func() {
		defer release()
		cycleID, err = r.runCycle(ctx, kind, specs)
		shouldNotify = ctx.Err() == nil
	}()
	if err == nil && shouldNotify && r.opts.AfterCycle != nil {
		if notificationErr := r.opts.AfterCycle(parent, cycleID); notificationErr != nil {
			r.opts.Log("价格更新已完成，本轮提醒发送未完成：%v", notificationErr)
		}
	}
	return cycleID, err
}

func (r *Runtime) PoolAppIDs(ctx context.Context) ([]int64, error) {
	return r.planner.ResolveScope(ctx, "pool", nil)
}

func (r *Runtime) runCycle(ctx context.Context, kind string, specs []Spec) (cycleID int64, err error) {
	if kind == "" {
		kind = "manual"
	}
	started := r.now()
	err = r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, e := conn.ExecContext(ctx, `INSERT INTO price_cycles(kind,status,scope,started_at,batches_done) VALUES(?,'planning','pool',?,0)`, kind, timestamp(started))
		if e != nil {
			return e
		}
		cycleID, e = result.LastInsertId()
		return e
	})
	if err != nil {
		return 0, err
	}
	r.mu.Lock()
	r.cycleID = cycleID
	r.mu.Unlock()
	r.opts.Log("价格更新周期 %d 已开始", cycleID)
	defer func() {
		if err != nil {
			if e := r.finishCycle(context.Background(), cycleID, "failed", err.Error()); e != nil {
				r.opts.Log("价格更新收尾失败：%v", e)
			}
		}
	}()
	regions, err := r.planner.Regions(ctx, nil)
	if err != nil {
		return cycleID, err
	}
	allIDs := []int64{}
	seen := map[int64]bool{}
	batches := 0
	for i := range specs {
		spec := &specs[i]
		if spec.Scope == "" {
			continue
		}
		ids := spec.ResolvedAppIDs
		if ids == nil {
			ids, err = r.planner.ResolveScope(ctx, spec.Scope, spec.AppIDs)
		}
		if err != nil {
			spec.Execution = map[string]any{"state": "deferred", "reason": err.Error()}
			err = nil
			continue
		}
		spec.ResolvedAppIDs = []int64{}
		spec.ResolvedRegions = regionCodes(regions)
		for _, id := range ids {
			if !seen[id] {
				seen[id] = true
				allIDs = append(allIDs, id)
				spec.ResolvedAppIDs = append(spec.ResolvedAppIDs, id)
			}
		}
		batches += (len(spec.ResolvedAppIDs) + 299) / 300 * len(regions)
	}
	expected, _ := json.Marshal(map[string]any{"appids": allIDs, "regions": regionCodes(regions)})
	err = r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, `UPDATE price_cycles SET expected_json=?,specs_json=?,batches_expected=?,status='running',running_at=? WHERE id=? AND status='planning'`, string(expected), jsonString(specs), batches, timestamp(r.now()), cycleID)
		return e
	})
	if err != nil {
		return cycleID, err
	}
	statuses := []string{}
	deferred := false
	networkBlocked := false
	for i := range specs {
		spec := &specs[i]
		if ctx.Err() != nil {
			state := "deferred"
			if errors.Is(context.Cause(ctx), errUserStopped) {
				state = "cancelled"
			}
			spec.Execution = map[string]any{"state": state}
			continue
		}
		if r.opts.NetworkBlocked != nil && r.opts.NetworkBlocked(ctx) {
			for j := i; j < len(specs); j++ {
				specs[j].Execution = map[string]any{"state": "deferred", "reason": networkBlockedReason}
			}
			deferred = true
			networkBlocked = true
			r.opts.Log("价格更新周期 %d 暂停：%s", cycleID, networkBlockedReason)
			break
		}
		if spec.Execution != nil && spec.Execution["state"] == "deferred" {
			deferred = true
			continue
		}
		var tasks []TargetBatch
		if spec.Scope == "" {
			tasks, err = r.planner.Missing(ctx, 24*time.Hour)
		} else {
			tasks = makeBatches(spec.ResolvedAppIDs, regions)
		}
		if err != nil {
			spec.Execution = map[string]any{"state": "deferred", "reason": err.Error()}
			deferred = true
			err = nil
			continue
		}
		if len(tasks) == 0 {
			spec.Execution = map[string]any{"state": "empty"}
			continue
		}
		jobKind := spec.Kind
		if jobKind == "" {
			jobKind = "scheduled"
		}
		var jobID int64
		var state string
		jobScope := spec.Scope
		if spec.ResolvedAppIDs != nil {
			jobScope = "appids"
		}
		jobID, state, err = r.runJob(ctx, cycleID, jobKind, jobScope, kind, tasks, regions, nil)
		if jobID > 0 && ctx.Err() == nil && state != "stopped" {
			ids := []int64{}
			for _, task := range tasks {
				ids = append(ids, task.AppIDs...)
			}
			r.afterJob(ctx, ids)
		}
		spec.Execution = map[string]any{"state": "done"}
		if jobID > 0 {
			spec.Execution["jobId"] = jobID
			statuses = append(statuses, state)
		}
		var failed, success int
		if jobID > 0 {
			if e := r.db.QueryRowContext(context.Background(), `SELECT COALESCE(json_extract(stats_json,'$.failed'),0),COALESCE(json_extract(stats_json,'$.success'),0) FROM crawl_jobs WHERE id=?`, jobID).Scan(&failed, &success); e != nil {
				return cycleID, e
			}
		}
		if errors.Is(context.Cause(ctx), errUserStopped) || state == "stopped" {
			spec.Execution["state"] = "cancelled"
		} else if err != nil || state == "failed" || (failed > 0 && success == 0) {
			spec.Execution["state"] = "deferred"
			if err != nil {
				spec.Execution["reason"] = err.Error()
			} else {
				spec.Execution["reason"] = "本阶段尚未取得可保存的价格结果"
			}
			deferred = true
			r.opts.Log("价格更新周期 %d 的 %s 阶段未完成：%v", cycleID, spec.Scope, spec.Execution["reason"])
		}
		err = nil
		if e := r.saveSpecs(context.Background(), cycleID, specs); e != nil {
			return cycleID, e
		}
	}
	if err = r.saveSpecs(context.Background(), cycleID, specs); err != nil {
		return cycleID, err
	}
	terminal := "completed"
	reason := ""
	enteredRepairing := false
	if ctx.Err() == nil {
		pending, e := r.planner.Missing(ctx, 0)
		if e != nil {
			return cycleID, e
		}
		if len(pending) > 0 {
			enteredRepairing = true
			if e = r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
				_, e := conn.ExecContext(ctx, "UPDATE price_cycles SET status='repairing',repairing_at=? WHERE id=? AND status='running'", timestamp(r.now()), cycleID)
				return e
			}); e != nil {
				return cycleID, e
			}
		}
	}
	if ctx.Err() != nil {
		terminal = "failed"
		reason = "进程退出中断，未完成地区将自动补全"
		if errors.Is(context.Cause(ctx), errUserStopped) {
			terminal = "cancelled"
			reason = "用户已停止"
		}
	} else {
		hasDone, hasOther := false, false
		for _, s := range statuses {
			if s == "done" {
				hasDone = true
			} else {
				hasOther = true
			}
		}
		if len(statuses) == 0 && len(allIDs)*len(regions) > 0 || len(statuses) > 0 && !hasDone {
			terminal = "failed"
			reason = "本轮没有成功完成的抓取阶段"
		} else if deferred || hasOther || enteredRepairing {
			terminal = "partial"
			reason = "本轮有未更新的游戏，已保留并将在后续更新中优先补跑"
		}
		if networkBlocked {
			reason = networkBlockedReason
		}
	}
	finishCtx := context.Background()
	if err = r.gate.WithWrite(finishCtx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(finishCtx, `UPDATE price_cycles SET status='finalizing',finalizing_at=? WHERE id=? AND status IN ('running','repairing')`, timestamp(r.now()), cycleID)
		return e
	}); err != nil {
		return cycleID, err
	}
	r.bus.Publish("price_cycle.finalizing", events.KV{Key: "cycleId", Val: cycleID})
	if ctx.Err() == nil {
		if _, e := priceevents.New(r.db, r.gate).Detect(finishCtx, cycleID); e != nil {
			r.opts.Log("价格变化检测失败：%v", e)
		}
	}
	if err = r.freezeStats(finishCtx, cycleID, allIDs, regions, started, r.now()); err != nil {
		return cycleID, err
	}
	err = r.finishCycle(finishCtx, cycleID, terminal, reason)
	return cycleID, err
}

func (r *Runtime) saveSpecs(ctx context.Context, id int64, specs []Spec) error {
	return r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(ctx, "UPDATE price_cycles SET specs_json=? WHERE id=?", jsonString(specs), id)
		return e
	})
}
func (r *Runtime) finishCycle(ctx context.Context, id int64, status, reason string) error {
	changed := false
	err := r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, e := conn.ExecContext(ctx, `UPDATE price_cycles SET status=?,finished_at=?,error=? WHERE id=? AND status IN ('planning','running','repairing','finalizing')`, status, timestamp(r.now()), nullable(reason), id)
		if e != nil {
			return e
		}
		n, e := result.RowsAffected()
		changed = n > 0
		return e
	})
	if err == nil && changed {
		r.opts.Log("价格更新周期 %d 已结束：%s", id, status)
		r.bus.Publish("price_cycle.completed", events.KV{Key: "cycleId", Val: id}, events.KV{Key: "status", Val: status})
	}
	return err
}

func jsonString(v any) string { b, _ := json.Marshal(v); return string(b) }
func nullable(s string) any {
	if s == "" {
		return nil
	}
	return s
}
func regionCodes(regions []Region) []string {
	codes := make([]string, 0, len(regions))
	for _, r := range regions {
		codes = append(codes, strings.ToLower(r.Code))
	}
	return codes
}
func makeBatches(ids []int64, regions []Region) []TargetBatch {
	tasks := []TargetBatch{}
	for _, region := range regions {
		for _, batch := range storebrowse.PlanBatches(ids, region.Code, "english", false, 300) {
			tasks = append(tasks, TargetBatch{AppIDs: batch, Region: strings.ToLower(region.Code)})
		}
	}
	return tasks
}

func (r *Runtime) runJob(ctx context.Context, cycleID int64, kind, scope, trigger string, tasks []TargetBatch, regions []Region, onStart func(int64)) (jobID int64, status string, err error) {
	strategy, e := r.strategy(ctx)
	if e != nil {
		return 0, "", e
	}
	trace := &proxyRunTrace{exits: map[string]proxypool.ExitStatsRow{}, nodes: map[string]string{}, errors: map[string]int{}}
	var transport crawlTransport
	proxyMode := strategy != "direct_only"
	if proxyMode {
		transport, e = r.planProxyTransport(ctx, kind, trace)
		if e != nil {
			return 0, "", e
		}
	}
	ids := []int64{}
	seen := map[int64]bool{}
	for _, task := range tasks {
		for _, id := range task.AppIDs {
			if !seen[id] {
				seen[id] = true
				ids = append(ids, id)
			}
		}
	}
	started := r.now()
	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)
	stats := map[string]any{"trigger": trigger, "total": len(tasks), "processed": 0, "success": 0, "failed": 0, "targetStartedAt": started.Format("2006-01-02T15:04:05.000000")}
	if scope == "" {
		frozen := []map[string]any{}
		for _, task := range tasks {
			frozen = append(frozen, map[string]any{"appids": task.AppIDs, "region": strings.ToUpper(task.Region)})
		}
		stats["targets"] = map[string]any{"tasks": frozen}
	} else {
		upper := []string{}
		for _, reg := range regions {
			upper = append(upper, strings.ToUpper(reg.Code))
		}
		stats["targets"] = map[string]any{"appids": ids, "regions": upper}
	}
	err = r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, e := conn.ExecContext(ctx, `INSERT INTO crawl_jobs(kind,status,mode,regions_json,stats_json,started_at,cycle_id) VALUES(?,'running','app',?,?,?,?)`, kind, jsonString(regionCodes(regions)), jsonString(stats), timestamp(started), nullableID(cycleID))
		if e != nil {
			return e
		}
		jobID, e = result.LastInsertId()
		return e
	})
	if err != nil {
		return 0, "", err
	}
	r.mu.Lock()
	r.jobID = jobID
	r.jobCancel = func() { cancel(errUserStopped) }
	workCtx, cancelWork := context.WithCancel(r.owner)
	r.workCancel = cancelWork
	r.mu.Unlock()
	defer cancelWork()
	r.opts.Log("价格任务 %d 已开始，归属周期 %d，共 %d 批", jobID, cycleID, len(tasks))
	r.bus.Publish("job.started", events.KV{Key: "job_id", Val: jobID}, events.KV{Key: "scope", Val: scope}, events.KV{Key: "count", Val: len(ids)}, events.KV{Key: "regions", Val: regionCodes(regions)})
	if onStart != nil {
		onStart(jobID)
	}
	status = "failed"
	var proxyRunID int64
	defer func() {
		if ctx.Err() != nil {
			status = "failed"
			if errors.Is(context.Cause(ctx), errUserStopped) {
				status = "stopped"
			} else if err == nil {
				err = ctx.Err()
			}
		}
		finishCtx := context.Background()
		if e := r.finishJob(finishCtx, jobID, cycleID, status, stats, err); e != nil {
			err = errors.Join(err, e)
		}
		r.finishProxyRun(finishCtx, proxyRunID, trace, started, ctx.Err() != nil, stats, err)
		r.mu.Lock()
		r.jobID = 0
		r.jobCancel = nil
		r.workCancel = nil
		r.mu.Unlock()
	}()
	if !proxyMode {
		transport, e = r.directTransport(ctx)
		if e != nil {
			return jobID, status, e
		}
	}
	proxyRunID = r.startProxyRun(ctx, jobID, transport, regions, started)
	metadata, e := r.loadMetadata(ctx, ids)
	if e != nil {
		return jobID, status, e
	}
	metadataIDs := ids
	if proxyMode {
		metadataIDs = nil
		for _, id := range ids {
			if _, known := metadata[id]; !known {
				metadataIDs = append(metadataIDs, id)
			}
		}
	}
	localized := r.prefetchMetadata(ctx, workCtx, metadataIDs, transport)
	if ctx.Err() != nil {
		return jobID, status, nil
	}
	games := map[int64]dbwriter.GameData{}
	for _, id := range ids {
		old, has := metadata[id]
		var preserved *dbwriter.GameData
		if has {
			preserved = &old
		}
		game, ok := storebrowse.BuildGame(id, localized["schinese"][id], localized["english"][id], preserved, r.now())
		if ok {
			games[id] = game
		}
	}
	writer := dbwriter.NewWriter(r.db, r.gate)
	writer.Now = r.now
	writer.EventKey = r.opts.EventKey
	writer.Log = r.opts.Log
	if e := writer.LoadFxRates(ctx); e != nil {
		r.opts.Log("汇率读取失败：%v", e)
	}
	currencies := map[string]string{}
	for _, reg := range regions {
		currencies[strings.ToLower(reg.Code)] = reg.Currency
	}
	if e := r.loadTaskCurrencies(ctx, tasks, currencies); e != nil {
		return jobID, status, e
	}
	var resultMu sync.Mutex
	unpersisted, skipped := []string{}, []string{}
	router := crawler.NewRouter()
	err = router.Register("app", func(taskCtx *crawler.Context) error {
		task := taskCtx.Task.Body.(TargetBatch)
		fetch := taskCtx.Client.(crawlFetch)
		items, fetchErr := fetch(workCtx, task.AppIDs, task.Region, "english", false)
		if fetchErr == nil {
			items, fetchErr = storebrowse.FollowParentOptions(workCtx, items, task.AppIDs, func(ctx context.Context, ids []int64) (map[int64]storebrowse.Item, error) {
				return fetch(ctx, ids, task.Region, "english", true)
			})
		}
		entries := []dbwriter.BatchEntry{}
		missing := []dbwriter.RegionPair{}
		clearMissing := []dbwriter.RegionPair{}
		types := map[int64]string{}
		tags := map[int64][][2]int64{}
		for _, id := range task.AppIDs {
			game, known := games[id]
			if !known {
				missing = append(missing, dbwriter.RegionPair{AppID: id, RegionCode: task.Region})
				continue
			}
			item, has := items[id]
			if fetchErr != nil || !has {
				missing = append(missing, dbwriter.RegionPair{AppID: id, RegionCode: task.Region})
				continue
			}
			if rows := storebrowse.Tags(item); len(rows) > 0 {
				tags[id] = rows
			}
			entry, state, _ := storebrowse.Classify(id, task.Region, currencies[task.Region], item, game)
			if state == "skip" {
				if game.Type != nil {
					types[id] = *game.Type
				}
				clearMissing = append(clearMissing, dbwriter.RegionPair{AppID: id, RegionCode: task.Region})
				resultMu.Lock()
				skipped = append(skipped, fmt.Sprintf("%d:%s", id, strings.ToUpper(task.Region)))
				resultMu.Unlock()
				continue
			}
			if state == "missing" {
				missing = append(missing, dbwriter.RegionPair{AppID: id, RegionCode: task.Region})
				continue
			}
			entries = append(entries, entry)
		}
		results, writeErr := writer.UpsertTaskBatch(workCtx, entries)
		if e := writer.MarkNonGameTypesBatch(workCtx, types); e != nil {
			writeErr = errors.Join(writeErr, e)
		}
		if _, e := writer.ClearMissingRegionsBatch(workCtx, clearMissing); e != nil {
			writeErr = errors.Join(writeErr, e)
		}
		if e := writer.UpsertTagsBatch(workCtx, tags); e != nil {
			r.opts.Log("游戏标签保存失败：%v", e)
		}
		for i, ok := range results {
			if !ok {
				resultMu.Lock()
				unpersisted = append(unpersisted, fmt.Sprintf("%d:%s", entries[i].Game.AppID, strings.ToUpper(task.Region)))
				resultMu.Unlock()
			}
		}
		if writeErr != nil {
			resultMu.Lock()
			for _, entry := range entries {
				unpersisted = append(unpersisted, fmt.Sprintf("%d:%s", entry.Game.AppID, strings.ToUpper(task.Region)))
			}
			resultMu.Unlock()
		}
		if _, markErr := writer.MarkRegionStatusMissingBatch(workCtx, missing); markErr != nil {
			resultMu.Lock()
			for _, pair := range missing {
				unpersisted = append(unpersisted, fmt.Sprintf("%d:%s", pair.AppID, strings.ToUpper(pair.RegionCode)))
			}
			resultMu.Unlock()
			writeErr = errors.Join(writeErr, markErr)
		}
		return errors.Join(fetchErr, writeErr)
	})
	if err != nil {
		return jobID, status, err
	}
	pool := transport.scheduler(router, crawler.WithProgress(func(p crawler.Progress) {
		r.bus.Publish("crawl.progress", events.KV{Key: "done", Val: p.Done}, events.KV{Key: "ok", Val: p.Ok}, events.KV{Key: "fail", Val: p.Fail}, events.KV{Key: "total", Val: p.Total}, events.KV{Key: "qsize", Val: p.Queued}, events.KV{Key: "speed", Val: p.Speed})
	}), crawler.WithErrorSink(func(task crawler.Task, e error) {
		trace.noteError(e)
		r.opts.Log("价格任务 %d 的 %s 批次未完成：%v", jobID, task.ID, e)
	}))
	work := make([]crawler.Task, 0, len(tasks))
	for i, task := range tasks {
		work = append(work, crawler.Task{ID: fmt.Sprintf("%s:%d", task.Region, i), Type: "app", Body: task})
	}
	runBatches(ctx, pool, work)
	done, ok, failed := pool.Counts()
	stats["processed"] = done
	stats["success"] = ok
	stats["failed"] = failed
	stats["unpersistedUnits"] = uniqueStrings(unpersisted)
	stats["skipUnits"] = uniqueStrings(skipped)
	stats["elapsed_seconds"] = r.now().Sub(started).Seconds()
	stats["skipped_no_discount"] = 0
	stats["discount_ended"] = 0
	status = "done"
	if len(unpersisted) > 0 {
		status = "failed"
		err = fmt.Errorf("部分地区数据未能保存，将自动补全")
	}
	return jobID, status, err
}

func uniqueStrings(s []string) []string {
	set := map[string]bool{}
	for _, v := range s {
		set[v] = true
	}
	out := []string{}
	for v := range set {
		out = append(out, v)
	}
	sort.Strings(out)
	return out
}
func nullableID(id int64) any {
	if id == 0 {
		return nil
	}
	return id
}

func (r *Runtime) afterJob(ctx context.Context, ids []int64) {
	if r.opts.AfterJob != nil && len(ids) > 0 {
		if e := r.opts.AfterJob(ctx, ids); e != nil {
			r.opts.Log("价格已保存，后续监控维护未完成：%v", e)
		}
	}
}
func (r *Runtime) finishJob(ctx context.Context, id, cycleID int64, status string, stats map[string]any, jobErr error) error {
	var reason any
	if jobErr != nil {
		reason = jobErr.Error()
	}
	changed := false
	err := r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, e := conn.ExecContext(ctx, `UPDATE crawl_jobs SET status=?,stats_json=?,finished_at=?,error=? WHERE id=? AND status='running'`, status, jsonString(stats), timestamp(r.now()), reason, id)
		if e != nil {
			return e
		}
		n, e := result.RowsAffected()
		if e != nil {
			return e
		}
		changed = n > 0
		if cycleID != 0 {
			_, e = conn.ExecContext(ctx, `UPDATE price_cycles SET batches_done=(SELECT COALESCE(SUM(json_extract(stats_json,'$.processed')),0) FROM crawl_jobs WHERE cycle_id=?) WHERE id=?`, cycleID, cycleID)
		}
		return e
	})
	if err == nil && changed {
		r.opts.Log("价格任务 %d 已结束：%s，已处理 %v/%v 批", id, status, stats["processed"], stats["total"])
		r.bus.Publish("job.status", events.KV{Key: "job_id", Val: id}, events.KV{Key: "status", Val: status}, events.KV{Key: "error", Val: reason})
	}
	return err
}
