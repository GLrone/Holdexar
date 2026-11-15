// http_read.go agent 读面五端点（等价 Python domains/agent/router.py 的
// GET /tasks、GET /runs、GET /runs/{id}、GET /runs/{id}/events、
// POST /runs/{id}/cancel）：三源合并运行账的 HTTP 投影与精确取消。
// 挂 /api/v1/agent/*，与 MCP MountPath（/api/v1/mcp）共存不冲突。
package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

// httpRunView 运行视图（Python schemas.AgentRunView 字段序逐项一致）。
type httpRunView struct {
	RunID      string                 `json:"run_id"`
	SessionID  string                 `json:"session_id"`
	Runner     string                 `json:"runner"`
	Trigger    string                 `json:"trigger"`
	Meta       map[string]any         `json:"meta"`
	Status     string                 `json:"status"`
	ErrorCode  *string                `json:"error_code"`
	CreatedAt  *string                `json:"created_at"`
	StartedAt  *string                `json:"started_at"`
	FinishedAt *string                `json:"finished_at"`
}

// httpTaskItem 任务名条目（Python AgentTaskItem 同形）。
type httpTaskItem struct {
	Kind  string `json:"kind"`
	Label string `json:"label"`
}

// taskLabelOrder TASK_LABELS 固定顺序（Python dict 插入序）。
var taskLabelOrder = []httpTaskItem{
	{Kind: "price_refresh", Label: "priceRefresh"},
	{Kind: "price_repair", Label: "priceRepair"},
	{Kind: "price_missing", Label: "priceMissing"},
	{Kind: "dev_pytest", Label: "devPytest"},
}

// RegisterHTTPRead 装配 agent 读面五端点（与 MCP 面共用 db 与 mux）。
func RegisterHTTPRead(mux *http.ServeMux, db *sql.DB, deps *Deps) {
	h := &httpRead{db: db, deps: deps, fetch: newMuxFetcher(mux), post: newMuxPoster(mux)}
	mux.HandleFunc("GET /api/v1/agent/tasks", h.tasks)
	mux.HandleFunc("GET /api/v1/agent/runs", h.runs)
	mux.HandleFunc("GET /api/v1/agent/runs/{run_id}", h.runDetail)
	mux.HandleFunc("GET /api/v1/agent/runs/{run_id}/events", h.runEvents)
	mux.HandleFunc("POST /api/v1/agent/runs/{run_id}/cancel", h.runCancel)
}

type httpRead struct {
	db    *sql.DB
	deps  *Deps
	fetch muxFetcher
	post  muxPoster
}

func (h *httpRead) tasks(w http.ResponseWriter, r *http.Request) {
	writeRunJSON(w, http.StatusOK, taskLabelOrder)
}

// runs GET /runs?status=&limit=20：三源合并（账本行 ∪ price_cycles ∪ 无轮次
// 挂靠抓取任务），created_at 降序 + run_id 降序，limit 夹取 1..100。
func (h *httpRead) runs(w http.ResponseWriter, r *http.Request) {
	limit, err := httpQueryInt(r, "limit", 20)
	if err != nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []runValErr{*err}})
		return
	}
	if limit < 1 {
		limit = 1
	}
	if limit > 100 {
		limit = 100
	}
	views, listErr := h.listRuns(r.Context(), r.URL.Query().Get("status"), limit)
	if listErr != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	writeRunJSON(w, http.StatusOK, map[string]any{"items": views})
}

func (h *httpRead) runDetail(w http.ResponseWriter, r *http.Request) {
	view, err := h.getRun(r.Context(), r.PathValue("run_id"))
	if err != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	if view == nil {
		writeRunJSON(w, http.StatusNotFound, map[string]any{"detail": "run not found"})
		return
	}
	writeRunJSON(w, http.StatusOK, view)
}

