package proxypool

// L0 传输层探测（Python health.py L0 段对齐）：经控制器 /proxies/{name}/delay
// 让内核经指定节点探一次目标。三层分工里 L0 只作诊断不作准入；响应区分
// 200 {"delay":n}=成功，404=名字没定位到（不构成健康证据），其余非 200=探测失败。
// valid=False 表示这一探没测到节点本身，不累加失败计数、不推进状态。

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
)

// L0 探测目标：204、无响应体、请求最轻。
const ProbeTargetURL = "http://www.gstatic.com/generate_204"

const (
	ProbeLevelL0             = "L0"
	DefaultProbeTimeoutMS    = 5000
	defaultProbeClientWait   = 30 * time.Second
	detailNodeNotFound       = "内核里没有这个节点：名字没定位到（HTTP 404）"
	detailProbeFailed        = "内核探测失败"
	detailControllerAuthFail = "控制器认证失败"
)

// ProbeResult 一次 L0 探测的原始结论。
type ProbeResult struct {
	RuntimeName string
	OK          bool
	DelayMS     int64
	Detail      string
	Valid       bool
}

// ProbeOptions L0 探测参数（零值 = 缺省目标与超时）。
type ProbeOptions struct {
	URL           string
	TimeoutMS     int
	ClientTimeout time.Duration
}

// probeHTTPClient 不走环境代理（Python trust_env=False 语义），TLS 校验保持默认。
func probeHTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{
		Timeout:   timeout,
		Transport: &http.Transport{Proxy: nil},
	}
}

// ProbeNode 让内核经指定节点探测目标 URL，返回最基础的一次 L0 观测。
func ProbeNode(ctx context.Context, controllerURL, secret, runtimeName string, opts ProbeOptions) ProbeResult {
	if opts.URL == "" {
		opts.URL = ProbeTargetURL
	}
	if opts.TimeoutMS <= 0 {
		opts.TimeoutMS = DefaultProbeTimeoutMS
	}
	if opts.ClientTimeout <= 0 {
		opts.ClientTimeout = defaultProbeClientWait
	}
	target := strings.TrimRight(controllerURL, "/") +
		"/proxies/" + url.PathEscape(runtimeName) + "/delay" +
		"?url=" + url.QueryEscape(opts.URL) +
		"&timeout=" + strconv.Itoa(opts.TimeoutMS)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, target, nil)
	if err != nil {
		return ProbeResult{RuntimeName: runtimeName, Detail: fmt.Sprintf("控制器请求异常：%T", err)}
	}
	if secret != "" {
		req.Header.Set("Authorization", "Bearer "+secret)
	}
	resp, err := probeHTTPClient(opts.ClientTimeout).Do(req)
	if err != nil {
		// 控制器不可达 ≠ 节点坏：这一探根本没测到节点，不产生健康证据
		return ProbeResult{RuntimeName: runtimeName, Detail: fmt.Sprintf("控制器请求异常：%T", err)}
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	switch resp.StatusCode {
	case http.StatusNotFound:
		return ProbeResult{RuntimeName: runtimeName, Detail: detailNodeNotFound}
	case http.StatusUnauthorized, http.StatusForbidden:
		return ProbeResult{RuntimeName: runtimeName,
			Detail: fmt.Sprintf("%s（HTTP %d）", detailControllerAuthFail, resp.StatusCode)}
	}
	if resp.StatusCode != http.StatusOK {
		return ProbeResult{RuntimeName: runtimeName,
			Detail: fmt.Sprintf("%s（HTTP %d）", detailProbeFailed, resp.StatusCode), Valid: true}
	}
	var payload struct {
		Delay any `json:"delay"`
	}
	if err := json.Unmarshal(raw, &payload); err != nil {
		return ProbeResult{RuntimeName: runtimeName,
			Detail: detailProbeFailed + "（响应不是 JSON）", Valid: true}
	}
	delay, ok := pyToProbeInt(payload.Delay)
	if !ok || delay <= 0 {
		return ProbeResult{RuntimeName: runtimeName,
			Detail: fmt.Sprintf("%s（delay=%s）", detailProbeFailed, pyReprValue(payload.Delay)), Valid: true}
	}
	return ProbeResult{RuntimeName: runtimeName, OK: true, DelayMS: delay, Valid: true}
}

// PoolProbeLanes 池运行时配置里的探测 lane；端口没在听的剔除——走死端口的探测
// 会把「通道不通」误判成「节点不通」。解析失败 / 无 lane 返回空。
func PoolProbeLanes(configText string) []clashcfg.ProbeLane {
	lanes, err := clashcfg.ParseProbeLanes(configText)
	if err != nil {
		return nil
	}
	out := lanes[:0:0]
	for _, lane := range lanes {
		if lane.Port > 0 && clashkernel.PortListening(lane.Port) {
			out = append(out, lane)
		}
	}
	return out
}

// failCooldownMinutes 失败冷却档位（Python bridge 口径）：按连续失败次数取档，
// 单位分钟；成功或收到任何 Steam 响应即清冷却。
var failCooldownMinutes = []int{30, 60, 120, 240, 480, 1440}

// CooldownMinutesFor 连续失败次数 → 冷却分钟数（第 1 次失败起 30 分钟，逐档翻倍封顶）。
func CooldownMinutesFor(consecutiveFailures int64) int {
	tier := consecutiveFailures
	if tier < 1 {
		tier = 1
	}
	if tier > int64(len(failCooldownMinutes)) {
		tier = int64(len(failCooldownMinutes))
	}
	return failCooldownMinutes[tier-1]
}

// pyToProbeInt Python _to_int 语义：None/空串 → 无值；字符串/数值按
// int(float(v)) 截断；NaN/Inf 不可转换；bool 按数值 1/0。
func pyToProbeInt(v any) (int64, bool) {
	switch x := v.(type) {
	case nil:
		return 0, false
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	case int:
		return int64(x), true
	case int64:
		return x, true
	case float64:
		if x != x || x > 1e308 || x < -1e308 {
			return 0, false
		}
		return int64(x), true
	case json.Number:
		return pyToProbeIntFromFloat(string(x))
	case string:
		if x == "" {
			return 0, false
		}
		return pyToProbeIntFromFloat(x)
	}
	return 0, false
}

func pyToProbeIntFromFloat(s string) (int64, bool) {
	f, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
	if err != nil || f != f || f > 1e308 || f < -1e308 {
		return 0, false
	}
	return int64(f), true
}

// pyReprValue 数值观测值的 Python repr 形态（detail 文案对齐用）。
func pyReprValue(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case string:
		return "'" + x + "'"
	case json.Number:
		return string(x)
	case float64:
		return strconv.FormatFloat(x, 'g', -1, 64)
	case bool:
		if x {
			return "True"
		}
		return "False"
	}
	return fmt.Sprintf("%v", v)
}
