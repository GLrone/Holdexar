package clashcfg

// 池文件 → 内核运行配置生成：crawl-pool.yaml（只读产物）→ crawl-runtime.yaml
// （内核实际启动文件）。lane = 一个 select 组（成员 = 池内全部节点）+ 一个
// mixed listener；探测 lane 在池专属端口段注入；热重载通道钉住现役端点。

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// RuntimeConfigError 运行配置不可用（如热重载缺现役端点）。
type RuntimeConfigError struct{ Detail string }

func (e *RuntimeConfigError) Error() string { return e.Detail }

// PoolBuildError 池文件形态不可用。
type PoolBuildError struct{ Detail string }

func (e *PoolBuildError) Error() string { return e.Detail }

// LaneCountFor 由池规模决定开几条 lane：一个节点最多占一条工位，上限 MaxLanes。
// requested < 0 表示未指定（取池规模）。池为空返回 0。
func LaneCountFor(poolSize int, requested int) int {
	if poolSize <= 0 {
		return 0
	}
	n := poolSize
	if requested >= 0 {
		n = requested
	}
	if n < 0 {
		n = 0
	}
	result := MaxLanes
	if poolSize < result {
		result = poolSize
	}
	if n < result {
		result = n
	}
	if result < 0 {
		result = 0
	}
	return result
}

// PinnedEndpoints 现役运行配置里可被热重载沿用的端点。
type PinnedEndpoints struct {
	Controller     string
	Secret         string
	MixedPort      int
	LanePorts      []int
	ProbeLanePorts []int
}

// PinnedRuntimeEndpoints 读现役运行配置文本里可沿用的端点；缺失或不可解析返回
// nil（热重载没有可沿用的端点，调用方必须走冷通道）。
func PinnedRuntimeEndpoints(runtimeText string) *PinnedEndpoints {
	doc, err := LoadYAML(runtimeText)
	if err != nil {
		return nil
	}
	controller, ok := doc["external-controller"].(string)
	if !ok || strings.TrimSpace(controller) == "" {
		return nil
	}
	mixedPort, ok := toInt(doc["mixed-port"])
	if !ok {
		return nil
	}
	listeners, ok := doc["listeners"].([]any)
	if !ok {
		listeners = nil
	}
	oldLanePorts := map[int]int{}
	oldProbePorts := map[int]int{}
	for _, entry := range listeners {
		em, isMap := entry.(map[string]any)
		if !isMap {
			continue
		}
		name := pyStr(em["name"])
		port, isInt := toInt(em["port"])
		if !isInt || port <= 0 {
			continue
		}
		switch {
		case strings.HasPrefix(name, LaneGroupPrefix):
			tail := strings.SplitN(name[len(LaneGroupPrefix):], "-", 2)[0]
			if isAllDigits(tail) {
				oldLanePorts[atoiSafe(tail)] = port
			}
		case strings.HasPrefix(name, ProbeLaneGroupPrefix):
			tail := strings.SplitN(strings.Trim(name[len(ProbeLaneGroupPrefix):], "-"), "-", 2)[0]
			if isAllDigits(tail) {
				oldProbePorts[atoiSafe(tail)] = port
			}
		}
	}
	return &PinnedEndpoints{
		Controller:     strings.TrimSpace(controller),
		Secret:         pyStr(doc["secret"]),
		MixedPort:      mixedPort,
		LanePorts:      mapOrderedPorts(oldLanePorts),
		ProbeLanePorts: mapOrderedPorts(oldProbePorts),
	}
}

func isAllDigits(s string) bool {
	if s == "" {
		return false
	}
	for _, c := range s {
		if c < '0' || c > '9' {
			return false
		}
	}
	return true
}

func mapOrderedPorts(byIndex map[int]int) []int {
	if len(byIndex) == 0 {
		return nil
	}
	maxIndex := 0
	for idx := range byIndex {
		if idx > maxIndex {
			maxIndex = idx
		}
	}
	out := make([]int, 0, len(byIndex))
	for i := 0; i <= maxIndex; i++ {
		if port, ok := byIndex[i]; ok {
			out = append(out, port)
		}
	}
	return out
}

