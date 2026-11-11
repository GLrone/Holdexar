package steamevents

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

const dayFormat = "2006-01-02"
const stampFormat = "2006-01-02 15:04:05.000000"

type Event struct {
	Key          string  `json:"key"`
	Category     string  `json:"category"`
	NameEn       string  `json:"nameEn"`
	NameZh       *string `json:"nameZh"`
	Start        string  `json:"start"`
	End          string  `json:"end"`
	PreciseStart *int64  `json:"preciseStartTs"`
	PreciseEnd   *int64  `json:"preciseEndTs"`
	StoreURL     *string `json:"storeUrl"`
}

type ListResult struct {
	Events    []Event `json:"events"`
	Live      []Event `json:"live"`
	Upcoming  []Event `json:"upcoming"`
	Next      *Event  `json:"next"`
	FetchedAt *string `json:"fetchedAt"`
	Stale     bool    `json:"stale"`
}

type SyncResult struct {
	Count      int    `json:"count"`
	Backfilled int    `json:"backfilled"`
	FetchedAt  string `json:"fetchedAt"`
}

type Options struct {
	DataDir  string
	Fetch    func(context.Context, string) (string, error)
	Now      func() time.Time
	Log      func(string, ...any)
	Recorder *jobrun.Recorder
}

type Service struct {
	db      *sql.DB
	gate    *store.WriteGate
	opts    Options
	slot    chan struct{}
	Windows *Windows
	close   func()
	mu      sync.Mutex
	closing bool
	jobs    sync.WaitGroup
}

func New(db *sql.DB, gate *store.WriteGate, opts Options) *Service {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	s := &Service{db: db, gate: gate, opts: opts, slot: make(chan struct{}, 1), Windows: NewWindows(db, gate, opts.Now)}
	if s.opts.Fetch == nil {
		client := sourceClient()
		s.close = client.CloseIdleConnections
		s.opts.Fetch = client.Fetch
	}
	return s
}

func (s *Service) enter() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closing {
		return false
	}
	s.jobs.Add(1)
	return true
}

