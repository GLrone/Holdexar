package games

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"sync"

	"holdexar/server-go/internal/crawler/dbwriter"
	"holdexar/server-go/internal/store"
)

// HistorySource 按 appid×region 取回观测批次，去重与合流写入由核心执行。
type HistorySource interface {
	Fetch(ctx context.Context, appid int64, region string) ([]dbwriter.CommunityObservation, error)
}

var ErrHistorySourceUnavailable = errors.New("社区历史数据源未安装，可在设置 → 插件中添加")

// HistoryBackfill 持有进程级后台上下文，请求断开不中断在途回填。
type HistoryBackfill struct {
	background context.Context
	db         *sql.DB
	gate       *store.WriteGate
	writer     *dbwriter.Writer
	source     HistorySource
	log        func(format string, args ...any)

	mu       sync.Mutex
	inflight map[string]struct{}
}

func NewHistoryBackfill(ctx context.Context, db *sql.DB, gate *store.WriteGate, log func(format string, args ...any)) *HistoryBackfill {
	writer := dbwriter.NewWriter(db, gate)
	writer.Log = log
	return &HistoryBackfill{
		background: ctx, db: db, gate: gate, writer: writer, log: log,
		inflight: map[string]struct{}{},
	}
}

func (b *HistoryBackfill) SetSource(source HistorySource) {
	b.mu.Lock()
	b.source = source
	b.mu.Unlock()
}

func (b *HistoryBackfill) sourceSnapshot() HistorySource {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.source
}

func backfillKey(appid int64, regionUpper string) string {
	return strconv.FormatInt(appid, 10) + "|" + regionUpper
}

func (b *HistoryBackfill) Start(appid int64, region string) (bool, string, error) {
	regionUpper := strings.ToUpper(strings.TrimSpace(region))
	if regionUpper == "" {
		return false, "", errors.New("缺少地区")
	}
	key := backfillKey(appid, regionUpper)
	b.mu.Lock()
	if _, busy := b.inflight[key]; busy {
		b.mu.Unlock()
		return false, "running", nil
	}
	source := b.source
	if source == nil {
		b.mu.Unlock()
		return false, "", ErrHistorySourceUnavailable
	}
	if availability, ok := source.(interface {
		Available(context.Context) (bool, error)
	}); ok {
		b.mu.Unlock()
		available, err := availability.Available(b.background)
		if err != nil {
			return false, "", err
		}
		if !available {
			return false, "", ErrHistorySourceUnavailable
		}
		b.mu.Lock()
		if _, busy := b.inflight[key]; busy {
			b.mu.Unlock()
			return false, "running", nil
		}
	}
	b.inflight[key] = struct{}{}
	b.mu.Unlock()

	go func() {
		defer func() {
			b.mu.Lock()
			delete(b.inflight, key)
			b.mu.Unlock()
		}()
		b.run(b.background, appid, regionUpper, source)
	}()
	return true, "running", nil
}

func (b *HistoryBackfill) run(ctx context.Context, appid int64, regionUpper string, source HistorySource) {
	obs, err := source.Fetch(ctx, appid, regionUpper)
	if err != nil {
		b.recordState(appid, regionUpper, 0, err.Error())
		if b.log != nil {
			b.log("社区历史回填失败 │ 游戏=%d，地区=%s，原因=%v", appid, regionUpper, err)
		}
		return
	}
	inserted, err := b.writer.MergeCommunityHistory(ctx, appid, regionUpper, obs)
	if err != nil {
		b.recordState(appid, regionUpper, 0, err.Error())
		if b.log != nil {
			b.log("社区历史合流失败 │ 游戏=%d，地区=%s，原因=%v", appid, regionUpper, err)
		}
		return
	}
	b.recordState(appid, regionUpper, inserted, "")
	if b.log != nil {
		b.log("社区历史回填完成 │ 游戏=%d，地区=%s，上游=%d，入库=%d", appid, regionUpper, len(obs), inserted)
	}
}

