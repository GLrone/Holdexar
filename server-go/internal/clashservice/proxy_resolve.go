package clashservice

import (
	"context"

	"holdexar/server-go/internal/proxypool"
)

func (s *Service) ResolveOutboundProxy(ctx context.Context) (string, bool) {
	proxyURL, _, err := proxypool.ResolveStrategyProxyWith(ctx, s.DB, s.Gate, s.DataDir, proxypool.StrategyResolveOptions{
		KernelStatus: func() (bool, int) {
			st := s.Kernel.Status()
			return st.Running, st.Port
		},
		PortListening:           s.portListening,
		SystemProxy:             s.SystemProxy,
		LocalFallback:           s.LocalFallback,
		IncludeKernelOnFallback: true,
		FailSoft:                true,
	})
	if err != nil || proxyURL == "" {
		return "", false
	}
	return proxyURL, true
}