// runEvents GET /runs/{id}/events?after_seq=&limit=200：按 seq 升序回放；
// 域行无事件账本恒空集（Python list_events 同径）。
func (h *httpRead) runEvents(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("run_id")
	afterSeq, err := httpQueryInt(r, "after_seq", 0)
	if err == nil {
		afterSeq, err = httpQueryInt(r, "limit", 200)
	}
	if err != nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []runValErr{*err}})
		return
	}
	limit, err := httpQueryInt(r, "limit", 200)
	if err != nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []runValErr{*err}})
		return
	}
	afterSeq, err = httpQueryInt(r, "after_seq", 0)
	if err != nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []runValErr{*err}})
		return
	}
	if limit < 1 {
		limit = 1
	}
	if limit > 200 {
		limit = 200
	}
	view, dbErr := h.getRun(r.Context(), runID)
	if dbErr != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	if view == nil {
		writeRunJSON(w, http.StatusNotFound, map[string]any{"detail": "run not found"})
		return
	}
	items := []map[string]any{}
	if _, _, isDomain := splitDomainRunID(runID); !isDomain {
		rows, qErr := h.db.QueryContext(r.Context(),
			`SELECT seq, event_type, CAST(payload AS TEXT), CAST(created_at AS TEXT)
			 FROM agent_events WHERE run_id=? AND seq > ? ORDER BY seq ASC LIMIT ?`,
			runID, afterSeq, limit)
		if qErr != nil {
			writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
			return
		}
		scanErr := error(nil)
		for rows.Next() {
			var seq int64
			var eventType string
			var payload, createdAt sql.NullString
			if err := rows.Scan(&seq, &eventType, &payload, &createdAt); err != nil {
				scanErr = err
				break
			}
			item := map[string]any{"seq": seq, "event_type": eventType}
			if payload.Valid && payload.String != "" {
				item["payload"] = json.RawMessage(payload.String)
			} else {
				item["payload"] = nil
			}
			item["created_at"] = nullIfEmpty(isoStamp(createdAt.String))
			items = append(items, item)
		}
		if rowsErr := rows.Err(); rowsErr != nil {
			scanErr = rowsErr
		}
		rows.Close()
		if scanErr != nil {
			writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
			return
		}
	}
	writeRunJSON(w, http.StatusOK, map[string]any{"run_id": runID, "items": items})
}

// runCancel POST /runs/{id}/cancel {reason}：域行走 crawl stop 两支路；
// 账本 fake 行走取消跃迁写；runner≠fake 无执行句柄 → 409（Python
// RunCancellationUnavailable 同径）。
func (h *httpRead) runCancel(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("run_id")
	reason, err := decodeCancelBody(r)
	if err != nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []runValErr{*err}})
		return
	}
	view, dbErr := h.getRun(r.Context(), runID)
	if dbErr != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	if view == nil {
		writeRunJSON(w, http.StatusNotFound, map[string]any{"detail": "run not found: " + runID})
		return
	}
	if isTerminalRunStatus(view.Status) {
		writeRunJSON(w, http.StatusOK, map[string]any{"accepted": false, "status": view.Status})
		return
	}
	if source, id, ok := splitDomainRunID(runID); ok {
		live := h.liveJobID(r.Context())
		chain := "false"
		if source == "cycle" {
			live = h.liveCycleID(r.Context())
			chain = "true"
		}
		if id != live {
			writeRunJSON(w, http.StatusConflict,
				map[string]any{"detail": "run has no matching live execution"})
			return
		}
		path := "/api/v1/crawl/stop?chain=" + chain
		if source == "crawl" {
			path = "/api/v1/crawl/stop?jobId=" + strconv.FormatInt(id, 10) + "&chain=false"
		}
		var out map[string]any
		if _, body, err := h.post(r.Context(), path, nil); err == nil {
			_ = json.Unmarshal(body, &out)
		}
		accepted := false
		if stopped, ok := out["stopped"].(bool); ok {
			accepted = stopped
		}
		fresh, err := h.getRun(r.Context(), runID)
		if err != nil {
			writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
			return
		}
		if fresh == nil {
			writeRunJSON(w, http.StatusNotFound, map[string]any{"detail": "run not found: " + runID})
			return
		}
		writeRunJSON(w, http.StatusOK, map[string]any{"accepted": accepted, "status": fresh.Status})
		return
	}
	if view.Runner != "fake" {
		writeRunJSON(w, http.StatusConflict, map[string]any{"detail": "run has no live stop handle"})
		return
	}
	status, cancelErr := h.cancelLedgerRun(r.Context(), runID, view.Status, reason)
	if cancelErr != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	writeRunJSON(w, http.StatusOK, map[string]any{"accepted": true, "status": status})
}

