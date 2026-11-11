package humblechoice

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sync"
	"sync/atomic"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/modulectl"
	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const stateKey = "hb_choice_last_month"

type Options struct {
	Client        *http.Client
	MembershipURL string
	SearchURL     string
	Now           func() time.Time
	Crawl         func(context.Context, []int64) (int64, error)
	AutoPrice     func(context.Context) (bool, error)
	Log           func(string, ...any)
}

type Service struct {
	db   *sql.DB
	gate *store.WriteGate
	opts Options
	slot chan struct{}
	ctl  *modulectl.Control
	busy atomic.Bool
}

func New(db *sql.DB, gate *store.WriteGate, opts Options) *Service {
	if opts.Client == nil {
		opts.Client = publicClient()
	}
	if opts.MembershipURL == "" {
		opts.MembershipURL = membershipURL
	}
	if opts.SearchURL == "" {
		opts.SearchURL = steamSearchURL
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	return &Service{db: db, gate: gate, opts: opts, slot: make(chan struct{}, 1), ctl: modulectl.New(ModuleEnabledKey)}
}

type state struct {
	Machine string `json:"machineName"`
	Product string `json:"productName"`
	Marked  int    `json:"marked"`
}

type mark struct {
	AppID int64  `json:"appid"`
	Title string `json:"title"`
}

func (s *Service) state(ctx context.Context) (state, error) {
	var value state
	var raw sql.NullString
	err := s.db.QueryRowContext(ctx, "SELECT value_json FROM app_settings WHERE key=?", stateKey).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) {
		return value, nil
	}
	if err != nil {
		return value, err
	}
	if json.Unmarshal([]byte(raw.String), &value) != nil {
		return state{}, nil
	}
	return value, nil
}

func (s *Service) Run(ctx context.Context, force bool) (map[string]any, error) {
	if enabled, err := s.moduleEnabled(ctx); err != nil {
		return nil, err
	} else if !enabled {
		return map[string]any{"skipped": ErrDisabled.Error()}, nil
	}
	select {
	case s.slot <- struct{}{}:
		s.busy.Store(true)
		defer func() { s.busy.Store(false); <-s.slot }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	result, refreshErr := s.refresh(ctx, force)
	if ctx.Err() != nil {
		return result, ctx.Err()
	}
	if !force && s.opts.AutoPrice != nil {
		enabled, err := s.opts.AutoPrice(ctx)
		if err != nil || !enabled {
			return result, errors.Join(refreshErr, err)
		}
	}
	backfill, err := s.backfill(ctx)
	if result != nil && backfill != nil {
		result["backfill"] = backfill
	}
	return result, errors.Join(refreshErr, err)
}

func (s *Service) refresh(ctx context.Context, force bool) (map[string]any, error) {
	raw, err := s.request(ctx, s.opts.MembershipURL, 4<<20)
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return map[string]any{"source": "hb-choice", "ok": false, "error": err.Error()}, nil
	}
	m, err := parseMembership(raw)
	if err != nil {
		return map[string]any{"source": "hb-choice", "ok": false, "error": err.Error()}, nil
	}
	previous, err := s.state(ctx)
	if err != nil {
		return nil, err
	}
	if previous.Machine == m.Machine && !force {
		return map[string]any{"source": "hb-choice", "ok": true, "skipped": true, "machineName": m.Machine, "lastMarked": previous.Marked}, nil
	}
	matched := make([]int64, len(m.Titles))
	var wg sync.WaitGroup
	for i, title := range m.Titles {
		wg.Go(func() { matched[i] = s.resolve(ctx, title) })
	}
	wg.Wait()
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	marked, unresolved := []mark{}, []string{}
	seen := map[int64]bool{}
	for i, id := range matched {
		if id <= 0 {
			unresolved = append(unresolved, m.Titles[i])
			continue
		}
		if !seen[id] {
			marked = append(marked, mark{id, m.Titles[i]})
			seen[id] = true
		}
	}
	label := monthLabel(m)
	complete := len(unresolved) == 0
	if err := s.persist(ctx, m, label, marked, complete); err != nil {
		return nil, err
	}
	if !complete {
		s.opts.Log("Humble Choice 尚有 %d 款待匹配，下轮继续", len(unresolved))
	}
	return map[string]any{
		"source": "hb-choice", "ok": true, "skipped": false, "machineName": m.Machine, "productName": m.Product,
		"label": label, "marked": marked, "unresolved": unresolved, "recorded": complete,
	}, nil
}

