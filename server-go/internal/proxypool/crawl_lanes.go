package proxypool

import (
	"cmp"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net"
	"net/http"
	"net/netip"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"holdexar/server-go/internal/clashcfg"
	"holdexar/server-go/internal/clashkernel"
)

const crawlLanePlanFilename = "crawl-lanes.yaml"
const crawlLaneReadyTimeout = 5 * time.Second

type CrawlLane struct {
	ProxyURL string
	ExitIP   string
	Node     string
}

type crawlSlot struct {
	ExitIP       string
	Node         string
	Alternatives []any
}

func selectCrawlSlots(nodes []poolNodeRow, materialized map[string]bool) []crawlSlot {
	type candidate struct {
		node, exit string
		state      int
		delay      int64
		l2, l1     time.Time
		id         int64
	}
	candidates := make([]candidate, 0, len(nodes))
	for _, node := range nodes {
		if node.RuntimeName == "" || (materialized != nil && !materialized[node.RuntimeName]) {
			continue
		}
		ip, err := netip.ParseAddr(node.ExitIP.String)
		if err != nil || ip.Zone() != "" {
			continue
		}
		state := 2
		switch node.State {
		case NodeActive:
			state = 0
		case NodeNew:
			state = 1
		}
		delay := node.L0DelayMS.Int64
		if delay <= 0 {
			delay = math.MaxInt64
		}
		l2, _ := parsePyDatetime(node.LastL2At.String)
		l1, _ := parsePyDatetime(node.LastL1At.String)
		candidates = append(candidates, candidate{node.RuntimeName, ip.Unmap().String(), state, delay, l2, l1, node.ID})
	}
	slices.SortFunc(candidates, func(a, b candidate) int {
		return cmp.Or(cmp.Compare(a.state, b.state), cmp.Compare(a.delay, b.delay), b.l2.Compare(a.l2),
			b.l1.Compare(a.l1), cmp.Compare(a.exit, b.exit), cmp.Compare(a.id, b.id))
	})
	var slots []crawlSlot
	byExit := make(map[string]int)
	for _, c := range candidates {
		if i, found := byExit[c.exit]; found {
			slots[i].Alternatives = append(slots[i].Alternatives, c.node)
		} else if len(slots) < clashcfg.MaxCrawlWorkers {
			byExit[c.exit] = len(slots)
			slots = append(slots, crawlSlot{ExitIP: c.exit, Node: c.node})
		}
	}
	return slots
}

func currentCrawlSlots(ctx context.Context, db *sql.DB, dataDir string) ([]crawlSlot, error) {
	materialized := make(map[string]bool)
	for _, name := range PoolFileNames(dataDir) {
		materialized[name] = true
	}
	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()
	nodes, err := eligibleNodes(ctx, tx, time.Now())
	if err != nil {
		return nil, err
	}
	return selectCrawlSlots(nodes, materialized), nil
}

// CrawlLaneCount is the number of distinct qualified exits in the materialized pool.
func CrawlLaneCount(ctx context.Context, db *sql.DB, dataDir string) (int, error) {
	slots, err := currentCrawlSlots(ctx, db, dataDir)
	return len(slots), err
}

func crawlLanePlanPath(dataDir string) string {
	return filepath.Join(dataDir, "proxypool", crawlLanePlanFilename)
}

// InvalidateCrawlLanes withdraws the plan before any runtime configuration change.
func InvalidateCrawlLanes(dataDir string) error {
	err := os.Remove(crawlLanePlanPath(dataDir))
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	return err
}

type crawlRuntimeEndpoints struct {
	controller string
	secret     string
	ports      []int
}

