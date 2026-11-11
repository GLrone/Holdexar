package family

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"
)

type libraryMember struct {
	SID    string `json:"steamid"`
	Role   string `json:"role"`
	Name   string `json:"personaName"`
	Avatar string `json:"avatarUrl"`
	Owned  int    `json:"ownedCount"`
}

type libraryPlay struct {
	AppID     int64 `json:"appid"`
	Minutes   int64 `json:"minutes"`
	Minutes2w int64 `json:"minutes2w"`
	Last      int64 `json:"last"`
}

type libraryGame struct {
	gameInfo
	Owners     []string `json:"owners"`
	OwnerCount int      `json:"ownerCount"`
	Presence   int      `json:"presence"`
	Excluded   bool     `json:"excluded"`
	Shared     bool     `json:"inSharedLib"`
	Acquired   int64    `json:"timeAcquired"`
	Buyer      any      `json:"buyer"`
	Playtime   int64    `json:"playtimeMinutes"`
	LastPlayed int64    `json:"lastPlayed"`
}

type libraryPayload struct {
	GroupID      string                   `json:"familyGroupid"`
	Name         any                      `json:"familyName"`
	Members      []libraryMember          `json:"members"`
	Games        []libraryGame            `json:"games"`
	Play         map[string][]libraryPlay `json:"memberPlay"`
	SharedCount  int                      `json:"sharedCount"`
	FromSnapshot bool                     `json:"fromSnapshot,omitempty"`
}

type libraryState struct {
	binding                 bindingVersion
	bound                   bool
	group, members, updated string
}

type libraryFlight struct {
	done  chan struct{}
	data  *libraryPayload
	state libraryState
	err   error
}

type libraryCache struct {
	data        *libraryPayload
	state       libraryState
	updated     time.Time
	attempt     libraryState
	lastAttempt time.Time
	flight      *libraryFlight
}

func loadLibraryState(ctx context.Context, q rowQuery, sid string) (libraryState, *groupRow, error) {
	var state libraryState
	v, err := readIdentity(ctx, q, sid)
	if err != nil && !errors.Is(err, errIdentityChanged) {
		return state, nil, err
	}
	state.binding, state.bound = v, err == nil
	g, err := readGroup(ctx, q, sid)
	if g != nil {
		state.group, state.members, state.updated = g.Group.String, packed(g.Members), g.Updated.String
	}
	return state, g, err
}

func (s *Service) Library(parent context.Context, requested string, force bool) (*libraryPayload, error) {
	ctx, done, err := s.work(parent)
	if err != nil {
		return nil, err
	}
	defer done()
	sid, err := s.owner(ctx, requested)
	if err != nil {
		return nil, err
	}
	if sid == localGroup {
		return nil, &InputError{"尚未绑定 Steam 账号"}
	}
	state, group, err := loadLibraryState(ctx, s.db, sid)
	if err != nil {
		return nil, err
	}
	now := s.opts.Now()
	s.mu.Lock()
	var cached *libraryPayload
	var fresh, recent bool
	if entry := s.libraries[sid]; entry != nil {
		if force {
			entry.data = nil
		} else if entry.state == state {
			cached = entry.data
			fresh = now.Sub(entry.updated) < 5*time.Minute
		}
		recent = entry.attempt == state && now.Sub(entry.lastAttempt) < time.Minute
	}
	s.mu.Unlock()
	if cached != nil && fresh {
		return cached, nil
	}
	if cached == nil {
		cached, err = s.librarySnapshot(ctx, group)
		if err != nil {
			return nil, err
		}
	}
	if !state.bound || s.opts.Token == nil {
		if cached != nil {
			return cached, nil
		}
		return nil, &InputError{"该账号无可用登录 Cookie，请先绑定"}
	}
	if cached != nil {
		if force || !recent {
			s.startLibrary(sid, state)
		}
		return cached, nil
	}
	flight := s.startLibrary(sid, state)
	if flight == nil {
		return nil, context.Canceled
	}
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-flight.done:
	}
	if flight.err != nil {
		return nil, flight.err
	}
	current, _, err := loadLibraryState(ctx, s.db, sid)
	if err != nil {
		return nil, err
	}
	if current != flight.state {
		return nil, errIdentityChanged
	}
	return flight.data, nil
}

