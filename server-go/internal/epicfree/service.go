package epicfree

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"slices"
	"sync"
	"time"

	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const snapshotKey = "metadata.epic_offers_cache"
const offersTTL = 30 * time.Minute

type OffersPayload struct {
	Source    string       `json:"source"`
	OK        bool         `json:"ok"`
	Offers    []Offer      `json:"offers"`
	Mobile    *MobileOffer `json:"mobile"`
	FetchedAt *string      `json:"fetchedAt"`
	Cached    bool         `json:"cached,omitempty"`
	Stale     bool         `json:"stale,omitempty"`
}

type Options struct {
	Now          func() time.Time
	Log          func(string, ...any)
	Fetch        func(context.Context) ([]Offer, error)
	Match        func(context.Context, []Offer) []Offer
	Mobile       func(context.Context) (*MobileOffer, error)
	Breaker      func(context.Context) (*MobileOffer, error)
	MobileFromPC func(context.Context, []Offer) (*MobileOffer, error)
}

type offersSnapshot struct {
	At      float64       `json:"fetched_at"`
	Payload OffersPayload `json:"payload"`
}

type flight struct {
	done    chan struct{}
	payload OffersPayload
	pc      []Offer
	pcErr   error
}

type Service struct {
	db          *sql.DB
	gate        *store.WriteGate
	opts        Options
	client      *http.Client
	life        context.Context
	cancel      context.CancelFunc
	module      context.Context
	stopModule  context.CancelFunc
	transition  sync.Mutex
	mu          sync.Mutex
	enabled     bool
	initialized bool
	closed      bool
	loaded      bool
	cached      *offersSnapshot
	active      *flight
	operations  int
	jobs        sync.WaitGroup
}

func New(parent context.Context, db *sql.DB, gate *store.WriteGate, opts Options) *Service {
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	client := newPublicClient()
	if opts.Fetch == nil {
		opts.Fetch = func(ctx context.Context) ([]Offer, error) { return FetchPC(ctx, client) }
	}
	if opts.Match == nil {
		opts.Match = func(ctx context.Context, offers []Offer) []Offer { return MatchSteam(ctx, client, offers) }
	}
	if opts.Mobile == nil {
		opts.Mobile = func(ctx context.Context) (*MobileOffer, error) { return DiscoverMobile(ctx, client) }
	}
	if opts.Breaker == nil {
		opts.Breaker = func(ctx context.Context) (*MobileOffer, error) { return FetchMobileBreaker(ctx, client) }
	}
	if opts.MobileFromPC == nil {
		opts.MobileFromPC = func(ctx context.Context, offers []Offer) (*MobileOffer, error) {
			return ResolveMobileFromPC(ctx, client, offers)
		}
	}
	ctx, cancel := context.WithCancel(parent)
	module, stopModule := context.WithCancel(ctx)
	return &Service{db: db, gate: gate, opts: opts, client: client, life: ctx, cancel: cancel,
		module: module, stopModule: stopModule, enabled: true}
}

func (s *Service) Close(ctx context.Context) error {
	s.transition.Lock()
	defer s.transition.Unlock()
	s.mu.Lock()
	s.closed = true
	s.cancel()
	s.mu.Unlock()
	if err := s.wait(ctx); err != nil {
		return err
	}
	s.client.CloseIdleConnections()
	return nil
}

func (s *Service) hydrate(ctx context.Context) (*offersSnapshot, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.loaded {
		return s.cached, nil
	}
	var raw sql.NullString
	err := s.db.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, snapshotKey).Scan(&raw)
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return nil, err
	}
	var snap offersSnapshot
	if raw.Valid && json.Unmarshal([]byte(raw.String), &snap) == nil && snap.At > 0 && snap.Payload.OK {
		if snap.Payload.Offers == nil {
			snap.Payload.Offers = []Offer{}
		}
		s.cached = &snap
	}
	s.loaded = true
	return s.cached, nil
}

func emptyOffers() OffersPayload {
	return OffersPayload{Source: "epic-offers", Offers: []Offer{}}
}

func (s *Service) start(parent context.Context) (*flight, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closed || s.life.Err() != nil {
		return nil, context.Canceled
	}
	if !s.enabled {
		return nil, ErrDisabled
	}
	if s.active != nil {
		return s.active, nil
	}
	f := &flight{done: make(chan struct{})}
	old := s.cached
	s.active = f
	s.jobs.Add(1)
	ctx, cancel := context.WithCancel(parent)
	stop := context.AfterFunc(s.module, cancel)
	go func() {
		defer s.jobs.Done()
		defer func() { stop(); cancel() }()
		f.payload, f.pc, f.pcErr = s.fetch(ctx, old)
		s.mu.Lock()
		s.active = nil
		close(f.done)
		s.mu.Unlock()
	}()
	return f, nil
}

func (s *Service) Offers(ctx context.Context, force bool) (OffersPayload, error) {
	ctx, done, err := s.begin(ctx)
	if err != nil {
		return emptyOffers(), err
	}
	defer done()
	snap, err := s.hydrate(ctx)
	if err != nil {
		return emptyOffers(), err
	}
	if !force {
		enabled, err := settings.EpicFreeEnabled(ctx, s.db)
		if err != nil {
			return emptyOffers(), err
		}
		if snap != nil {
			payload := snap.Payload
			age := float64(s.opts.Now().UnixNano())/float64(time.Second) - snap.At
			payload.Cached = true
			payload.Stale = !enabled || age < 0 || age >= offersTTL.Seconds()
			if enabled && payload.Stale {
				_, _ = s.start(s.life)
			}
			return payload, nil
		}
		if !enabled {
			return emptyOffers(), nil
		}
	}
	f, err := s.start(ctx)
	if err != nil {
		return emptyOffers(), err
	}
	select {
	case <-f.done:
		if err := ctx.Err(); err != nil {
			return emptyOffers(), err
		}
		if errors.Is(f.pcErr, context.Canceled) || errors.Is(f.pcErr, context.DeadlineExceeded) {
			return emptyOffers(), f.pcErr
		}
		return f.payload, nil
	case <-ctx.Done():
		return emptyOffers(), ctx.Err()
	}
}