func (s *Service) Close(ctx context.Context) error {
	s.mu.Lock()
	s.closing = true
	s.mu.Unlock()
	done := make(chan struct{})
	go func() {
		s.jobs.Wait()
		close(done)
	}()
	select {
	case <-done:
		if s.close != nil {
			s.close()
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

var slugSeparators = regexp.MustCompile(`[^a-z0-9]+`)

func eventKey(row parsedEvent) string {
	switch row.Category {
	case "themed_fest":
		if row.Slug != "" {
			return "themed_" + path.Base(row.Slug)
		}
	case "next_fest":
		return "nextfest_" + row.Start.Format("2006_01")
	case "seasonal_sale":
		for _, season := range []struct{ name, zh string }{{"spring", "春"}, {"summer", "夏"}, {"autumn", "秋"}, {"winter", "冬"}} {
			if strings.Contains(strings.ToLower(row.Name), season.name) || strings.HasPrefix(row.Name, season.zh) {
				return "seasonal_" + row.Start.Format("2006") + "_" + season.name
			}
		}
	}
	normalized := strings.Trim(slugSeparators.ReplaceAllString(strings.ToLower(row.Name), "_"), "_")
	if normalized == "" {
		normalized = row.Start.Format(dayFormat)
	}
	if row.Category == "seasonal_sale" {
		return "seasonal_" + row.Start.Format("2006") + "_" + normalized
	}
	return row.Category + "_" + normalized
}

type bilingualWindow struct {
	category, start, end string
}

func windowOf(row parsedEvent) bilingualWindow {
	return bilingualWindow{row.Category, row.Start.Format(dayFormat), row.End.Format(dayFormat)}
}

func (s *Service) merge(english, chinese []parsedEvent, localized map[string]string) []Event {
	names := make(map[bilingualWindow]string, len(chinese))
	for _, row := range chinese {
		window := windowOf(row)
		if _, ok := names[window]; !ok {
			names[window] = row.Name
		}
	}
	out := make([]Event, 0, len(english))
	for _, row := range english {
		window := windowOf(row)
		item := Event{Key: eventKey(row), Category: row.Category, NameEn: row.Name, Start: window.start, End: window.end}
		name := names[window]
		if name == "" && row.Category == "themed_fest" && row.Slug != "" {
			name = localized[strings.ToLower(row.Slug)]
		}
		if name != "" {
			item.NameZh = &name
		} else if row.Category == "seasonal_sale" {
			for _, candidate := range chinese {
				if candidate.Category == row.Category && (candidate.Start.Equal(row.Start) || candidate.Name == row.Name) {
					s.opts.Log("Steam 活动中英文日期不一致，采用英文页 │ 活动=%s，开始=%s", row.Name, window.start)
					break
				}
			}
		}
		out = append(out, item)
	}
	return out
}

func (s *Service) Sync(ctx context.Context) (SyncResult, error) {
	if !s.enter() {
		return SyncResult{}, context.Canceled
	}
	defer s.jobs.Done()
	select {
	case s.slot <- struct{}{}:
		defer func() { <-s.slot }()
	case <-ctx.Done():
		return SyncResult{}, ctx.Err()
	}
	var pages [2]string
	var errs [2]error
	var pending sync.WaitGroup
	for i, language := range []string{"en", "zh"} {
		pending.Go(func() { pages[i], errs[i] = s.opts.Fetch(ctx, language) })
	}
	pending.Wait()
	if errs[0] != nil {
		return SyncResult{}, fmt.Errorf("Steam 英文活动页无法获取：%w", errs[0])
	}
	now := s.opts.Now().In(sched.DefaultLoc)
	en := parseUpcoming(pages[0])
	if err := validateEvents(en, now); err != nil {
		return SyncResult{}, fmt.Errorf("Steam 活动日历未通过校验：%w", err)
	}
	var zh []parsedEvent
	var zhNames map[string]string
	if errs[1] == nil {
		zh, zhNames = parseUpcoming(pages[1]), parseZHNames(pages[1])
	} else {
		s.opts.Log("Steam 中文活动页暂未更新，保留已有名称 │ 原因=%v", errs[1])
	}
	events := s.merge(en, zh, zhNames)
	if err := s.upsert(ctx, events, now); err != nil {
		return SyncResult{}, err
	}
	if err := s.Windows.Reload(ctx); err != nil {
		return SyncResult{}, fmt.Errorf("活动窗口未能重新载入：%w", err)
	}
	for i, language := range []string{"en", "zh"} {
		if s.opts.DataDir != "" && errs[i] == nil && pages[i] != "" {
			if err := saveSource(s.opts.DataDir, language, pages[i]); err != nil {
				s.opts.Log("Steam 活动页快照未能保存 │ 语言=%s，原因=%v", language, err)
			}
		}
	}
	keys := make([]string, 0, len(events))
	for _, event := range events {
		keys = append(keys, event.Key)
	}
	tagged, err := s.Windows.Backfill(ctx, keys)
	if err != nil {
		return SyncResult{}, fmt.Errorf("已更新活动日历，部分价格历史尚待标记：%w", err)
	}
	s.opts.Log("Steam 活动日历已同步 │ 活动=%d，标记价格观测=%d", len(events), tagged)
	return SyncResult{Count: len(events), Backfilled: tagged, FetchedAt: stampISO(now)}, nil
}

func (s *Service) upsert(ctx context.Context, events []Event, now time.Time) error {
	payload, err := json.Marshal(events)
	if err != nil {
		return err
	}
	raw := string(payload)
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		rows, err := conn.QueryContext(ctx, `SELECT e.event_key FROM steam_events e JOIN json_each(?) item
			ON e.event_key=json_extract(item.value,'$.key') WHERE
			e.category<>json_extract(item.value,'$.category') OR e.start_date<>json_extract(item.value,'$.start')
			OR e.end_date<>json_extract(item.value,'$.end')`, raw)
		if err != nil {
			return err
		}
		var changed []string
		for rows.Next() {
			var key string
			if err := rows.Scan(&key); err != nil {
				rows.Close()
				return err
			}
			changed = append(changed, key)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if err := InvalidateBackfill(ctx, conn, changed); err != nil {
			return err
		}
		_, err = conn.ExecContext(ctx, `INSERT INTO steam_events(event_key,category,name_en,name_zh,start_date,end_date,source_updated_at)
			SELECT json_extract(value,'$.key'),json_extract(value,'$.category'),json_extract(value,'$.nameEn'),
			COALESCE(json_extract(value,'$.nameZh'),json_extract(value,'$.nameEn')),
			json_extract(value,'$.start'),json_extract(value,'$.end'),?
			FROM json_each(?) WHERE true ON CONFLICT(event_key) DO UPDATE SET
			category=excluded.category,name_en=excluded.name_en,name_zh=CASE WHEN excluded.name_zh=excluded.name_en
				AND COALESCE(steam_events.name_zh,'')<>'' AND steam_events.name_zh<>steam_events.name_en
				THEN steam_events.name_zh ELSE excluded.name_zh END,
			start_date=excluded.start_date,end_date=excluded.end_date,source_updated_at=excluded.source_updated_at`,
			now.Format(stampFormat), raw)
		return err
	})
}

func stampISO(stamp time.Time) string {
	if stamp.Nanosecond()/1000 == 0 {
		return stamp.Format("2006-01-02T15:04:05")
	}
	return stamp.Format("2006-01-02T15:04:05.000000")
}

func eventTimestamp(raw string) (time.Time, error) {
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999", time.RFC3339Nano} {
		if parsed, err := time.ParseInLocation(layout, raw, sched.DefaultLoc); err == nil {
			return parsed.In(sched.DefaultLoc), nil
		}
	}
	return time.Time{}, errors.New("活动数据的时间格式无效")
}

func (s *Service) List(ctx context.Context) (ListResult, error) {
	result := ListResult{Events: []Event{}, Live: []Event{}, Upcoming: []Event{}, Stale: true}
	rows, err := s.db.QueryContext(ctx, `SELECT event_key,category,name_en,name_zh,CAST(start_date AS TEXT),CAST(end_date AS TEXT),
		precise_start_ts,precise_end_ts,store_url,CAST(source_updated_at AS TEXT) FROM steam_events ORDER BY start_date,id`)
	if err != nil {
		return result, err
	}
	defer rows.Close()
	now := s.opts.Now().In(sched.DefaultLoc)
	today := now.Format(dayFormat)
	var fetched time.Time
	for rows.Next() {
		var event Event
		var stamp sql.NullString
		if err := rows.Scan(&event.Key, &event.Category, &event.NameEn, &event.NameZh, &event.Start, &event.End,
			&event.PreciseStart, &event.PreciseEnd, &event.StoreURL, &stamp); err != nil {
			return result, err
		}
		result.Events = append(result.Events, event)
		switch {
		case event.Start <= today && event.End >= today:
			result.Live = append(result.Live, event)
		case event.Start > today && len(result.Upcoming) < 12:
			result.Upcoming = append(result.Upcoming, event)
		}
		if stamp.Valid {
			parsed, err := eventTimestamp(stamp.String)
			if err == nil && parsed.After(fetched) {
				fetched = parsed
			}
		}
	}
	if !fetched.IsZero() {
		formatted := stampISO(fetched)
		result.FetchedAt, result.Stale = &formatted, now.Sub(fetched) > 7*24*time.Hour
	}
	if len(result.Upcoming) != 0 {
		result.Next = &result.Upcoming[0]
	}
	return result, rows.Err()
}

func saveSource(root, language, raw string) error {
	dir := filepath.Join(root, "steam_events")
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	file, err := os.CreateTemp(dir, ".upcoming-*")
	if err != nil {
		return err
	}
	defer os.Remove(file.Name())
	_, err = file.WriteString(raw)
	if err = errors.Join(err, file.Close()); err != nil {
		return err
	}
	return os.Rename(file.Name(), filepath.Join(dir, "upcoming_"+language+".html"))
}
