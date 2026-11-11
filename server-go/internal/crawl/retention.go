package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"slices"

	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/storebrowse"
)

const unfinishedJobUnitsSQL = `CASE WHEN json_valid(crawl_jobs.stats_json) THEN
	CASE WHEN crawl_jobs.status IN ('failed','interrupted') THEN
		COALESCE(json_array_length(crawl_jobs.stats_json,'$.targets.appids'),0) *
		COALESCE(json_array_length(crawl_jobs.stats_json,'$.targets.regions'),0) +
		COALESCE(json_array_length(crawl_jobs.stats_json,'$.targets.tasks'),0)
	ELSE 0 END ELSE 0 END > 0
	AND NOT EXISTS (SELECT 1 FROM price_cycles c WHERE c.id=crawl_jobs.cycle_id AND c.status='cancelled')`

const compactSettledJobsSQL = jobTargetResultsSQL + `,
	changed AS MATERIALIZED (SELECT DISTINCT job_id FROM unit_results WHERE settled=1),
	remaining AS MATERIALIZED (
		SELECT DISTINCT job_id,appid,region FROM unit_results WHERE settled=0 AND job_id IN (SELECT job_id FROM changed)
	), buckets AS (
		SELECT job_id,region,appid,(ROW_NUMBER() OVER (PARTITION BY job_id,region ORDER BY appid)-1)/?2 bucket
		FROM remaining
	), groups AS (
		SELECT job_id,region,bucket,json_group_array(appid ORDER BY appid) appids FROM buckets GROUP BY job_id,region,bucket
	), targets AS (
		SELECT job_id,CASE WHEN COUNT(*)=COUNT(DISTINCT appid)*COUNT(DISTINCT region) THEN
			json_object('appids',json_group_array(DISTINCT appid ORDER BY appid),'regions',json_group_array(DISTINCT region ORDER BY region))
		ELSE (SELECT json_object('tasks',json_group_array(json_object('region',region,'appids',json(appids)) ORDER BY region,bucket))
			FROM groups WHERE groups.job_id=remaining.job_id) END value
		FROM remaining GROUP BY job_id
	)
	UPDATE crawl_jobs SET stats_json=CASE WHEN targets.value IS NULL THEN json_remove(stats_json,'$.targets')
		ELSE json_set(stats_json,'$.targets',json(targets.value)) END
	FROM changed LEFT JOIN targets ON targets.job_id=changed.job_id WHERE crawl_jobs.id=changed.job_id`

func (r *Runtime) PruneJobs(ctx context.Context) (int64, bool, error) {
	return r.pruneJobs(ctx, 5000, 20)
}

func (r *Runtime) pruneJobs(ctx context.Context, chunk, maxChunks int) (int64, bool, error) {
	// Record settled targets before deleting the old runs that proved a target could be skipped.
	changed, err := r.planner.planIDs(ctx, jobTargetResultsSQL+` SELECT DISTINCT job_id FROM unit_results WHERE settled=1`, nil)
	if err != nil {
		return 0, false, err
	}
	for ids := range slices.Chunk(changed, 50) {
		encoded, err := json.Marshal(ids)
		if err != nil {
			return 0, false, err
		}
		err = r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			_, err := conn.ExecContext(ctx, compactSettledJobsSQL, string(encoded), storebrowse.DefaultBatchSize)
			return err
		})
		if err != nil {
			return 0, false, err
		}
	}
	cutoff := timestamp(r.now().AddDate(0, 0, -90))
	return r.gate.PruneBatches(ctx, `DELETE FROM crawl_jobs WHERE id IN (
		SELECT id FROM crawl_jobs WHERE finished_at<? AND status NOT IN ('pending','running')
		AND NOT EXISTS (SELECT 1 FROM price_events e WHERE e.cycle_id=-crawl_jobs.id)
		AND NOT EXISTS (SELECT 1 FROM notification_candidates n WHERE n.cycle_id=-crawl_jobs.id)
		AND NOT (`+unfinishedJobUnitsSQL+`) ORDER BY id LIMIT ?)`, chunk, maxChunks, cutoff)
}