// PrepareRuntimeConfig 由 crawl-pool.yaml 生成 crawl-runtime.yaml（写盘，tmp+replace）。
// lanes < 0 = 未指定（按池规模，钳到 MaxLanes）；pinRunning 时 controller /
// secret / mixed-port / 既有 lane 端口全部沿用现役运行配置——热重载后所有已交出
// 的入口 URL 继续有效；现役配置缺失时抛错（热重载的前提不成立）。
// 返回 (落盘路径, 最终文本, 错误)。
func PrepareRuntimeConfig(dataDir string, lanes int, pinRunning bool) (string, string, error) {
	var pinned *PinnedEndpoints
	if pinRunning {
		runtimePath := filepath.Join(dataDir, "proxypool", RuntimeConfigFilename)
		raw, err := os.ReadFile(runtimePath)
		if err != nil {
			return "", "", &RuntimeConfigError{Detail: "热重载要求沿用现役运行配置的端点，但现役配置缺失或不可解析"}
		}
		pinned = PinnedRuntimeEndpoints(string(raw))
		if pinned == nil {
			return "", "", &RuntimeConfigError{Detail: "热重载要求沿用现役运行配置的端点，但现役配置缺失或不可解析"}
		}
	}

	poolPath := filepath.Join(dataDir, "proxypool", "crawl-pool.yaml")
	poolRaw, err := os.ReadFile(poolPath)
	if err != nil {
		return "", "", &PoolBuildError{Detail: fmt.Sprintf("池文件不可读，无法生成运行配置：%s", poolPath)}
	}
	doc, err := LoadYAML(string(poolRaw))
	if err != nil {
		return "", "", &PoolBuildError{Detail: fmt.Sprintf("池文件形态不对，无法生成运行配置：%s", poolPath)}
	}
	rawProxies, ok := doc["proxies"].([]any)
	if !ok {
		return "", "", &PoolBuildError{Detail: fmt.Sprintf("池文件形态不对，无法生成运行配置：%s", poolPath)}
	}
	names := make([]string, 0, len(rawProxies))
	for _, entry := range rawProxies {
		em, isMap := entry.(map[string]any)
		if !isMap {
			return "", "", &PoolBuildError{Detail: fmt.Sprintf("池文件形态不对，无法生成运行配置：%s", poolPath)}
		}
		name := pyStr(em["name"])
		if name == "" {
			return "", "", &PoolBuildError{Detail: fmt.Sprintf("池文件形态不对，无法生成运行配置：%s", poolPath)}
		}
		names = append(names, name)
	}

	nLanes := LaneCountFor(len(names), lanes)
	rawGroups, _ := doc["proxy-groups"].([]any)
	groups := append([]any{}, rawGroups...)
	for i := 0; i < nLanes; i++ {
		members := make([]any, len(names))
		for j, name := range names {
			members[j] = name
		}
		groups = append(groups, map[string]any{
			"name": fmt.Sprintf("%s%d", LaneGroupPrefix, i),
			"type": "select", "proxies": members,
		})
	}
	var oldLanePorts []int
	if pinned != nil {
		oldLanePorts = pinned.LanePorts
	}
	listeners := make([]any, 0, nLanes)
	for i := 0; i < nLanes; i++ {
		port := 0
		if i < len(oldLanePorts) {
			port = oldLanePorts[i]
		} else {
			port, err = FreeLocalPort()
			if err != nil {
				return "", "", err
			}
		}
		listeners = append(listeners, map[string]any{
			"name":   fmt.Sprintf("%s%d-in", LaneGroupPrefix, i),
			"type":   "mixed",
			"port":   port,
			"listen": RuntimeBindAddress,
			"proxy":  fmt.Sprintf("%s%d", LaneGroupPrefix, i),
		})
	}

	proxiesCopy := append([]any{}, rawProxies...)
	controller := ""
	secret := ""
	mixedPort := 0
	if pinned != nil {
		controller = pinned.Controller
		secret = pinned.Secret
		mixedPort = pinned.MixedPort
	} else {
		controller, err = freeControllerAddr()
		if err != nil {
			return "", "", err
		}
		secret = NewControllerSecret()
		mixedPort, err = FreeLocalPort()
		if err != nil {
			return "", "", err
		}
	}
	out := NewDoc()
	out.Set("mode", RuntimeMode)
	out.Set("allow-lan", false)
	out.Set("bind-address", RuntimeBindAddress)
	out.Set("external-controller", controller)
	out.Set("secret", secret)
	out.Set("mixed-port", mixedPort)
	out.Set("proxies", proxiesCopy)
	out.Set("proxy-groups", groups)
	out.Set("listeners", listeners)

	var probePins []int
	if pinned != nil {
		probePins = pinned.ProbeLanePorts
	}
	text := InjectProbeLanes(DumpYAML(out), ProbeLaneOptions{
		PortBase: PoolProbeLanePortBase,
		PortSpan: PoolProbeLanePortSpan,
		PinPorts: probePins,
	})
	if err := ValidateExecutionConfigText(text); err != nil {
		return "", "", err
	}
	outPath := filepath.Join(dataDir, "proxypool", RuntimeConfigFilename)
	if err := os.MkdirAll(filepath.Dir(outPath), 0o755); err != nil {
		return "", "", fmt.Errorf("运行配置目录创建失败：%w", err)
	}
	tmpPath := filepath.Join(filepath.Dir(outPath), ".crawl-runtime.yaml.tmp")
	if err := os.WriteFile(tmpPath, []byte(text), 0o644); err != nil {
		return "", "", fmt.Errorf("运行配置写入失败：%w", err)
	}
	if err := os.Rename(tmpPath, outPath); err != nil {
		_ = os.Remove(tmpPath)
		return "", "", fmt.Errorf("运行配置落盘失败：%w", err)
	}
	return outPath, text, nil
}

func freeControllerAddr() (string, error) {
	port, err := FreeLocalPort()
	if err != nil {
		return "", err
	}
	return fmt.Sprintf("%s:%d", RuntimeControllerHost, port), nil
}
