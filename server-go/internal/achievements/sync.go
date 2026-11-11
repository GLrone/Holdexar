package achievements

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/jobrun"
	"holdexar/server-go/internal/modulectl"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/store"
)

const syncKey = "achievements.sync_state"

var (
	errSyncBusy     = errors.New("成就同步已在进行中")
	errSyncOwner    = errors.New("Steam 账号格式无效")
	errNoCredential = errors.New("无可用凭证，请重新绑定 Steam 账号或配置 Web API Key")
	errOwnerChanged = errors.New("账号已解绑，本轮成就同步已停止")
)

type Snapshot struct {
	Running bool   `json:"running"`
	OK      *bool  `json:"ok"`
	Stage   string `json:"stage"`
	Done    int    `json:"done"`
	Total   int    `json:"total"`
	Current string `json:"current"`
	Error   string `json:"error"`
	Started string `json:"startedAt"`
	Synced  string `json:"syncedAt"`
	SteamID string `json:"steamid"`
}

type SyncOptions struct {
	Steam    *Steam
	APIKey   func(context.Context) (string, error)
	Token    func(context.Context, string) (string, error)
	HasToken func(context.Context, string) (bool, error)
	Shared   func(context.Context, string) (map[int64]string, bool, error)
	Award    func(context.Context, string) error
	Recorder *jobrun.Recorder
	Log      func(string, ...any)
	Now      func() time.Time
}

type syncFlight struct {
	done chan struct{}
	err  error
}

type SyncService struct {
	db     *sql.DB
	gate   *store.WriteGate
	opts   SyncOptions
	life   context.Context
	cancel context.CancelFunc
	mu     sync.Mutex
	flight *syncFlight
	warmed time.Time
	ctl    *modulectl.Control
}

func NewSync(parent context.Context, db *sql.DB, gate *store.WriteGate, opts SyncOptions) *SyncService {
	if opts.Steam == nil {
		opts.Steam = NewSteam(SteamOptions{})
	}
	if opts.Now == nil {
		opts.Now = time.Now
	}
	if opts.Log == nil {
		opts.Log = func(string, ...any) {}
	}
	life, cancel := context.WithCancel(parent)
	return &SyncService{db: db, gate: gate, opts: opts, life: life, cancel: cancel, ctl: modulectl.New(ModuleEnabledKey)}
}

func (s *SyncService) primary(ctx context.Context) (string, error) {
	var sid string
	err := s.db.QueryRowContext(ctx, `SELECT COALESCE(
		(SELECT steam_id FROM steam_accounts ORDER BY bound_at,steam_id LIMIT 1),
		(SELECT CASE WHEN json_valid(value_json) THEN json_extract(value_json,'$') END FROM app_settings WHERE key='account.steam_id'),'')`).Scan(&sid)
	return strings.TrimSpace(sid), err
}

type syncOwner struct {
	id         string
	tokenOwner string
	boundAt    sql.NullString
	bound      bool
}

func (s *SyncService) owner(ctx context.Context, sid string) (syncOwner, error) {
	owner := syncOwner{id: strings.TrimSpace(sid)}
	primary, err := s.primary(ctx)
	if err != nil {
		return owner, err
	}
	if owner.id == "" {
		owner.id = primary
	}
	if owner.id == "" {
		return owner, nil
	}
	err = s.db.QueryRowContext(ctx, `SELECT CAST(bound_at AS TEXT) FROM steam_accounts WHERE steam_id=?`, owner.id).Scan(&owner.boundAt)
	if err == nil {
		owner.tokenOwner, owner.bound = owner.id, true
		return owner, nil
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return owner, err
	}
	var family bool
	err = s.db.QueryRowContext(ctx, `SELECT EXISTS(
		SELECT 1 FROM family_groups g, json_each(CASE WHEN json_valid(g.members_json) THEN g.members_json ELSE '[]' END) m
		WHERE g.steamid=? AND g.family_groupid<>'' AND CAST(json_extract(CASE WHEN m.type='object' THEN m.value END,'$.steamid') AS TEXT)=?)`, primary, owner.id).Scan(&family)
	if family {
		owner.tokenOwner = primary
	}
	return owner, err
}