func (s *Service) persist(ctx context.Context, m month, label string, marked []mark, complete bool) error {
	now := s.opts.Now().In(sched.DefaultLoc)
	stamp := now.Format("2006-01-02 15:04:05.000000")
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		ids := make([]int64, len(marked))
		for i, tag := range marked {
			ids[i] = tag.AppID
		}
		if len(ids) > 0 {
			before, err := derived.CatalogInputs(ctx, conn, ids)
			if err != nil {
				return err
			}
			raw, err := json.Marshal(marked)
			if err != nil {
				return err
			}
			_, err = conn.ExecContext(ctx, `INSERT INTO games(appid,name,is_hb,hb_data,is_epic,family_sharing,trading_cards,
				hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,created_at)
				SELECT json_extract(value,'$.appid'),json_extract(value,'$.title'),1,?,0,0,0,0,0,0,0,0,0,0,0,0,?
				FROM json_each(?) WHERE true ON CONFLICT(appid) DO UPDATE SET is_hb=1,
				hb_data=CASE WHEN instr(coalesce(games.hb_data,''),excluded.hb_data)>0 THEN games.hb_data
				WHEN coalesce(games.hb_data,'')='' THEN excluded.hb_data ELSE games.hb_data||', '||excluded.hb_data END
				WHERE games.is_hb<>1 OR instr(coalesce(games.hb_data,''),excluded.hb_data)=0`, label, stamp, string(raw))
			if err != nil {
				return err
			}
			if _, err := derived.UpdatePriceDerived(ctx, conn, ids, derived.Options{Now: now, History: true, CatalogBefore: before}); err != nil {
				return err
			}
		}
		if !complete {
			return nil
		}
		if err := settings.WriteValue(ctx, conn, stateKey, state{m.Machine, m.Product, len(marked)}); err != nil {
			return err
		}
		return recordNotice(ctx, conn, m, label, marked, now, stamp)
	})
}

func recordNotice(ctx context.Context, conn *sql.Conn, m month, label string, marked []mark, now time.Time, stamp string) error {
	items := make([]map[string]any, 0, len(marked))
	names := make([]string, 0, len(marked))
	raw, err := json.Marshal(marked)
	if err != nil {
		return err
	}
	rows, err := conn.QueryContext(ctx, `SELECT g.appid,g.name FROM json_each(?) t JOIN games g ON g.appid=json_extract(t.value,'$.appid') ORDER BY CAST(t.key AS INTEGER)`, string(raw))
	if err != nil {
		return err
	}
	for rows.Next() {
		var id int64
		var name string
		if err := rows.Scan(&id, &name); err != nil {
			rows.Close()
			return err
		}
		items = append(items, map[string]any{"appid": id, "name": name, "url": monthURL(m.Machine)})
		names = append(names, name)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	payload := map[string]any{
		"appid": 0, "label": label, "productName": m.Product, "count": len(marked),
		"title":  fmt.Sprintf("Humble Choice %s：%d 款可兑换", label, len(marked)),
		"labels": []string{"Humble Choice"}, "games": names, "url": monthURL(m.Machine), "end": m.End, "items": items,
	}
	raw, err = json.Marshal(payload)
	if err != nil {
		return err
	}
	var id int64
	err = conn.QueryRowContext(ctx, `INSERT INTO fact_notices(source,kind,fact_key,data,occurred_at)
		SELECT 'hb_choice','bundle_changed',?,?,? WHERE NOT EXISTS(
			SELECT 1 FROM fact_notices WHERE source='hb_choice' AND kind='bundle_changed' AND fact_key=?
		) ON CONFLICT(fact_key) DO NOTHING RETURNING id`,
		"hb_choice:"+m.Machine, string(raw), stamp, "hb_choice:"+label).Scan(&id)
	if errors.Is(err, sql.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	payload["eventType"], payload["source"], payload["kind"] = "HB_CHOICE", "hb_choice", "bundle_changed"
	payload["observedAt"] = now.Format(time.RFC3339)
	_, err = notifications.EnqueueFactsTx(ctx, conn, "fact_notice", []notifications.Fact{{ID: id, EventType: "HB_CHOICE", Payload: payload, OccurredAt: now}}, now)
	return err
}

func (s *Service) backfill(ctx context.Context) (map[string]any, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT appid FROM games g WHERE is_hb=1 AND removed_at IS NULL
		AND coalesce(free_kind,'')<>'f2p' AND NOT EXISTS(SELECT 1 FROM game_current_prices p WHERE p.appid=g.appid)
		ORDER BY appid LIMIT 60`)
	if err != nil {
		return nil, err
	}
	ids := []int64{}
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			rows.Close()
			return nil, err
		}
		ids = append(ids, id)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return nil, err
	}
	result := map[string]any{"source": "hb-backfill", "ok": true, "pending": len(ids), "started": false}
	if len(ids) == 0 {
		return result, nil
	}
	result["jobId"] = nil
	if s.opts.Crawl != nil {
		id, err := s.opts.Crawl(ctx, ids)
		if id > 0 {
			result["jobId"], result["started"] = id, true
		}
		if err != nil && ctx.Err() == nil {
			s.opts.Log("Humble Choice 补价留待后续更新：%v", err)
		}
	}
	return result, ctx.Err()
}
