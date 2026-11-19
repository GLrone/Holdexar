// Package clashservice 管理订阅内核实例，与池内核实例结构性分离。
package clashservice

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/sched"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const SelectedSubKey = "proxy.selected_clash_sub_id"

type StatusError struct {
	Status int
	Detail string
}

func (e *StatusError) Error() string { return e.Detail }

func statusErr(status int, format string, args ...any) *StatusError {
	return &StatusError{Status: status, Detail: fmt.Sprintf(format, args...)}
}

type KernelRuntime interface {
	Start(exePath, configPath string, opts clashkernel.StartOptions) (clashkernel.Status, error)
	EnsureRunning(exePath, configPath string, opts clashkernel.EnsureRunningOptions) (clashkernel.EnsureRunResult, error)
	Stop() clashkernel.Status
	Status() clashkernel.Status
	RunningSubscriptionIs(subURL string) bool
}

var _ KernelRuntime = (*clashkernel.Runtime)(nil)

type SubscriptionFetch struct {
	Raw      []byte
	Title    string
	Userinfo string
	Nodes    int
}

type Service struct {
	DB         *sql.DB
	Gate       *store.WriteGate
	Kernel     KernelRuntime
	BundledDir string
	DataDir    string

	Now  func() time.Time
	Logf func(format string, args ...any)

	Channels func() []proxypool.Channel
	// FetchOnline 返回 nil 且无错时表示本轮无新快照，调用方回退缓存。
	FetchOnline    func(ctx context.Context, subID int64, subURL string) (*SubscriptionFetch, error)
	VersionProbe   func(exePath string) string
	InstallMirrors []string
	ReleaseAPIURL  string

	FirstCheck func(subID int64, probeAll bool)

	// TestLock 检测串行锁：切换热重载与节点检测共用，防并发打断污染账本。
	TestLock sync.Mutex

	testMu      sync.Mutex
	testSession *testSessionSnapshot

	SteamProbe        func(ctx context.Context, port int) (ok bool, status int)
	SystemProxy       func(ctx context.Context) (string, error)
	ExitIPProbe       func(ctx context.Context, port int) (ip string)
	PortListeningFn   func(port int) bool
	LocalFallback     func() int
	ControllerFactory func(baseURL, secret string) clashTestController

	progress progressStore
}

func New(db *sql.DB, gate *store.WriteGate, kernel *clashkernel.Runtime, bundledDir, dataDir string) *Service {
	svc := &Service{DB: db, Gate: gate, Kernel: kernel, BundledDir: bundledDir, DataDir: dataDir}
	svc.FirstCheck = svc.SpawnFirstCheck
	return svc
}

func (s *Service) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func (s *Service) logf(format string, args ...any) {
	if s.Logf != nil {
		s.Logf(format, args...)
	}
}

func (s *Service) RememberSelectedSub(ctx context.Context, subID int64) error {
	return s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, SelectedSubKey, subID)
	})
}

func (s *Service) SelectedSubID(ctx context.Context) *int64 {
	var raw sql.NullString
	err := s.DB.QueryRowContext(ctx,
		`SELECT CAST(json_extract(value_json,'$') AS TEXT) FROM app_settings WHERE key=?`,
		SelectedSubKey).Scan(&raw)
	if err != nil || !raw.Valid {
		return nil
	}
	id, perr := strconv.ParseInt(strings.TrimSpace(raw.String), 10, 64)
	if perr != nil {
		return nil
	}
	return &id
}

func (s *Service) recordEvent(ctx context.Context, kind, target, proxyLabel string) {
	ts := s.now().In(sched.DefaultLoc).Format("2006-01-02 15:04:05.000000")
	err := s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx,
			`INSERT INTO proxy_events(ts,kind,target,proxy_label) VALUES (?,?,?,?)`,
			ts, kind, target, proxyLabel)
		return err
	})
	if err != nil {
		s.logf("记录走线日志失败：%v", err)
	}
}

// MarkImported 整体替换 last_stats 写入导入流量与节点数。
func (s *Service) MarkImported(ctx context.Context, subID int64, traffic string, nodes *int, cached bool) error {
	payload := map[string]any{"traffic": nil, "nodes": nodes, "cached": cached}
	if traffic != "" {
		payload["traffic"] = traffic
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	return s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		_, err := conn.ExecContext(ctx,
			`UPDATE proxy_subscriptions SET last_imported_at=?, last_stats=? WHERE id=?`,
			s.now().In(sched.DefaultLoc).Format("2006-01-02 15:04:05.000000"), string(raw), subID)
		return err
	})
}

// ApplySubscriptionNameChain 名称优先级：用户改名 > 机场名 > 面板同步值（用户命名锁死）。
func (s *Service) ApplySubscriptionNameChain(ctx context.Context, subID int64, panelName string) (string, error) {
	var out string
	err := s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		var label, statsRaw sql.NullString
		err := conn.QueryRowContext(ctx,
			`SELECT label, CAST(last_stats AS TEXT) FROM proxy_subscriptions WHERE id=?`, subID).
			Scan(&label, &statsRaw)
		if errors.Is(err, sql.ErrNoRows) {
			return nil
		}
		if err != nil {
			return err
		}
		stats := map[string]any{}
		_ = json.Unmarshal([]byte(statsRaw.String), &stats)
		source, _ := stats["labelSource"].(string)
		if source != "user" && source != "panel" {
			source = ""
		}
		current := label.String
		name := truncateRunes(strings.TrimSpace(panelName), 100)
		if current != "" && source == "user" {
			out = current
			return nil
		}
		if current != "" && source == "" && (name == "" || name != current) {
			out = current
			return nil
		}
		if name != "" && name != current {
			stats["labelSource"] = "panel"
			raw, merr := json.Marshal(stats)
			if merr != nil {
				return merr
			}
			if _, merr := conn.ExecContext(ctx,
				`UPDATE proxy_subscriptions SET label=?, last_stats=? WHERE id=?`,
				name, string(raw), subID); merr != nil {
				return merr
			}
			out = name
			return nil
		}
		if name != "" && name == current && source != "panel" {
			stats["labelSource"] = "panel"
			raw, merr := json.Marshal(stats)
			if merr != nil {
				return merr
			}
			if _, merr := conn.ExecContext(ctx,
				`UPDATE proxy_subscriptions SET last_stats=? WHERE id=?`, string(raw), subID); merr != nil {
				return merr
			}
		}
		out = current
		return nil
	})
	return out, err
}

func truncateRunes(text string, limit int) string {
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	return string(runes[:limit])
}

func briefError(err error) string {
	text := strings.Join(strings.Fields(err.Error()), " ")
	if len([]rune(text)) <= 160 {
		return text
	}
	return string([]rune(text)[:157]) + "…"
}
