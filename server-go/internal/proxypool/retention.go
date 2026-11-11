package proxypool

import (
	"context"
	"database/sql"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

const RetentionJob = "proxypool_retention"

var snapshotHash = regexp.MustCompile(`^[a-f0-9]{64}$`)

func PruneTelemetry(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string, now time.Time) (map[string]any, error) {
	return pruneTelemetry(ctx, db, gate, dataDir, now, 5000, 20)
}

func pruneTelemetry(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string, now time.Time, chunk, maxChunks int) (map[string]any, error) {
	result := map[string]any{"snapshots": 0, "truncated": false}
	tables := []struct {
		key, table, condition string
		days                  int
	}{
		{"job_runs", "proxy_job_runs", "started_at < ? AND id NOT IN (SELECT id FROM proxy_job_runs ORDER BY id DESC LIMIT 200)", 90},
		{"run_exits", "proxy_run_exits", "created_at < ?", 90},
		{"health_runs", "health_runs", "started_at < ?", 90},
		{"health_observations", "health_observations", "observed_at < ?", 30},
		{"proxy_events", "proxy_events", "ts < ?", 90},
	}
	for _, table := range tables {
		query := `DELETE FROM ` + table.table + ` WHERE id IN (SELECT id FROM ` + table.table + ` WHERE ` + table.condition + ` ORDER BY id LIMIT ?)`
		cutoff := pyTimeText(now.AddDate(0, 0, -table.days))
		total := 0
		for range maxChunks {
			var count int64
			err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
				deleted, err := conn.ExecContext(ctx, query, cutoff, chunk)
				if err != nil {
					return err
				}
				count, err = deleted.RowsAffected()
				return err
			})
			if err != nil {
				result[table.key] = total
				return result, err
			}
			total += int(count)
			if count < int64(chunk) {
				break
			}
			if total == chunk*maxChunks {
				result["truncated"] = true
			}
		}
		result[table.key] = total
	}
	cutoff := pyTimeText(now.AddDate(0, 0, -180))
	for n := range maxChunks {
		removed, err := deleteSnapshotRows(ctx, gate, cutoff, chunk)
		if err != nil {
			return result, err
		}
		result["snapshots"] = result["snapshots"].(int) + len(removed)
		if n+1 == maxChunks && len(removed) == chunk {
			result["truncated"] = true
		}
		filesCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 5*time.Second)
		err = reclaimSnapshotFiles(filesCtx, db, gate, dataDir, removed)
		cancel()
		if err != nil {
			return result, err
		}
		if len(removed) < chunk {
			break
		}
	}
	return result, nil
}

type retainedSnapshotFile struct {
	subID     int64
	sha, path string
}

func deleteSnapshotRows(ctx context.Context, gate *store.WriteGate, cutoff string, chunk int) ([]retainedSnapshotFile, error) {
	var removed []retainedSnapshotFile
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		rows, err := conn.QueryContext(ctx, `DELETE FROM subscription_snapshots WHERE id IN (
			SELECT s.id FROM subscription_snapshots s WHERE s.fetched_at < ?
			AND s.id NOT IN (SELECT id FROM subscription_snapshots WHERE subscription_id=s.subscription_id
			AND status='OK' ORDER BY id DESC LIMIT 3)
			ORDER BY s.id LIMIT ?) RETURNING subscription_id,sha256,COALESCE(raw_path,'')`, cutoff, chunk)
		if err != nil {
			return err
		}
		defer rows.Close()
		for rows.Next() {
			var row retainedSnapshotFile
			if err := rows.Scan(&row.subID, &row.sha, &row.path); err != nil {
				return err
			}
			removed = append(removed, row)
		}
		return rows.Err()
	})
	return removed, err
}

func snapshotPathKey(path string) string {
	if path == "" {
		return ""
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return ""
	}
	if runtime.GOOS == "windows" {
		return strings.ToLower(abs)
	}
	return abs
}

func reclaimSnapshotFiles(ctx context.Context, db *sql.DB, gate *store.WriteGate, dataDir string, removed []retainedSnapshotFile) error {
	type identity struct {
		subID int64
		sha   string
	}
	candidates := make(map[identity]string)
	for _, row := range removed {
		if row.subID <= 0 || !snapshotHash.MatchString(row.sha) {
			continue
		}
		rel := filepath.Join(snapshotDir(""), strconv.FormatInt(row.subID, 10), row.sha+".bin")
		if actual := snapshotPathKey(row.path); actual != "" && actual == snapshotPathKey(filepath.Join(dataDir, rel)) {
			candidates[identity{row.subID, row.sha}] = rel
		}
	}
	if len(candidates) == 0 {
		return nil
	}
	// Snapshot writers hold this same lease from file creation through row commit.
	lease, err := gate.Acquire(ctx, store.PriorityBackground)
	if err != nil {
		return err
	}
	defer lease.Done()
	paths := make(map[string]identity, len(candidates))
	for key, rel := range candidates {
		paths[snapshotPathKey(filepath.Join(dataDir, rel))] = key
	}
	rows, err := db.QueryContext(ctx, `SELECT subscription_id,sha256,COALESCE(raw_path,'') FROM subscription_snapshots`)
	if err != nil {
		return err
	}
	for rows.Next() {
		var row retainedSnapshotFile
		if err := rows.Scan(&row.subID, &row.sha, &row.path); err != nil {
			rows.Close()
			return err
		}
		delete(candidates, identity{row.subID, row.sha})
		if key, found := paths[snapshotPathKey(row.path)]; found {
			delete(candidates, key)
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil || len(candidates) == 0 {
		return err
	}
	root, err := os.OpenRoot(dataDir)
	if err != nil {
		return err
	}
	defer root.Close()
	var failures error
	for _, rel := range candidates {
		if err := ctx.Err(); err != nil {
			return errors.Join(failures, err)
		}
		if err := removeSnapshotFile(root, rel); err != nil && !errors.Is(err, os.ErrNotExist) {
			failures = errors.Join(failures, err)
		}
	}
	return failures
}

func removeSnapshotFile(root *os.Root, rel string) error {
	for _, dir := range []string{filepath.Dir(snapshotDir("")), snapshotDir(""), filepath.Dir(rel)} {
		info, err := root.Lstat(dir)
		if err != nil || !info.IsDir() {
			return err
		}
	}
	info, err := root.Lstat(rel)
	if err != nil || !info.Mode().IsRegular() {
		return err
	}
	return root.Remove(rel)
}