// cancelLedgerRun 账本行取消跃迁：cancel.requested / run.state / run.finished
// 三事件 + 状态与 finished_at 落定（Python cancel_run write_session 分支同构）。
func (h *httpRead) cancelLedgerRun(ctx context.Context, runID, current, reason string) (string, error) {
	gate := (*store.WriteGate)(nil)
	if h.deps != nil {
		gate = h.deps.WriteGate
	}
	stamp := stampBeijing(time.Now())
	write := func(conn *sql.Conn) error {
		var sequence int64
		if err := conn.QueryRowContext(ctx,
			"SELECT COALESCE(MAX(seq),0) FROM agent_events WHERE run_id=?", runID).Scan(&sequence); err != nil {
			return err
		}
		events := []map[string]any{
			{"type": "cancel.requested", "payload": map[string]any{"reason": reason, "by": "user"}},
			{"type": "run.state", "payload": map[string]any{"from": current, "to": "cancelled", "reason": reason}},
			{"type": "run.finished", "payload": map[string]any{"status": "cancelled", "error_code": nil}},
		}
		raw, err := json.Marshal(events)
		if err != nil {
			return err
		}
		if _, err := conn.ExecContext(ctx,
			`UPDATE agent_runs SET status='cancelled', finished_at=? WHERE id=? AND status=?`,
			stamp, runID, current); err != nil {
			return err
		}
		_, err = conn.ExecContext(ctx,
			`INSERT INTO agent_events(run_id,seq,event_type,payload,created_at) SELECT ?,?+CAST(key AS INTEGER)+1,json_extract(value,'$.type'),json_extract(value,'$.payload'),? FROM json_each(?)`,
			runID, sequence, stamp, string(raw))
		return err
	}
	if gate != nil {
		err := gate.WithWrite(ctx, store.PriorityInteractive, write)
		if err != nil {
			return "", err
		}
		return "cancelled", nil
	}
	return "", errors.New("写闸未装配，取消请求无法落账")
}

// listRuns 三源合并读（Python list_runs 同构）。
func (h *httpRead) listRuns(ctx context.Context, status string, limit int) ([]httpRunView, error) {
	mergeLimit := 100
	views := []httpRunView{}
	rows, args := buildAgentRunQuery(status, mergeLimit)
	cursor, err := h.db.QueryContext(ctx, rows, args...)
	if err != nil {
		return nil, err
	}
	for cursor.Next() {
		var id, sessionID, runner, trigger, status string
		var metaRaw sql.NullString
		var errorCode sql.NullString
		var createdAt, startedAt, finishedAt sql.NullString
		if err := cursor.Scan(&id, &sessionID, &runner, &trigger, &metaRaw, &status, &errorCode,
			&createdAt, &startedAt, &finishedAt); err != nil {
			cursor.Close()
			return nil, err
		}
		meta := map[string]any{}
		if metaRaw.Valid && metaRaw.String != "" {
			_ = jsonDecoder([]byte(metaRaw.String)).Decode(&meta)
		}
		meta["cancellable"] = !isTerminalRunStatus(status) && runner == "fake"
		views = append(views, httpRunView{
			RunID: id, SessionID: sessionID, Runner: runner, Trigger: trigger, Meta: meta,
			Status: status, ErrorCode: nullIfEmpty(errorCode.String),
			CreatedAt: nullIfEmpty(isoStamp(createdAt.String)),
			StartedAt: nullIfEmpty(isoStamp(startedAt.String)),
			FinishedAt: nullIfEmpty(isoStamp(finishedAt.String)),
		})
	}
	if err := cursor.Err(); err != nil {
		cursor.Close()
		return nil, err
	}
	cursor.Close()
	if err := h.appendDomainRuns(ctx, status, mergeLimit, &views); err != nil {
		return nil, err
	}
	sort.SliceStable(views, func(i, j int) bool {
		ti, tj := taskTimeKey(stringOrStar(views[i].CreatedAt)), taskTimeKey(stringOrStar(views[j].CreatedAt))
		if !ti.Equal(tj) {
			return ti.After(tj)
		}
		return views[i].RunID > views[j].RunID
	})
	if len(views) > limit {
		views = views[:limit]
	}
	return views, nil
}

