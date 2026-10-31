package clashkernel

// 控制器 REST 客户端与热重载生效性校验。

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"holdexar/server-go/internal/clashcfg"
)

// ReloadRejectedError 内核明确拒载新配置（HTTP 4xx）或新配置本身不合法。
// 与「热重载通路故障」不同档：配置被拒时内核还好好跑着旧配置，正确动作是
// 回滚磁盘、保留运行实例。
type ReloadRejectedError struct{ Detail string }

func (e *ReloadRejectedError) Error() string { return e.Detail }

// Controller 内核 RESTful 控制器客户端（Bearer 密钥鉴权，不走系统代理）。
type Controller struct {
	BaseURL string
	Secret  string
	HTTP    *http.Client
}

// NewController 构造控制器客户端（默认 8s 超时）。
func NewController(baseURL, secret string) *Controller {
	return &Controller{
		BaseURL: baseURL,
		Secret:  secret,
		HTTP:    &http.Client{Timeout: 8 * time.Second},
	}
}

func (c *Controller) do(method, path string, body io.Reader, timeout time.Duration) (int, []byte, error) {
	req, err := http.NewRequest(method, strings.TrimRight(c.BaseURL, "/")+path, body)
	if err != nil {
		return 0, nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	if c.Secret != "" {
		req.Header.Set("Authorization", "Bearer "+c.Secret)
	}
	client := c.HTTP
	if timeout > 0 && client != nil {
		clone := *client
		clone.Timeout = timeout
		client = &clone
	}
	resp, err := client.Do(req)
	if err != nil {
		return 0, nil, err
	}
	defer resp.Body.Close()
	raw, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return resp.StatusCode, nil, err
	}
	return resp.StatusCode, raw, nil
}

// Version 控制器 /version 是否可用（200 才算）。
func (c *Controller) Version() error {
	status, _, err := c.do(http.MethodGet, "/version", nil, 8*time.Second)
	if err != nil {
		return fmt.Errorf("控制器不可达：%w", err)
	}
	if status != 200 {
		return fmt.Errorf("控制器不可用（HTTP %d）", status)
	}
	return nil
}

// Proxies GET /proxies 的完整返回体。
func (c *Controller) Proxies() (map[string]any, error) {
	status, raw, err := c.do(http.MethodGet, "/proxies", nil, 8*time.Second)
	if err != nil {
		return nil, fmt.Errorf("控制器不可达：%w", err)
	}
	if status != 200 {
		return nil, fmt.Errorf("控制器 /proxies 不可用（HTTP %d）", status)
	}
	var body map[string]any
	if err := json.Unmarshal(raw, &body); err != nil {
		return nil, fmt.Errorf("控制器 /proxies 返回解析失败：%w", err)
	}
	return body, nil
}

// ProxyNames /proxies 里内核当前装载的全部代理名。
func (c *Controller) ProxyNames() (map[string]bool, error) {
	body, err := c.Proxies()
	if err != nil {
		return nil, err
	}
	proxies, _ := body["proxies"].(map[string]any)
	names := make(map[string]bool, len(proxies))
	for name := range proxies {
		names[name] = true
	}
	return names, nil
}

// PutConfigsForce 让在跑的内核加载指定路径的新配置（进程不动）。
// 非 2xx 视为内核拒绝这份配置。
func (c *Controller) PutConfigsForce(configPath string) error {
	payload := fmt.Sprintf(`{"path":%q}`, configPath)
	status, _, err := c.do(http.MethodPut, "/configs?force=true",
		strings.NewReader(payload), 15*time.Second)
	if err != nil {
		return fmt.Errorf("热重载通路故障：%w", err)
	}
	if status >= 300 {
		return &ReloadRejectedError{Detail: fmt.Sprintf("内核拒绝新配置（HTTP %d）", status)}
	}
	return nil
}

// VerifyReloaded 热重载生效性校验：控制器活着 + 新配置的节点真的出现在内核里 +
// 探测 lane 端口在听 + 混合口归属当前进程。任一不满足返回错误，调用方回退。
func VerifyReloaded(controller *Controller, injectedText string, pid int) error {
	if err := controller.Version(); err != nil {
		return fmt.Errorf("重载后控制器不可用：%w", err)
	}
	observed, err := controller.ProxyNames()
	if err != nil {
		return fmt.Errorf("重载后控制器不可用：%w", err)
	}
	var missing []string
	for _, name := range clashcfg.ParseNodeNames(injectedText) {
		if !observed[name] {
			missing = append(missing, name)
		}
	}
	if len(missing) > 0 {
		return fmt.Errorf("重载后内核缺 %d 个新配置节点（如 %q）", len(missing), missing[0])
	}
	lanes, _ := clashcfg.ParseProbeLanes(injectedText)
	if len(lanes) > 0 {
		anyListening := false
		for _, lane := range lanes {
			if PortListening(lane.Port) {
				anyListening = true
				break
			}
		}
		if !anyListening {
			return fmt.Errorf("重载后探测 lane 端口无一在听")
		}
	}
	newPort := clashcfg.ParseMixedPort(injectedText)
	if newPort > 0 && !(pid > 0 && PortListening(newPort) && PortOwnedByProcess(newPort, uint32(pid))) {
		return fmt.Errorf("重载后混合端口 %d 未由当前内核监听", newPort)
	}
	return nil
}
