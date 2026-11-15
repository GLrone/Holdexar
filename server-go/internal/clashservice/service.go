// Package clashservice Clash 订阅内核控制面（库层）：状态/安装/启动候选链/切换/点选/停止。
// 订阅内核实例由持有方传入，与池内核（serverd proxySchedule.kernel）结构性分离——
// 共用实例会让一次池重建停掉订阅内核，隔离必须是结构性的。
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

// SelectedSubKey 用户最近一次显式选中的 Clash 订阅（点选/切换/启动成功都落这里）：
// 内核未运行时跨页面与重启保留点选，启动缺省用它，前端回显同源。
const SelectedSubKey = "proxy.selected_clash_sub_id"

// StatusError 携带 HTTP 语义的错误（http 层直接映射成 detail 响应）。
type StatusError struct {
	Status int    // 400 / 404 / 502
	Detail string // 用户语言文案
}

func (e *StatusError) Error() string { return e.Detail }

func statusErr(status int, format string, args ...any) *StatusError {
	return &StatusError{Status: status, Detail: fmt.Sprintf(format, args...)}
}

// KernelRuntime Service 对内核实例的全部操作面（*clashkernel.Runtime 天然满足；
// 测试注入桩实现，不真拉内核进程）。
type KernelRuntime interface {
	Start(exePath, configPath string, opts clashkernel.StartOptions) (clashkernel.Status, error)
	EnsureRunning(exePath, configPath string, opts clashkernel.EnsureRunningOptions) (clashkernel.EnsureRunResult, error)
	Stop() clashkernel.Status
	Status() clashkernel.Status
	RunningSubscriptionIs(subURL string) bool
}

var _ KernelRuntime = (*clashkernel.Runtime)(nil)

// SubscriptionFetch 单订阅一次取回的产物（原文来自成功快照的 raw_content）。
type SubscriptionFetch struct {
	Raw      []byte // 订阅原文（来源证据 + 离线缓存正文）
	Title    string // 机场面板名
	Userinfo string // 流量头（subscription-userinfo 原文）
	Nodes    int    // 节点数
}

// Service Clash 订阅内核控制面。DB/出网/版本探测全部可注入以便测试。
type Service struct {
	DB     *sql.DB
	Gate   *store.WriteGate
	Kernel KernelRuntime // 订阅内核实例（与池内核分离的第二个 Runtime）
	BundledDir string    // 随包内核资产目录（assets/clash）
	DataDir    string

	Now  func() time.Time
	Logf func(format string, args ...any)

	// Channels 订阅在线拉取通道（serverd 装：池 lane + 系统代理 + 直连；nil=直连）。
	Channels func() []proxypool.Channel
	// FetchOnline 单订阅在线拉取（nil 时用 proxypool.Syncer.SyncOne 默认实现；
	// 返回 nil fetch 且无错 = 本轮没有产生新快照，调用方走缓存兜底）。
	FetchOnline func(ctx context.Context, subID int64, subURL string) (*SubscriptionFetch, error)
	// VersionProbe 内核版本探测（默认 clashkernel.KernelVersion；测试注入可控输出）。
	VersionProbe func(exePath string) string
	// InstallMirrors 内核下载镜像链（nil = clashkernel.DefaultMirrors；测试注入本地桩）。
	InstallMirrors []string

	// FirstCheck 启动/切换成功后的首检挂点（New 默认接线 SpawnFirstCheck；
	// 显式置 nil 可关掉）。
	FirstCheck func(subID int64, probeAll bool)

	// TestLock 检测串行锁：切换的热重载持锁，防打断首检误判污染账本
	// （手动检测/定时体检落地后与其共用）。
	TestLock sync.Mutex

	// ── 节点检测链（test.go）：会话存储 + 出网可注入面（nil = 生产默认）──
	testMu      sync.Mutex
	testSession *testSessionSnapshot

	// SteamProbe 经指定本地代理端口的业务探针（200 且 JSON 对象才算活）。
	SteamProbe func(ctx context.Context, port int) (ok bool, status int)
	// SystemProxy 系统代理解析（nil = netenv.ResolveSystemProxy；测试注入桩，
	// 全局 resolver 的 30s 缓存与注册表读取不可在单测内直接操控）。
	SystemProxy func(ctx context.Context) (string, error)
	// ExitIPProbe 经指定本地代理端口的出口 IP 探测。
	ExitIPProbe func(ctx context.Context, port int) (ip string)
	// PortListeningFn lane 端口在听预检（默认 clashkernel.PortListening）。
	PortListeningFn func(port int) bool
	// ControllerFactory 内核控制器客户端构造（测试注入桩）。
	ControllerFactory func(baseURL, secret string) clashTestController

	progress progressStore
}

// New 生产构造：订阅内核实例与池内核必须是两个 Runtime。
func New(db *sql.DB, gate *store.WriteGate, kernel *clashkernel.Runtime, bundledDir, dataDir string) *Service {
	svc := &Service{DB: db, Gate: gate, Kernel: kernel, BundledDir: bundledDir, DataDir: dataDir}
	svc.FirstCheck = svc.SpawnFirstCheck // 首检挂点默认接线（复用窗口内跳过）
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

// ─── selected KV（跨重启的点选事实源）─────────────────────────

// RememberSelectedSub 记录用户最近一次显式选中（点选/切换/启动成功共用）。
func (s *Service) RememberSelectedSub(ctx context.Context, subID int64) error {
	return s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, SelectedSubKey, subID)
	})
}

// SelectedSubID 读回最近一次显式选中；未落库或值不合法返回 nil。
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

// ─── 走线日志 / 导入回填 / 名称三档链 ─────────────────────────

// recordEvent 走线日志（失败只记日志，不影响主流程）。
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

// MarkImported 启动顺手回填流量/节点数（last_stats 整体替换，对齐 Python mark_imported）。
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

// ApplySubscriptionNameChain 名称三档链（用户改名 > 机场名 > 缓存上次值）。
// 用户亲手动过的名字锁死；机场改名跟随；来源标记落 last_stats.labelSource。
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
		// ① 用户命名：锁死，任何机场名都不许覆盖（含老数据按「名字≠机场当前名」归位）
		if current != "" && source == "user" {
			out = current
			return nil
		}
		if current != "" && source == "" && (name == "" || name != current) {
			out = current
			return nil
		}
		// ② 本次机场名：用户没改过就用它（含机场自己改名的情况）
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
		// ②' 名字与机场名一致：补 panel 标记（历史行归位）
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

// ─── 小工具 ──────────────────────────────────────────────────

func truncateRunes(text string, limit int) string {
	runes := []rune(text)
	if len(runes) <= limit {
		return text
	}
	return string(runes[:limit])
}

// briefError 给用户看的失败原因一句话：剥掉多行 traceback 尾巴，超 160 字截断。
func briefError(err error) string {
	text := strings.Join(strings.Fields(err.Error()), " ")
	if len([]rune(text)) <= 160 {
		return text
	}
	return string([]rune(text)[:157]) + "…"
}
