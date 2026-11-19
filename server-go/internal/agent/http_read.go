package agent

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/store"
)

type httpRunView struct {
	RunID      string         `json:"run_id"`
	SessionID  string         `json:"session_id"`
	Runner     string         `json:"runner"`
	Trigger    string         `json:"trigger"`
	Meta       map[string]any `json:"meta"`
	Status     string         `json:"status"`
	ErrorCode  *string        `json:"error_code"`
	CreatedAt  *string        `json:"created_at"`
	StartedAt  *string        `json:"started_at"`
	FinishedAt *string        `json:"finished_at"`
}

type httpTaskItem struct {
	Kind  string `json:"kind"`
	Label string `json:"label"`
}

var taskLabelOrder = []httpTaskItem{
	{Kind: "price_refresh", Label: "priceRefresh"},
	{Kind: "price_repair", Label: "priceRepair"},
	{Kind: "price_missing", Label: "priceMissing"},
	{Kind: "dev_pytest", Label: "devPytest"},
}

func RegisterHTTPRead(mux *http.ServeMux, db *sql.DB, deps *Deps) {
	h := &httpRead{
		db: db, deps: deps, fetch: newMuxFetcher(mux), post: newMuxPoster(mux),
		fakeCancels: map[string]context.CancelFunc{},
	}
	mux.HandleFunc("GET /api/v1/agent/tasks", h.tasks)
	mux.HandleFunc("POST /api/v1/agent/runs", h.createRun)
	mux.HandleFunc("GET /api/v1/agent/runs", h.runs)
	mux.HandleFunc("GET /api/v1/agent/runs/{run_id}", h.runDetail)
	mux.HandleFunc("GET /api/v1/agent/runs/{run_id}/events", h.runEvents)
	mux.HandleFunc("POST /api/v1/agent/runs/{run_id}/cancel", h.runCancel)
	if db != nil {
		h.registerMemoryRoutes(mux)
	}
}

type httpRead struct {
	db          *sql.DB
	deps        *Deps
	fetch       muxFetcher
	post        muxPoster
	mu          sync.Mutex
	fakeCancels map[string]context.CancelFunc
}

func (h *httpRead) tasks(w http.ResponseWriter, r *http.Request) {
	writeRunJSON(w, http.StatusOK, taskLabelOrder)
}

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

func (h *httpRead) runEvents(w http.ResponseWriter, r *http.Request) {
	runID := r.PathValue("run_id")
	afterSeq, err := httpQueryInt(r, "after_seq", 0)
	if err != nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []runValErr{*err}})
		return
	}
	limit, err := httpQueryInt(r, "limit", 200)
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
	h.signalFakeCancel(runID)
	status, cancelErr := h.cancelLedgerRun(r.Context(), runID, view.Status, reason)
	if cancelErr != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": nil})
		return
	}
	writeRunJSON(w, http.StatusOK, map[string]any{"accepted": true, "status": status})
}

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
			CreatedAt:  nullIfEmpty(isoStamp(createdAt.String)),
			StartedAt:  nullIfEmpty(isoStamp(startedAt.String)),
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
	slices.SortStableFunc(views, func(a, b httpRunView) int {
		ta, tb := taskTimeKey(stringOrStar(a.CreatedAt)), taskTimeKey(stringOrStar(b.CreatedAt))
		if c := tb.Compare(ta); c != 0 {
			return c
		}
		return strings.Compare(b.RunID, a.RunID)
	})
	if len(views) > limit {
		views = views[:limit]
	}
	return views, nil
}

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
				CreatedAt:  nullIfEmpty(isoStamp(startedAt.String)),
				StartedAt:  nullIfEmpty(isoStamp(startedAt.String)),
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

func domainStatusMatches(status string) (matches, unknowns []string) {
	if status == "" {
		return nil, nil
	}
	for domain, mapped := range domainStatusToRun {
		if mapped == status {
			matches = append(matches, domain)
		}
	}
	slices.Sort(matches)
	if status == "failed" {
		for domain := range domainStatusToRun {
			unknowns = append(unknowns, domain)
		}
		slices.Sort(unknowns)
	}
	return matches, unknowns
}

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
		CreatedAt:  nullIfEmpty(isoStamp(createdAt.String)),
		StartedAt:  nullIfEmpty(isoStamp(startedAt.String)),
		FinishedAt: nullIfEmpty(isoStamp(finishedAt.String)),
	}, nil
}

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
			CreatedAt:  nullIfEmpty(isoStamp(startedAt.String)),
			StartedAt:  nullIfEmpty(isoStamp(startedAt.String)),
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
		CreatedAt:  nullIfEmpty(isoStamp(startedAt.String)),
		StartedAt:  nullIfEmpty(isoStamp(startedAt.String)),
		FinishedAt: nullIfEmpty(isoStamp(finishedAt.String)),
	}, nil
}

