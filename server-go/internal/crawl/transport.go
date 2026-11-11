package crawl

import (
	"context"
	"errors"
	"net/netip"
	"net/url"
	"strconv"
	"strings"

	"holdexar/server-go/internal/crawler"
	"holdexar/server-go/internal/netenv"
	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/steamhttp"
	"holdexar/server-go/internal/storebrowse"
)

var errProxyNotReady = errors.New("代理抓取通道尚未就绪，已保留本阶段待后续更新")

const networkBlockedReason = netenv.TakeoverReason
const proxyRepairWorkers = 6

type crawlFetch func(context.Context, []int64, string, string, bool) (map[int64]storebrowse.Item, error)

type crawlTransport struct {
	direct      crawlFetch
	lanes       []*crawler.Lane
	proxyURL    string
	workerCount int
}

type Readiness struct {
	SchedulerReady   bool   `json:"schedulerReady"`
	CollectionReady  bool   `json:"collectionReady"`
	AutomaticEnabled bool   `json:"automaticEnabled"`
	TransportReady   bool   `json:"transportReady"`
	Strategy         string `json:"strategy"`
	Lanes            int    `json:"lanes"`
	Reason           string `json:"reason"`
	ActiveJobID      any    `json:"activeJobId"`
}

func (r *Runtime) strategy(ctx context.Context) (string, error) {
	value, err := r.setting(ctx, "proxy.strategy", "direct_only")
	strategy, ok := value.(string)
	if !ok {
		strategy = "proxy_first"
	}
	return strategy, err
}

func (r *Runtime) Readiness(ctx context.Context, readLanes func(context.Context) ([]proxypool.CrawlLane, error)) (Readiness, error) {
	r.mu.Lock()
	scheduler, activeJob := r.scheduler, nullableID(r.jobID)
	r.mu.Unlock()
	state := Readiness{SchedulerReady: scheduler != nil && scheduler.Running(), ActiveJobID: activeJob}
	var err error
	state.AutomaticEnabled, err = r.AutoEnabled(ctx)
	if err != nil {
		return state, err
	}
	state.Strategy, err = r.strategy(ctx)
	if err != nil {
		return state, err
	}
	regions, err := r.planner.Regions(ctx, nil)
	if err != nil && !errors.Is(err, errNoRegions) {
		return state, err
	}
	switch {
	case r.opts.NetworkBlocked != nil && r.opts.NetworkBlocked(ctx):
		state.Reason = networkBlockedReason
	case state.Strategy == "direct_only":
		state.TransportReady = true
	default:
		lanes, err := readLanes(ctx)
		state.TransportReady = err == nil && len(lanes) > 0
		if state.TransportReady {
			state.Lanes = len(lanes)
		} else {
			state.Reason = errProxyNotReady.Error()
		}
	}
	if len(regions) == 0 {
		state.Reason = "未启用采集地区"
	}
	state.CollectionReady = state.SchedulerReady && len(regions) > 0 && state.TransportReady
	return state, nil
}