func readCrawlRuntime(dataDir string, ownerPID []uint32) (crawlRuntimeEndpoints, error) {
	raw, err := os.ReadFile(filepath.Join(dataDir, "proxypool", clashcfg.RuntimeConfigFilename))
	if err != nil {
		return crawlRuntimeEndpoints{}, err
	}
	doc, err := clashcfg.LoadYAML(string(raw))
	if err != nil {
		return crawlRuntimeEndpoints{}, err
	}
	controller, _ := doc["external-controller"].(string)
	host, portText, err := net.SplitHostPort(controller)
	controllerPort, _ := strconv.Atoi(portText)
	if err != nil || host != "127.0.0.1" || controllerPort < 1 || controllerPort > 65535 {
		return crawlRuntimeEndpoints{}, errors.New("crawl runtime has no loopback controller")
	}
	if len(ownerPID) > 0 && !clashkernel.PortOwnedByProcess(controllerPort, ownerPID[0]) {
		return crawlRuntimeEndpoints{}, errors.New("crawl controller is not owned by the kernel")
	}
	listeners, _ := doc["listeners"].([]any)
	ports := make(map[int]int)
	usedPorts := map[int]bool{controllerPort: true, crawlPlanInt(doc["mixed-port"]): true}
	for _, raw := range listeners {
		listener, _ := raw.(map[string]any)
		name, _ := listener["name"].(string)
		tail, found := strings.CutPrefix(name, clashcfg.LaneGroupPrefix)
		if !found {
			continue
		}
		index, err := strconv.Atoi(strings.TrimSuffix(tail, "-in"))
		port := crawlPlanInt(listener["port"])
		if err != nil || index < 0 || index >= clashcfg.MaxCrawlWorkers || name != crawlLaneGroup(index)+"-in" ||
			listener["proxy"] != crawlLaneGroup(index) || listener["type"] != "mixed" || listener["listen"] != "127.0.0.1" ||
			port < 1 || port > 65535 || ports[index] != 0 || usedPorts[port] {
			return crawlRuntimeEndpoints{}, errors.New("invalid crawl listener configuration")
		}
		ports[index] = port
		usedPorts[port] = true
	}
	secret, _ := doc["secret"].(string)
	endpoints := crawlRuntimeEndpoints{controller: "http://" + controller, secret: secret}
	for i := range len(ports) {
		if ports[i] == 0 {
			return crawlRuntimeEndpoints{}, errors.New("crawl listener configuration has a missing lane")
		}
		endpoints.ports = append(endpoints.ports, ports[i])
	}
	return endpoints, nil
}

func crawlPlanInt(value any) int {
	switch n := value.(type) {
	case int:
		return n
	case int64:
		return int(n)
	default:
		return -1
	}
}

func crawlLaneGroup(index int) string {
	return clashcfg.LaneGroupPrefix + strconv.Itoa(index)
}

func crawlLaneAddress(port int) string {
	return net.JoinHostPort("127.0.0.1", strconv.Itoa(port))
}

func checkCrawlListener(ctx context.Context, port int, ownerPID []uint32) error {
	dialer := net.Dialer{Timeout: 300 * time.Millisecond}
	conn, err := dialer.DialContext(ctx, "tcp4", crawlLaneAddress(port))
	if err != nil {
		return err
	}
	_ = conn.Close()
	if len(ownerPID) > 0 && !clashkernel.PortOwnedByProcess(port, ownerPID[0]) {
		return fmt.Errorf("crawl listener %d is not owned by the kernel", port)
	}
	return nil
}

func checkCrawlLanes(ports []int, check func(i, port int) error) error {
	var workers sync.WaitGroup
	errs := make([]error, len(ports))
	for i, port := range ports {
		workers.Go(func() { errs[i] = check(i, port) })
	}
	workers.Wait()
	for i, err := range errs {
		if err != nil {
			return fmt.Errorf("crawl lane %d: %w", i, err)
		}
	}
	return nil
}

