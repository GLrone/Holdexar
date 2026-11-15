// adjacency.go 生命周期邻接链：订阅保存前自动装内核、新订阅即时生效链、
// 服务自启（对齐 Python add_subscription / router._effectuate_new_subscription /
// ensure_clash_running）。全部 fail-soft：生效链失败不影响保存，自启失败不影响服务。
package clashservice

import (
	"context"
	"errors"
	"fmt"

	"holdexar/server-go/internal/clashkernel"
	"holdexar/server-go/internal/proxypool"
)

// EnsureKernelForAdd 订阅保存前置：kind=clash 且内核缺失 → 自动安装（随包资产
// 优先，缺失才网络下载，进度可轮询）；安装失败拒绝保存（对齐 add_subscription
// 的内核前置分支）。
func (s *Service) EnsureKernelForAdd(ctx context.Context) error {
	if s.DetectKernel().Found {
		return nil
	}
	result := s.InstallKernel(ctx)
	if !result.OK {
		return statusErr(400, "内核安装失败：%s；可稍后重试，或手动放置 mihomo 内核到 data/clash/",
			deref(result.Error))
	}
	return nil
}

// EffectuateNewSubscription 新订阅即时生效链：未运行 → 用本地缓存热重载/拉起 +
// 记住选中 + 走线日志 + 首检。每一步 fail-soft：添加本身已成功，任何一环失败都
// 记日志留给 30min 同步拍 / 手动启动兜底。返回追加进响应的额外键（warning /
// kernelStarted）。
func (s *Service) EffectuateNewSubscription(ctx context.Context, subID int64) map[string]any {
	extra := map[string]any{}
	subURL, ok := s.subscriptionURL(ctx, subID)
	if !ok {
		return extra
	}
	detect := s.DetectKernel()
	if !detect.Found {
		extra["warning"] = "订阅已保存，但内核未安装，无法自动启动体检"
		return extra
	}
	status := s.Kernel.Status()
	if !status.Running || !s.Kernel.RunningSubscriptionIs(subURL) {
		cachePath := SubscriptionConfigPath(s.DataDir, subID)
		if !fileExists(cachePath) {
			// Go 订阅同步只落库快照不写缓存文件（Python 侧由 add 链的
			// download_subscription target_path 落缓存）——这里从最近快照
			// 物化缓存（离线，不重拉），语义等价。
			if _, err := s.FetchSubscriptionToCache(ctx, subID, subURL); err != nil {
				s.logf("订阅 %d 自动启动内核失败，留待手动启动或重启自启 │ 订阅=%d 原因=%v", subID, subID, err)
				extra["warning"] = fmt.Sprintf("订阅已保存，但自动启动内核失败：%v（可稍后手动启动）", err)
				return extra
			}
		}
		runtimePath, err := MaterializeSubscriptionConfig(cachePath)
		if err != nil {
			s.logf("订阅 %d 自动启动内核失败，留待手动启动或重启自启 │ 订阅=%d 原因=%v", subID, subID, err)
			extra["warning"] = fmt.Sprintf("订阅已保存，但自动启动内核失败：%v（可稍后手动启动）", err)
			return extra
		}
		mixedPort, allocate, err := s.KernelPortOptions(ctx)
		if err != nil {
			s.logf("订阅 %d 自动启动内核失败，留待手动启动或重启自启 │ 订阅=%d 原因=%v", subID, subID, err)
			extra["warning"] = fmt.Sprintf("订阅已保存，但自动启动内核失败：%v（可稍后手动启动）", err)
			return extra
		}
		s.TestLock.Lock()
		ensure, err := s.ensureSubKernelRunning(deref(detect.Path), runtimePath, subURL, mixedPort, allocate)
		s.TestLock.Unlock()
		if err != nil {
			s.logf("订阅 %d 自动启动内核失败，留待手动启动或重启自启 │ 订阅=%d 原因=%v", subID, subID, err)
			extra["warning"] = fmt.Sprintf("订阅已保存，但自动启动内核失败：%v（可稍后手动启动）", err)
			return extra
		}
		if err := s.RememberSelectedSub(ctx, subID); err != nil {
			s.logf("订阅 %d 选中记录暂未保存，不影响生效 │ 订阅=%d 原因=%v", subID, subID, err)
		}
		s.recordEvent(ctx, "clash", "subscription", fmt.Sprintf("clash:%d", ensure.Status.Port))
		extra["kernelStarted"] = true
	}
	if s.Kernel.Status().Running {
		s.FirstCheck(subID, true)
	}
	return extra
}

// EnsureClashRunning 服务自启：按自启设置和本地订阅快照确保订阅内核运行
// （snapshot_only 模式只用本地缓存，不在线拉取）。自启失败不阻塞服务。
func (s *Service) EnsureClashRunning(ctx context.Context, snapshotOnly bool) {
	defer func() {
		if failure := recover(); failure != nil {
			s.logf("Clash 内核自启失败，不影响服务运行 │ 原因=%v", failure)
		}
	}()
	if s.Kernel.Status().Running {
		return
	}
	if !s.kvBool(ctx, "proxy.autostart", true) {
		s.logf("内核自启开关已关闭，跳过 Clash 内核自启")
		return
	}
	if _, err := clashkernel.EnsureKernel(s.BundledDir, s.DataDir); err != nil {
		// 随包资产缺失不算失败：本机已有内核照样能自启
		s.logf("随包内核资产不可用（%v），按本机现存内核继续", err)
	}
	detect := s.DetectKernel()
	if !detect.Found {
		s.logf("本机没有 Clash 内核，跳过内核自启")
		return
	}
	subs, err := s.listClashSubscriptions(ctx)
	if err != nil {
		s.logf("Clash 内核自启失败，不影响服务运行 │ 原因=%v", err)
		return
	}
	usable := []ClashSubscription{}
	for _, sub := range subs {
		if !sub.Deprecated {
			usable = append(usable, sub)
		}
	}
	if len(usable) == 0 {
		if len(subs) > 0 {
			s.logf("Clash 订阅当前全部不可用，等待订阅或节点恢复")
		} else {
			s.logf("还没有 Clash 订阅，跳过内核自启")
		}
		return
	}
	picked, err := s.ResolveStartableClash(ctx, nil, snapshotOnly)
	if err != nil {
		var status *StatusError
		reason := err.Error()
		if errors.As(err, &status) {
			reason = status.Detail
		}
		s.logf("内核自启放弃，没有能选出的可用订阅 │ 原因=%s", reason)
		return
	}
	mixedPort, allocate, err := s.KernelPortOptions(ctx)
	if err != nil {
		s.logf("Clash 内核自启失败，不影响服务运行 │ 原因=%v", err)
		return
	}
	ensure, err := s.ensureSubKernelRunning(deref(detect.Path), picked.ConfigPath, picked.Subscription.URL, mixedPort, allocate)
	if err != nil {
		s.logf("Clash 内核自启失败，不影响服务运行 │ 原因=%v", err)
		return
	}
	s.logf("Clash 内核已随服务自启 │ 端口=%d", ensure.Status.Port)
}

func deref(value *string) string {
	if value == nil {
		return ""
	}
	return *value
}

func (s *Service) subscriptionURL(ctx context.Context, subID int64) (string, bool) {
	var stored string
	if err := s.DB.QueryRowContext(ctx,
		"SELECT url FROM proxy_subscriptions WHERE id=?", subID).Scan(&stored); err != nil {
		return "", false
	}
	return proxypool.OpenURL(s.DataDir, stored), true
}