func (h *httpRead) liveCycleID(ctx context.Context) int64 {
	var id int64
	_ = h.db.QueryRowContext(ctx,
		`SELECT COALESCE(MAX(id),0) FROM price_cycles
		 WHERE status IN ('planning','running','repairing','finalizing')`).Scan(&id)
	return id
}

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

type runValErr = httpx.ValErr

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
	httpx.WriteJSON(w, status, payload)
}

func randomHex16() string {
	buf := make([]byte, 16)
	_, _ = rand.Read(buf)
	return hex.EncodeToString(buf)
}

func (h *httpRead) withWriteConn(ctx context.Context, fn func(*sql.Conn) error) error {
	if gate := h.liveWriteGate(); gate != nil {
		return gate.WithWrite(ctx, store.PriorityInteractive, fn)
	}
	conn, err := h.db.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	return fn(conn)
}

func (h *httpRead) liveWriteGate() *store.WriteGate {
	if h.deps != nil {
		return h.deps.WriteGate
	}
	return nil
}

func (h *httpRead) signalFakeCancel(runID string) {
	h.mu.Lock()
	cancel := h.fakeCancels[runID]
	h.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (h *httpRead) createRun(w http.ResponseWriter, r *http.Request) {
	var body struct {
		SessionID *string  `json:"session_id"`
		Prompt    *string  `json:"prompt"`
		Trigger   *string  `json:"trigger"`
		Runner    *string  `json:"runner"`
		Scenario  *string  `json:"scenario"`
		Steps     *int     `json:"steps"`
		StepDelay *float64 `json:"step_delay"`
	}
	if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20)).Decode(&body); err != nil {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "invalid request body"})
		return
	}
	runner := "fake"
	if body.Runner != nil {
		runner = *body.Runner
	}
	if runner == "task" {
		writeRunJSON(w, http.StatusGone, map[string]any{"detail": "start price work through the crawl domain"})
		return
	}
	if runner != "fake" {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "invalid runner"})
		return
	}
	scenario := "success"
	if body.Scenario != nil {
		scenario = *body.Scenario
	}
	if scenario != "success" && scenario != "failure" {
		writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "unknown scenario"})
		return
	}
	steps := 3
	if body.Steps != nil {
		if *body.Steps < 1 || *body.Steps > 20 {
			writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "steps out of range"})
			return
		}
		steps = *body.Steps
	}
	stepDelay := 0.0
	if body.StepDelay != nil {
		if *body.StepDelay < 0 || *body.StepDelay > 5.0 {
			writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "step_delay out of range"})
			return
		}
		stepDelay = *body.StepDelay
	}
	trigger := "manual"
	if body.Trigger != nil && *body.Trigger != "" {
		trigger = *body.Trigger
	}
	prompt := ""
	if body.Prompt != nil {
		if len([]rune(*body.Prompt)) > 2000 {
			writeRunJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": "prompt too long"})
			return
		}
		prompt = *body.Prompt
	}

	meta := map[string]any{"scenario": scenario, "steps": steps, "step_delay": stepDelay}
	metaRaw, _ := json.Marshal(meta)
	runID := randomHex16()
	ts := stampBeijing(time.Now())
	var sid string
	var notFound bool
	err := h.withWriteConn(r.Context(), func(conn *sql.Conn) error {
		if body.SessionID == nil || *body.SessionID == "" {
			sid = randomHex16()
			if _, err := conn.ExecContext(r.Context(),
				`INSERT INTO agent_sessions(id, created_at, updated_at) VALUES(?,?,?)`, sid, ts, ts); err != nil {
				return err
			}
		} else {
			sid = *body.SessionID
			var existing string
			err := conn.QueryRowContext(r.Context(),
				`SELECT id FROM agent_sessions WHERE id=?`, sid).Scan(&existing)
			if errors.Is(err, sql.ErrNoRows) {
				notFound = true
				return nil
			}
			if err != nil {
				return err
			}
			if _, err := conn.ExecContext(r.Context(),
				`UPDATE agent_sessions SET updated_at=? WHERE id=?`, ts, sid); err != nil {
				return err
			}
		}
		if _, err := conn.ExecContext(r.Context(),
			`INSERT INTO agent_runs(id, session_id, runner, "trigger", meta, status, created_at) VALUES(?,?,?,?,?,'queued',?)`,
			runID, sid, runner, trigger, string(metaRaw), ts); err != nil {
			return err
		}
		if prompt != "" {
			payload, _ := json.Marshal(map[string]any{"text": prompt})
			if _, err := conn.ExecContext(r.Context(),
				`INSERT INTO agent_events(run_id, seq, event_type, payload, created_at) VALUES(?,1,'turn.received',?,?)`,
				runID, string(payload), ts); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		writeRunJSON(w, http.StatusInternalServerError, map[string]any{"detail": err.Error()})
		return
	}
	if notFound {
		writeRunJSON(w, http.StatusNotFound, map[string]any{"detail": "session not found: " + sid})
		return
	}

	runCtx, cancel := context.WithCancel(context.Background())
	h.mu.Lock()
	h.fakeCancels[runID] = cancel
	h.mu.Unlock()
	go h.executeFakeRun(runCtx, runID, scenario, steps, stepDelay)
	writeRunJSON(w, http.StatusOK, map[string]any{
		"run_id": runID, "session_id": sid, "status": "queued",
	})
}

func (h *httpRead) executeFakeRun(ctx context.Context, runID, scenario string, steps int, stepDelay float64) {
	defer func() {
		h.mu.Lock()
		delete(h.fakeCancels, runID)
		h.mu.Unlock()
	}()
	if !h.transitionFakeRun(runID, "queued", "running", "", "") {
		return
	}
	for n := 1; n <= steps; n++ {
		if ctx.Err() != nil {
			return
		}
		if !h.appendFakeEvent(runID, "step.started", map[string]any{"step": n}) {
			return
		}
		if scenario == "failure" && n == 1 {
			_ = h.transitionFakeRun(runID, "running", "failed", "", "fake_error")
			return
		}
		if stepDelay > 0 {
			timer := time.NewTimer(time.Duration(stepDelay * float64(time.Second)))
			select {
			case <-ctx.Done():
				timer.Stop()
				return
			case <-timer.C:
			}
		}
		if !h.appendFakeEvent(runID, "step.completed", map[string]any{
			"step": n, "summary": fmt.Sprintf("fake step %d", n),
		}) {
			return
		}
	}
	if ctx.Err() == nil {
		_ = h.transitionFakeRun(runID, "running", "done", "", "")
	}
}

func (h *httpRead) appendFakeEvent(runID, eventType string, payload map[string]any) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ts := stampBeijing(time.Now())
	raw, _ := json.Marshal(payload)
	var written bool
	err := h.withWriteConn(ctx, func(conn *sql.Conn) error {
		var status string
		if err := conn.QueryRowContext(ctx, `SELECT status FROM agent_runs WHERE id=?`, runID).Scan(&status); err != nil {
			return err
		}
		if isTerminalRunStatus(status) {
			return nil
		}
		var seq int64
		if err := conn.QueryRowContext(ctx, `SELECT COALESCE(MAX(seq),0) FROM agent_events WHERE run_id=?`, runID).Scan(&seq); err != nil {
			return err
		}
		if _, err := conn.ExecContext(ctx,
			`INSERT INTO agent_events(run_id, seq, event_type, payload, created_at) VALUES(?,?,?,?,?)`,
			runID, seq+1, eventType, string(raw), ts); err != nil {
			return err
		}
		written = true
		return nil
	})
	return err == nil && written
}

