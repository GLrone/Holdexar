package systemx

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"mime"
	"net/http"
	"os"
	"strings"
	"time"

	"holdexar/server-go/internal/backup"
	"holdexar/server-go/internal/dataexport"
	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/occupancy"
	"holdexar/server-go/internal/secretbox"
	"holdexar/server-go/internal/store"
)

// RestartExitCode 恢复换库后的重启请求退出码：窗口宿主据此重启服务并凭
// 新引导票据重载页面（跨进程契约，窗口宿主与开发监视同值）。
const RestartExitCode = 75

// RegisterData 数据维护与用户数据面：原位恢复、空间回收、加密导出、
// 旧爬虫库导入、密钥保护。onRequestRestart 在响应送达后触发服务重启。
func RegisterData(mux *http.ServeMux, db *sql.DB, gate *store.WriteGate, dataDir string,
	backups *backup.Service, onRequestRestart func()) {
	mux.HandleFunc("POST /api/v1/system/backup/{name}/restore", func(w http.ResponseWriter, r *http.Request) {
		result, err := backups.Restore(r.Context(), r.PathValue("name"))
		writeData(w, result, err)
		if err == nil && result.Restarting || errors.Is(err, backup.ErrRestartPending) {
			// 响应先落定，再交棒重启（窗口宿主/开发监视重启后端并重载页面）
			go func() {
				time.Sleep(300 * time.Millisecond)
				onRequestRestart()
			}()
		}
	})
	mux.HandleFunc("POST /api/v1/system/compact", func(w http.ResponseWriter, r *http.Request) {
		result, err := backups.Compact(r.Context())
		writeData(w, result, busyAsUnavailable(err))
	})
	mux.HandleFunc("POST /api/v1/export", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Password string `json:"password"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&req); err != nil {
			writeData(w, nil, fmt.Errorf("请求体无法读取"))
			return
		}
		result, err := dataexport.Create(r.Context(), db, dataDir, req.Password)
		writeData(w, result, classifyExport(err))
	})
	mux.HandleFunc("GET /api/v1/export/list", func(w http.ResponseWriter, r *http.Request) {
		writeData(w, map[string]any{"items": dataexport.List(dataDir)}, nil)
	})
	mux.HandleFunc("GET /api/v1/export/download/{name}", func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		path, err := dataexport.SafeExportPath(dataDir, name)
		if err != nil {
			writeData(w, nil, statusError{http.StatusBadRequest, err.Error()})
			return
		}
		file, err := os.Open(path)
		if err != nil {
			writeData(w, nil, statusError{http.StatusNotFound, "导出文件不存在"})
			return
		}
		defer file.Close()
		info, err := file.Stat()
		if err != nil || !info.Mode().IsRegular() {
			writeData(w, nil, statusError{http.StatusNotFound, "导出文件不存在"})
			return
		}
		w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": name}))
		w.Header().Set("Content-Type", "application/octet-stream")
		http.ServeContent(w, r, name, info.ModTime(), file)
	})
	mux.HandleFunc("DELETE /api/v1/export/{name}", func(w http.ResponseWriter, r *http.Request) {
		name := r.PathValue("name")
		path, err := dataexport.SafeExportPath(dataDir, name)
		if err != nil {
			writeData(w, nil, statusError{http.StatusBadRequest, err.Error()})
			return
		}
		if _, err := os.Stat(path); err != nil {
			writeData(w, nil, statusError{http.StatusNotFound, "导出文件不存在"})
			return
		}
		writeData(w, map[string]bool{"removed": os.Remove(path) == nil}, nil)
	})
	mux.HandleFunc("GET /api/v1/system/security", func(w http.ResponseWriter, r *http.Request) {
		writeData(w, secretbox.Status(dataDir), nil)
	})
	mux.HandleFunc("POST /api/v1/system/security/mode", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Mode              string  `json:"mode"`
			Passphrase        *string `json:"passphrase"`
			CurrentPassphrase *string `json:"current_passphrase"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&req); err != nil {
			writeData(w, nil, statusError{http.StatusBadRequest, "请求体无法读取"})
			return
		}
		result, err := secretbox.SwitchMode(r.Context(), gate, dataDir, req.Mode,
			deref(req.Passphrase), deref(req.CurrentPassphrase))
		if err != nil {
			writeData(w, nil, classifyKeyring(err))
			return
		}
		writeData(w, map[string]any{"ok": true, "counts": result.Counts, "security": secretbox.Status(dataDir)}, nil)
	})
	mux.HandleFunc("POST /api/v1/system/security/unlock", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Passphrase string `json:"passphrase"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&req); err != nil {
			writeData(w, nil, statusError{http.StatusBadRequest, "请求体无法读取"})
			return
		}
		ok, err := secretbox.Unlock(dataDir, req.Passphrase)
		if err != nil {
			writeData(w, nil, classifyKeyring(err))
			return
		}
		if !ok {
			writeData(w, nil, statusError{http.StatusBadRequest, "口令不正确"})
			return
		}
		writeData(w, map[string]any{"ok": true, "security": secretbox.Status(dataDir)}, nil)
	})
	mux.HandleFunc("POST /api/v1/system/security/lock", func(w http.ResponseWriter, r *http.Request) {
		secretbox.Lock()
		writeData(w, map[string]any{"ok": true, "security": secretbox.Status(dataDir)}, nil)
	})
	mux.HandleFunc("POST /api/v1/import-legacy", func(w http.ResponseWriter, r *http.Request) {
		var req struct {
			Path string `json:"path"`
		}
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 8192)).Decode(&req); err != nil {
			writeData(w, nil, statusError{http.StatusBadRequest, "请求体无法读取"})
			return
		}
		result, err := importLegacySnapshot(r.Context(), db, gate, req.Path)
		writeData(w, result, err)
	})
}

// importLegacySnapshot 旧爬虫库（steam_games / steam_prices）最新快照并入：
// current 表每 (appid, 区服) 取最新一条，history 落同一条，派生与人民币价同事务。
func importLegacySnapshot(ctx context.Context, db *sql.DB, gate *store.WriteGate, path string) (map[string]any, error) {
	info, err := os.Stat(path)
	if err != nil || info.IsDir() {
		return nil, statusError{http.StatusNotFound, fmt.Sprintf("文件不存在: %s", path)}
	}
	legacy, err := sql.Open("sqlite", "file:"+fmt.Sprintf("%s", toSlash(path))+"?mode=ro")
	if err != nil {
		return nil, err
	}
	defer legacy.Close()
	type priceRow struct {
		appid                      int64
		region, currency           string
		price, original            any
		discount                   any
		subID                      any
		gold                       bool
		versionSuffix, priceStatus string
	}
	var games []map[string]any
	var prices []priceRow
	total := int64(0)
	if err := func() error {
		rows, err := legacy.QueryContext(ctx, "SELECT appid, name FROM steam_games")
		if err != nil {
			return fmt.Errorf("旧库缺少 steam_games 表：%w", err)
		}
		defer rows.Close()
		for rows.Next() {
			var appid int64
			var name any
			if err := rows.Scan(&appid, &name); err != nil {
				return err
			}
			games = append(games, map[string]any{"appid": appid, "name": name})
		}
		return rows.Err()
	}(); err != nil {
		return nil, err
	}
	if err := func() error {
		if err := legacy.QueryRowContext(ctx, "SELECT COUNT(*) FROM steam_prices").Scan(&total); err != nil {
			return fmt.Errorf("旧库缺少 steam_prices 表：%w", err)
		}
		rows, err := legacy.QueryContext(ctx, `SELECT appid, region_code, currency, price, original_price,
			discount_percent, sub_id, is_gold, version_suffix, price_status FROM (
			SELECT *, ROW_NUMBER() OVER (PARTITION BY appid, upper(region_code)
			ORDER BY crawled_at DESC, rowid DESC) AS rn FROM steam_prices) WHERE rn = 1`)
		if err != nil {
			return fmt.Errorf("旧库 steam_prices 结构无法读取：%w", err)
		}
		defer rows.Close()
		for rows.Next() {
			var row priceRow
			if err := rows.Scan(&row.appid, &row.region, &row.currency, &row.price, &row.original,
				&row.discount, &row.subID, &row.gold, &row.versionSuffix, &row.priceStatus); err != nil {
				return err
			}
			row.region = strings.ToUpper(row.region)
			prices = append(prices, row)
		}
		return rows.Err()
	}(); err != nil {
		return nil, err
	}

	ids := map[int64]bool{}
	for _, game := range games {
		ids[game["appid"].(int64)] = true
	}
	for _, row := range prices {
		ids[row.appid] = true
	}
	idList := make([]int64, 0, len(ids))
	for id := range ids {
		idList = append(idList, id)
	}
	now := time.Now()
	err = gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		before, err := derived.CatalogInputs(ctx, conn, idList)
		if err != nil {
			return err
		}
		for _, game := range games {
			appid := game["appid"].(int64)
			name, _ := game["name"].(string)
			if name == "" {
				name = fmt.Sprintf("AppID_%d", appid)
			}
			// 默认值对齐原模型（default=False/0 的 NOT NULL 列）
			if _, err := conn.ExecContext(ctx, `INSERT INTO games(appid, name, family_sharing, trading_cards,
				is_epic, is_hb, hl_flag, pp_flag, diff_fen, is_adult, is_visual_novel,
				removed_strikes, positive_reviews, review_count, view_count)
				VALUES(?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
				ON CONFLICT(appid) DO UPDATE SET name=excluded.name`, appid, name); err != nil {
				return err
			}
		}
		for _, row := range prices {
			status := row.priceStatus
			if status == "" {
				status = "ok"
			}
			success := status == "ok" || status == "locked"
			answer := any(nil)
			if status == "locked" {
				answer = "locked"
			} else if status == "ok" {
				answer = "ok"
			}
			lastSuccess := any(nil)
			if success {
				lastSuccess = now
			}
			if _, err := conn.ExecContext(ctx, `INSERT INTO game_current_prices(appid, region_code, currency, price,
				original_price, discount_percent, sub_id, price_status, fail_count, attempt_outcome, steam_answer,
				last_success_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
				ON CONFLICT(appid, region_code) DO UPDATE SET currency=excluded.currency, price=excluded.price,
				original_price=excluded.original_price, discount_percent=excluded.discount_percent,
				sub_id=excluded.sub_id, price_status=excluded.price_status, attempt_outcome=excluded.attempt_outcome,
				steam_answer=excluded.steam_answer, last_success_at=excluded.last_success_at,
				updated_at=excluded.updated_at`,
				row.appid, row.region, row.currency, row.price, row.original, row.discount, row.subID,
				status, successStatus(success), answer, lastSuccess, now); err != nil {
				return err
			}
			if _, err := conn.ExecContext(ctx, `INSERT INTO game_price_history(appid, region_code, currency, price,
				original_price, discount_percent, sub_id, is_gold, is_bundle, price_status, version_suffix, snapshot_at)
				VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)`,
				row.appid, row.region, row.currency, row.price, row.original, row.discount, row.subID,
				row.gold, status, row.versionSuffix, now); err != nil {
				return err
			}
		}
		if len(prices) > 0 {
			encoded, err := json.Marshal(idList)
			if err != nil {
				return err
			}
			if _, err := conn.ExecContext(ctx, `UPDATE game_current_prices AS p SET
				cny_fen=price_cny_fen(p.price, r.rate_to_cny) FROM fx_rates r
				WHERE UPPER(p.currency)=r.currency_code AND p.price IS NOT NULL
				AND p.appid IN (SELECT value FROM json_each(?))`, string(encoded)); err != nil {
				return err
			}
			if _, err := conn.ExecContext(ctx, `UPDATE game_price_history AS p SET
				cny_fen=price_cny_fen(p.price, r.rate_to_cny) FROM fx_rates r
				WHERE UPPER(p.currency)=r.currency_code AND p.price IS NOT NULL
				AND p.appid IN (SELECT value FROM json_each(?)) AND p.snapshot_at=?`,
				string(encoded), now); err != nil {
				return err
			}
		}
		_, err = derived.UpdatePriceDerived(ctx, conn, idList, derived.Options{Now: now, CatalogBefore: before})
		return err
	})
	if err != nil {
		return nil, err
	}
	return map[string]any{
		"games": len(games), "priceRows": total, "currentUpserted": len(prices),
	}, nil
}

func successStatus(success bool) string {
	if success {
		return "success"
	}
	return "failed"
}

func toSlash(path string) string {
	out := make([]rune, 0, len(path))
	for _, r := range path {
		if r == '\\' {
			r = '/'
		}
		out = append(out, r)
	}
	return string(out)
}

func deref(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

type statusError struct {
	status int
	msg    string
}

func (e statusError) Error() string { return e.msg }

// busyAsUnavailable 写入竞争映射 503（语义对齐原实现「稍后重试」；
// 保留具体拒因便于诊断）。
func busyAsUnavailable(err error) error {
	if err == nil {
		return nil
	}
	var busy *occupancy.BusyError
	if errors.As(err, &busy) || errors.Is(err, store.ErrWritesPaused) {
		return statusError{http.StatusServiceUnavailable, err.Error()}
	}
	return err
}

// classifyExport 口令过短 400，凭据锁定 409，其余原样。
func classifyExport(err error) error {
	if err == nil {
		return nil
	}
	var boxErr *secretbox.SecretBoxError
	if errors.As(err, &boxErr) {
		return statusError{http.StatusConflict, err.Error()}
	}
	return err
}

// classifyKeyring 密钥托管失败 400。
func classifyKeyring(err error) error {
	if err == nil {
		return nil
	}
	var keyringErr *secretbox.KeyringError
	if errors.As(err, &keyringErr) {
		return statusError{http.StatusBadRequest, err.Error()}
	}
	return err
}

// writeData 统一 JSON 出口：状态码语义见各 classify。
func writeData(w http.ResponseWriter, value any, err error) {
	status := http.StatusOK
	if err != nil {
		status = http.StatusInternalServerError
		detail := err.Error()
		var se statusError
		switch {
		case errors.As(err, &se):
			status, detail = se.status, se.msg
		case errors.Is(err, os.ErrNotExist):
			status = http.StatusNotFound
		case errors.Is(err, store.ErrWritesPaused):
			status = http.StatusServiceUnavailable
			detail = "有写入正在进行，请稍后重试"
		case errors.Is(err, store.ErrMaintenanceProtected):
			detail = store.ErrMaintenanceProtected.Error()
		}
		value = map[string]string{"detail": detail}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
