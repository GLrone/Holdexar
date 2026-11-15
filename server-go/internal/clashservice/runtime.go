// GET /clash 的状态汇编：内核检测只认本应用目录 data/clash/（不检索用户自装的
// Clash——内核从哪来必须可控），found=true 时 builtin 恒 true（内核只可能来自随包资产）。
package clashservice

import (
	"context"
	"os"

	"holdexar/server-go/internal/clashkernel"
)

// KernelInfo 内核就位情况（detect_kernel 对齐形状）。
type KernelInfo struct {
	Found   bool    `json:"found"`
	Path    *string `json:"path"`
	Builtin bool    `json:"builtin"`
}

// DetectKernel 检测内核就位情况：只认 data/clash/mihomo(.exe)。
func (s *Service) DetectKernel() KernelInfo {
	exe := clashkernel.KernelExe(s.DataDir)
	if info, err := os.Stat(exe); err == nil && info.Mode().IsRegular() {
		path := exe
		return KernelInfo{Found: true, Path: &path, Builtin: true}
	}
	return KernelInfo{Found: false, Path: nil, Builtin: false}
}

func (s *Service) kernelVersion(exePath string) string {
	if s.VersionProbe != nil {
		return s.VersionProbe(exePath)
	}
	return clashkernel.KernelVersion(exePath)
}

// BaseStatus runtime.status() 对齐形状（未运行时 port/controllerUrl/subscriptionUrl 置 null；
// configPath 保留——stop 不清账）。
type BaseStatus struct {
	Running         bool    `json:"running"`
	Port            *int    `json:"port"`
	EntryReady      bool    `json:"entryReady"`
	ConfigPath      *string `json:"configPath"`
	ControllerURL   *string `json:"controllerUrl"`
	SubscriptionURL *string `json:"subscriptionUrl"`
}

func baseStatus(st clashkernel.Status) BaseStatus {
	out := BaseStatus{Running: st.Running, EntryReady: st.EntryReady}
	if st.Running && st.Port > 0 {
		port := st.Port
		out.Port = &port
	}
	if st.ConfigPath != "" {
		out.ConfigPath = &st.ConfigPath
	}
	if st.Running && st.ControllerURL != "" {
		out.ControllerURL = &st.ControllerURL
	}
	if st.Running && st.SubscriptionURL != "" {
		out.SubscriptionURL = &st.SubscriptionURL
	}
	return out
}

// StatusPayload GET /clash 响应（ClashStatus 契约 + controllerUrl 透传）。
type StatusPayload struct {
	BaseStatus
	Kernel                 KernelInfo `json:"kernel"`
	Version                *string    `json:"version"`
	KernelDir              string     `json:"kernelDir"`
	SelectedSubscriptionID *int64     `json:"selectedSubscriptionId"`
}

// StatusPayload 组装当前状态快照（未装内核 found=false，version 恒 null）。
func (s *Service) StatusPayload(ctx context.Context) StatusPayload {
	detect := s.DetectKernel()
	var version *string
	if detect.Found && detect.Path != nil {
		if v := s.kernelVersion(*detect.Path); v != "" {
			version = &v
		}
	}
	payload := StatusPayload{
		BaseStatus:             baseStatus(s.Kernel.Status()),
		Kernel:                 detect,
		Version:                version,
		KernelDir:              clashkernel.KernelDir(s.DataDir),
		SelectedSubscriptionID: s.SelectedSubID(ctx),
	}
	return payload
}