func (h *httpRead) transitionFakeRun(runID, from, to, reason, errorCode string) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	ts := stampBeijing(time.Now())
	var changed bool
	err := h.withWriteConn(ctx, func(conn *sql.Conn) error {
		var res sql.Result
		var err error
		if to == "running" {
			res, err = conn.ExecContext(ctx,
				`UPDATE agent_runs SET status='running', started_at=COALESCE(started_at, ?) WHERE id=? AND status=?`,
				ts, runID, from)
		} else {
			var ec any
			if errorCode != "" {
				ec = errorCode
			}
			res, err = conn.ExecContext(ctx,
				`UPDATE agent_runs SET status=?, finished_at=?, error_code=COALESCE(?, error_code) WHERE id=? AND status=?`,
				to, ts, ec, runID, from)
		}
		if err != nil {
			return err
		}
		n, err := res.RowsAffected()
		if err != nil || n == 0 {
			return err
		}
		var seq int64
		if err := conn.QueryRowContext(ctx, `SELECT COALESCE(MAX(seq),0) FROM agent_events WHERE run_id=?`, runID).Scan(&seq); err != nil {
			return err
		}
		statePayload, _ := json.Marshal(map[string]any{"from": from, "to": to, "reason": nullStringPtr(reason)})
		if _, err := conn.ExecContext(ctx,
			`INSERT INTO agent_events(run_id, seq, event_type, payload, created_at) VALUES(?,?,'run.state',?,?)`,
			runID, seq+1, string(statePayload), ts); err != nil {
			return err
		}
		if isTerminalRunStatus(to) {
			finPayload, _ := json.Marshal(map[string]any{"status": to, "error_code": nullStringPtr(errorCode)})
			if _, err := conn.ExecContext(ctx,
				`INSERT INTO agent_events(run_id, seq, event_type, payload, created_at) VALUES(?,?,'run.finished',?,?)`,
				runID, seq+2, string(finPayload), ts); err != nil {
				return err
			}
		}
		changed = true
		return nil
	})
	return err == nil && changed
}

func nullStringPtr(s string) any {
	if s == "" {
		return nil
	}
	return s
}