func (s *SyncService) Account(ctx context.Context, target string) (string, bool, error) {
	owner, err := s.owner(ctx, target)
	if err != nil || owner.id == "" {
		return owner.id, false, err
	}
	if s.opts.APIKey != nil {
		key, err := s.opts.APIKey(ctx)
		if err == nil && key != "" {
			return owner.id, true, nil
		}
	}
	if owner.tokenOwner != "" && s.opts.HasToken != nil {
		present, err := s.opts.HasToken(ctx, owner.tokenOwner)
		if err != nil && ctx.Err() == nil {
			s.opts.Log("成就页暂无法核验本机登录凭据 │ SteamID=%s", owner.id)
			return owner.id, false, nil
		}
		return owner.id, present, err
	}
	return owner.id, false, nil
}

func (owner syncOwner) check(ctx context.Context, conn *sql.Conn) error {
	if !owner.bound {
		return nil
	}
	var present bool
	err := conn.QueryRowContext(ctx, `SELECT EXISTS(SELECT 1 FROM steam_accounts WHERE steam_id=? AND CAST(bound_at AS TEXT) IS ?)`, owner.id, owner.boundAt).Scan(&present)
	if err == nil && !present {
		return errOwnerChanged
	}
	return err
}

func (s *SyncService) Status(ctx context.Context) (Snapshot, error) {
	var snap Snapshot
	var raw sql.NullString
	err := s.db.QueryRowContext(ctx, `SELECT value_json FROM app_settings WHERE key=?`, syncKey).Scan(&raw)
	if errors.Is(err, sql.ErrNoRows) || (err == nil && !raw.Valid) {
		return snap, nil
	}
	if err != nil {
		return snap, err
	}
	err = json.Unmarshal([]byte(raw.String), &snap)
	return snap, err
}

func (s *SyncService) SnapshotInfo(ctx context.Context) (string, string, bool, error) {
	snap, err := s.Status(ctx)
	return snap.SteamID, snap.Synced, snap.Running, err
}

func (s *SyncService) saveSnapshot(ctx context.Context, snap Snapshot) error {
	raw, err := json.Marshal(snap)
	if err != nil {
		return err
	}
	return s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) VALUES(?,?,?)
			ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, syncKey, string(raw), s.opts.Now().UTC().Format(time.RFC3339Nano))
		return err
	})
}

func (s *SyncService) Recover(ctx context.Context) error {
	snap, err := s.Status(ctx)
	if err != nil || !snap.Running {
		return err
	}
	ok := false
	snap.OK, snap.Running = &ok, false
	snap.Stage, snap.Current, snap.Error = "", "", "上次成就同步因程序退出而中断"
	return s.saveSnapshot(ctx, snap)
}

// flightSnapshot 锁内取当前在飞同步指针（nil = 无在飞）。
func (s *SyncService) flightSnapshot() *syncFlight {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.flight
}

func (s *SyncService) start(ctx context.Context, target string) (Snapshot, *syncFlight, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := s.life.Err(); err != nil {
		return Snapshot{}, nil, err
	}
	if enabled, err := s.moduleEnabled(ctx); err != nil {
		return Snapshot{}, nil, err
	} else if !enabled {
		return Snapshot{}, nil, ErrModuleDisabled
	}
	if s.flight != nil {
		return Snapshot{}, nil, errSyncBusy
	}
	owner, err := s.owner(ctx, target)
	if err != nil {
		return Snapshot{}, nil, err
	}
	if owner.id != "" {
		if id, err := strconv.ParseUint(owner.id, 10, 64); err != nil || id == 0 {
			return Snapshot{}, nil, errSyncOwner
		}
	}
	old, err := s.Status(ctx)
	if err != nil {
		return Snapshot{}, nil, err
	}
	snap := Snapshot{Running: true, Stage: "library", SteamID: owner.id, Started: s.opts.Now().UTC().Format(time.RFC3339Nano), Synced: old.Synced}
	if old.SteamID != "" && old.SteamID != owner.id {
		snap.Synced = ""
	}
	if err := s.saveSnapshot(ctx, snap); err != nil {
		return Snapshot{}, nil, err
	}
	f := &syncFlight{done: make(chan struct{})}
	s.flight = f
	go s.run(owner, snap, f)
	return snap, f, nil
}

