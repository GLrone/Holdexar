package proxypool

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

const ProbeTargetURL = "http://www.gstatic.com/generate_204"

const (
	ProbeLevelL0             = "L0"
	DefaultProbeTimeoutMS    = 5000
	defaultProbeClientWait   = 30 * time.Second
	detailNodeNotFound       = "内核里没有这个节点：名字没定位到（HTTP 404）"
	detailProbeFailed        = "内核探测失败"
	detailControllerAuthFail = "控制器认证失败"
)

type ProbeResult struct {
	RuntimeName string
	OK          bool
	DelayMS     int64
	Detail      string
	Valid       bool
}

type ProbeOptions struct {
	URL           string
	TimeoutMS     int
	ClientTimeout time.Duration
}

func probeHTTPClient(timeout time.Duration) *http.Client {
	return &http.Client{
		Timeout:   timeout,
		Transport: &http.Transport{Proxy: nil},
	}
}

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

// PoolProbeLanes 剔除未在听端口，防通道不通误判节点不通。
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

var failCooldownMinutes = []int{30, 60, 120, 240, 480, 1440}

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
