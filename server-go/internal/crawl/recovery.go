package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
)

type recoveryCycle struct {
	id                int64
	kind              string
	started, finished sql.NullString
	specs             []map[string]any
}
type recoveryStage struct {
	cycle           *recoveryCycle
	spec, execution map[string]any
	jobID           int64
}
type recoveryJob struct {
	status  string
	targets bool
}
type recoveryInsert struct {
	stage                *recoveryStage
	kind, regions, stats string
}

func (r *Runtime) recoverPendingStages(ctx context.Context, conn *sql.Conn) error {
	rows, err := conn.QueryContext(ctx, `SELECT id,kind,CAST(started_at AS TEXT),CAST(finished_at AS TEXT),specs_json FROM price_cycles WHERE status IN('failed','partial') AND EXISTS(SELECT 1 FROM json_each(price_cycles.specs_json) s WHERE json_type(s.value,'$.resolved_appids')='array' AND COALESCE(json_extract(s.value,'$.execution.state'),'pending') IN('pending','deferred','recovering')) ORDER BY id`)
	if err != nil {
		return err
	}
	cycles := []*recoveryCycle{}
	for rows.Next() {
		c := &recoveryCycle{}
		var raw string
		if err = rows.Scan(&c.id, &c.kind, &c.started, &c.finished, &raw); err != nil {
			rows.Close()
			return err
		}
		if err = json.Unmarshal([]byte(raw), &c.specs); err != nil {
			rows.Close()
			return fmt.Errorf("cycle %d recovery specs: %w", c.id, err)
		}
		cycles = append(cycles, c)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	stages := []*recoveryStage{}
	existingIDs := []int64{}
	for _, c := range cycles {
		for _, spec := range c.specs {
			old, _ := spec["execution"].(map[string]any)
			state, exists := old["state"]
			if !exists {
				state = "pending"
			}
			if state != "pending" && state != "deferred" && state != "recovering" {
				continue
			}
			if _, exists = spec["resolved_appids"]; !exists {
				continue
			}
			execution := map[string]any{}
			for key, value := range old {
				execution[key] = value
			}
			stage := &recoveryStage{cycle: c, spec: spec, execution: execution, jobID: readInt(execution["jobId"])}
			stages = append(stages, stage)
			if stage.jobID != 0 {
				existingIDs = append(existingIDs, stage.jobID)
			}
		}
	}
	jobs := map[int64]recoveryJob{}
	if len(existingIDs) > 0 {
		encoded, _ := json.Marshal(existingIDs)
		rows, err = conn.QueryContext(ctx, `SELECT id,status,stats_json FROM crawl_jobs WHERE id IN(SELECT value FROM json_each(?))`, string(encoded))
		if err != nil {
			return err
		}
		for rows.Next() {
			var id int64
			var job recoveryJob
			var raw sql.NullString
			if err = rows.Scan(&id, &job.status, &raw); err != nil {
				rows.Close()
				return err
			}
			if raw.Valid {
				var stats map[string]any
				if err = json.Unmarshal([]byte(raw.String), &stats); err != nil {
					rows.Close()
					return fmt.Errorf("job %d recovery stats: %w", id, err)
				}
				targets, _ := stats["targets"].(map[string]any)
				job.targets = len(targets) > 0
			}
			jobs[id] = job
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
	}
	inserts := []recoveryInsert{}
	for _, stage := range stages {
		ids, _ := stage.spec["resolved_appids"].([]any)
		if stage.jobID != 0 || len(ids) == 0 {
			continue
		}
		c := stage.cycle
		if !c.started.Valid {
			return fmt.Errorf("cycle %d frozen recovery has no start time", c.id)
		}
		at, e := readTime(c.started.String)
		if e != nil {
			return e
		}
		regions := stage.spec["resolved_regions"]
		if regions == nil {
			regions = []any{}
		}
		kind := c.kind
		if value, ok := stage.spec["kind"]; ok {
			kind, _ = value.(string)
		}
		regionJSON, e := json.Marshal(regions)
		if e != nil {
			return e
		}
		statsJSON, e := json.Marshal(map[string]any{"targetStartedAt": readTimeISO(at), "targets": map[string]any{"appids": ids, "regions": regions}})
		if e != nil {
			return e
		}
		inserts = append(inserts, recoveryInsert{stage: stage, kind: kind, regions: string(regionJSON), stats: string(statsJSON)})
	}
	for offset := 0; offset < len(inserts); offset += 500 {
		batch := inserts[offset:min(offset+500, len(inserts))]
		values := make([]string, len(batch))
		args := []any{}
		waiting := map[string][]*recoveryStage{}
		for i, insert := range batch {
			c := insert.stage.cycle
			values[i] = "(?,'interrupted','app',?,?,?,?,?,?)"
			args = append(args, insert.kind, insert.regions, insert.stats, recoveryNullable(c.started), recoveryNullable(c.finished), "进程中断，未完成地区将自动补全", c.id)
			key := recoveryIdentity(c.id, insert.kind, insert.stats)
			waiting[key] = append(waiting[key], insert.stage)
		}
		rows, err = conn.QueryContext(ctx, `INSERT INTO crawl_jobs(kind,status,mode,regions_json,stats_json,started_at,finished_at,error,cycle_id) VALUES `+strings.Join(values, ",")+` RETURNING id,cycle_id,kind,stats_json`, args...)
		if err != nil {
			return err
		}
		for rows.Next() {
			var id, cid int64
			var kind, stats string
			if err = rows.Scan(&id, &cid, &kind, &stats); err != nil {
				rows.Close()
				return err
			}
			key := recoveryIdentity(cid, kind, stats)
			stages := waiting[key]
			if len(stages) == 0 {
				rows.Close()
				return fmt.Errorf("recovered job %d has no frozen stage", id)
			}
			stage := stages[0]
			waiting[key] = stages[1:]
			stage.jobID = id
			jobs[id] = recoveryJob{status: "interrupted", targets: true}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
	}
	changed := map[int64]bool{}
	for _, stage := range stages {
		execution := stage.execution
		job, exists := jobs[stage.jobID]
		if exists {
			execution["jobId"] = stage.jobID
		}
		state := "done"
		if exists && job.status == "stopped" {
			state = "cancelled"
		} else if exists && job.status != "done" && job.targets {
			state = "recovering"
		}
		execution["state"] = state
		if jsonString(execution) != jsonString(stage.spec["execution"]) {
			stage.spec["execution"] = execution
			changed[stage.cycle.id] = true
		}
	}
	updates := []map[string]any{}
	for _, cycle := range cycles {
		if changed[cycle.id] {
			updates = append(updates, map[string]any{"id": cycle.id, "specs": cycle.specs})
		}
	}
	if len(updates) == 0 {
		return nil
	}
	encoded, err := json.Marshal(updates)
	if err != nil {
		return err
	}
	_, err = conn.ExecContext(ctx, `WITH changes AS(SELECT value->>'id' id,value->>'specs' specs FROM json_each(?)) UPDATE price_cycles SET specs_json=(SELECT specs FROM changes WHERE changes.id=price_cycles.id) WHERE id IN(SELECT id FROM changes)`, string(encoded))
	return err
}

func recoveryIdentity(cycleID int64, kind, stats string) string {
	return fmt.Sprintf("%d\x00%s\x00%s", cycleID, kind, stats)
}
func recoveryNullable(value sql.NullString) any {
	if value.Valid {
		return value.String
	}
	return nil
}