func (s *Service) startLibrary(sid string, state libraryState) *libraryFlight {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closing {
		return nil
	}
	entry := s.libraries[sid]
	if entry == nil {
		entry = &libraryCache{}
		s.libraries[sid] = entry
	}
	if entry.flight != nil {
		return entry.flight
	}
	flight := &libraryFlight{done: make(chan struct{})}
	entry.flight = flight
	entry.attempt, entry.lastAttempt = state, s.opts.Now()
	s.jobs.Add(1)
	go func() {
		defer s.jobs.Done()
		flight.data, flight.state, flight.err = s.refreshLibrary(s.life, sid)
		if flight.err != nil && s.life.Err() == nil {
			s.opts.Log("家庭库刷新未完成 │ SteamID=%s，原因=%v", sid, flight.err)
		}
		s.mu.Lock()
		if flight.err == nil {
			entry.data, entry.state, entry.updated = flight.data, flight.state, s.opts.Now()
		}
		entry.flight = nil
		close(flight.done)
		s.mu.Unlock()
	}()
	return flight
}

func savedPlay(g *groupRow) (map[string][]libraryPlay, error) {
	play := map[string][]libraryPlay{}
	if g == nil || !g.Play.Valid || g.Play.String == "null" {
		return play, nil
	}
	err := json.Unmarshal([]byte(g.Play.String), &play)
	return play, err
}

func (s *Service) librarySnapshot(ctx context.Context, g *groupRow) (*libraryPayload, error) {
	if g == nil || g.Group.String == "" {
		return nil, nil
	}
	rows, err := s.db.QueryContext(ctx, `SELECT appid,name,owners_json,time_acquired,presence,excluded FROM family_library_snapshots WHERE family_groupid=? ORDER BY time_acquired DESC,appid`, g.Group.String)
	if err != nil {
		return nil, err
	}
	games := []libraryGame{}
	ids := []int64{}
	owners := []string{}
	counts := map[string]int{}
	for rows.Next() {
		game := libraryGame{Shared: true, Owners: []string{}}
		var raw sql.NullString
		if err = rows.Scan(&game.AppID, &game.Name, &raw, &game.Acquired, &game.Presence, &game.Excluded); err != nil {
			rows.Close()
			return nil, err
		}
		if raw.Valid && raw.String != "null" {
			if err = json.Unmarshal([]byte(raw.String), &game.Owners); err != nil {
				rows.Close()
				return nil, err
			}
		}
		for _, sid := range game.Owners {
			if counts[sid] == 0 {
				owners = append(owners, sid)
			}
			counts[sid]++
		}
		game.OwnerCount = len(game.Owners)
		if game.Presence == 0 {
			game.Presence = game.OwnerCount
		}
		if game.OwnerCount > 0 {
			game.Buyer = game.Owners[game.OwnerCount-1]
		}
		games, ids = append(games, game), append(ids, game.AppID)
	}
	err = rows.Err()
	rows.Close()
	if err != nil || len(games) == 0 {
		return nil, err
	}
	meta, err := s.gameMetadata(ctx, ids)
	if err != nil {
		return nil, err
	}
	play, err := savedPlay(g)
	if err != nil {
		return nil, err
	}
	seen, excluded := map[string]bool{}, map[string]bool{}
	members := []libraryMember{}
	for _, m := range g.Members {
		sid := str(m["steamid"])
		if yes(m["excluded"]) {
			excluded[sid] = true
			delete(play, sid)
		}
	}
	for _, m := range g.Members {
		sid := str(m["steamid"])
		if sid == "" || seen[sid] || excluded[sid] {
			continue
		}
		seen[sid] = true
		members = append(members, libraryMember{SID: sid, Role: str(m["role"]), Name: memberName(m), Avatar: memberAvatar(m), Owned: counts[sid]})
	}
	if len(g.Members) == 0 {
		for _, sid := range owners {
			if sid != "" && !excluded[sid] {
				members = append(members, libraryMember{SID: sid, Owned: counts[sid]})
			}
		}
	}
	if len(g.Members) > 0 {
		for sid := range play {
			if !seen[sid] {
				delete(play, sid)
			}
		}
	}
	for i := range games {
		games[i].gameInfo = gameWithInfo(games[i].AppID, meta[games[i].AppID], str(games[i].Name))
	}
	applyPlay(games, play)
	return &libraryPayload{GroupID: g.Group.String, Name: optional(g.Name.String), Members: members, Games: games, Play: play, SharedCount: len(games), FromSnapshot: true}, nil
}

func applyPlay(games []libraryGame, play map[string][]libraryPlay) {
	byID := make(map[int64]*libraryGame, len(games))
	for i := range games {
		byID[games[i].AppID] = &games[i]
	}
	for _, items := range play {
		for _, p := range items {
			if game := byID[p.AppID]; game != nil {
				game.Playtime += p.Minutes
				game.LastPlayed = max(game.LastPlayed, p.Last)
			}
		}
	}
}