// BindCrawlLanes publishes the plan only after the running kernel confirms every selection.
func BindCrawlLanes(ctx context.Context, db *sql.DB, dataDir string, ownerPID ...uint32) error {
	if err := InvalidateCrawlLanes(dataDir); err != nil {
		return err
	}
	slots, err := currentCrawlSlots(ctx, db, dataDir)
	if err != nil || len(slots) == 0 {
		return err
	}
	endpoints, err := readCrawlRuntime(dataDir, ownerPID)
	if err != nil {
		return err
	}
	if len(endpoints.ports) != len(slots) {
		return fmt.Errorf("crawl listener count %d differs from exit count %d", len(endpoints.ports), len(slots))
	}
	ctx, cancel := context.WithTimeout(ctx, crawlLaneReadyTimeout)
	defer cancel()
	if err := checkCrawlLanes(endpoints.ports, func(i, port int) error {
		ticker := time.NewTicker(50 * time.Millisecond)
		defer ticker.Stop()
		for {
			if err := checkCrawlListener(ctx, port, ownerPID); err == nil {
				break
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-ticker.C:
			}
		}
		failure := selectSelector(ctx, endpoints.controller, endpoints.secret, crawlLaneGroup(i), slots[i].Node, crawlLaneReadyTimeout)
		if failure != "" {
			return errors.New(failure)
		}
		return nil
	}); err != nil {
		return err
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	entries := make([]any, 0, len(slots))
	for i, slot := range slots {
		entries = append(entries, map[string]any{"lane": i, "port": endpoints.ports[i], "exitIp": slot.ExitIP,
			"node": slot.Node, "alternatives": slot.Alternatives})
	}
	path := crawlLanePlanPath(dataDir)
	tmp, err := os.CreateTemp(filepath.Dir(path), ".crawl-lanes-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	_, writeErr := tmp.WriteString(clashcfg.DumpYAML(map[string]any{"lanes": entries}))
	if err := errors.Join(writeErr, tmp.Close()); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

// ReadCrawlLanes requires the persisted plan, the live kernel and current exit facts to agree.
func ReadCrawlLanes(ctx context.Context, db *sql.DB, dataDir string, ownerPID ...uint32) ([]CrawlLane, error) {
	raw, err := os.ReadFile(crawlLanePlanPath(dataDir))
	if err != nil {
		return nil, err
	}
	doc, err := clashcfg.LoadYAML(string(raw))
	if err != nil {
		return nil, err
	}
	entries, _ := doc["lanes"].([]any)
	endpoints, err := readCrawlRuntime(dataDir, ownerPID)
	if err != nil {
		return nil, err
	}
	if len(entries) == 0 || len(entries) != len(endpoints.ports) {
		return nil, errors.New("crawl plan does not match its listener configuration")
	}
	lanes := make([]CrawlLane, 0, len(entries))
	for i, raw := range entries {
		entry, _ := raw.(map[string]any)
		port := endpoints.ports[i]
		node, _ := entry["node"].(string)
		exitIP, _ := entry["exitIp"].(string)
		ip, err := netip.ParseAddr(exitIP)
		if crawlPlanInt(entry["lane"]) != i || crawlPlanInt(entry["port"]) != port || node == "" || err != nil || ip.Zone() != "" {
			return nil, fmt.Errorf("invalid crawl plan entry %d", i)
		}
		lanes = append(lanes, CrawlLane{ProxyURL: "http://" + crawlLaneAddress(port), ExitIP: ip.Unmap().String(), Node: node})
	}
	if err := verifyCrawlSelections(ctx, endpoints, lanes); err != nil {
		return nil, err
	}
	if err := checkCrawlLanes(endpoints.ports, func(_ int, port int) error {
		return checkCrawlListener(ctx, port, ownerPID)
	}); err != nil {
		return nil, err
	}
	slots, err := currentCrawlSlots(ctx, db, dataDir)
	if err != nil {
		return nil, err
	}
	if len(lanes) != len(slots) {
		return nil, errors.New("crawl plan does not match the current exit count")
	}
	for i, slot := range slots {
		if lanes[i].ExitIP != slot.ExitIP || lanes[i].Node != slot.Node {
			return nil, fmt.Errorf("crawl lane %d does not match the current exit slot", i)
		}
	}
	return lanes, nil
}

func verifyCrawlSelections(ctx context.Context, endpoints crawlRuntimeEndpoints, lanes []CrawlLane) error {
	client := probeHTTPClient(crawlLaneReadyTimeout)
	defer client.CloseIdleConnections()
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoints.controller+"/proxies", nil)
	if err != nil {
		return err
	}
	if endpoints.secret != "" {
		req.Header.Set("Authorization", "Bearer "+endpoints.secret)
	}
	resp, err := client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("crawl controller returned HTTP %d", resp.StatusCode)
	}
	var payload struct {
		Proxies map[string]struct{ Now string } `json:"proxies"`
	}
	if err := json.NewDecoder(io.LimitReader(resp.Body, 8<<20)).Decode(&payload); err != nil {
		return err
	}
	for i, lane := range lanes {
		_, loaded := payload.Proxies[lane.Node]
		if !loaded || payload.Proxies[crawlLaneGroup(i)].Now != lane.Node {
			return fmt.Errorf("crawl lane %d does not match the running kernel selection", i)
		}
	}
	return nil
}