const PriceRetentionJob = "price_cycle_retention"

type PriceRetentionResult struct {
	Events    int64 `json:"events_deleted"`
	Cycles    int64 `json:"rows_deleted"`
	Truncated bool  `json:"truncated"`
}

const protectedNotificationScanSQL = `(CASE WHEN json_valid(c.notification_scan_json) THEN
	COALESCE(json_extract(c.notification_scan_json,'$.state'),'') IN ('pending','scanning')
	ELSE c.notification_scan_json IS NOT NULL END)`

func (r *Runtime) PrunePriceHistory(ctx context.Context) (PriceRetentionResult, error) {
	return r.prunePriceHistory(ctx, 50, 20)
}

func (r *Runtime) prunePriceHistory(ctx context.Context, chunk, maxChunks int) (PriceRetentionResult, error) {
	cutoff := timestamp(r.now().AddDate(0, 0, -90))
	events, capped, err := r.gate.PruneBatches(ctx, `DELETE FROM price_events WHERE id IN (
		SELECT id FROM price_events WHERE occurred_at<?
		AND NOT EXISTS (SELECT 1 FROM notification_candidates n WHERE n.source_kind='price_event' AND n.event_id=price_events.id)
		AND NOT EXISTS (SELECT 1 FROM price_cycles c WHERE c.id=price_events.cycle_id AND `+protectedNotificationScanSQL+`)
		ORDER BY id LIMIT ?)`, chunk, maxChunks, cutoff)
	result := PriceRetentionResult{Events: events, Truncated: capped}
	if err != nil {
		return result, err
	}
	for range maxChunks {
		var removed int64
		err := r.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
			history, err := readCycleHistory(ctx, conn, `SELECT c.id,c.specs_json,COALESCE((c.started_at<?
				AND c.status IN('completed','partial','failed','cancelled')
				AND NOT EXISTS (SELECT 1 FROM price_events e WHERE e.cycle_id=c.id)
				AND NOT EXISTS (SELECT 1 FROM notification_candidates n WHERE n.cycle_id=c.id)
				AND NOT EXISTS (SELECT 1 FROM crawl_jobs j WHERE j.cycle_id=c.id)
				AND NOT `+protectedNotificationScanSQL+`),0) FROM price_cycles c ORDER BY c.id`, cutoff)
			if err != nil {
				return err
			}
			ids := prunableCycles(history, chunk)
			if len(ids) == 0 {
				return nil
			}
			raw, err := json.Marshal(ids)
			if err != nil {
				return err
			}
			deleted, err := conn.ExecContext(ctx, `DELETE FROM price_cycles WHERE id IN (SELECT value FROM json_each(?))`, string(raw))
			if err != nil {
				return err
			}
			removed, err = deleted.RowsAffected()
			return err
		})
		if err != nil {
			return result, err
		}
		result.Cycles += removed
		if removed < int64(chunk) {
			return result, nil
		}
	}
	result.Truncated = true
	return result, nil
}

func prunableCycles(history []cycleHistoryRow, limit int) []int64 {
	stages := resolveCycleStages(history)
	byID := make(map[int64][]Spec, len(history))
	protected := map[int64]bool{}
	var queue []int64
	keep := func(id int64) {
		if id != 0 && !protected[id] {
			protected[id] = true
			queue = append(queue, id)
		}
	}
	for _, cycle := range history {
		byID[cycle.id] = cycle.specs
		if !cycle.removable && len(cycle.specs) > 0 {
			keep(cycle.id)
		}
	}
	for _, id := range stages.pending {
		keep(id)
	}
	// A retained stage must keep its later completion evidence, even when the cycle survives for another reason.
	for len(queue) > 0 {
		id := queue[0]
		queue = queue[1:]
		for _, spec := range byID[id] {
			key := planSpecKey(spec)
			keep(stages.latest[key])
			keep(stages.finished[key])
		}
	}
	ids := make([]int64, 0, limit)
	for _, cycle := range history {
		if cycle.removable && !protected[cycle.id] {
			ids = append(ids, cycle.id)
			if len(ids) == limit {
				break
			}
		}
	}
	return ids
}