// recordState 全量覆写最近一轮同步状态（非事实账本，丢失可重抓幂等重建）。
func (b *HistoryBackfill) recordState(appid int64, regionUpper string, pointsStored int, failure string) {
	now := b.writer.Now().Format("2006-01-02 15:04:05")
	var lastOk, lastEmpty, lastErrAt, lastErr any
	switch {
	case failure != "":
		lastErrAt, lastErr = now, failure
	case pointsStored > 0:
		lastOk = now
	default:
		lastEmpty = now
	}
	e := b.gate.WithWrite(b.background, store.PriorityBackground, func(conn *sql.Conn) error {
		_, e := conn.ExecContext(b.background, `INSERT INTO history_backfill_state
			(appid, region_code, last_ok_at, last_empty_at, last_error_at, last_error, points_stored, updated_at)
			VALUES (?,?,?,?,?,?,?,?)
			ON CONFLICT(appid, region_code) DO UPDATE SET
			last_ok_at=excluded.last_ok_at,
			last_empty_at=excluded.last_empty_at,
			last_error_at=excluded.last_error_at,
			last_error=excluded.last_error,
			points_stored=excluded.points_stored,
			updated_at=excluded.updated_at`,
			appid, regionUpper, lastOk, lastEmpty, lastErrAt, lastErr, pointsStored, now)
		return e
	})
	if e != nil && b.log != nil {
		b.log("回填状态写入失败 │ 游戏=%d，地区=%s，原因=%v", appid, regionUpper, e)
	}
}

func (b *HistoryBackfill) Brief(ctx context.Context, appid int64, regionUpper string) historyBrief {
	b.mu.Lock()
	_, busy := b.inflight[backfillKey(appid, regionUpper)]
	b.mu.Unlock()
	if busy {
		return historyBrief{State: "running"}
	}
	var lastOk, lastEmpty, lastErrAt, lastErr sql.NullString
	e := b.db.QueryRowContext(ctx,
		`SELECT CAST(last_ok_at AS TEXT), CAST(last_empty_at AS TEXT), CAST(last_error_at AS TEXT), last_error
		 FROM history_backfill_state WHERE appid=? AND region_code=?`,
		appid, regionUpper).Scan(&lastOk, &lastEmpty, &lastErrAt, &lastErr)
	if e != nil {
		return historyBrief{State: "idle"}
	}
	state := "ok"
	if lastErrAt.Valid && (!lastOk.Valid || lastErrAt.String > lastOk.String) {
		state = "error"
	} else if lastEmpty.Valid && (!lastOk.Valid || lastEmpty.String > lastOk.String) {
		state = "empty"
	}
	brief := historyBrief{State: state}
	if state != "error" && lastOk.Valid {
		v := isoNaive(lastOk.String)
		brief.LastOkAt = &v
	}
	if state == "error" && lastErr.Valid {
		v := lastErr.String
		brief.LastError = &v
	}
	return brief
}

func (h *handler) handleBackfill(w http.ResponseWriter, r *http.Request) {
	appid, err := parseAppID(r.PathValue("appid"))
	if err != nil {
		writeJSON(w, http.StatusUnprocessableEntity, intParseDetail(r.PathValue("appid")))
		return
	}
	region := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("region")))
	if region == "" {
		region = "cn"
	}
	if len(region) > 5 {
		writeJSON(w, http.StatusUnprocessableEntity, queryError("region", "string_too_long", "String should have at most 5 characters", r.URL.Query().Get("region")))
		return
	}
	if h.backfill == nil {
		writeJSON(w, http.StatusConflict, map[string]any{"detail": "社区历史数据源未安装"})
		return
	}
	started, state, e := h.backfill.Start(int64(appid), region)
	if e != nil {
		writeJSON(w, http.StatusConflict, map[string]any{"detail": e.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"started": started, "state": state})
}

func (h *handler) brief(ctx context.Context, appid int64, regionUpper string) historyBrief {
	if h.backfill == nil {
		return historyBrief{State: "idle"}
	}
	return h.backfill.Brief(ctx, appid, regionUpper)
}