// buildAgentRunQuery 账本行筛选（Python list_runs 主查询同构）。
func buildAgentRunQuery(status string, limit int) (string, []any) {
	query := `SELECT id, session_id, runner, "trigger", CAST(meta AS TEXT), status, error_code,
		CAST(created_at AS TEXT), CAST(started_at AS TEXT), CAST(finished_at AS TEXT)
		FROM agent_runs WHERE (runner <> 'task' OR COALESCE(json_extract(meta,'$.task'),'') = 'dev_pytest')
		AND NOT (runner = 'job' AND COALESCE(json_extract(meta,'$.task'),'') IN ('price_refresh','price_repair','price_missing'))`
	args := []any{}
	if status != "" {
		query += ` AND status = ?`
		args = append(args, status)
	}
	query += ` ORDER BY created_at DESC, id DESC LIMIT ?`
	args = append(args, limit)
	return query, args
}

// appendDomainRuns 域行并入（cycle ∪ 无轮次挂靠 crawl job），状态过滤按
// 域状态→run 状态映射展开；failed 附带未知域态兜底（Python 同径）。
func (h *httpRead) appendDomainRuns(ctx context.Context, status string, limit int, views *[]httpRunView) error {
	liveCycle := h.liveCycleID(ctx)
	liveJob := h.liveJobID(ctx)
	matches, unknowns := domainStatusMatches(status)
	for _, source := range []string{"cycle", "crawl"} {
		var query string
		args := []any{}
		if source == "cycle" {
			query = `SELECT id, kind, status, batches_done, batches_expected, units_failed,
				CAST(started_at AS TEXT), CAST(finished_at AS TEXT), error FROM price_cycles WHERE 1=1`
		} else {
			query = `SELECT id, kind, status, CAST(stats_json AS TEXT), CAST(started_at AS TEXT),
				CAST(finished_at AS TEXT), error FROM crawl_jobs WHERE cycle_id IS NULL`
		}
		if len(matches) > 0 {
			query += fmt.Sprintf(` AND status IN (%s)`, strings.TrimSuffix(strings.Repeat("?,", len(matches)), ","))
			for _, m := range matches {
				args = append(args, m)
			}
			if len(unknowns) > 0 {
				query += fmt.Sprintf(` OR status NOT IN (%s)`, strings.TrimSuffix(strings.Repeat("?,", len(unknowns)), ","))
				for _, m := range unknowns {
					args = append(args, m)
				}
			}
		}
		query += ` ORDER BY started_at DESC, id DESC LIMIT ?`
		args = append(args, limit)
		cursor, err := h.db.QueryContext(ctx, query, args...)
		if err != nil {
			return err
		}
		for cursor.Next() {
			var id int64
			var kind, status string
			var statsRaw, startedAt, finishedAt sql.NullString
			var done, expected, failed any
			var errorCode sql.NullString
			if source == "cycle" {
				err = cursor.Scan(&id, &kind, &status, &done, &expected, &failed, &startedAt, &finishedAt, &errorCode)
			} else {
				err = cursor.Scan(&id, &kind, &status, &statsRaw, &startedAt, &finishedAt, &errorCode)
			}
			if err != nil {
				cursor.Close()
				return err
			}
			runID := source + ":" + strconv.FormatInt(id, 10)
			var progress map[string]any
			trigger := kind
			var task string
			if source == "cycle" {
				task = "price_refresh"
				progress = map[string]any{"done": done, "total": expected, "failed": failed}
			} else {
				task = mapCrawlTask(kind)
				stats := map[string]any{}
				if statsRaw.Valid && statsRaw.String != "" {
					_ = jsonDecoder([]byte(statsRaw.String)).Decode(&stats)
				}
				if text, ok := stats["trigger"].(string); ok && text != "" {
					trigger = text
				} else {
					trigger = "unknown"
				}
				progress = map[string]any{"done": stats["processed"], "total": stats["total"]}
				for _, key := range []string{"success", "failed", "skipped_no_discount", "discount_ended"} {
					if value, ok := stats[key]; ok {
						progress[key] = value
					}
				}
			}
			runStatus := mapDomainStatus(status)
			activeID := liveJob
			if source == "cycle" {
				activeID = liveCycle
			}
			meta := map[string]any{
				"domainStatus": status, "progress": progress,
				"cancellable": !isTerminalRunStatus(runStatus) && activeID == id,
			}
			if task != "" {
				meta["task"] = task
			} else {
				meta["job"] = kind
			}
			*views = append(*views, httpRunView{
				RunID: runID, SessionID: "runtime", Runner: "task", Trigger: trigger, Meta: meta,
				Status: runStatus, ErrorCode: nullIfEmpty(errorCode.String),
				CreatedAt: nullIfEmpty(isoStamp(startedAt.String)),
				StartedAt: nullIfEmpty(isoStamp(startedAt.String)),
				FinishedAt: nullIfEmpty(isoStamp(finishedAt.String)),
			})
		}
		if err := cursor.Err(); err != nil {
			cursor.Close()
			return err
		}
		cursor.Close()
	}
	return nil
}

