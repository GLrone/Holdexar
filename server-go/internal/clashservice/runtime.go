package clashservice

import (
	"context"
	"os"

	"holdexar/server-go/internal/clashkernel"
)

type KernelInfo struct {
	Found   bool    `json:"found"`
	Path    *string `json:"path"`
	Builtin bool    `json:"builtin"`
}

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

// BaseStatus 未运行时 port/controllerUrl/subscriptionUrl 置 nil，configPath 保留。
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

type StatusPayload struct {
	BaseStatus
	Kernel                 KernelInfo `json:"kernel"`
	Version                *string    `json:"version"`
	KernelDir              string     `json:"kernelDir"`
	SelectedSubscriptionID *int64     `json:"selectedSubscriptionId"`
}

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
