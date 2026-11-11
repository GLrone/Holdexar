package family

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/account"
	"holdexar/server-go/internal/events"
	"holdexar/server-go/internal/store"
	"holdexar/server-go/internal/wishlist"
)

const localGroup = "local"
const regionsKey = "family.member_regions"
const cleanupKey = "family.region_legacy_cleanup"

type InputError struct{ Message string }

func (e *InputError) Error() string { return e.Message }

type Options struct {
	Lists                           *wishlist.Steam
	Token                           func(context.Context, string) (string, error)
	Profile                         func(context.Context, string) (map[string]any, error)
	APIKey                          func(context.Context) (string, error)
	OnMembers                       func([]string)
	Bus                             *events.Bus
	Now                             func() time.Time
	Log                             func(string, ...any)
	GroupURL, LinkURL, SummariesURL string
	SharedURL                       string
	OnUncatalogued                  func(context.Context, []int64) error
}
type Service struct {
	db                        *sql.DB
	gate                      *store.WriteGate
	opts                      Options
	life                      context.Context
	cancel                    context.CancelFunc
	mu                        sync.Mutex
	closing, ownLists         bool
	jobs                      sync.WaitGroup
	active, pending           map[string]bool
	avatarUntil, countryUntil time.Time
	libraries                 map[string]*libraryCache
	wishlistKick              time.Time
}
type groupRow struct {
	SID                         string
	Group, Name, Error, Updated sql.NullString
	Members                     []map[string]any
	Count                       int
	FetchOK                     bool
	Play                        sql.NullString
}
type boundRow struct {
	SID, Name string
	Wallet    map[string]any
}
type rowQuery interface {
	QueryRowContext(context.Context, string, ...any) *sql.Row
}

