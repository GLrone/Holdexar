package proxypool

import (
	"database/sql"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"strconv"
	"time"
)

const runPayloadSQL = `json_object(
	'id',id,'status',status,'kind',kind,'crawlJobId',crawl_job_id,
	'startedAt',replace(started_at,' ','T'),'finishedAt',replace(finished_at,' ','T'),'durationMs',duration_ms,
	'taskCount',task_count,'successCount',success_count,'errorCount',error_count,
	'node',selected_node,'nodeExitIp',node_exit_ip,'poolNodeCount',pool_node_count,
	'poolExitIpCount',pool_exit_ip_count,'poolSha256',pool_sha256,'proxyUrl',proxy_url,
	'regions',json(COALESCE(NULLIF(regions_json,'null'),'[]')),'workers',workers,
	'subscriptionIds',json(COALESCE(NULLIF(subscription_ids_json,'null'),'[]')),
	'snapshotIds',json(COALESCE(NULLIF(snapshot_ids_json,'null'),'[]')),
	'byError',json_extract(error_summary,'$.by_error'),
	'errorSummaryTruncated',json(CASE WHEN json_extract(error_summary,'$.truncated') THEN 'true' ELSE 'false' END),
	'interrupted',json(CASE WHEN json_extract(error_summary,'$.interrupted') THEN 'true' ELSE 'false' END))`

const runExitsSQL = `SELECT json_group_array(json_object(
	'exitIp',exit_ip,'endpoint',endpoint,'node',node,'requests',requests,'success',success,
	'e429',e429,'e4xx',e4xx,'e5xx',e5xx,'timeout',timeout,'connectError',connect_error,
	'other',other,'durationMs',duration_ms))
	FROM (SELECT * FROM proxy_run_exits WHERE run_id=? ORDER BY exit_ip,endpoint)`

func RegisterRuns(mux *http.ServeMux, db *sql.DB) {
	mux.HandleFunc("GET /api/v1/proxypool/job-runs", func(w http.ResponseWriter, r *http.Request) {
		limit, okLimit := runPageValue(r, "limit", 20, 1, 100)
		offset, okOffset := runPageValue(r, "offset", 0, 0, math.MaxInt64)
		if !okLimit || !okOffset {
			runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": "分页参数不正确"})
			return
		}
		now := time.Now()
		day := now.Format(time.DateOnly)
		next := now.AddDate(0, 0, 1).Format(time.DateOnly)
		const statement = `WITH today AS (
			SELECT * FROM proxy_job_runs WHERE started_at>=? AND started_at<?
		), page AS (
			SELECT ` + runPayloadSQL + ` AS payload FROM proxy_job_runs ORDER BY id DESC LIMIT ? OFFSET ?
		)
		SELECT json_object(
			'summary', json((SELECT json_object(
				'day',?,'runs',COUNT(*),'success',COUNT(*) FILTER(WHERE status='success'),
				'partial',COUNT(*) FILTER(WHERE status='partial'),'failed',COUNT(*) FILTER(WHERE status='failed'),
				'interrupted',COUNT(*) FILTER(WHERE status='interrupted'),'running',COUNT(*) FILTER(WHERE status='running'),
				'avgDurationMs',CAST(round(AVG(duration_ms)) AS INTEGER),
				'poolExitIpCount',(SELECT pool_exit_ip_count FROM today WHERE pool_exit_ip_count IS NOT NULL ORDER BY id DESC LIMIT 1))
			FROM today)),
			'total',(SELECT COUNT(*) FROM proxy_job_runs),
			'items',json((SELECT json_group_array(json(payload)) FROM page)))`
		var payload string
		err := db.QueryRowContext(r.Context(), statement, day, next, limit, offset, day).Scan(&payload)
		runPayload(w, payload, err)
	})
	mux.HandleFunc("GET /api/v1/proxypool/job-runs/{run_id}", func(w http.ResponseWriter, r *http.Request) {
		id, err := strconv.ParseInt(r.PathValue("run_id"), 10, 64)
		if err != nil {
			runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": "作业编号不正确"})
			return
		}
		var payload string
		err = db.QueryRowContext(r.Context(), `SELECT json_set(`+runPayloadSQL+`,'$.exits',json((`+runExitsSQL+`)))
			FROM proxy_job_runs WHERE id=?`, id, id).Scan(&payload)
		if errors.Is(err, sql.ErrNoRows) {
			runResponse(w, http.StatusNotFound, map[string]any{"detail": "作业记录不存在"})
			return
		}
		runPayload(w, payload, err)
	})
	mux.HandleFunc("GET /api/v1/proxypool/health-runs", func(w http.ResponseWriter, r *http.Request) {
		limit, ok := runPageValue(r, "limit", 20, 1, 50)
		if !ok {
			runResponse(w, http.StatusUnprocessableEntity, map[string]any{"detail": "分页参数不正确"})
			return
		}
		var payload string
		err := db.QueryRowContext(r.Context(), `SELECT json_object('runs',json_group_array(json_object(
			'id',id,'channel',channel,'subscriptionId',subscription_id,'startedAt',replace(started_at,' ','T'),
			'finishedAt',replace(finished_at,' ','T'),'durationMs',duration_ms,
			'total',total,'steamOk',steam_ok,'ipKnown',ip_known,'failed',failed,'endReason',end_reason)))
			FROM (SELECT * FROM health_runs ORDER BY started_at DESC,id DESC LIMIT ?)`, limit).Scan(&payload)
		runPayload(w, payload, err)
	})
}

func runPageValue(r *http.Request, key string, defaultValue, minimum, maximum int64) (int64, bool) {
	raw := r.URL.Query().Get(key)
	if raw == "" {
		return defaultValue, true
	}
	n, err := strconv.ParseInt(raw, 10, 64)
	return n, err == nil && n >= minimum && n <= maximum
}

func runPayload(w http.ResponseWriter, payload string, err error) {
	if err != nil {
		runResponse(w, http.StatusInternalServerError, map[string]any{"detail": "暂时无法读取代理运行记录"})
		return
	}
	runResponse(w, http.StatusOK, json.RawMessage(payload))
}

func runResponse(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