func (s *SyncService) run(owner syncOwner, snap Snapshot, f *syncFlight) {
	s.opts.Log("成就同步开始 │ SteamID=%s", owner.id)
	err := s.syncAll(s.life, owner, &snap)
	ok := err == nil
	snap.Running, snap.OK = false, &ok
	snap.Stage, snap.Current = "", ""
	snap.Synced = s.opts.Now().UTC().Format(time.RFC3339Nano)
	if err != nil {
		snap.Error = "成就同步未完成，详见服务日志"
		switch {
		case errors.Is(err, context.Canceled):
			snap.Error = "成就同步已停止"
		case errors.Is(err, errNoCredential), errors.Is(err, errOwnerChanged):
			snap.Error = err.Error()
		}
		s.opts.Log("成就同步未完成 │ SteamID=%s，原因=%v", owner.id, err)
	} else {
		s.opts.Log("成就同步完成 │ SteamID=%s", owner.id)
	}
	finish, cancel := context.WithTimeout(context.WithoutCancel(s.life), 5*time.Second)
	defer cancel()
	if saveErr := s.saveSnapshot(finish, snap); saveErr != nil {
		err = errors.Join(err, saveErr)
		s.opts.Log("成就同步结果未保存 │ SteamID=%s，原因=%v", owner.id, saveErr)
	}
	s.mu.Lock()
	f.err = err
	s.flight = nil
	close(f.done)
	s.mu.Unlock()
}

func (s *SyncService) Warm(sid string) {
	s.mu.Lock()
	now := s.opts.Now()
	if s.life.Err() != nil || s.flight != nil || now.Sub(s.warmed) < 10*time.Minute {
		s.mu.Unlock()
		return
	}
	s.warmed = now
	s.mu.Unlock()
	go func() {
		if _, _, err := s.start(s.life, sid); err != nil && !errors.Is(err, errSyncBusy) && s.life.Err() == nil {
			s.opts.Log("成就后台补拉未启动 │ SteamID=%s，原因=%v", sid, err)
		}
	}()
}

func (s *SyncService) Close(ctx context.Context) error {
	s.cancel()
	s.mu.Lock()
	f := s.flight
	s.mu.Unlock()
	if f != nil {
		select {
		case <-f.done:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	s.opts.Steam.Close()
	return nil
}

func (s *SyncService) RegisterSchedules(ctx context.Context, clock *sched.Scheduler) error {
	return clock.RegisterDaily("achievements_sync", 5, 20, func(*sched.RunCtx) {
		body := func() (map[string]any, error) {
			if enabled, err := s.moduleEnabled(ctx); err != nil {
				return nil, err
			} else if !enabled {
				return map[string]any{"status": "disabled"}, nil
			}
			sid, ready, err := s.Account(ctx, "")
			if err != nil {
				return nil, err
			}
			if !ready {
				s.opts.Log("成就定时同步跳过 │ 无可用账号凭据")
				return map[string]any{"status": "no_credential"}, nil
			}
			_, f, err := s.start(ctx, sid)
			if errors.Is(err, errSyncBusy) {
				return map[string]any{"status": "busy"}, nil
			}
			if err != nil {
				return nil, err
			}
			select {
			case <-f.done:
				return map[string]any{"steamid": sid}, f.err
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		var err error
		if s.opts.Recorder != nil {
			_, err = s.opts.Recorder.Run(ctx, "achievements_sync", body)
		} else {
			_, err = body()
		}
		if err != nil && ctx.Err() == nil {
			s.opts.Log("成就定时同步未完成 │ 原因=%v", err)
		}
	}, sched.WithLabel("成就同步"))
}

func (s *SyncService) Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /api/v1/achievements/sync", func(w http.ResponseWriter, r *http.Request) {
		snap, err := s.Status(r.Context())
		if err != nil {
			s.syncResponse(w, http.StatusInternalServerError, map[string]string{"detail": "成就同步状态暂不可读"})
			return
		}
		s.syncResponse(w, http.StatusOK, snap)
	})
	mux.HandleFunc("POST /api/v1/achievements/sync", func(w http.ResponseWriter, r *http.Request) {
		snap, _, err := s.start(r.Context(), r.URL.Query().Get("steamid"))
		switch {
		case errors.Is(err, ErrModuleDisabled):
			s.syncResponse(w, http.StatusConflict, map[string]string{"detail": err.Error()})
		case errors.Is(err, errSyncBusy):
			s.syncResponse(w, http.StatusConflict, map[string]string{"detail": err.Error()})
		case errors.Is(err, errSyncOwner):
			s.syncResponse(w, http.StatusBadRequest, map[string]string{"detail": err.Error()})
		case err != nil:
			s.opts.Log("成就同步启动失败 │ 原因=%v", err)
			s.syncResponse(w, http.StatusBadGateway, map[string]string{"detail": "成就同步暂时无法启动"})
		default:
			s.syncResponse(w, http.StatusOK, snap)
		}
	})
}

func (s *SyncService) syncResponse(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if err := json.NewEncoder(w).Encode(payload); err != nil {
		s.opts.Log("成就同步响应未发送 │ 原因=%v", err)
	}
}
