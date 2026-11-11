package crawl

import (
	"context"
	"crypto/x509"
	"database/sql"
	"errors"
	"net"
	"sort"
	"sync"
	"time"

	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/steamhttp"
	"holdexar/server-go/internal/store"
)

type proxyRunTrace struct {
	mu     sync.Mutex
	exits  map[string]proxypool.ExitStatsRow
	nodes  map[string]string
	errors map[string]int
}

func (t *proxyRunTrace) noteRequest(record steamhttp.RequestRecord) {
	t.mu.Lock()
	defer t.mu.Unlock()
	row := t.exits[record.ExitKey]
	row.ExitIP = record.ExitKey
	row.Endpoint = "store_browse"
	row.Requests++
	row.DurationMS += record.DurationMS
	switch record.Outcome {
	case steamhttp.OutcomeOK:
		row.Success++
	case steamhttp.OutcomeRateLimited:
		row.E429++
	case steamhttp.OutcomeE4xx:
		row.E4XX++
	case steamhttp.OutcomeE5xx:
		row.E5XX++
	case steamhttp.OutcomeTimeout:
		row.Timeout++
	case steamhttp.OutcomeConnectErr:
		row.ConnectError++
	default:
		row.Other++
	}
	t.exits[record.ExitKey] = row
}

func (t *proxyRunTrace) noteError(err error) {
	kind := proxyErrorKind(err)
	t.mu.Lock()
	t.errors[kind]++
	t.mu.Unlock()
}

func proxyErrorKind(err error) string {
	var rate *steamhttp.RateLimitError
	var status *steamhttp.StatusError
	var dns *net.DNSError
	var op *net.OpError
	var netErr net.Error
	var certificate x509.UnknownAuthorityError
	var hostname x509.HostnameError
	var invalidCert x509.CertificateInvalidError
	switch {
	case errors.As(err, &rate):
		return "rate_limit"
	case errors.As(err, &netErr) && netErr.Timeout():
		return "timeout"
	case errors.As(err, &certificate), errors.As(err, &hostname), errors.As(err, &invalidCert):
		return "tls"
	case errors.As(err, &dns):
		return "dns"
	case errors.As(err, &status) && status.Status >= 500:
		return "http_5xx"
	case status != nil && status.Status >= 400:
		return "http_4xx"
	case errors.As(err, &op) && op.Op == "proxyconnect":
		return "proxy_error"
	case op != nil && op.Op == "dial":
		return "connect"
	default:
		return "other"
	}
}

func (r *Runtime) startProxyRun(ctx context.Context, jobID int64, transport crawlTransport, regions []Region, started time.Time) int64 {
	var id int64
	stamp := timestamp(started.Local())
	err := r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, err := conn.ExecContext(ctx, `INSERT INTO proxy_job_runs
			(status,kind,crawl_job_id,started_at,regions_json,workers,proxy_url,created_at)
			VALUES('running','crawl',?,?,?,?,?,?)`, jobID, stamp, jsonString(regionCodes(regions)), transport.workers(), nullable(transport.proxyURL), stamp)
		if err != nil {
			return err
		}
		id, err = result.LastInsertId()
		return err
	})
	if err != nil {
		r.opts.Log("价格任务 %d 的代理作业记录未能创建：%v", jobID, err)
		return 0
	}
	return id
}

func (r *Runtime) finishProxyRun(ctx context.Context, id int64, trace *proxyRunTrace, started time.Time, interrupted bool, stats map[string]any, jobErr error) {
	if id == 0 {
		return
	}
	planned, successes, failures := stats["total"].(int), stats["success"].(int), stats["failed"].(int)
	status := "success"
	switch {
	case interrupted:
		status = "interrupted"
	case jobErr != nil || failures > 0 && successes == 0:
		status = "failed"
	case failures > 0:
		status = "partial"
	}
	if jobErr != nil && !interrupted && len(trace.errors) == 0 {
		trace.noteError(jobErr)
	}
	summary := map[string]any{"by_error": trace.errors}
	if interrupted {
		summary["interrupted"] = true
	}
	now := r.now().Local()
	err := r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE proxy_job_runs SET status=?,finished_at=?,duration_ms=?,
			task_count=?,success_count=?,error_count=?,error_summary=? WHERE id=? AND status='running'`,
			status, timestamp(now), max(0, now.Sub(started).Milliseconds()), planned, successes, failures, jsonString(summary), id)
		return err
	})
	if err != nil {
		r.opts.Log("代理作业 %d 的作业记录未能结算：%v", id, err)
	}
	rows := make([]proxypool.ExitStatsRow, 0, len(trace.exits))
	for _, row := range trace.exits {
		rows = append(rows, row)
	}
	sort.Slice(rows, func(i, j int) bool { return rows[i].ExitIP < rows[j].ExitIP })
	if _, err = proxypool.RecordRunExits(ctx, r.gate, id, rows, trace.nodes, &now); err != nil {
		r.opts.Log("代理作业 %d 的出口记录未能保存：%v", id, err)
	}
}

func (r *Runtime) recoverProxyRuns(ctx context.Context, cutoff string) {
	err := r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `UPDATE proxy_job_runs SET status='interrupted',finished_at=?,
			duration_ms=COALESCE(duration_ms,MAX(0,CAST(ROUND((julianday(?)-julianday(started_at))*86400000) AS INTEGER))),
			error_summary=json_set(CASE WHEN json_valid(error_summary) THEN error_summary ELSE '{"by_error":{}}' END,'$.interrupted',json('true'))
			WHERE status='running' AND (started_at IS NULL OR started_at<?)`, cutoff, cutoff, cutoff)
		return err
	})
	if err != nil {
		r.opts.Log("上次运行的代理作业记录未能恢复：%v", err)
	}
}
