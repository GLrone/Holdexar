// 启动候选链与启动/切换/点选/停止。候选淘汰仅当「下载不了且无缓存」——
// 订阅链接失效不该连累内核起不来，节点好不好交给首检与废弃判定。
package clashservice

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/settings"
	"holdexar/server-go/internal/store"
)

const (
	portModeKey = "proxy.clash_port_mode"
	portKey     = "proxy.clash_port"
)

// ClashSubscription 订阅行投影（URL 为明文，库内专用不进响应体）。
type ClashSubscription struct {
	ID               int64
	Kind             string
	URL              string
	Label            *string
	Deprecated       bool
	DeprecatedReason *string
}

func (sub *ClashSubscription) deprecatedReasonOr() string {
	if sub.DeprecatedReason != nil && *sub.DeprecatedReason != "" {
		return *sub.DeprecatedReason
	}
	return "不可用节点超过 95%"
}

// listClashSubscriptions kind=clash 的订阅清单（reveal 语义 + 废弃投影：
// deprecated 列或资源封锁判定任一命中即废弃）。
func (s *Service) listClashSubscriptions(ctx context.Context) ([]ClashSubscription, error) {
	rows, err := s.DB.QueryContext(ctx, `SELECT id, kind, url, label, CAST(last_stats AS TEXT),
		COALESCE(deprecated,0), deprecated_reason FROM proxy_subscriptions WHERE kind='clash' ORDER BY id`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []ClashSubscription{}
	now := s.now()
	for rows.Next() {
		var sub ClashSubscription
		var storedURL string
		var stats sql.NullString
		var label sql.NullString
		var deprecated bool
		var reason sql.NullString
		if err := rows.Scan(&sub.ID, &sub.Kind, &storedURL, &label, &stats, &deprecated, &reason); err != nil {
			return nil, err
		}
		if label.Valid && label.String != "" {
			sub.Label = &label.String
		}
		sub.URL = proxypool.OpenURL(s.DataDir, storedURL)
		if block := proxypool.SubscriptionBlockReason(stats.String, now); block != "" || deprecated {
			sub.Deprecated = true
			if block != "" {
				sub.DeprecatedReason = &block
			} else if reason.Valid && reason.String != "" {
				sub.DeprecatedReason = &reason.String
			}
		}
		out = append(out, sub)
	}
	return out, rows.Err()
}

func findSub(subs []ClashSubscription, id int64) *ClashSubscription {
	for i := range subs {
		if subs[i].ID == id {
			return &subs[i]
		}
	}
	return nil
}

// ─── 端口选项（fixed 存档口 / 在跑沿用现口 / 动态分配三态）────

func (s *Service) writeKV(ctx context.Context, key string, value any) {
	err := s.Gate.WithWrite(ctx, store.PriorityInteractive, func(conn *sql.Conn) error {
		return settings.WriteValue(ctx, conn, key, value)
	})
	if err != nil {
		s.logf("设置写回失败（%s）：%v", key, err)
	}
}

// kernelPortConfig 归一化端口模式（无效存档口回落 auto 并写回）。
func (s *Service) kernelPortConfig(ctx context.Context) (mode string, savedPort int, portOK bool, err error) {
	var modeRaw, portRaw sql.NullString
	err = s.DB.QueryRowContext(ctx, `SELECT
			(SELECT CAST(json_extract(value_json,'$') AS TEXT) FROM app_settings WHERE key=?),
			(SELECT CAST(json_extract(value_json,'$') AS TEXT) FROM app_settings WHERE key=?)`,
		portModeKey, portKey).Scan(&modeRaw, &portRaw)
	if err != nil {
		return "", 0, false, err
	}
	if modeRaw.Valid {
		mode = strings.TrimSpace(modeRaw.String)
	}
	if portRaw.Valid && portRaw.String != "" {
		if parsed, perr := strconv.Atoi(strings.TrimSpace(portRaw.String)); perr == nil {
			savedPort, portOK = parsed, true
		}
	}
	if mode != "auto" && mode != "fixed" {
		if portOK {
			mode = "fixed"
		} else {
			mode = "auto"
		}
		s.writeKV(ctx, portModeKey, mode)
	}
	if portOK && (savedPort < 1 || savedPort > 65535) {
		portOK, savedPort = false, 0
	}
	if mode == "fixed" && !portOK {
		mode = "auto"
		s.writeKV(ctx, portModeKey, mode)
	}
	return mode, savedPort, portOK, nil
}

// KernelPortOptions 内核启动的端口选项：fixed 模式用存档口；在跑沿用现口；
// 否则动态分配（allocate 只在未运行时出现，与 Python 调用面等价）。
func (s *Service) KernelPortOptions(ctx context.Context) (mixedPort int, allocate bool, err error) {
	mode, savedPort, portOK, err := s.kernelPortConfig(ctx)
	if err != nil {
		return 0, false, err
	}
	if mode == "fixed" && portOK {
		return savedPort, false, nil
	}
	status := s.Kernel.Status()
	if status.Running && status.Port > 0 {
		return status.Port, false, nil
	}
	return 0, true, nil
}

// ─── 启动候选链 ──────────────────────────────────────────────

// AttemptRecord 被淘汰候选的失败原因。
type AttemptRecord struct {
	ID    int64  `json:"id"`
	Label string `json:"label"`
	Error string `json:"error"`
}

// StartableClash 选出的「现在就能把内核拉起来」的候选。
type StartableClash struct {
	Subscription ClashSubscription
	ConfigPath   string // 项目生成的运行配置（交给内核的）
	RawPath      string // 原文缓存路径（来源证据）
	Title        string
	Userinfo     string
	Nodes        int
	Cached       bool
	Attempts     []AttemptRecord
}

// ResolveStartableClash 挑一条现在就能用的订阅。候选序：请求的那条在前，
// 其余非废弃按新→旧倒序；无显式请求时吃落库的「最近一次显式选中」按请求对待。
// 在线取到的配置优先于缓存回退（无指定订阅时缓存命中留作兜底继续找）；
// 显式指定的订阅命中即返回（尊重用户选择）。
func (s *Service) ResolveStartableClash(ctx context.Context, requestedID *int64, snapshotOnly bool) (*StartableClash, error) {
	subs, err := s.listClashSubscriptions(ctx)
	if err != nil {
		return nil, err
	}
	usable := make([]ClashSubscription, 0, len(subs))
	for _, sub := range subs {
		if !sub.Deprecated {
			usable = append(usable, sub)
		}
	}
	if len(usable) == 0 {
		return nil, statusErr(400, "没有可用的 Clash 订阅（全部已废弃或尚未添加），请在订阅区手动删除或更换订阅")
	}
	if requestedID == nil {
		requestedID = s.SelectedSubID(ctx)
	}
	ordered := make([]ClashSubscription, 0, len(usable))
	if requestedID != nil {
		for _, sub := range usable {
			if sub.ID == *requestedID {
				ordered = append(ordered, sub)
			}
		}
		for i := len(usable) - 1; i >= 0; i-- {
			if usable[i].ID != *requestedID {
				ordered = append(ordered, usable[i])
			}
		}
	} else {
		for i := len(usable) - 1; i >= 0; i-- {
			ordered = append(ordered, usable[i])
		}
	}
	deferCached := requestedID == nil
	attempts := []AttemptRecord{}
	var cachedFallback *StartableClash
	for _, sub := range ordered {
		entry, ferr := s.prepareCandidate(ctx, sub, snapshotOnly)
		if ferr != nil {
			attempt := AttemptRecord{ID: sub.ID, Error: briefError(ferr)}
			if sub.Label != nil {
				attempt.Label = *sub.Label
			}
			attempts = append(attempts, attempt)
			s.logf("启动候选订阅 %d 不可用，换下一个候选 │ 原因=%s", sub.ID, ferr)
			continue
		}
		entry.Attempts = attempts
		if deferCached && entry.Cached {
			if cachedFallback == nil {
				cachedFallback = entry
				s.logf("启动候选订阅 %d 只能读到本地缓存，留作兜底并继续找能在线取到的候选 │ 名称=%s", sub.ID, sub.Label)
			}
			continue
		}
		return entry, nil
	}
	if cachedFallback != nil {
		return cachedFallback, nil
	}
	detail := make([]string, 0, len(attempts))
	for _, a := range attempts {
		label := a.Label
		if label == "" {
			label = fmt.Sprintf("id=%d", a.ID)
		}
		detail = append(detail, fmt.Sprintf("%s：%s", label, a.Error))
	}
	return nil, statusErr(400, "所有可用订阅都取不到配置（%s），请更换订阅链接后重试", strings.Join(detail, "；"))
}

// prepareCandidate 单候选取配置：在线下载优先、缓存兜底，再渲染运行配置。
func (s *Service) prepareCandidate(ctx context.Context, sub ClashSubscription, snapshotOnly bool) (*StartableClash, error) {
	cachePath := SubscriptionConfigPath(s.DataDir, sub.ID)
	var fetch *SubscriptionFetch
	cached := false
	if snapshotOnly {
		restored, rerr := s.FetchSubscriptionToCache(ctx, sub.ID, sub.URL)
		if rerr != nil {
			// 库内无成功快照：回落读缓存正文（snapshot_only 的本地文件分支）
			raw, ferr := os.ReadFile(cachePath)
			nodes, nerr := countSubscriptionNodes(raw)
			if ferr != nil || nerr != nil {
				return nil, fmt.Errorf("没有可恢复的成功快照或有效订阅缓存")
			}
			restored = &SubscriptionFetch{Raw: raw, Title: subscriptionAutoNameFromURL(sub.URL), Nodes: nodes}
		}
		fetch, cached = restored, true
	} else {
		var err error
		fetch, cached, err = s.fetchToCache(ctx, sub)
		if err != nil {
			return nil, err
		}
	}
	runtimePath, err := MaterializeSubscriptionConfig(cachePath)
	if err != nil {
		return nil, err
	}
	return &StartableClash{
		Subscription: sub, ConfigPath: runtimePath, RawPath: cachePath,
		Title: fetch.Title, Userinfo: fetch.Userinfo, Nodes: fetch.Nodes, Cached: cached,
	}, nil
}

// fetchToCache 在线拉取并写缓存正文；在线失败或本轮无新快照时回落库内成功快照。
// 返回 cached=是否走的缓存兜底。
func (s *Service) fetchToCache(ctx context.Context, sub ClashSubscription) (*SubscriptionFetch, bool, error) {
	fetched, err := s.fetch(ctx, sub.ID, sub.URL)
	if err == nil && fetched != nil {
		if ferr := atomicWriteFile(SubscriptionConfigPath(s.DataDir, sub.ID), fetched.Raw); ferr != nil {
			return nil, false, ferr
		}
		if fetched.Title == "" {
			fetched.Title = subscriptionAutoNameFromURL(sub.URL)
		}
		return fetched, false, nil
	}
	restored, rerr := s.FetchSubscriptionToCache(ctx, sub.ID, sub.URL)
	if rerr != nil {
		if err != nil {
			return nil, false, err // 在线失败原因优先（对齐 sync.failures 文案）
		}
		return nil, false, rerr
	}
	return restored, true, nil
}

func (s *Service) fetch(ctx context.Context, subID int64, subURL string) (*SubscriptionFetch, error) {
	if s.FetchOnline != nil {
		return s.FetchOnline(ctx, subID, subURL)
	}
	return s.fetchOnlineDefault(ctx, subID, subURL)
}

// fetchOnlineDefault 默认在线拉取：proxypool.Syncer.SyncOne（闸外并发抓取 + 闸内落库）。
func (s *Service) fetchOnlineDefault(ctx context.Context, subID int64, subURL string) (*SubscriptionFetch, error) {
	var stored string
	err := s.DB.QueryRowContext(ctx, `SELECT url FROM proxy_subscriptions WHERE id=?`, subID).Scan(&stored)
	if err != nil || proxypool.OpenURL(s.DataDir, stored) != subURL {
		return nil, statusErr(400, "订阅地址与缓存归属不一致")
	}
	syncer := proxypool.Syncer{DB: s.DB, Gate: s.Gate, DataDir: s.DataDir}
	if s.Channels != nil {
		syncer.Channels = s.Channels()
	}
	result, err := syncer.SyncOne(ctx, subID)
	if err != nil {
		return nil, err
	}
	if msg := result.Failures[subID]; msg != "" {
		return nil, fmt.Errorf("%s", msg)
	}
	snapshot := result.Snapshots[subID]
	if snapshot == nil {
		return nil, nil // 本轮没有产生新快照 → 调用方走缓存兜底
	}
	return &SubscriptionFetch{
		Raw: snapshot.RawContent, Title: snapshot.ProfileTitle,
		Userinfo: snapshot.Userinfo, Nodes: snapshot.NodeCount,
	}, nil
}

func countSubscriptionNodes(raw []byte) (int, error) {
	format, err := proxypool.DetectFormat(raw)
	if err != nil {
		return 0, err
	}
	nodes, err := proxypool.ParseNodes(raw, format)
	if err != nil {
		return 0, err
	}
	return len(nodes), nil
}

// ─── 启动 / 切换 / 点选 / 停止 ────────────────────────────────

// ensureSubKernelRunning 确保订阅内核跑着指定配置（对齐 Python ensure_running 的
// inject_lanes 缺省 True）：未运行经 Start 显式 InjectLanes=true（EnsureRunningOptions
// 无该位、池路径零值即所需，订阅内核不能漏）；在跑走 EnsureRunning 热重载
// （runningKernelText 恒注入 lane）。端口选项三态保证 allocate 只在未运行时出现。
func (s *Service) ensureSubKernelRunning(exePath, configPath, subURL string, mixedPort int, allocate bool) (clashkernel.EnsureRunResult, error) {
	if !s.Kernel.Status().Running {
		status, err := s.Kernel.Start(exePath, configPath, clashkernel.StartOptions{
			SubscriptionURL: subURL, MixedPort: mixedPort, AllocateMixedPort: allocate,
			InjectLanes: true, BundledDir: s.BundledDir,
		})
		return clashkernel.EnsureRunResult{Status: status, Started: err == nil}, err
	}
	return s.Kernel.EnsureRunning(exePath, configPath, clashkernel.EnsureRunningOptions{
		SubscriptionURL: subURL, MixedPort: mixedPort, AllocateMixedPort: allocate,
	})
}

// StartSubRef 启动响应里实际拉起内核的订阅。
type StartSubRef struct {
	ID    int64   `json:"id"`
	Label *string `json:"label"`
}

// AttemptRef 非空 = 请求的订阅取不到配置，已降级到其他订阅启动。
type AttemptRef struct {
	ID    int64   `json:"id"`
	Label *string `json:"label"`
}

// StartResult POST /clash/start 响应（status + started/reloaded + 启动扩展）。
type StartResult struct {
	BaseStatus
	Started     bool        `json:"started"`
	Reloaded    bool        `json:"reloaded"`
	Nodes       *int        `json:"nodes"`
	UsedCache   bool        `json:"usedCache"`
	Subscription StartSubRef `json:"subscription"`
	FallbackFrom *AttemptRef `json:"fallbackFrom"`
}

// StartClash 启动内核全链：候选链取配置 → 拉起/热重载 → 名称链/导入回填/
// 选中落库/走线日志/首检挂点。
func (s *Service) StartClash(ctx context.Context, requestedID *int64) (*StartResult, error) {
	detect := s.DetectKernel()
	if !detect.Found {
		return nil, statusErr(400, "未找到内核，请先安装")
	}
	subs, err := s.listClashSubscriptions(ctx)
	if err != nil {
		return nil, err
	}
	if len(subs) == 0 {
		return nil, statusErr(400, "尚未保存 Clash 订阅链接，请先添加")
	}
	if requestedID != nil {
		sub := findSub(subs, *requestedID)
		if sub == nil {
			return nil, statusErr(404, "指定的订阅不存在")
		}
		if sub.Deprecated {
			return nil, statusErr(400, "该订阅已废弃（%s），后端不再使用；如需恢复请先检测节点确认恢复达标，或手动删除该订阅", sub.deprecatedReasonOr())
		}
	}
	picked, err := s.ResolveStartableClash(ctx, requestedID, false)
	if err != nil {
		return nil, err
	}
	mixedPort, allocate, err := s.KernelPortOptions(ctx)
	if err != nil {
		return nil, err
	}
	result, err := s.ensureSubKernelRunning(detectPath(detect), picked.ConfigPath, picked.Subscription.URL, mixedPort, allocate)
	if err != nil {
		return nil, statusErr(502, "内核启动失败: %v", err)
	}
	subID := picked.Subscription.ID
	if _, err := s.ApplySubscriptionNameChain(ctx, subID, picked.Title); err != nil {
		return nil, err
	}
	if picked.Userinfo != "" {
		if err := s.MarkImported(ctx, subID, picked.Userinfo, &picked.Nodes, picked.Cached); err != nil {
			return nil, err
		}
	}
	if err := s.RememberSelectedSub(ctx, subID); err != nil {
		return nil, err
	}
	s.recordEvent(ctx, "clash", "subscription", fmt.Sprintf("clash:%d", result.Status.Port))
	if s.FirstCheck != nil {
		s.FirstCheck(subID, true)
	}
	out := &StartResult{
		BaseStatus:   baseStatus(result.Status),
		Started:      result.Started,
		Reloaded:     result.Reloaded,
		Nodes:        &picked.Nodes,
		UsedCache:    picked.Cached,
		Subscription: StartSubRef{ID: subID, Label: picked.Subscription.Label},
	}
	if len(picked.Attempts) > 0 {
		first := picked.Attempts[0]
		ref := &AttemptRef{ID: first.ID}
		if first.Label != "" {
			ref.Label = &first.Label
		}
		out.FallbackFrom = ref
	}
	return out, nil
}

// SwitchResult POST /clash/switch 响应。
type SwitchResult struct {
	BaseStatus
	Switched bool `json:"switched"`
	Started  bool `json:"started"`
	Reloaded bool `json:"reloaded"`
}

// SwitchClash 切换运行中内核的订阅：有缓存直接热重载（秒级），无缓存先下载；
// 热重载持检测串行锁——打断首检会对着新配置探测、整轮误判污染账本。
func (s *Service) SwitchClash(ctx context.Context, subscriptionID int64) (*SwitchResult, error) {
	status := s.Kernel.Status()
	if !status.Running {
		return nil, statusErr(400, "Clash 未运行：点「启动」拉起内核，启动时将使用选中的订阅")
	}
	subs, err := s.listClashSubscriptions(ctx)
	if err != nil {
		return nil, err
	}
	sub := findSub(subs, subscriptionID)
	if sub == nil {
		return nil, statusErr(404, "指定的订阅不存在")
	}
	if sub.Deprecated {
		return nil, statusErr(400, "该订阅已废弃（%s），后端不再使用", sub.deprecatedReasonOr())
	}
	if s.Kernel.RunningSubscriptionIs(sub.URL) {
		return &SwitchResult{BaseStatus: baseStatus(status)}, nil
	}
	detect := s.DetectKernel()
	if !detect.Found {
		return nil, statusErr(400, "未找到内核，请先安装")
	}
	cachePath := SubscriptionConfigPath(s.DataDir, sub.ID)
	if !fileExists(cachePath) {
		if _, _, err := s.fetchToCache(ctx, *sub); err != nil {
			return nil, err
		}
	}
	runtimePath, err := MaterializeSubscriptionConfig(cachePath)
	if err != nil {
		return nil, err
	}
	s.TestLock.Lock()
	defer s.TestLock.Unlock()
	mixedPort, allocate, err := s.KernelPortOptions(ctx)
	if err != nil {
		return nil, err
	}
	result, err := s.ensureSubKernelRunning(detectPath(detect), runtimePath, sub.URL, mixedPort, allocate)
	if err != nil {
		var rejected *clashkernel.ReloadRejectedError
		if errors.As(err, &rejected) {
			return nil, statusErr(400, "%s", err.Error())
		}
		return nil, statusErr(502, "切换失败: %v", err)
	}
	if s.FirstCheck != nil {
		s.FirstCheck(sub.ID, true)
	}
	if err := s.RememberSelectedSub(ctx, sub.ID); err != nil {
		return nil, err
	}
	return &SwitchResult{
		BaseStatus: baseStatus(result.Status), Switched: true,
		Started: result.Started, Reloaded: result.Reloaded,
	}, nil
}

// SelectClash 内核未运行时记录订阅行点选（跨页面与重启保留，启动缺省用它）。
func (s *Service) SelectClash(ctx context.Context, subscriptionID int64) (int64, error) {
	subs, err := s.listClashSubscriptions(ctx)
	if err != nil {
		return 0, err
	}
	if findSub(subs, subscriptionID) == nil {
		return 0, statusErr(404, "指定的订阅不存在")
	}
	if err := s.RememberSelectedSub(ctx, subscriptionID); err != nil {
		return 0, err
	}
	return subscriptionID, nil
}

// StopClash 停止内核并清运行态账目（configPath 保留）。
func (s *Service) StopClash() BaseStatus {
	return baseStatus(s.Kernel.Stop())
}

func detectPath(detect KernelInfo) string {
	if detect.Path != nil {
		return *detect.Path
	}
	return ""
}
