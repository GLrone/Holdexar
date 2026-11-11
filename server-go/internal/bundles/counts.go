package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const CountsJob = "bartervg_bundles"
const countsStateKey = "bartervg_bundles_last_fetch"
const countsSource = "bartervg-bundles"
const countsURL = "https://bartervg.com/browse/bundles/json/"
const minArchiveRecords = 10000
const maxArchiveBytes = 16 << 20

type countsState struct {
	FetchedAt string `json:"fetchedAt"`
	Records   *int   `json:"records"`
	Updated   *int64 `json:"updated"`
}

type Counts struct {
	db       *sql.DB
	gate     *store.WriteGate
	now      func() time.Time
	fetch    func(context.Context) (map[int64]int64, error)
	slot     chan struct{}
	nextPull time.Time
}

func NewCounts(db *sql.DB, gate *store.WriteGate) *Counts {
	s := &Counts{db: db, gate: gate, now: time.Now, slot: make(chan struct{}, 1)}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	transport.Proxy = func(r *http.Request) (*url.URL, error) {
		proxy, err := netenv.ResolveSystemProxy(r.Context())
		if err != nil || proxy == "" {
			return nil, err
		}
		return url.Parse(proxy)
	}
	client := &http.Client{Transport: transport, Timeout: 90 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	s.fetch = func(ctx context.Context) (map[int64]int64, error) {
		// Space requests according to the feed's 240-per-hour limit.
		if delay := time.Until(s.nextPull); delay > 0 {
			timer := time.NewTimer(delay)
			defer timer.Stop()
			select {
			case <-timer.C:
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		s.nextPull = time.Now().Add(15 * time.Second)
		return fetchCounts(ctx, client, countsURL)
	}
	return s
}

func fetchCounts(ctx context.Context, client *http.Client, address string) (map[int64]int64, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	raw, err := io.ReadAll(io.LimitReader(resp.Body, maxArchiveBytes+1))
	if err != nil {
		return nil, err
	}
	if len(raw) > maxArchiveBytes {
		return nil, errors.New("bundles 档案超过大小上限")
	}
	return parseCounts(raw)
}

func parseCounts(raw []byte) (map[int64]int64, error) {
	var archive map[string]json.RawMessage
	if err := json.Unmarshal(raw, &archive); err != nil || archive == nil {
		return nil, errors.New("bundles 档案顶层不是对象或响应不完整")
	}
	if len(archive) < minArchiveRecords {
		return nil, fmt.Errorf("bundles 档案疑似截断（记录数 %d < %d）", len(archive), minArchiveRecords)
	}
	counts := make(map[int64]int64, len(archive))
	for key, entry := range archive {
		id, err := strconv.ParseUint(key, 10, 32)
		if err != nil || id == 0 {
			continue
		}
		var value struct {
			Bundles int64 `json:"bundles"`
		}
		if json.Unmarshal(entry, &value) == nil && value.Bundles > 0 {
			counts[int64(id)] = value.Bundles
		}
	}
	if len(counts) == 0 {
		return nil, errors.New("bundles 档案没有有效的 Steam 游戏计数")
	}
	return counts, nil
}

func (s *Counts) last(ctx context.Context) (countsState, error) {
	var state countsState
	var raw sql.NullString
	err := s.db.QueryRowContext(ctx, "SELECT value_json FROM app_settings WHERE key=?", countsStateKey).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return state, nil
	}
	if err != nil || !raw.Valid {
		return state, err
	}
	if json.Unmarshal([]byte(raw.String), &state) != nil {
		return countsState{}, nil
	}
	return state, nil
}

func (s *Counts) Refresh(ctx context.Context, force bool) (map[string]any, error) {
	select {
	case s.slot <- struct{}{}:
		defer func() { <-s.slot }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	if !force {
		state, err := s.last(ctx)
		if err != nil {
			return nil, err
		}
		when, err := time.ParseInLocation("2006-01-02T15:04:05", state.FetchedAt, sched.DefaultLoc)
		if err == nil && s.now().Sub(when) < 48*time.Hour {
			return map[string]any{"source": countsSource, "ok": true, "skipped": true,
				"fetchedAt": state.FetchedAt, "lastUpdated": state.Updated}, nil
		}
	}
	counts, err := s.fetch(ctx)
	if err != nil {
		return map[string]any{"source": countsSource, "ok": false, "error": err.Error()}, err
	}
	raw, err := json.Marshal(counts)
	if err != nil {
		return nil, err
	}
	var updated int64
	err = s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, err := conn.ExecContext(ctx, `UPDATE games AS g SET bundle_count=j.value
			FROM json_each(?) AS j WHERE g.appid=CAST(j.key AS INTEGER) AND g.bundle_count IS NOT j.value`, string(raw))
		if err != nil {
			return err
		}
		updated, err = result.RowsAffected()
		if err != nil {
			return err
		}
		records := len(counts)
		state := countsState{FetchedAt: s.now().In(sched.DefaultLoc).Format("2006-01-02T15:04:05.999999"), Records: &records, Updated: &updated}
		return settings.WriteValue(ctx, conn, countsStateKey, state)
	})
	if err != nil {
		return nil, err
	}
	return map[string]any{"source": countsSource, "ok": true, "skipped": false, "records": len(counts), "updated": updated}, nil
}

func (s *Counts) Register(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/v1/metadata/bundles/refresh", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.Refresh(r.Context(), true)
		if err != nil && result == nil {
			http.Error(w, "Internal Server Error", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(result)
	})
	mux.HandleFunc("GET /api/v1/metadata/status", s.status)
}

func (s *Counts) status(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	state, err := s.last(ctx)
	if err != nil {
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}
	var epic, xgp, hb, bundle int
	if err = s.db.QueryRowContext(ctx, `SELECT count(CASE WHEN is_epic THEN 1 END),count(xgp_tier),
		count(CASE WHEN is_hb THEN 1 END),count(bundle_count) FROM games`).Scan(&epic, &xgp, &hb, &bundle); err != nil {
		http.Error(w, "Internal Server Error", http.StatusInternalServerError)
		return
	}
	base := r.URL.Query().Get("source_dir")
	if base == "" {
		base = os.Getenv("METADATA_DATA_DIR")
	}
	base = filepath.Clean(base)
	var fetchedAt any
	if state.FetchedAt != "" {
		fetchedAt = state.FetchedAt
	}
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{
		"sourceDir": base,
		"files": map[string]string{
			"epic": filepath.Join(base, "epic_free_games_clean_final.json"), "xgpTiered": filepath.Join(base, "xgp_tiered_steam.json"),
			"xgpLinks": filepath.Join(base, "xgp_steam_links.json"), "hb": filepath.Join(base, "steam_historical_lows.db"),
		},
		"counts":   map[string]int{"isEpic": epic, "xgpTier": xgp, "isHb": hb, "bundleCount": bundle},
		"bartervg": map[string]any{"fetchedAt": fetchedAt, "records": state.Records},
	})
}