func New(parent context.Context, db *sql.DB, gate *store.WriteGate, opts Options) *Service {
	own := opts.Lists == nil
	if own {
		opts.Lists = wishlist.NewSteam(wishlist.SteamOptions{})
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	if opts.GroupURL == "" {
		opts.GroupURL = "https://api.steampowered.com/IFamilyGroupsService/GetFamilyGroupForUser/v1/"
	}
	if opts.LinkURL == "" {
		opts.LinkURL = "https://api.steampowered.com/IPlayerService/GetPlayerLinkDetails/v1/"
	}
	if opts.SharedURL == "" {
		opts.SharedURL = "https://api.steampowered.com/IFamilyGroupsService/GetSharedLibraryApps/v1/"
	}
	life, cancel := context.WithCancel(parent)
	return &Service{db: db, gate: gate, opts: opts, life: life, cancel: cancel, ownLists: own, active: map[string]bool{}, pending: map[string]bool{}, libraries: map[string]*libraryCache{}}
}
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
		if s.ownLists {
			s.opts.Lists.Close()
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func str(v any) string {
	switch v := v.(type) {
	case string:
		return strings.TrimSpace(v)
	case json.Number:
		return v.String()
	}
	return ""
}
func yes(v any) bool      { b, _ := v.(bool); return b }
func packed(v any) string { b, _ := json.Marshal(v); return string(b) }
func optional(v string) any {
	if v == "" {
		return nil
	}
	return v
}
func validSID(s string) bool {
	if len(s) != 17 {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}
func (s *Service) stamp() string { return s.opts.Now().UTC().Format("2006-01-02 15:04:05.000000") }
func iso(v sql.NullString) any {
	if !v.Valid || v.String == "" {
		return nil
	}
	return strings.Replace(v.String, " ", "T", 1)
}
func readGroup(ctx context.Context, q rowQuery, sid string) (*groupRow, error) {
	g := &groupRow{SID: sid, Members: []map[string]any{}}
	var raw sql.NullString
	e := q.QueryRowContext(ctx, `SELECT family_groupid,family_name,members_json,fetch_ok,last_error,member_count,CAST(updated_at AS TEXT),play_json FROM family_groups WHERE steamid=?`, sid).Scan(&g.Group, &g.Name, &raw, &g.FetchOK, &g.Error, &g.Count, &g.Updated, &g.Play)
	if errors.Is(e, sql.ErrNoRows) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	if raw.Valid && raw.String != "null" {
		if e = json.Unmarshal([]byte(raw.String), &g.Members); e != nil {
			return nil, e
		}
	}
	members := []map[string]any{}
	for _, m := range g.Members {
		if m != nil {
			members = append(members, m)
		}
	}
	g.Members = members
	return g, nil
}
func (s *Service) bound(ctx context.Context) ([]boundRow, error) {
	rows, e := s.db.QueryContext(ctx, `SELECT steam_id,persona_name,wallet_json FROM steam_accounts ORDER BY bound_at,steam_id`)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []boundRow{}
	for rows.Next() {
		var a boundRow
		var name, wallet sql.NullString
		if e = rows.Scan(&a.SID, &name, &wallet); e != nil {
			return nil, e
		}
		a.Name = name.String
		if wallet.Valid {
			if e = json.Unmarshal([]byte(wallet.String), &a.Wallet); e != nil {
				return nil, e
			}
		}
		out = append(out, a)
	}
	return out, rows.Err()
}
func kv(ctx context.Context, q rowQuery, key string, target any) error {
	var raw sql.NullString
	e := q.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, key).Scan(&raw)
	if errors.Is(e, sql.ErrNoRows) || e == nil && (!raw.Valid || raw.String == "null") {
		return nil
	}
	if e != nil {
		return e
	}
	return json.Unmarshal([]byte(raw.String), target)
}
func (s *Service) putKV(ctx context.Context, c *sql.Conn, key string, value any) error {
	_, e := c.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, key, packed(value), s.stamp())
	return e
}
func (s *Service) owner(ctx context.Context, requested string) (string, error) {
	if strings.TrimSpace(requested) != "" {
		return strings.TrimSpace(requested), nil
	}
	accounts, e := s.bound(ctx)
	if e != nil {
		return "", e
	}
	if len(accounts) > 0 {
		return accounts[0].SID, nil
	}
	var old string
	if e = kv(ctx, s.db, "account.steam_id", &old); e != nil {
		return "", e
	}
	if old != "" {
		return old, nil
	}
	return localGroup, nil
}
func (s *Service) Resolve(ctx context.Context, raw string) (map[string]any, error) {
	ctx, done, e := s.work(ctx)
	if e != nil {
		return nil, e
	}
	defer done()
	sid, e := s.opts.Lists.Resolve(ctx, raw)
	if e != nil {
		return nil, &InputError{e.Error()}
	}
	kind := "steamid64"
	if len(strings.TrimSpace(raw)) >= 4 && len(strings.TrimSpace(raw)) <= 10 && !strings.HasPrefix(strings.TrimSpace(raw), "7656") {
		kind = "friend_code"
	}
	profile := map[string]any{}
	if s.opts.Profile != nil {
		if p, e := s.opts.Profile(ctx, sid); e == nil {
			profile = p
		}
	}
	return map[string]any{"steamid": sid, "kind": kind, "personaName": str(profile["persona_name"]), "avatarUrl": account.NormalizeAvatar(str(profile["avatar_url"]))}, nil
}
func (s *Service) Bind(ctx context.Context, raw string) (map[string]any, error) {
	ctx, done, e := s.work(ctx)
	if e != nil {
		return nil, e
	}
	defer done()
	v, e := s.Resolve(ctx, raw)
	if e != nil {
		return nil, e
	}
	e = s.gate.WithWrite(ctx, store.PriorityInteractive, func(c *sql.Conn) error { return s.putKV(ctx, c, "account.steam_id", v["steamid"]) })
	if e != nil {
		return nil, e
	}
	if s.opts.Bus != nil {
		s.opts.Bus.Publish("settings.changed", events.KV{Key: "keys", Val: []string{"account.steam_id"}})
	}
	v["message"] = "已保存为我的 SteamID64"
	return v, nil
}
