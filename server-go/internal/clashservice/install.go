// 内核安装与请求同步阻塞，不可改为后台异步，否则进页续挂进度错位。
package clashservice

import (
	"context"
	"os"
	"sync"

	"holdexar/server-go/internal/clashkernel"
)

type InstallResult struct {
	OK      bool    `json:"ok"`
	Path    *string `json:"path,omitempty"`
	Source  *string `json:"source,omitempty"`
	Via     *string `json:"via,omitempty"`
	Version *string `json:"version,omitempty"`
	Error   *string `json:"error,omitempty"`
}

type InstallProgress struct {
	Running  bool     `json:"running"`
	Phase    *string  `json:"phase"`
	Percent  *float64 `json:"percent"`
	Received int64    `json:"received"`
	Total    *int64   `json:"total"`
	Source   *string  `json:"source"`
	Via      *string  `json:"via"`
	Error    *string  `json:"error"`
	OK       bool     `json:"ok"`
}

// progressStore 合并非零进度字段（终帧只带 ok/percent 时保留 phase/source/received）。
type progressStore struct {
	mu   sync.Mutex
	snap clashkernel.Progress
}

func (p *progressStore) reset() {
	p.mu.Lock()
	p.snap = clashkernel.Progress{}
	p.mu.Unlock()
}

func (p *progressStore) record(frame clashkernel.Progress) {
	p.mu.Lock()
	defer p.mu.Unlock()
	snap := &p.snap
	snap.Running = frame.Running
	if frame.Phase != "" {
		snap.Phase = frame.Phase
	}
	if frame.Received > 0 {
		snap.Received = frame.Received
	}
	if frame.Total > 0 {
		snap.Total = frame.Total
	}
	if frame.Percent > 0 {
		snap.Percent = frame.Percent
	}
	if frame.Source != "" {
		snap.Source = frame.Source
	}
	if frame.Via != "" {
		snap.Via = frame.Via
	}
	snap.OK = frame.OK
	if frame.Error != "" {
		snap.Error = frame.Error
	}
}

func (p *progressStore) snapshot() InstallProgress {
	p.mu.Lock()
	defer p.mu.Unlock()
	snap := p.snap
	out := InstallProgress{
		Running:  snap.Running,
		Received: snap.Received,
		OK:       snap.OK,
	}
	if snap.Phase != "" {
		out.Phase = &snap.Phase
	}
	if snap.Percent > 0 {
		percent := snap.Percent
		out.Percent = &percent
	}
	if snap.Total > 0 {
		total := snap.Total
		out.Total = &total
	}
	if snap.Source != "" {
		out.Source = &snap.Source
	}
	if snap.Via != "" {
		out.Via = &snap.Via
	}
	if snap.Error != "" {
		out.Error = &snap.Error
	}
	return out
}

func (s *Service) InstallProgress() InstallProgress {
	return s.progress.snapshot()
}

func (s *Service) KernelVersion(exePath string) string {
	return s.kernelVersion(exePath)
}

func (s *Service) InstallKernel(ctx context.Context) InstallResult {
	if _, err := clashkernel.EnsureKernel(s.BundledDir, s.DataDir); err != nil {
		s.logf("随包内核就位失败：%v", err)
	}
	target := clashkernel.KernelExe(s.DataDir)
	if info, err := os.Stat(target); err == nil && info.Mode().IsRegular() {
		return InstallResult{OK: true, Path: &target, Source: strPtr("bundled"), Via: strPtr("随包资产")}
	}
	s.logf("随包内核不可用，回退网络下载 │ 目标版本=%s", clashkernel.MIHOMOVersion)
	return s.downloadKernel(ctx)
}

func (s *Service) downloadKernel(ctx context.Context) InstallResult {
	s.progress.reset()
	result := clashkernel.DownloadKernel(ctx, s.DataDir, clashkernel.DownloadOptions{
		Mirrors:       s.installMirrors(),
		ReleaseAPIURL: s.ReleaseAPIURL,
		Log:           func(format string, args ...any) { s.logf(format, args...) },
		Progress:      s.progress.record,
	})
	if result.OK {
		return InstallResult{OK: true, Path: &result.Path, Source: &result.Source, Via: &result.Via}
	}
	return InstallResult{OK: false, Error: &result.Error}
}

func (s *Service) installMirrors() []string {
	if len(s.InstallMirrors) > 0 {
		return s.InstallMirrors
	}
	return clashkernel.DefaultMirrors
}

func strPtr(v string) *string { return &v }