// domainStatusMatches run 状态过滤 → 域状态集合（failed 追加未知域态兜底）。
func domainStatusMatches(status string) (matches, unknowns []string) {
	if status == "" {
		return nil, nil
	}
	for domain, mapped := range domainStatusToRun {
		if mapped == status {
			matches = append(matches, domain)
		}
	}
	sort.Strings(matches)
	if status == "failed" {
		for domain := range domainStatusToRun {
			unknowns = append(unknowns, domain)
		}
		sort.Strings(unknowns)
	}
	return matches, unknowns
}

// getRun 按运行标识读单条（域键读域行，其余读账本；显式 cycle:/crawl: 前缀
// 解析失败一律 not found，不回落账本查询，Python get_run 同径）。
func (h *httpRead) getRun(ctx context.Context, runID string) (*httpRunView, error) {
	if strings.HasPrefix(runID, "cycle:") || strings.HasPrefix(runID, "crawl:") {
		source, id, ok := splitDomainRunID(runID)
		if !ok {
			return nil, nil
		}
		return h.getDomainRun(ctx, source, id)
	}
	var id, sessionID, runner, trigger, status string
	var metaRaw sql.NullString
	var errorCode sql.NullString
	var createdAt, startedAt, finishedAt sql.NullString
	err := h.db.QueryRowContext(ctx,
		`SELECT id, session_id, runner, "trigger", CAST(meta AS TEXT), status, error_code,
		 CAST(created_at AS TEXT), CAST(started_at AS TEXT), CAST(finished_at AS TEXT)
		 FROM agent_runs WHERE id=?`, runID).
		Scan(&id, &sessionID, &runner, &trigger, &metaRaw, &status, &errorCode,
			&createdAt, &startedAt, &finishedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	meta := map[string]any{}
	if metaRaw.Valid && metaRaw.String != "" {
		_ = jsonDecoder([]byte(metaRaw.String)).Decode(&meta)
	}
	meta["cancellable"] = !isTerminalRunStatus(status) && runner == "fake"
	return &httpRunView{
		RunID: id, SessionID: sessionID, Runner: runner, Trigger: trigger, Meta: meta,
		Status: status, ErrorCode: nullIfEmpty(errorCode.String),
		CreatedAt: nullIfEmpty(isoStamp(createdAt.String)),
		StartedAt: nullIfEmpty(isoStamp(startedAt.String)),
		FinishedAt: nullIfEmpty(isoStamp(finishedAt.String)),
	}, nil
}

// getDomainRun 域行单条（crawl 行挂了轮次不可见，Python 同径）。
func (h *httpRead) getDomainRun(ctx context.Context, source string, id int64) (*httpRunView, error) {
	liveCycle := h.liveCycleID(ctx)
	liveJob := h.liveJobID(ctx)
	activeID := liveJob
	if source == "cycle" {
		activeID = liveCycle
		var kind, status string
		var done, expected, failed any
		var startedAt, finishedAt sql.NullString
		var errorCode sql.NullString
		err := h.db.QueryRowContext(ctx,
			`SELECT kind, status, batches_done, batches_expected, units_failed,
			 CAST(started_at AS TEXT), CAST(finished_at AS TEXT), error FROM price_cycles WHERE id=?`, id).
			Scan(&kind, &status, &done, &expected, &failed, &startedAt, &finishedAt, &errorCode)
		if errors.Is(err, sql.ErrNoRows) {
			return nil, nil
		}
		if err != nil {
			return nil, err
		}
		runStatus := mapDomainStatus(status)
		return &httpRunView{
			RunID: source + ":" + strconv.FormatInt(id, 10), SessionID: "runtime",
			Runner: "task", Trigger: kind,
			Meta: map[string]any{
				"domainStatus": status,
				"progress":     map[string]any{"done": done, "total": expected, "failed": failed},
				"cancellable":  !isTerminalRunStatus(runStatus) && activeID == id,
				"task":         "price_refresh",
			},
			Status: runStatus, ErrorCode: nullIfEmpty(errorCode.String),
			CreatedAt: nullIfEmpty(isoStamp(startedAt.String)),
			StartedAt: nullIfEmpty(isoStamp(startedAt.String)),
			FinishedAt: nullIfEmpty(isoStamp(finishedAt.String)),
		}, nil
	}
	var kind, status string
	var statsRaw, startedAt, finishedAt sql.NullString
	var errorCode sql.NullString
	err := h.db.QueryRowContext(ctx,
		`SELECT kind, status, CAST(stats_json AS TEXT), CAST(started_at AS TEXT),
		 CAST(finished_at AS TEXT), error FROM crawl_jobs WHERE id=? AND cycle_id IS NULL`, id).
		Scan(&kind, &status, &statsRaw, &startedAt, &finishedAt, &errorCode)
	if errors.Is(err, sql.ErrNoRows) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	stats := map[string]any{}
	if statsRaw.Valid && statsRaw.String != "" {
		_ = jsonDecoder([]byte(statsRaw.String)).Decode(&stats)
	}
	progress := map[string]any{"done": stats["processed"], "total": stats["total"]}
	for _, key := range []string{"success", "failed", "skipped_no_discount", "discount_ended"} {
		if value, ok := stats[key]; ok {
			progress[key] = value
		}
	}
	trigger := "unknown"
	if text, ok := stats["trigger"].(string); ok && text != "" {
		trigger = text
	}
	runStatus := mapDomainStatus(status)
	meta := map[string]any{
		"domainStatus": status, "progress": progress,
		"cancellable": !isTerminalRunStatus(runStatus) && activeID == id,
	}
	if task := mapCrawlTask(kind); task != "" {
		meta["task"] = task
	} else {
		meta["job"] = kind
	}
	return &httpRunView{
		RunID: source + ":" + strconv.FormatInt(id, 10), SessionID: "runtime",
		Runner: "task", Trigger: trigger, Meta: meta,
		Status: runStatus, ErrorCode: nullIfEmpty(errorCode.String),
		CreatedAt: nullIfEmpty(isoStamp(startedAt.String)),
		StartedAt: nullIfEmpty(isoStamp(startedAt.String)),
		FinishedAt: nullIfEmpty(isoStamp(finishedAt.String)),
	}, nil
}

// liveCycleID 当前在跑的价格轮（执行态近似，tools_ops_runtime 同口径）。
func (h *httpRead) liveCycleID(ctx context.Context) int64 {
	var id int64
	_ = h.db.QueryRowContext(ctx,
		`SELECT COALESCE(MAX(id),0) FROM price_cycles
		 WHERE status IN ('planning','running','repairing','finalizing')`).Scan(&id)
	return id
}

// liveJobID 当前在跑的抓取任务（/crawl/active 运行时事实）。
func (h *httpRead) liveJobID(ctx context.Context) int64 {
	status, body, err := h.fetch(ctx, "/api/v1/crawl/active")
	if err != nil || status != http.StatusOK {
		return 0
	}
	var snap map[string]any
	if json.Unmarshal(body, &snap) != nil {
		return 0
	}
	raw, ok := snap["activeJobId"]
	if !ok || raw == nil {
		return 0
	}
	switch number := raw.(type) {
	case json.Number:
		if id, err := number.Int64(); err == nil {
			return id
		}
	case float64:
		return int64(number)
	}
	return 0
}

// decodeCancelBody cancel 请求体校验（reason 缺省 "user"、max_length=100）。
func decodeCancelBody(r *http.Request) (string, *runValErr) {
	raw, err := io.ReadAll(http.MaxBytesReader(nil, r.Body, 1<<20))
	if err != nil {
		return "user", nil
	}
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" {
		return "user", nil
	}
	var top any
	dec := json.NewDecoder(strings.NewReader(trimmed))
	dec.UseNumber()
	if err := dec.Decode(&top); err != nil {
		return "", &runValErr{Type: "json_invalid", Loc: []any{"body"}, Msg: "JSON decode error", Input: map[string]any{}}
	}
	fields, ok := top.(map[string]any)
	if !ok {
		return "", &runValErr{Type: "model_attributes_type", Loc: []any{"body"},
			Msg: "Input should be a valid dictionary or object to extract fields from", Input: top}
	}
	value, present := fields["reason"]
	if !present || value == nil {
		return "user", nil
	}
	text, isStr := value.(string)
	if !isStr {
		return "", &runValErr{Type: "string_type", Loc: []any{"body", "reason"},
			Msg: "Input should be a valid string", Input: value}
	}
	if len(text) > 100 {
		return "", &runValErr{Type: "string_too_long", Loc: []any{"body", "reason"},
			Msg: "String should have at most 100 characters", Input: text,
			Ctx: map[string]any{"max_length": 100}}
	}
	return text, nil
}

// runValErr 校验错误项（Pydantic 实测形状）。
type runValErr struct {
	Type  string         `json:"type"`
	Loc   []any          `json:"loc"`
	Msg   string         `json:"msg"`
	Input any            `json:"input"`
	Ctx   map[string]any `json:"ctx,omitempty"`
}

// httpQueryInt 查询参数整数校验（Pydantic query 校验同形状）。
func httpQueryInt(r *http.Request, key string, fallback int) (int, *runValErr) {
	raw := r.URL.Query().Get(key)
	if raw == "" {
		return fallback, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 0, &runValErr{Type: "int_parsing", Loc: []any{"query", key},
			Msg: "Input should be a valid integer, unable to parse string as an integer", Input: raw}
	}
	return n, nil
}

// isoStamp 库内裸时刻 → FastAPI datetime ISO 形态（空格换 T）。
func isoStamp(stamp string) string {
	if stamp == "" {
		return ""
	}
	return strings.Replace(stamp, " ", "T", 1)
}

func nullIfEmpty(value string) *string {
	if value == "" {
		return nil
	}
	return &value
}

func stringOrStar(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func writeRunJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
