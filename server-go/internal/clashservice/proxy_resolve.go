// 出站代理解析：为单次出站任务（工具检索/联网校验等）按 proxy.strategy
// 解析代理 URL。等价 Python proxies_service.resolve_proxy_url 的工具出站面，
// 失败语义收敛为直连（Go 侧调用方不处理解析异常）。
package clashservice

import (
	"context"
	"fmt"

	"holdexar/server-go/internal/netenv"
)

// ResolveOutboundProxy 策略引擎：为本次出站解析代理 URL。ok=true 用 proxyURL，
// ok=false 直连；任何失败一律回落直连，不返回错误。
//
// 策略语义（对齐 proxies/service.py:441-482）：
//   - direct_only：托管用户本机网络环境——TUN 型加速器裸连即走；系统代理型由
//     系统代理解析找到并显式挂上，找不到即直连；
//   - direct_first：恒直连；
//   - proxy_first / proxy_only（及未知策略）：订阅内核在跑且混合端口在听 →
//     内核端口；否则直连。
//
// 与 Python 的差异（登记）：proxy_first 的代理池节点加权轮询与 7897 兜底口
// 未移植——Go 工具出站只认订阅内核混合端口，池节点轮询仍属爬取 lane 体系。
func (s *Service) ResolveOutboundProxy(ctx context.Context) (string, bool) {
	strategy := s.kvString(ctx, "proxy.strategy", "direct_only")
	switch strategy {
	case "direct_first":
		return "", false
	case "direct_only":
		resolve := s.SystemProxy
		if resolve == nil {
			resolve = netenv.ResolveSystemProxy
		}
		proxyURL, err := resolve(ctx)
		if err != nil || proxyURL == "" {
			return "", false
		}
		return proxyURL, true
	default:
		st := s.Kernel.Status()
		if st.Running && st.Port > 0 && s.portListening(st.Port) {
			return fmt.Sprintf("http://127.0.0.1:%d", st.Port), true
		}
		return "", false
	}
}