func (s *Service) fetch(ctx context.Context, old *offersSnapshot) (OffersPayload, []Offer, error) {
	var pc []Offer
	var mobile, breaker *MobileOffer
	var pcErr, mobileErr, breakerErr error
	var pending sync.WaitGroup
	pending.Go(func() { pc, pcErr = s.opts.Fetch(ctx) })
	pending.Go(func() { mobile, mobileErr = s.opts.Mobile(ctx) })
	pending.Go(func() { breaker, breakerErr = s.opts.Breaker(ctx) })
	pending.Wait()
	if err := ctx.Err(); err != nil {
		return emptyOffers(), nil, err
	}
	if mobileErr != nil {
		s.opts.Log("Epic 移动促销来源暂不可用 │ 原因=%v", mobileErr)
	}
	if breakerErr != nil {
		s.opts.Log("Epic 移动商店备用图暂不可用 │ 原因=%v", breakerErr)
	}
	if mobile == nil && len(pc) > 0 {
		mobile, mobileErr = s.opts.MobileFromPC(ctx, pc)
		if mobileErr != nil {
			s.opts.Log("Epic 移动促销备用查询未完成 │ 原因=%v", mobileErr)
		}
	}
	if mobile == nil {
		mobile = breaker
	} else if mobile.Image == "" && breaker != nil {
		copy := *mobile
		copy.Image = breaker.Image
		mobile = &copy
	}
	now := s.opts.Now().In(sched.DefaultLoc)
	fetched := now.Format(time.RFC3339)
	result := emptyOffers()
	if pcErr != nil {
		s.opts.Log("Epic 电脑端促销暂不可用 │ 原因=%v", pcErr)
		if mobile != nil {
			result.Mobile, result.FetchedAt, result.OK = mobile, &fetched, true
			if old != nil {
				result.Offers, result.FetchedAt = old.Payload.Offers, old.Payload.FetchedAt
				result.Cached, result.Stale = true, true
			}
		}
		return result, nil, pcErr
	}
	if pc == nil {
		pc = []Offer{}
	}
	result.OK, result.Offers, result.Mobile, result.FetchedAt = true, pc, mobile, &fetched
	snapshot := &offersSnapshot{At: float64(now.UnixNano()) / float64(time.Second), Payload: result}
	if err := s.persistSnapshot(ctx, snapshot, now); err != nil {
		s.opts.Log("Epic 促销快照未能保存 │ 原因=%v", err)
		return result, pc, err
	}
	if old != nil {
		newItems := currentDifference(old.Payload.Offers, pc)
		if _, err := s.recordNotices(ctx, newItems, now); err != nil {
			s.opts.Log("Epic 促销变化暂未记录，下一轮自动核对 │ 原因=%v", err)
		}
	}
	s.mu.Lock()
	s.cached = snapshot
	s.mu.Unlock()
	return result, pc, nil
}

func (s *Service) persistSnapshot(ctx context.Context, snap *offersSnapshot, now time.Time) error {
	raw, err := json.Marshal(snap)
	if err != nil {
		return err
	}
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?)
			ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`,
			snapshotKey, string(raw), now.Format("2006-01-02 15:04:05.000000"))
		return err
	})
}

func (s *Service) Refresh(ctx context.Context) (map[string]any, error) {
	ctx, done, err := s.begin(ctx)
	if err != nil {
		return nil, err
	}
	defer done()
	if _, err := s.hydrate(ctx); err != nil {
		return nil, err
	}
	f, err := s.start(ctx)
	if err != nil {
		return nil, err
	}
	select {
	case <-f.done:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	if f.pcErr != nil || len(f.pc) == 0 {
		message := "促销端点本轮无白送元素"
		if f.pcErr != nil {
			message = fmt.Sprintf("Epic 促销刷新未完成：%v", f.pcErr)
		}
		return map[string]any{"source": "epic-free", "ok": false, "error": message}, nil
	}
	matched := s.opts.Match(ctx, slices.Clone(f.pc))
	now := s.opts.Now().In(sched.DefaultLoc)
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	tags := make([]gameTag, 0, len(matched))
	unresolved := []map[string]any{}
	for _, offer := range matched {
		if offer.AppID == nil || *offer.AppID <= 0 {
			unresolved = append(unresolved, map[string]any{"title": offer.Title, "freeStart": offer.Start})
			continue
		}
		tags = append(tags, gameTag{AppID: *offer.AppID, Name: offer.TitleCn, Title: offer.Title, Start: normalizeEpicDate(offer.Start)})
	}
	marked, err := s.mark(ctx, tags, now)
	if err != nil {
		return nil, err
	}
	if _, err := s.recordNotices(ctx, f.pc, now); err != nil {
		s.opts.Log("Epic 促销提醒暂未记录，下一轮自动核对 │ 原因=%v", err)
	}
	s.opts.Log("Epic 免费窗口已核对 │ 条目=%d，新标记=%d，未匹配=%d", len(f.pc), len(marked), len(unresolved))
	changed := make([]map[string]any, 0, len(marked))
	for _, tag := range marked {
		changed = append(changed, map[string]any{"appid": tag.AppID, "title": tag.Title, "freeStart": tag.Start})
	}
	return map[string]any{"source": "epic-free", "ok": true, "window": len(f.pc), "marked": changed, "unresolved": unresolved}, nil
}
