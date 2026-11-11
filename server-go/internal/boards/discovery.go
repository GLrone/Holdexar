package boards

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"sync"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/wishlist"
)

type DiscoveryOptions struct {
	Crawl    func(context.Context, string, []int64) (int64, error)
	Recorder *jobrun.Recorder
	Now      func() time.Time
	Log      func(string, ...any)
}
type Discovery struct {
	db      *sql.DB
	client  *Client
	pool    *wishlist.Pool
	opts    DiscoveryOptions
	mu      sync.Mutex
	closing bool
	jobs    sync.WaitGroup
}
type discoveryBoard struct {
	key, kind string
	limit     int
	period    time.Duration
	onlyNew   bool
}

var discoveryBoards = []discoveryBoard{
	{"topsellers", "top100_backfill", 500, time.Hour, false},
	{"popularnew", "popularnew_backfill", 100, 24 * time.Hour, false},
	{"comingsoon", "comingsoon_backfill", 100, 24 * time.Hour, true},
}

func discoveryDefinition(key string) (discoveryBoard, error) {
	for _, def := range discoveryBoards {
		if def.key == key {
			return def, nil
		}
	}
	return discoveryBoard{}, fmt.Errorf("未知榜单发现源: %s", key)
}
func NewDiscovery(db *sql.DB, client *Client, pool *wishlist.Pool, opts DiscoveryOptions) *Discovery {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	return &Discovery{db: db, client: client, pool: pool, opts: opts}
}
func (d *Discovery) Backfill(ctx context.Context, key string, ids []int64, limit int) ([]int64, error) {
	def, e := discoveryDefinition(key)
	if e != nil {
		return nil, e
	}
	raw, e := json.Marshal(ids)
	if e != nil {
		return nil, e
	}
	rows, e := d.db.QueryContext(ctx, `SELECT CAST(j.value AS INTEGER) FROM json_each(?) j LEFT JOIN games g ON g.appid=j.value WHERE g.appid IS NULL OR g.removed_at IS NOT NULL OR (?=0 AND g.updated_at IS NULL AND NOT EXISTS(SELECT 1 FROM game_current_prices p WHERE p.appid=g.appid)) ORDER BY CAST(j.key AS INTEGER) LIMIT ?`, string(raw), def.onlyNew, max(0, limit))
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []int64{}
	for rows.Next() {
		var id int64
		if e = rows.Scan(&id); e != nil {
			return nil, e
		}
		out = append(out, id)
	}
	return out, rows.Err()
}
func (d *Discovery) Run(ctx context.Context, key string) (map[string]any, error) {
	d.mu.Lock()
	if d.closing {
		d.mu.Unlock()
		return nil, context.Canceled
	}
	d.jobs.Add(1)
	d.mu.Unlock()
	defer d.jobs.Done()
	body := func() (map[string]any, error) { return d.run(ctx, key) }
	if d.opts.Recorder != nil {
		return d.opts.Recorder.Run(ctx, "board_"+key, body)
	}
	return body()
}
func (d *Discovery) run(ctx context.Context, key string) (map[string]any, error) {
	def, e := discoveryDefinition(key)
	if e != nil {
		return nil, e
	}
	var raw sql.NullString
	e = d.db.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key='fetch.boards'`).Scan(&raw)
	if e != nil && e != sql.ErrNoRows {
		return nil, e
	}
	if e == nil {
		var enabled any
		if raw.Valid {
			if e = json.Unmarshal([]byte(raw.String), &enabled); e != nil {
				return nil, e
			}
		}
		off := enabled == nil || enabled == false || enabled == float64(0) || enabled == ""
		if off {
			return map[string]any{"skipped": "Steam 排行榜自动更新已关闭"}, nil
		}
	}
	ids, e := d.client.Refresh(ctx, key)
	if e != nil {
		return nil, e
	}
	result := map[string]any{"board": key, "count": len(ids)}
	if len(ids) == 0 {
		return result, nil
	}
	d.opts.Log("榜单缓存预热完成 │ 榜单=%s，收录=%d", key, len(ids))
	if landed, e := d.pool.EnsureBoard(ctx, ids, "board"); e != nil {
		d.opts.Log("榜单条目入池未完成，继续首爬 │ 榜单=%s，原因=%v", key, e)
	} else {
		result["pool"] = landed
	}
	if key == "topsellers" {
		if n, e := d.pool.RecordBoard(ctx, ids); e != nil {
			d.opts.Log("热销榜预设登记未完成：%v", e)
		} else {
			result["preset"] = n
		}
	}
	pending, e := d.Backfill(ctx, key, ids, def.limit)
	if e != nil {
		return result, e
	}
	result["pending"] = len(pending)
	if len(pending) > 0 {
		if d.opts.Crawl == nil {
			return result, fmt.Errorf("榜单首爬尚未接入")
		}
		id, e := d.opts.Crawl(ctx, def.kind, pending)
		result["jobId"] = id
		if e != nil {
			return result, e
		}
		d.opts.Log("榜单条目首爬完成 │ 榜单=%s，任务=%d，条目=%d", key, id, len(pending))
	}
	return result, nil
}
func (d *Discovery) RegisterSchedules(ctx context.Context, clock *sched.Scheduler) error {
	for _, def := range discoveryBoards {
		if e := clock.RegisterInterval("board_"+def.key, d.opts.Now().Add(def.period), def.period, func(*sched.RunCtx) {
			_, e := d.Run(ctx, def.key)
			if e != nil {
				d.opts.Log("榜单自动更新未完成 │ 榜单=%s，原因=%v", def.key, e)
			}
		}); e != nil {
			return e
		}
	}
	return nil
}
func (d *Discovery) Close(ctx context.Context) error {
	d.mu.Lock()
	d.closing = true
	d.mu.Unlock()
	done := make(chan struct{})
	go func() { d.jobs.Wait(); close(done) }()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