func (r *Runtime) planProxyTransport(ctx context.Context, kind string, trace *proxyRunTrace) (crawlTransport, error) {
	if r.opts.ProxyLanes == nil {
		return crawlTransport{}, errProxyNotReady
	}
	plan, err := r.opts.ProxyLanes(ctx)
	if err != nil {
		if ctx.Err() != nil {
			return crawlTransport{}, ctx.Err()
		}
		r.opts.Log("代理抓取通道未就绪：%v", err)
		return crawlTransport{}, errProxyNotReady
	}
	if len(plan) == 0 {
		return crawlTransport{}, errProxyNotReady
	}
	workers := min(len(plan), crawler.MaxWorkers)
	if kind == "missing" || kind == "repair" {
		workers = min(workers, proxyRepairWorkers)
	}
	configured, err := r.setting(ctx, "crawl.workers", nil)
	if err != nil {
		return crawlTransport{}, err
	}
	if n, ok := configured.(float64); ok && n >= 1 && n < float64(workers) {
		workers = int(n)
	}
	keys := make([]string, len(plan))
	for i, lane := range plan {
		ip, err := netip.ParseAddr(lane.ExitIP)
		if err != nil || ip.Zone() != "" || strings.TrimSpace(lane.Node) == "" || !validLaneProxy(lane.ProxyURL) {
			r.opts.Log("代理抓取通道未就绪：第 %d 条通道缺少有效的代理地址或出口身份", i+1)
			return crawlTransport{}, errProxyNotReady
		}
		keys[i] = ip.Unmap().String()
	}
	limits := crawler.NewExitRateLimits(keys, crawler.DefaultRateMaxRequests, crawler.DefaultRateWindow, nil)
	breakers := crawler.NewExitBreakers(keys, nil)
	transport := crawlTransport{lanes: make([]*crawler.Lane, len(plan)), proxyURL: plan[0].ProxyURL, workerCount: workers}
	for i, entry := range plan {
		lane := &crawler.Lane{}
		trace.nodes[keys[i]] = entry.Node
		client := steamhttp.New(steamhttp.Config{
			ProxyURL: entry.ProxyURL, RateLimits: limits, Breakers: breakers,
			OnExitExhausted: func(string) { lane.MarkUnhealthy() },
			OnRequest: func(record steamhttp.RequestRecord) {
				if record.Outcome == steamhttp.OutcomeOK {
					lane.MarkHealthy()
				}
				trace.noteRequest(record)
			},
		})
		lane.Client = r.bindFetch(client, keys[i])
		transport.lanes[i] = lane
	}
	return transport, nil
}

func validLaneProxy(raw string) bool {
	u, err := url.Parse(raw)
	if err != nil || u.Scheme != "http" || u.User != nil || u.Path != "" || u.ForceQuery || u.RawQuery != "" || u.Fragment != "" {
		return false
	}
	ip, err := netip.ParseAddr(u.Hostname())
	if err != nil || !ip.IsLoopback() {
		return false
	}
	port, err := strconv.Atoi(u.Port())
	return err == nil && port > 0 && port <= 65535
}

func (r *Runtime) directTransport(ctx context.Context) (crawlTransport, error) {
	if r.opts.Fetch != nil {
		return crawlTransport{direct: r.opts.Fetch}, nil
	}
	proxy, err := netenv.ResolveSystemProxy(ctx)
	if err != nil {
		return crawlTransport{}, err
	}
	client := steamhttp.New(steamhttp.Config{ProxyURL: proxy})
	recordedProxy := ""
	if endpoint, err := url.Parse(proxy); err == nil && endpoint.Host != "" {
		recordedProxy = endpoint.Scheme + "://" + endpoint.Host
	}
	return crawlTransport{direct: r.bindFetch(client, ""), proxyURL: recordedProxy}, nil
}

func (r *Runtime) bindFetch(client *steamhttp.Client, exitKey string) crawlFetch {
	endpoint := r.opts.StoreBrowseURL
	if endpoint == "" {
		endpoint = storebrowse.BrowseURL
	}
	return func(ctx context.Context, ids []int64, cc, lang string, extras bool) (map[int64]storebrowse.Item, error) {
		batch, err := storebrowse.FetchAt(ctx, client, endpoint, ids, cc, lang, extras, exitKey)
		return batch.Items, err
	}
}

func (t crawlTransport) scheduler(router *crawler.Router, opts ...crawler.Option) *crawler.Scheduler {
	if len(t.lanes) == 0 {
		return crawler.NewScheduler(router, append(opts, crawler.WithWorkers(t.workers()), crawler.WithSharedClient(t.direct))...)
	}
	return crawler.NewScheduler(router, append(opts,
		crawler.WithWorkers(t.workers()),
		crawler.WithLaneCount(len(t.lanes)),
		crawler.WithLaneFactory(func(i int) *crawler.Lane { return t.lanes[i] }),
	)...)
}

func (t crawlTransport) workers() int {
	if len(t.lanes) == 0 {
		return crawler.MaxWorkers
	}
	return t.workerCount
}

func runBatches(ctx context.Context, pool *crawler.Scheduler, work []crawler.Task) {
	if ctx.Err() != nil {
		pool.Stop()
	}
	stop := context.AfterFunc(ctx, pool.Stop)
	defer stop()
	pool.Run(work)
}
