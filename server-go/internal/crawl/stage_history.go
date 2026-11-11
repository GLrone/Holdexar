package crawl

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"slices"
)

type cycleHistoryRow struct {
	id        int64
	specs     []Spec
	removable bool
}

func readCycleHistory(ctx context.Context, q interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}, query string, args ...any) ([]cycleHistoryRow, error) {
	rows, err := q.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var history []cycleHistoryRow
	for rows.Next() {
		var cycle cycleHistoryRow
		var raw sql.NullString
		if err := rows.Scan(&cycle.id, &raw, &cycle.removable); err != nil {
			return nil, err
		}
		if raw.Valid {
			if err := json.Unmarshal([]byte(raw.String), &cycle.specs); err != nil {
				return nil, fmt.Errorf("读取价格周期 %d 的阶段记录：%w", cycle.id, err)
			}
		}
		history = append(history, cycle)
	}
	return history, rows.Err()
}

type cycleStages struct {
	order    []string
	pending  map[string]int64
	latest   map[string]int64
	finished map[string]int64
}

func resolveCycleStages(history []cycleHistoryRow) cycleStages {
	stages := cycleStages{pending: map[string]int64{}, latest: map[string]int64{}, finished: map[string]int64{}}
	for _, cycle := range history {
		for _, spec := range cycle.specs {
			state, _ := spec.Execution["state"].(string)
			if state == "" {
				continue
			}
			key := planSpecKey(spec)
			stages.latest[key] = cycle.id
			switch state {
			case "pending", "deferred", "recovering":
				if _, exists := stages.pending[key]; !exists {
					stages.order = append(stages.order, key)
					stages.pending[key] = cycle.id
				}
			case "done", "empty", "cancelled":
				stages.finished[key] = cycle.id
				if index := slices.Index(stages.order, key); index >= 0 {
					stages.order = slices.Delete(stages.order, index, index+1)
					delete(stages.pending, key)
				}
			}
		}
	}
	return stages
}
