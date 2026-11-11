package miniseed

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const Job = "mini_seed"
const Filename = "holdexar_mini_seed.db"
const markerKey = "miniseed.imported_version"
const chunk = 500

type Result struct {
	Version string `json:"version,omitempty"`
	AsOf    string `json:"asOf,omitempty"`
	Skipped string `json:"skipped,omitempty"`
	Games   int    `json:"games"`
	Curated int64  `json:"curated"`
	Bundles int64  `json:"bundles"`
}

func Find(installRoot string) (string, error) {
	exe, err := os.Executable()
	if err != nil {
		return "", err
	}
	for _, dir := range []string{filepath.Join(filepath.Dir(exe), "seed"), filepath.Join(installRoot, "assets", "seed")} {
		path := filepath.Join(dir, Filename)
		info, err := os.Stat(path)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		if err != nil {
			return "", err
		}
		if info.Mode().IsRegular() {
			return path, nil
		}
	}
	return "", nil
}

type rowQuery interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func imported(ctx context.Context, q rowQuery, version string) (bool, error) {
	var raw string
	err := q.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, markerKey).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return false, nil
	}
	var saved string
	return err == nil && json.Unmarshal([]byte(raw), &saved) == nil && saved == version, err
}

func Import(ctx context.Context, db *sql.DB, gate *store.WriteGate, path, dataDir string) (Result, error) {
	if path == "" {
		return Result{Skipped: "未随包提供基础资料"}, nil
	}
	source, err := store.OpenReadOnly(path)
	if err != nil {
		return Result{}, err
	}
	defer source.Close()
	tx, err := source.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return Result{}, err
	}
	defer tx.Rollback()
	meta, err := readMeta(ctx, tx)
	if err != nil {
		return Result{}, err
	}
	version := meta["exported_at_utc"]
	stamp, err := parseStamp(version)
	if err != nil {
		return Result{}, err
	}
	result := Result{Version: version, AsOf: meta["source_data_as_of"]}
	already, err := imported(ctx, db, version)
	if err != nil || already {
		if already {
			result.Skipped = "基础资料已是当前版本"
		}
		return result, err
	}
	var input [3][]json.RawMessage
	for i, statement := range merges {
		input[i], err = readRows(ctx, tx, statement.read)
		if err != nil {
			return Result{}, err
		}
	}
	if err := tx.Commit(); err != nil {
		return Result{}, err
	}
	overrides, order := derived.LoadSeriesOverrides(dataDir)
	err = gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		already, err := imported(ctx, conn, version)
		if err != nil || already {
			if already {
				result.Skipped = "基础资料已是当前版本"
			}
			return err
		}
		var ids []int64
		for rows := range slices.Chunk(input[0], chunk) {
			raw, err := json.Marshal(rows)
			if err != nil {
				return err
			}
			inserted, err := insertCatalog(ctx, conn, string(raw), stamp)
			if err != nil {
				return err
			}
			ids = append(ids, inserted...)
		}
		if len(ids) > 0 {
			if _, err := derived.UpdatePriceDerived(ctx, conn, ids, derived.Options{Now: time.Now(), History: true,
				Overrides: overrides, OverrideOrder: order, CatalogBefore: map[int64]derived.CatalogRow{}}); err != nil {
				return err
			}
			if err := derived.UpdateMemberBundles(ctx, conn, ids); err != nil {
				return err
			}
		}
		result.Games = len(ids)
		result.Curated, err = applyBatches(ctx, conn, merges[1].write, input[1])
		if err != nil {
			return err
		}
		result.Bundles, err = applyBatches(ctx, conn, merges[2].write, input[2])
		if err != nil {
			return err
		}
		return settings.WriteValue(ctx, conn, markerKey, version)
	})
	if err != nil {
		return Result{}, err
	}
	return result, nil
}

func parseStamp(value string) (string, error) {
	for _, layout := range []string{time.RFC3339Nano, "2006-01-02T15:04:05.999999999", "2006-01-02 15:04:05.999999999"} {
		if stamp, err := time.Parse(layout, value); err == nil {
			return stamp.UTC().Format("2006-01-02 15:04:05.000000"), nil
		}
	}
	return "", errors.New("基础资料版本时间无效")
}

func readMeta(ctx context.Context, tx *sql.Tx) (map[string]string, error) {
	rows, err := tx.QueryContext(ctx, `SELECT type||':'||name FROM sqlite_schema WHERE type IN('table','view','trigger') ORDER BY name`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var tables []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		tables = append(tables, name)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if !slices.Equal(tables, []string{"table:bundle_index", "table:catalog", "table:curated", "table:seed_meta"}) {
		return nil, errors.New("基础资料包含未知结构")
	}
	var check string
	if err := tx.QueryRowContext(ctx, `PRAGMA quick_check`).Scan(&check); err != nil || check != "ok" {
		return nil, errors.Join(errors.New("基础资料完整性检查未通过"), err)
	}
	rows, err = tx.QueryContext(ctx, `SELECT key,value FROM seed_meta`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	meta := map[string]string{}
	for rows.Next() {
		var key, value string
		if err := rows.Scan(&key, &value); err != nil {
			return nil, err
		}
		if _, exists := meta[key]; exists {
			return nil, fmt.Errorf("基础资料重复声明 %s", key)
		}
		meta[key] = value
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	if meta["schema_version"] != "1" || meta["price_history_included"] != "false" || meta["current_prices_included"] != "false" {
		return nil, errors.New("基础资料版本未知或声明包含价格")
	}
	return meta, nil
}
