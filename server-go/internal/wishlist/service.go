package wishlist

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/jobrun"
)

var ErrAccountMissing = errors.New("追踪账户不存在或已更新")
var wishlistZone = time.FixedZone("Beijing", 8*60*60)

type ServiceOptions struct {
	Steam     *Steam
	Now       func() time.Time
	Log       func(string, ...any)
	Persona   func(context.Context, string) (map[string]any, error)
	Avatar    func(string) string
	Crawl     func(context.Context, []int64, []int64) (bool, error)
	CrawlBusy func(error) bool
	Recorder  *jobrun.Recorder
}
type Service struct {
	pool                       *Pool
	opts                       ServiceOptions
	life                       context.Context
	cancel                     context.CancelFunc
	mu                         sync.Mutex
	closing                    bool
	jobs                       sync.WaitGroup
	active                     map[string]bool
	pending                    map[string]bool
	priceVersion, pricePending uint64
	backfilling                bool
	backfillIdle               bool
}
type trackedRow struct {
	ID, Label, Name, Avatar string
	Kinds                   map[string]bool
	Created, Synced         sql.NullString
}

func NewService(parent context.Context, pool *Pool, opts ServiceOptions) *Service {
	if opts.Steam == nil {
		opts.Steam = NewSteam(SteamOptions{})
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	if opts.Avatar == nil {
		opts.Avatar = func(value string) string { return value }
	}
	life, cancel := context.WithCancel(parent)
	return &Service{pool: pool, opts: opts, life: life, cancel: cancel, active: map[string]bool{}, pending: map[string]bool{}}
}
func (s *Service) RequestSync(sid string, autoCrawl bool) bool {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return false
	}
	if s.active[sid] {
		s.pending[sid] = s.pending[sid] || autoCrawl
		s.mu.Unlock()
		return true
	}
	s.active[sid] = true
	s.jobs.Add(1)
	s.mu.Unlock()
	go func() {
		defer s.jobs.Done()
		defer s.finishSync(sid)
		if autoCrawl {
			enabled, e := s.autoPrice(s.life)
			autoCrawl = e == nil && enabled
		}
		if _, e := s.syncAccount(s.life, sid, autoCrawl, true); e != nil && s.life.Err() == nil {
			s.opts.Log("Steam 账户自动同步未完成 │ SteamID=%s，原因=%v", sid, e)
		}
	}()
	return true
}
func (s *Service) finishSync(sid string) {
	s.mu.Lock()
	delete(s.active, sid)
	auto, pending := s.pending[sid]
	delete(s.pending, sid)
	s.mu.Unlock()
	if pending {
		s.RequestSync(sid, auto)
	}
}
func (s *Service) now() time.Time      { return s.opts.Now().In(wishlistZone) }
func trackedStamp(at time.Time) string { return at.Format("2006-01-02 15:04:05.000000") }
func (s *Service) work(parent context.Context) (context.Context, func(), error) {
	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		return nil, nil, context.Canceled
	}
	s.jobs.Add(1)
	s.mu.Unlock()
	ctx, cancel := context.WithCancel(parent)
	stop := context.AfterFunc(s.life, cancel)
	return ctx, func() { stop(); cancel(); s.jobs.Done() }, nil
}
func (s *Service) Close(ctx context.Context) error {
	s.mu.Lock()
	s.closing = true
	s.cancel()
	s.mu.Unlock()
	done := make(chan struct{})
	go func() { s.jobs.Wait(); close(done) }()
	select {
	case <-done:
		s.opts.Steam.Close()
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (s *Service) tracked(ctx context.Context, q interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}, sid string) (trackedRow, error) {
	var row trackedRow
	var label, name, avatar, kinds sql.NullString
	e := q.QueryRowContext(ctx, `SELECT steamid,label,persona_name,avatar_url,kinds_json,CAST(created_at AS TEXT),CAST(last_sync_at AS TEXT) FROM tracked_accounts WHERE steamid=?`, sid).Scan(&row.ID, &label, &name, &avatar, &kinds, &row.Created, &row.Synced)
	if errors.Is(e, sql.ErrNoRows) {
		return row, fmt.Errorf("%w: %s", ErrAccountMissing, sid)
	}
	if e != nil {
		return row, e
	}
	row.Label, row.Name, row.Avatar = label.String, name.String, avatar.String
	if kinds.Valid && kinds.String != "null" {
		if e = json.Unmarshal([]byte(kinds.String), &row.Kinds); e != nil {
			return row, fmt.Errorf("账户同步类型格式无效")
		}
	}
	return row, nil
}
func syncKinds(kinds map[string]bool) map[string]bool {
	if len(kinds) == 0 {
		return map[string]bool{"wishlist": true, "owned": true}
	}
	return kinds
}
func text(value any) string {
	if value, ok := value.(string); ok {
		return strings.TrimSpace(value)
	}
	return ""
}
