package clashcfg

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

type RuntimeConfigError struct{ Detail string }

func (e *RuntimeConfigError) Error() string { return e.Detail }

type PoolBuildError struct{ Detail string }

func (e *PoolBuildError) Error() string { return e.Detail }

// LaneCountFor 按池规模算 lane 数，上限 MaxLanes；requested < 0 取池规模。
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

type PinnedEndpoints struct {
	Controller     string
	Secret         string
	MixedPort      int
	LanePorts      []int
	ProbeLanePorts []int
}

// PinnedRuntimeEndpoints 提取现役端点；不可用返回 nil，调用方走冷通道。
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

// PrepareRuntimeConfig 生成运行配置；pinRunning 沿用现役端点且现役缺失即报错。
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
