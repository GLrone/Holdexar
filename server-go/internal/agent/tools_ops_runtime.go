// 批次三工具面：运维 / 代理 / 诊断 / 运行时任务 / 网络出站（25 个，全部
// group=read）。形状逐条对齐 Python builtin/system.py、proxy_ops.py、
// crawlops.py、runtime_tools.py、catalog.py；fail-closed：只注册 Go 侧有
// 真实实现的只读投影，POST 只走同步受理类端点（与 Python MCP 准入面同闸）。
package agent

import (
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/netenv"
)

// networkTakenOver 本机接管判定（var 仅为测试注入缝；生产 = netenv.NetworkTakenOver）。
var networkTakenOver = netenv.NetworkTakenOver

// ─── 常量与词表 ─────────────────────────────────────────────────────────────

// priceEventTypes 价格事实事件闭集（Python crawl events.EVENT_TYPES）。
var priceEventTypes = map[string]bool{
	"PRICE_DROP": true, "PRICE_INCREASE": true,
	"NEW_HISTORICAL_LOW": true, "HISTORICAL_LOW_MATCH": true,
	"PERMANENT_PRICE_CHANGE": true,
	"REGION_LOCKED": true, "REGION_UNLOCKED": true,
	"PRICE_UNAVAILABLE": true, "PRICE_RESTORED": true,
	"FREE_PROMO": true, "REMOVED": true,
}

// taskLabels 任务类型 → 行词条（Python agent_service.TASK_LABELS）。
var taskLabels = map[string]string{
	"price_refresh": "priceRefresh", "price_repair": "priceRepair",
	"price_missing": "priceMissing", "dev_pytest": "devPytest",
}

// domainStatusToRun 域终态/执行态 → run 状态（Python _DOMAIN_STATUSES）。
var domainStatusToRun = map[string]string{
	"completed": "done", "partial": "done", "failed": "failed", "cancelled": "cancelled",
	"done": "done", "stopped": "cancelled",
	"pending": "queued", "planning": "running", "running": "running",
	"repairing": "running", "finalizing": "running", "interrupted": "failed",
}

// taskStatusRow run 状态 →（行值词条, 色调）（Python _TASK_STATUS_ROW）。
var taskStatusRow = map[string][2]string{
	"queued":            {"taskQueued", "warn"},
	"running":           {"taskRunning", "ok"},
	"awaiting_approval": {"taskQueued", "warn"},
	"done":              {"taskDone", "ok"},
	"failed":            {"taskFailed", "bad"},
	"cancelled":         {"taskCancelled", "warn"},
	"budget_exhausted":  {"taskCancelled", "warn"},
}

func isTerminalRunStatus(status string) bool {
	switch status {
	case "done", "failed", "cancelled", "budget_exhausted":
		return true
	}
	return false
}

// logSeverityWords 日志行首 [结果词] → severity（Python core/logging.py
// TAG_SEVERITY 同表；Go 日志行同格式：HH:MM:SS [结果词] 模块 · 主句）。
var logSeverityWords = map[string]string{
	"调试": "info", "信息": "info", "成功": "info", "开始": "info", "结束": "info",
	"清理": "info", "跳过": "info", "已修复": "info", "忽略": "info",
	"复用": "info", "延后": "info",
	"注意": "warning", "降级": "warning", "拒绝": "warning", "未完成": "warning",
	"失败": "error", "严重": "error",
}

// logSeverityFilters 级别过滤集（Python _LOG_SEVERITY_FILTERS）。
var logSeverityFilters = map[string]string{
	"info": "info", "warning": "warning", "error": "error",
}

// 出站常量：DuckDuckGo HTML 端点与 Steam storesearch（Python system.py /
// crawler/epic_free.py 同值）。
const (
	ddgSearchURL     = "https://html.duckduckgo.com/html/"
	ddgUserAgent     = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
	steamSearchURL   = "https://store.steampowered.com/api/storesearch/"
	steamSearchLimit = 5
	ingestMaxAppids  = 20
	logSnapshotLimit = 2000
	logTailMax       = 400
	writeLockWaitWarnMs = 30000
	writeLedgerSamples  = 8
)

// 出口延迟检测轮询窗（var 仅为测试可缩窗；生产语义 = 170s 略低于执行预算
// 180s、2s 步长，Python 同值）。
var (
	proxyTestWaitS = 170.0
	proxyTestPollS = 2.0
)

var steamHTTPHeaders = map[string]string{
	"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
		"(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
	"Accept":          "application/json",
	"Accept-Language": "en-US,en;q=0.9",
	"Referer":         "https://store.epicgames.com/en-US/",
}

// ─── 小件：形状与 Python 标量语义 ───────────────────────────────────────────

// rowsCard rows_result 移植：行截 8；total 传 nil 时按 Python 语义不落键。
func rowsCard(title string, rows []any, total any) *Obj {
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", title)
	if len(rows) > readRowsLimit {
		rows = rows[:readRowsLimit]
	}
	out.set("rows", rows)
	if total != nil {
		out.set("total", total)
	}
	return out
}

// truncateRunes Python s[:n] 字符语义（按 rune 截，不碎 UTF-8）。
func truncateRunes(s string, n int) string {
	runes := []rune(s)
	if len(runes) <= n {
		return s
	}
	return string(runes[:n])
}

// pyFStr Python f-string 标量形态：None → "None"；其余按 str()。
func pyFStr(v any) string {
	switch x := v.(type) {
	case nil:
		return "None"
	case string:
		return x
	case json.Number:
		return x.String()
	case bool:
		if x {
			return "True"
		}
		return "False"
	default:
		return fmt.Sprintf("%v", x)
	}
}

// notAvailable 依赖件未装配（Deps 零值）的不可用形状：failed 归一，
// 不冒充空数据（agent.go Deps 契约）。
func notAvailable() *ToolResult {
	obj := newObj()
	obj.set("kind", "failed")
	obj.set("note", "not_available")
	return &ToolResult{Status: StatusError, Data: obj, ErrCode: "tool_error"}
}

// postRaw 进程内 POST 并返回原始状态码与解码体（409/400 分支工具用）；
// 空响应体解码为 nil。
func (c *CallContext) postRaw(path string, body any) (int, map[string]any, error) {
	encoded, err := json.Marshal(body)
	if err != nil {
		return 0, nil, err
	}
	status, respBody, err := c.post(c.Ctx, path, encoded)
	if err != nil {
		return status, nil, err
	}
	var out map[string]any
	if len(respBody) > 0 {
		if err := jsonDecoder(respBody).Decode(&out); err != nil {
			out = nil
		}
	}
	return status, out, nil
}

// outboundClient 出网 HTTP client：D4 代理解析（未装配/解析失败按直连），
// 12s 总超时（Python 出网工具同口径）。
func (c *CallContext) outboundClient() *http.Client {
	transport := &http.Transport{}
	if c.deps != nil && c.deps.ResolveProxy != nil {
		if proxyURL, ok := c.deps.ResolveProxy(c.Ctx); ok && proxyURL != "" {
			if parsed, err := url.Parse(proxyURL); err == nil {
				transport.Proxy = http.ProxyURL(parsed)
			}
		}
	}
	return &http.Client{Transport: transport, Timeout: 12 * time.Second}
}

// intArgs 整数入参列表（Python [int(a) for a in raw]；解析失败按工具失败）。
func intArgs(v any) ([]int64, error) {
	raw, _ := v.([]any)
	out := make([]int64, 0, len(raw))
	for _, item := range raw {
		n, err := pyIntOr(item)
		if err != nil {
			return nil, err
		}
		out = append(out, n)
	}
	return out, nil
}

// gameName 库内游戏名（Python games_service.names_for 单点；无行回空，
// 其余库错误按工具失败上抛）。
func (c *CallContext) gameName(appid int64) (string, error) {
	var name string
	err := c.db.QueryRowContext(c.Ctx, `SELECT name FROM games WHERE appid=?`, appid).Scan(&name)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	return name, nil
}

// limitArg Python「or 缺省」语义的 limit 入参：0/空/缺席都用缺省值。
func limitArg(v any, def int64) any {
	if !pyBool(v) {
		return nil
	}
	return v
}

// ─── 爬取运维读面（crawlops.py）────────────────────────────────────────────

// crawlActive 当前活动任务、占用与限流排队（GET /crawl/active 同源投影）。
func crawlActive(c *CallContext) (*ToolResult, error) {
	snap := fetchMap(c, "/api/v1/crawl/active")
	if snap == nil {
		return nil, fmt.Errorf("内部读取失败：抓取状态不可用")
	}
	busy := pyBool(snap["busy"])
	rows := []any{}
	row := newObj()
	row.set("k", "抓取进行中")
	if busy {
		row.set("v", "是")
		row.set("tone", "ok")
	} else {
		row.set("v", "否")
		row.set("tone", "warn")
	}
	rows = append(rows, row)
	active := newObj()
	active.set("k", "活动任务")
	if pyBool(snap["activeJobId"]) {
		active.set("v", pyStrOr(snap["activeJobId"]))
	} else {
		active.set("v", "无")
	}
	rows = append(rows, active)
	if pyBool(snap["maintenance"]) {
		info, _ := snap["maintenanceInfo"].(map[string]any)
		mrow := newObj()
		mrow.set("k", "池体检进行中")
		mrow.set("v", pyStrOr(mapGet(info, "kind")))
		mrow.set("tone", "warn")
		rows = append(rows, mrow)
	}
	if throttled := statInt(snap, "throttled"); throttled != 0 {
		trow := newObj()
		trow.set("k", "限流排队")
		trow.set("v", strconv.FormatInt(throttled, 10))
		trow.set("tone", "warn")
		rows = append(rows, trow)
	}
	return &ToolResult{Data: rowsCard("crawlActive", rows, nil)}, nil
}

// priceFreshness 对象级价格新鲜度 + 全库时钟（deps.AppIDFreshness + GET
// /crawl/freshness/latest；对象一次最多 200 个，去重保序）。
func priceFreshness(c *CallContext) (*ToolResult, error) {
	if c.deps == nil || c.deps.AppIDFreshness == nil {
		return notAvailable(), nil
	}
	appids, err := intArgs(c.Args["appids"])
	if err != nil {
		return nil, err
	}
	seen := map[int64]bool{}
	clean := []int64{}
	for _, id := range appids {
		if id <= 0 || seen[id] {
			continue
		}
		seen[id] = true
		clean = append(clean, id)
		if len(clean) >= 200 {
			break
		}
	}
	if len(clean) == 0 {
		return emptyResult("no_appids"), nil
	}
	items, err := c.deps.AppIDFreshness(c.Ctx, clean)
	if err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(items))
	for _, it := range items {
		row := newObj()
		row.set("k", pyStrOr(it["appid"]))
		fresh := pyStrOr(it["freshness"])
		if fresh == "" {
			fresh = "no_data"
		}
		row.set("v", fresh)
		row.set("at", it["observedAt"])
		rows = append(rows, row)
	}
	var latest map[string]any
	if err := c.getJSON("/api/v1/crawl/freshness/latest", &latest); err != nil {
		return nil, err
	}
	out := rowsCard("freshness", rows, len(rows))
	out.set("lastSuccessAt", latest["lastSuccessAt"])
	return &ToolResult{Data: out}, nil
}

// recentCycles 最近价格刷新轮次（新→旧，默认 5 条）。
func recentCycles(c *CallContext) (*ToolResult, error) {
	limit := clampInt(limitArg(c.Args["limit"], 5), 5, 1, 20)
	var cycles []map[string]any
	if err := c.getJSON("/api/v1/crawl/cycles?limit="+strconv.FormatInt(limit, 10), &cycles); err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(cycles))
	for _, cy := range cycles {
		row := newObj()
		row.set("k", "#"+pyStrOr(cy["id"]))
		row.set("v", pyStrOr(cy["status"]))
		row.set("v2", pyFStr(cy["batchesDone"])+"/"+pyFStr(cy["batchesExpected"]))
		at := cy["finishedAt"]
		if !pyBool(at) {
			at = cy["startedAt"]
		}
		row.set("at", at)
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("cycles", rows, len(rows))}, nil
}

// priceEvents 最近价格事实事件（可按事件类型/游戏/轮次过滤）。
func priceEvents(c *CallContext) (*ToolResult, error) {
	eventType := pyStrOr(c.Args["event_type"])
	if eventType != "" && !priceEventTypes[eventType] {
		return emptyResult("bad_event_type"), nil
	}
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	cycleID, err := toolAppid(map[string]any{"appid": c.Args["cycle_id"]})
	if err != nil {
		return nil, err
	}
	limit := clampInt(limitArg(c.Args["limit"], 20), 20, 1, 100)
	query := urlValues()
	query.set("limit", strconv.FormatInt(limit, 10))
	if cycleID != 0 {
		query.set("cycle_id", strconv.FormatInt(cycleID, 10))
	}
	if appid != 0 {
		query.set("appid", strconv.FormatInt(appid, 10))
	}
	if eventType != "" {
		query.set("event_type", eventType)
	}
	var events []map[string]any
	if err := c.getJSON("/api/v1/crawl/price-events?"+query.encode(), &events); err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(events))
	for _, ev := range events {
		row := newObj()
		row.set("k", pyStrOr(ev["eventType"])+"·"+pyStrOr(ev["appid"]))
		row.set("v", pyStrOr(ev["region"]))
		at := ev["occurredAt"]
		if !pyBool(at) {
			at = ev["detectedAt"]
		}
		row.set("at", at)
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("priceEvents", rows, len(rows))}, nil
}

// crawlStop 停止当前爬取任务（整链停止，与任务页停止按钮同义）。
func crawlStop(c *CallContext) (*ToolResult, error) {
	var out map[string]any
	if err := c.postJSON("/api/v1/crawl/stop?chain=true", nil, &out); err != nil {
		return nil, err
	}
	ok := pyBool(out["stopped"])
	row := newObj()
	row.set("k", "停止请求")
	if ok {
		row.set("v", "已受理")
		row.set("tone", "ok")
	} else {
		row.set("v", "当前无任务")
		row.set("tone", "warn")
	}
	return &ToolResult{Data: rowsCard("crawlStop", []any{row}, nil)}, nil
}

// recentJobFailures 最近失败的抓取任务与原因（crawl_jobs 账本只读投影）。
func recentJobFailures(c *CallContext) (*ToolResult, error) {
	var jobs []map[string]any
	if err := c.getJSON("/api/v1/crawl/jobs?limit=20", &jobs); err != nil {
		return nil, err
	}
	rows := []any{}
	total := 0
	for _, j := range jobs {
		if pyStrOr(j["status"]) != "failed" {
			continue
		}
		total++
		row := newObj()
		k := "#" + pyStrOr(j["id"]) + " " + pyStrOr(j["kind"])
		row.set("k", strings.TrimSpace(k))
		row.set("v", truncateRunes(pyStrOr(j["error"]), 80))
		row.set("at", j["startedAt"])
		row.set("tone", "bad")
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("jobs", rows, total)}, nil
}

// ─── 写闸 / 日志 / 诊断 ─────────────────────────────────────────────────────

// writeGateStatus 写入调度状态（deps.WriteGate.Diagnostics 快照）。
func writeGateStatus(c *CallContext) (*ToolResult, error) {
	if c.deps == nil || c.deps.WriteGate == nil {
		return notAvailable(), nil
	}
	d := c.deps.WriteGate.Diagnostics(c.Ctx)
	if d == nil {
		return notAvailable(), nil
	}
	waiting := statInt(d, "waiting_interactive") + statInt(d, "waiting_background")
	busyRow := newObj()
	busyRow.set("k", "")
	if pyBool(d["busy"]) {
		busyRow.set("vKey", "gateBusy")
		busyRow.set("tone", "warn")
	} else {
		busyRow.set("vKey", "gateIdle")
		busyRow.set("tone", "ok")
	}
	busyRow.set("v", pyStrOr(d["owner_label"]))
	waitRow := newObj()
	waitRow.set("k", "")
	waitRow.set("vKey", "gateWaiting")
	data := newObj()
	data.set("n", waiting)
	waitRow.set("data", data)
	if waiting != 0 {
		waitRow.set("tone", "warn")
	} else {
		waitRow.set("tone", "ok")
	}
	return &ToolResult{Data: rowsCard("gate", []any{busyRow, waitRow}, nil)}, nil
}

// logLineSeverity 日志行首 [结果词] → severity（缺词回空）。
func logLineSeverity(line string) string {
	m := logLineTagRe.FindStringSubmatch(line)
	if m == nil {
		return ""
	}
	return logSeverityWords[m[1]]
}

var logLineTagRe = regexp.MustCompile(`\[([^\]]{1,6})\]`)

// readLogs 运行日志最近片段（deps.LogRing 快照；级别/关键词过滤 + 尾部窗口
// + 级别计数）。
func readLogs(c *CallContext) (*ToolResult, error) {
	if c.deps == nil || c.deps.LogRing == nil {
		return notAvailable(), nil
	}
	tail := clampInt(limitArg(c.Args["tail"], 120), 120, 10, int64(logTailMax))
	lines := c.deps.LogRing.Snapshot(logSnapshotLimit)
	if want, ok := logSeverityFilters[strings.ToLower(pyStrOr(c.Args["level"]))]; ok {
		filtered := make([]string, 0, len(lines))
		for _, ln := range lines {
			if logLineSeverity(ln) == want {
				filtered = append(filtered, ln)
			}
		}
		lines = filtered
	}
	if kw := strings.ToLower(strings.TrimSpace(pyStrOr(c.Args["keyword"]))); kw != "" {
		filtered := make([]string, 0, len(lines))
		for _, ln := range lines {
			if strings.Contains(strings.ToLower(ln), kw) {
				filtered = append(filtered, ln)
			}
		}
		lines = filtered
	}
	counts := newObj()
	for _, ln := range lines {
		sev := logLineSeverity(ln)
		if sev == "" {
			sev = "unknown"
		}
		if cur, ok := counts.get(sev).(int64); ok {
			counts.set(sev, cur+1)
		} else {
			counts.set(sev, int64(1))
		}
	}
	from := 0
	if len(lines) > int(tail) {
		from = len(lines) - int(tail)
	}
	rows := []any{}
	for _, ln := range lines[from:] {
		head, body, sep := strings.Cut(ln, " · ")
		if !sep {
			head, body = ln, ""
		}
		row := newObj()
		row.set("k", truncateRunes(head, 56))
		row.set("v", truncateRunes(body, 200))
		switch logLineSeverity(ln) {
		case "warning":
			row.set("tone", "warn")
		case "error":
			row.set("tone", "bad")
		default:
			row.set("tone", "")
		}
		rows = append(rows, row)
	}
	out := rowsCard("logs", rows, len(lines))
	if len(counts.keys) > 0 {
		out.set("counts", counts)
	}
	return &ToolResult{Data: out}, nil
}

// writeLedgerSample 写账样本（信号派生只用等写锁）。
type writeLedgerSample struct {
	lockwaitMs int64
}

// parseWriteLedger 环形日志 → 写账样本（Python _parse_write_ledger 同构；
// 只取 等写锁 一个数）。
func parseWriteLedger(lines []string, limit int) []writeLedgerSample {
	out := []writeLedgerSample{}
	for i := len(lines) - 1; i >= 0 && len(out) < limit; i-- {
		line := lines[i]
		if !strings.Contains(line, "写库分段完成") {
			continue
		}
		_, tail, hasTail := strings.Cut(line, " │ ")
		if !hasTail {
			continue
		}
		for _, pair := range strings.Fields(tail) {
			key, value, _ := strings.Cut(pair, "=")
			if key != "等写锁" {
				continue
			}
			digits := strings.Map(func(r rune) rune {
				if r >= '0' && r <= '9' {
					return r
				}
				return -1
			}, value)
			if digits != "" {
				if n, err := strconv.ParseInt(digits, 10, 64); err == nil {
					out = append(out, writeLedgerSample{lockwaitMs: n})
				}
			}
			break
		}
	}
	return out
}

// diagSignal 诊断信号行原料。
type diagSignal struct {
	code     string
	severity string
	text     string
}

// systemDiagnostics 机器排障证据面快照：身份 + 最近价格轮 + 异常信号
// （Python system/router.diagnostics 五段聚合中工具投影消费的子集；
// 一面失败不拖垮整体；六信号按 Python 派生序产出）。
func systemDiagnostics(c *CallContext) (*ToolResult, error) {
	rows := []any{}
	version := ""
	if info := fetchMap(c, "/api/v1/info"); info != nil {
		version = pyStrOr(info["version"])
	}
	strategy := ""
	exits := int64(0)
	identityOK := false
	if readiness := fetchMap(c, "/api/v1/system/readiness"); readiness != nil {
		strategy = pyStrOr(readiness["strategy"])
		poolOK, _, clashOK, _ := proxyAvailabilityCounts(c)
		if poolOK >= 0 {
			exits = poolOK + clashOK
			identityOK = true
		}
	}
	var cycles []map[string]any
	cyclesErr := c.getJSON("/api/v1/crawl/cycles?limit=5", &cycles)
	var lastCycleStatus, lastCycleError string
	if cyclesErr == nil && len(cycles) > 0 {
		lastCycleStatus = pyStrOr(cycles[0]["status"])
		lastCycleError = pyStrOr(cycles[0]["error"])
	}
	var writeLockMax int64
	hasWriteLock := false
	if c.deps != nil && c.deps.LogRing != nil {
		samples := parseWriteLedger(c.deps.LogRing.Snapshot(logSnapshotLimit), writeLedgerSamples)
		for _, s := range samples {
			if !hasWriteLock || s.lockwaitMs > writeLockMax {
				writeLockMax = s.lockwaitMs
				hasWriteLock = true
			}
		}
	}
	jobBad, jobTotal := failedJobRunCount(c)
	missingPending := pendingMissing(c)
	networkHijacked := identityOK && networkTakenOver(c.Ctx)
	needsPool := identityOK && (strategy == "proxy_first" || strategy == "proxy_only")
	poolUnavailable := needsPool && exits == 0
	if identityOK {
		row := newObj()
		row.set("k", "v"+pyFStr(orStr(version, "?")))
		row.set("v", "策略 "+pyFStr(orStr(strategy, "?"))+" · 出口 "+pyFStr(orStr(strconv.FormatInt(exits, 10), "?")))
		row.set("tone", "ok")
		rows = append(rows, row)
	}
	if lastCycleStatus != "" {
		row := newObj()
		row.set("k", "价格轮")
		row.set("v", "最近一轮 "+pyFStr(orStr(lastCycleStatus, "未知")))
		if lastCycleStatus == "completed" || lastCycleStatus == "partial" {
			row.set("tone", "ok")
		} else {
			row.set("tone", "bad")
		}
		rows = append(rows, row)
	}
	signals := []diagSignal{}
	if lastCycleStatus == "failed" {
		text := lastCycleError
		if text == "" {
			text = "原因未随轮留痕"
		}
		signals = append(signals, diagSignal{
			code: "last_cycle_failed", severity: "error",
			text: "上一轮价格刷新失败：" + text,
		})
	}
	if hasWriteLock && writeLockMax > writeLockWaitWarnMs {
		signals = append(signals, diagSignal{
			code: "write_lock_wait_high", severity: "warning",
			text: fmt.Sprintf("最近写账等写锁最高 %d 毫秒（预警线 %d），写库可能存在竞争",
				writeLockMax, writeLockWaitWarnMs),
		})
	}
	if jobTotal >= 0 && jobBad > 0 {
		signals = append(signals, diagSignal{
			code: "job_runs_failed", severity: "warning",
			text: fmt.Sprintf("最近 %d 次生产作业 %d 次失败/中断（作业台账面）", jobTotal, jobBad),
		})
	}
	if missingPending {
		signals = append(signals, diagSignal{
			code: "missing_backlog", severity: "info",
			text: "存在待补抓欠账（补抓通道按冷却自动消化）",
		})
	}
	if networkHijacked {
		signals = append(signals, diagSignal{
			code: "network_hijacked", severity: "error",
			text: netenv.TakeoverReason + "（节点体检与价格刷新已让路）",
		})
	}
	if poolUnavailable {
		signals = append(signals, diagSignal{
			code: "proxy_pool_unavailable", severity: "error",
			text: "当前策略需要代理池但无可用出口，价格轮会被拒绝（到网络页确认订阅与节点）",
		})
	}
	for i, s := range signals {
		if i >= 10 {
			break
		}
		row := newObj()
		row.set("k", truncateRunes(s.code, 32))
		row.set("v", truncateRunes(s.text, 140))
		switch s.severity {
		case "error":
			row.set("tone", "bad")
		case "warning":
			row.set("tone", "warn")
		default:
			row.set("tone", "")
		}
		rows = append(rows, row)
	}
	if len(signals) == 0 {
		row := newObj()
		row.set("k", "")
		row.set("v", "诊断面未检出异常信号")
		row.set("tone", "ok")
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("diag", rows, len(rows))}, nil
}

// orStr 空串兑底。
func orStr(s, fallback string) string {
	if s == "" {
		return fallback
	}
	return s
}

// fetchMap 单面 GET（失败回 nil：诊断面自身必须可用，Python
// _diagnostics_section 同语义）。
func fetchMap(c *CallContext, path string) map[string]any {
	var out map[string]any
	if err := c.getJSON(path, &out); err != nil {
		return nil
	}
	return out
}

// proxyAvailabilityCounts 可用性统计（Python pool_stats 口径）：手动池
// enabled&ok 计数 + Clash 按出口 IP 去重（ACTIVE/STALE 且有最近 L2 证据、
// 当前订阅未废弃才算可用）。pool/exit 负值 = 读取失败。
func proxyAvailabilityCounts(c *CallContext) (poolOK, poolTotal, exitAll, exitOK int64) {
	if err := c.db.QueryRowContext(c.Ctx,
		`SELECT COUNT(*), COALESCE(SUM(CASE WHEN enabled=1 AND status='ok' THEN 1 ELSE 0 END),0)
		 FROM proxies`).Scan(&poolTotal, &poolOK); err != nil {
		poolOK, poolTotal = -1, -1
	}
	clash := fetchMap(c, "/api/v1/proxies/clash")
	if clash == nil || !pyBool(clash["running"]) {
		return poolOK, poolTotal, 0, 0
	}
	conds := "1=1"
	args := []any{}
	subscriptionReady := false
	if subID := statInt(clash, "selectedSubscriptionId"); subID > 0 {
		conds = "subscription_id=?"
		args = append(args, subID)
		if err := c.db.QueryRowContext(c.Ctx,
			`SELECT EXISTS(SELECT 1 FROM proxy_subscriptions WHERE id=? AND COALESCE(deprecated,0)=0)`,
			subID).Scan(&subscriptionReady); err != nil {
			subscriptionReady = false
		}
	}
	exitRows, err := c.db.QueryContext(c.Ctx,
		`SELECT COALESCE(exit_ip,''), node_id, state, last_l2_at FROM proxy_nodes WHERE `+conds, args...)
	if err != nil {
		return poolOK, poolTotal, -1, -1
	}
	defer exitRows.Close()
	allIdents := map[string]bool{}
	okIdents := map[string]bool{}
	for exitRows.Next() {
		var exitIP, nodeID, state string
		var lastL2 any
		if err := exitRows.Scan(&exitIP, &nodeID, &state, &lastL2); err != nil {
			break
		}
		ident := exitIP
		if ident == "" {
			ident = "node:" + nodeID
		}
		allIdents[ident] = true
		if (state == "ACTIVE" || state == "STALE") && lastL2 != nil && subscriptionReady {
			okIdents[ident] = true
		}
	}
	return poolOK, poolTotal, int64(len(allIdents)), int64(len(okIdents))
}

// pendingMissing 是否存在可重试的待补欠账（missing 行 + 冷却外；Go 侧无
// 恢复排除面，直接对账本判据做直读近似，冷却 4 分钟）。
func pendingMissing(c *CallContext) bool {
	cutoff := time.Now().Add(-4 * time.Minute).Format("2006-01-02 15:04:05")
	var exists bool
	if err := c.db.QueryRowContext(c.Ctx,
		`SELECT EXISTS(SELECT 1 FROM game_current_prices
		 WHERE price_status='missing' AND region_code!='' AND updated_at < ?)`,
		cutoff).Scan(&exists); err != nil {
		return false
	}
	return exists
}

// failedJobRunCount 最近 10 次生产作业中的失败/中断数（total<0 = 读失败）。
func failedJobRunCount(c *CallContext) (int64, int64) {
	payload := fetchMap(c, "/api/v1/proxypool/job-runs?limit=10")
	if payload == nil {
		return 0, -1
	}
	items, _ := payload["items"].([]any)
	var bad, total int64
	for _, raw := range items {
		it, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		total++
		switch pyStrOr(it["status"]) {
		case "failed", "interrupted":
			bad++
		}
	}
	return bad, total
}

// ─── 代理面（proxy_ops.py / system.py 代理段）──────────────────────────────

// hostOf 订阅 URL → 域名级摘要（scheme://host；路径/查询里的 token 不出工具面）。
func hostOf(raw string) string {
	if raw == "" {
		return ""
	}
	parsed, err := url.Parse(raw)
	if err != nil {
		return ""
	}
	host := parsed.Hostname()
	if host == "" || parsed.Scheme == "" {
		return host
	}
	return parsed.Scheme + "://" + host
}

// subRow 订阅视图 → 工具行（绝不含订阅链接本身）。
func subRow(s map[string]any) *Obj {
	stats, _ := s["lastStats"].(map[string]any)
	bits := []string{}
	if host := hostOf(pyStrOr(s["url"])); host != "" {
		bits = append(bits, host)
	}
	alive, total := statInt(stats, "alive"), statInt(stats, "total")
	if total != 0 {
		bits = append(bits, fmt.Sprintf("%d/%d", alive, total))
	}
	if imported := pyStrOr(s["lastImportedAt"]); imported != "" {
		bits = append(bits, strings.Replace(truncateRunes(imported, 16), "T", " ", 1))
	}
	row := newObj()
	label := pyStrOr(s["label"])
	if label == "" {
		label = "#" + pyStrOr(s["id"])
	}
	row.set("k", label)
	row.set("v", strings.Join(bits, " · "))
	if pyBool(s["deprecated"]) {
		row.set("tone", "bad")
	} else {
		row.set("tone", "ok")
	}
	return row
}

// listSubscriptionItems GET /api/v1/proxies/subscriptions → items。
func listSubscriptionItems(c *CallContext, kind string) ([]map[string]any, error) {
	path := "/api/v1/proxies/subscriptions"
	if kind != "" {
		path += "?kind=" + kind
	}
	var resp struct {
		Items []map[string]any `json:"items"`
	}
	if err := c.getJSON(path, &resp); err != nil {
		return nil, err
	}
	return resp.Items, nil
}

// subscriptionsList 代理订阅清单（名称、来源域名、存活与上次拉取时间）。
func subscriptionsList(c *CallContext) (*ToolResult, error) {
	subs, err := listSubscriptionItems(c, "")
	if err != nil {
		return nil, err
	}
	if len(subs) == 0 {
		return emptyResult("subs_none"), nil
	}
	rows := make([]any, 0, len(subs))
	for _, s := range subs {
		rows = append(rows, subRow(s))
	}
	return &ToolResult{Data: rowsCard("subs", rows, len(rows))}, nil
}

// subscriptionSync 重拉指定的一条代理订阅。
func subscriptionSync(c *CallContext) (*ToolResult, error) {
	subID := clampInt(c.Args["sub_id"], 0, 0, math.MaxInt64)
	subs, err := listSubscriptionItems(c, "")
	if err != nil {
		return nil, err
	}
	var target map[string]any
	for _, s := range subs {
		if statInt(s, "id") == subID {
			target = s
			break
		}
	}
	if target == nil {
		return emptyResult("sub_missing"), nil
	}
	label := pyStrOr(target["label"])
	if label == "" {
		label = "#" + strconv.FormatInt(subID, 10)
	}
	status, body, err := c.postRaw("/api/v1/proxies/subscriptions/"+strconv.FormatInt(subID, 10)+"/refresh", nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return &ToolResult{Status: StatusError,
			Data: failedSyncResult(truncateRunes(detailText(body), 120))}, nil
	}
	row := newObj()
	row.set("k", label)
	row.set("vKey", "subsSynced")
	row.set("tone", "ok")
	if nodes, ok := body["nodes"].(json.Number); ok {
		data := newObj()
		data.set("n", nodes)
		row.set("data", data)
	}
	return &ToolResult{Data: rowsCard("subs", []any{row}, nil)}, nil
}

// failedSyncResult kind=failed + error 文案（Python {"kind":"failed","error":...}）。
func failedSyncResult(text string) *Obj {
	obj := newObj()
	obj.set("kind", "failed")
	obj.set("error", text)
	return obj
}

// detailText 端点错误体里的 detail 字段（原样文本，空回空）。
func detailText(body map[string]any) string {
	if body == nil {
		return ""
	}
	return pyStrOr(body["detail"])
}

// poolHealthHistory 代理池最近体检台账（最近 20 轮）。
func poolHealthHistory(c *CallContext) (*ToolResult, error) {
	var resp struct {
		Runs []map[string]any `json:"runs"`
	}
	if err := c.getJSON("/api/v1/proxypool/health-runs?limit=20", &resp); err != nil {
		return nil, err
	}
	if len(resp.Runs) == 0 {
		return emptyResult("no_health_runs"), nil
	}
	rows := make([]any, 0, len(resp.Runs))
	for _, r := range resp.Runs {
		failed := statInt(r, "failed")
		bits := []string{
			fmt.Sprintf("覆盖 %d · 可用 %d", statInt(r, "total"), statInt(r, "steamOk")),
		}
		if failed != 0 {
			bits = append(bits, fmt.Sprintf("失败 %d", failed))
		}
		if channel := pyStrOr(r["channel"]); channel != "" {
			bits = append(bits, channel)
		}
		if reason := pyStrOr(r["endReason"]); reason != "" {
			bits = append(bits, reason)
		}
		row := newObj()
		if when := healthRunWhen(pyStrOr(r["startedAt"])); when != "" {
			row.set("k", when)
		} else {
			row.set("k", "#"+pyStrOr(r["id"]))
		}
		row.set("v", strings.Join(bits, " · "))
		if failed != 0 || pyBool(r["endReason"]) {
			row.set("tone", "bad")
		} else {
			row.set("tone", "ok")
		}
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("healthRuns", rows, len(rows))}, nil
}

// healthRunWhen startedAt ISO → "MM-DD HH:MM"（解析失败回空，行键落 #id）。
func healthRunWhen(startedAt string) string {
	at, err := time.Parse("2006-01-02T15:04:05", truncateRunes(startedAt, 19))
	if err != nil {
		return ""
	}
	return at.Format("01-02 15:04")
}

// poolJobRuns 代理生产作业台账（附今日概览行）。
func poolJobRuns(c *CallContext) (*ToolResult, error) {
	limit := clampInt(c.Args["limit"], 10, 1, 100)
	var payload struct {
		Summary map[string]any   `json:"summary"`
		Items   []map[string]any `json:"items"`
	}
	if err := c.getJSON("/api/v1/proxypool/job-runs?limit="+strconv.FormatInt(limit, 10), &payload); err != nil {
		return nil, err
	}
	if len(payload.Items) == 0 {
		return emptyResult("no_job_runs"), nil
	}
	rows := []any{}
	if s := payload.Summary; s != nil && statInt(s, "runs") != 0 {
		head := newObj()
		head.set("k", "今日")
		head.set("v", fmt.Sprintf("%d 轮 · 成功 %d · 失败 %d",
			statInt(s, "runs"), statInt(s, "success"), statInt(s, "failed")))
		switch {
		case statInt(s, "failed") != 0:
			head.set("tone", "bad")
		case statInt(s, "partial") != 0:
			head.set("tone", "warn")
		default:
			head.set("tone", "ok")
		}
		rows = append(rows, head)
	}
	jobRunTones := map[string]string{
		"success": "ok", "partial": "warn", "running": "warn",
		"failed": "bad", "interrupted": "bad",
	}
	for _, it := range payload.Items {
		bits := []string{
			fmt.Sprintf("%d/%d", statInt(it, "successCount"), statInt(it, "taskCount")),
		}
		if kind := pyStrOr(it["kind"]); kind != "" {
			bits = append(bits, kind)
		}
		if node := pyStrOr(it["node"]); node != "" {
			bits = append(bits, node)
		}
		when := strings.Replace(truncateRunes(pyStrOr(it["startedAt"]), 16), "T", " ", 1)
		row := newObj()
		k := "#" + pyStrOr(it["id"]) + " " + when
		row.set("k", strings.TrimSpace(k))
		row.set("v", strings.Join(bits, " · "))
		row.set("tone", jobRunTones[pyStrOr(it["status"])])
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("jobRuns", rows, len(payload.Items))}, nil
}

// proxyPoolStatus 代理通道可用状态（手动池 + Clash 出口计数）。
func proxyPoolStatus(c *CallContext) (*ToolResult, error) {
	poolOK, poolTotal, exitAll, exitOK := proxyAvailabilityCounts(c)
	if poolOK < 0 {
		return nil, fmt.Errorf("内部读取失败：代理池统计不可用")
	}
	clash := fetchMap(c, "/api/v1/proxies/clash")
	if clash == nil {
		return nil, fmt.Errorf("内部读取失败：内核状态不可用")
	}
	running := pyBool(clash["running"])
	poolRow := newObj()
	poolRow.set("k", "")
	poolRow.set("vKey", "proxyPool")
	data := newObj()
	data.set("ok", poolOK)
	data.set("total", poolTotal)
	poolRow.set("data", data)
	if poolOK != 0 {
		poolRow.set("tone", "ok")
	} else {
		poolRow.set("tone", "warn")
	}
	exitRow := newObj()
	exitRow.set("k", "")
	exitRow.set("vKey", "proxyExits")
	exitData := newObj()
	exitData.set("ok", exitOK)
	exitData.set("total", exitAll)
	exitRow.set("data", exitData)
	if exitOK != 0 {
		exitRow.set("tone", "ok")
	} else {
		exitRow.set("tone", "warn")
	}
	runRow := newObj()
	runRow.set("k", "")
	if running {
		runRow.set("vKey", "proxyRunning")
		runRow.set("tone", "ok")
	} else {
		runRow.set("vKey", "proxyStopped")
		runRow.set("tone", "warn")
	}
	return &ToolResult{Data: rowsCard("proxy", []any{poolRow, exitRow, runRow}, nil)}, nil
}

// proxyExitDetail 出口节点明细（节点账本只读投影，不发起探测；存活按延迟
// 升序在前）。
func proxyExitDetail(c *CallContext) (*ToolResult, error) {
	clash := fetchMap(c, "/api/v1/proxies/clash")
	if clash == nil {
		return nil, fmt.Errorf("内部读取失败：内核状态不可用")
	}
	if !pyBool(clash["running"]) {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "proxyStopped")
		row.set("tone", "warn")
		return &ToolResult{Data: rowsCard("proxyExits", []any{row}, nil)}, nil
	}
	conds := "1=1"
	args := []any{}
	if subID := statInt(clash, "selectedSubscriptionId"); subID > 0 {
		conds = "subscription_id=?"
		args = append(args, subID)
	}
	type exitNode struct {
		name   string
		nodeID string
		state  string
		exitIP string
	}
	nodeRows, err := c.db.QueryContext(c.Ctx,
		`SELECT runtime_name, node_id, state, COALESCE(exit_ip,'') FROM proxy_nodes WHERE `+conds+
			` ORDER BY id`, args...)
	if err != nil {
		return nil, err
	}
	nodes := []exitNode{}
	for nodeRows.Next() {
		var n exitNode
		if err := nodeRows.Scan(&n.name, &n.nodeID, &n.state, &n.exitIP); err != nil {
			nodeRows.Close()
			return nil, err
		}
		nodes = append(nodes, n)
	}
	if err := nodeRows.Err(); err != nil {
		nodeRows.Close()
		return nil, err
	}
	nodeRows.Close()
	latencies, err := latestL2Latencies(c)
	if err != nil {
		return nil, err
	}
	latencyOrMax := func(n exitNode) int64 {
		if lat, ok := latencies[n.nodeID]; ok {
			return lat
		}
		return 1 << 30
	}
	sort.SliceStable(nodes, func(i, j int) bool {
		okI, okJ := exitStateOK(nodes[i].state), exitStateOK(nodes[j].state)
		if okI != okJ {
			return okI
		}
		return latencyOrMax(nodes[i]) < latencyOrMax(nodes[j])
	})
	rows := make([]any, 0, len(nodes))
	for _, n := range nodes {
		bits := []string{}
		if n.exitIP != "" {
			bits = append(bits, n.exitIP)
		}
		if lat, ok := latencies[n.nodeID]; ok {
			bits = append(bits, fmt.Sprintf("%dms", lat))
		}
		row := newObj()
		row.set("k", n.name)
		row.set("v", strings.Join(bits, " · "))
		if exitStateOK(n.state) {
			row.set("tone", "ok")
		} else {
			row.set("tone", "bad")
		}
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("proxyExits", rows, len(rows))}, nil
}

// exitStateOK 账本态 → 展示可用（ACTIVE/STALE=ok；DEAD/RETIRED 及未知=bad）。
func exitStateOK(state string) bool {
	return state == "ACTIVE" || state == "STALE"
}

// latestL2Latencies 每节点最近一次 L2 观测延迟（账本只读）。
func latestL2Latencies(c *CallContext) (map[string]int64, error) {
	out := map[string]int64{}
	rows, err := c.db.QueryContext(c.Ctx, `SELECT node_id, latency_ms FROM health_observations
		WHERE id IN (SELECT MAX(id) FROM health_observations WHERE level='L2' GROUP BY node_id)`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var nodeID string
		var latency any
		if err := rows.Scan(&nodeID, &latency); err != nil {
			return nil, err
		}
		if latency != nil {
			if n, err := pyIntOr(latency); err == nil {
				out[nodeID] = n
			}
		}
	}
	return out, rows.Err()
}

// proxyLatencyTest 发起全量出口延迟检测并等到收口（轮询窗 170s/2s；在飞
// 复用同会话；超窗回 pending 行）。
func proxyLatencyTest(c *CallContext) (*ToolResult, error) {
	snap, err := postSnap(c, "/api/v1/proxies/clash/test")
	if err != nil {
		return nil, err
	}
	deadline := time.Now().Add(time.Duration(proxyTestWaitS * float64(time.Second)))
	for snap != nil && (pyStrOr(snap["phase"]) == "queued" || pyStrOr(snap["phase"]) == "running") && time.Now().Before(deadline) {
		if err := ctxSleep(c, time.Duration(proxyTestPollS*float64(time.Second))); err != nil {
			return nil, err
		}
		if next := fetchMap(c, "/api/v1/proxies/clash/test/progress"); next != nil {
			snap = next
		}
	}
	if snap == nil {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "proxyStopped")
		row.set("tone", "warn")
		return &ToolResult{Data: rowsCard("proxyTest", []any{row}, nil)}, nil
	}
	phase := pyStrOr(snap["phase"])
	if phase != "done" && phase != "failed" {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "proxyTestPending")
		row.set("tone", "warn")
		return &ToolResult{Data: rowsCard("proxyTest", []any{row}, nil)}, nil
	}
	if phase == "failed" {
		row := newObj()
		row.set("k", "")
		row.set("v", truncateRunes(pyStrOr(snap["error"]), 80))
		row.set("tone", "bad")
		return &ToolResult{Data: rowsCard("proxyTest", []any{row}, nil)}, nil
	}
	nodeList, _ := snap["nodes"].([]any)
	type probedNode struct {
		name  string
		alive bool
		ms    any
		exit  string
	}
	probed := []probedNode{}
	for _, raw := range nodeList {
		it, ok := raw.(map[string]any)
		if !ok || !pyBool(it["probed"]) {
			continue
		}
		probed = append(probed, probedNode{
			name:  pyStrOr(it["name"]),
			alive: pyBool(it["alive"]),
			ms:    it["ms"],
			exit:  pyStrOr(it["exitIp"]),
		})
	}
	sort.SliceStable(probed, func(i, j int) bool {
		if probed[i].alive != probed[j].alive {
			return probed[i].alive
		}
		return nodeMS(probed[i].ms) < nodeMS(probed[j].ms)
	})
	alive := 0
	for _, n := range probed {
		if n.alive {
			alive++
		}
	}
	rows := []any{}
	head := newObj()
	head.set("k", "")
	head.set("v", fmt.Sprintf("%d/%d", alive, len(probed)))
	if alive != 0 {
		head.set("tone", "ok")
	} else {
		head.set("tone", "bad")
	}
	rows = append(rows, head)
	for _, n := range probed {
		row := newObj()
		row.set("k", n.name)
		if n.alive {
			exit := n.exit
			if exit == "" {
				exit = "-"
			}
			row.set("v", exit+" · "+pyFStr(n.ms)+"ms")
			row.set("tone", "ok")
		} else {
			row.set("v", "")
			row.set("tone", "bad")
		}
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("proxyTest", rows, len(probed))}, nil
}

// nodeMS 检测延迟排序键（None 排最后，Python 1<<30 同语义）。
func nodeMS(ms any) int64 {
	if ms == nil {
		return 1 << 30
	}
	if n, err := pyIntOr(ms); err == nil {
		return n
	}
	return 1 << 30
}

// postSnap POST 测速端点 → 会话快照（响应即 JSON 对象）。
func postSnap(c *CallContext, path string) (map[string]any, error) {
	status, body, err := c.postRaw(path, nil)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("内部请求失败（%d）：%s", status, detailText(body))
	}
	return body, nil
}

// ctxSleep 睡眠并尊重执行预算取消。
func ctxSleep(c *CallContext, d time.Duration) error {
	select {
	case <-time.After(d):
		return nil
	case <-c.Ctx.Done():
		return c.Ctx.Err()
	}
}

// syncSubscriptions 重新拉取全部启用的代理订阅（单条失败不影响其余；
// 回执只含订阅标签与结果，绝不含订阅链接本身）。
func syncSubscriptions(c *CallContext) (*ToolResult, error) {
	subs, err := listSubscriptionItems(c, "clash")
	if err != nil {
		return nil, err
	}
	rows := []any{}
	total := 0
	for _, s := range subs {
		if pyBool(s["deprecated"]) {
			continue
		}
		total++
		label := pyStrOr(s["label"])
		if label == "" {
			label = "#" + pyStrOr(s["id"])
		}
		id := statInt(s, "id")
		row := newObj()
		row.set("k", label)
		if id <= 0 {
			row.set("vKey", "subsFailed")
			row.set("v", "订阅编号无效")
			row.set("tone", "bad")
			rows = append(rows, row)
			continue
		}
		status, body, refreshErr := c.postRaw("/api/v1/proxies/subscriptions/"+strconv.FormatInt(id, 10)+"/refresh", nil)
		if refreshErr == nil && status == http.StatusOK {
			row.set("vKey", "subsSynced")
			row.set("tone", "ok")
		} else {
			text := ""
			if refreshErr != nil {
				text = refreshErr.Error()
			} else {
				text = detailText(body)
			}
			row.set("vKey", "subsFailed")
			row.set("v", truncateRunes(text, 60))
			row.set("tone", "bad")
		}
		rows = append(rows, row)
	}
	if total == 0 {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "subsNone")
		row.set("tone", "warn")
		return &ToolResult{Data: rowsCard("subs", []any{row}, nil)}, nil
	}
	return &ToolResult{Data: rowsCard("subs", rows, total)}, nil
}

// ─── 网络出站（web_search / search_steam）──────────────────────────────────

var ddgResultRe = regexp.MustCompile(`(?s)class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>`)

var htmlTagRe = regexp.MustCompile(`<[^>]+>`)

// ddgHit 一条搜索结果（纯函数输出，便于单测）。
type ddgHit struct {
	title   string
	url     string
	snippet string
}

// parseDDG DuckDuckGo HTML 结果解析（Python _parse_ddg 纯函数移植：
// uddg 反解 + 去标签 + 纯文本化，上限 limit 条）。
func parseDDG(html string, limit int) []ddgHit {
	out := []ddgHit{}
	for _, m := range ddgResultRe.FindAllStringSubmatch(html, -1) {
		href := m[1]
		if strings.Contains(href, "uddg=") {
			if parsed, err := url.Parse(strings.ReplaceAll(href, "&amp;", "&")); err == nil {
				if v := parsed.Query().Get("uddg"); v != "" {
					if unq, err := url.QueryUnescape(v); err == nil {
						href = unq
					} else {
						href = v
					}
				}
			}
		}
		title := strings.TrimSpace(htmlTagRe.ReplaceAllString(m[2], ""))
		snippet := strings.TrimSpace(htmlTagRe.ReplaceAllString(m[3], ""))
		if href == "" || title == "" {
			continue
		}
		out = append(out, ddgHit{
			title:   truncateRunes(title, 120),
			url:     truncateRunes(href, 300),
			snippet: truncateRunes(snippet, 160),
		})
		if len(out) >= limit {
			break
		}
	}
	return out
}

// webSearch 互联网搜索（DuckDuckGo HTML 端点，免密钥；只外发搜索词本身）。
func webSearch(c *CallContext) (*ToolResult, error) {
	term := truncateRunes(strings.Join(strings.Fields(pyStrOr(c.Args["query"])), " "), 200)
	if term == "" {
		return emptyResult("bad_query"), nil
	}
	html := fetchDDGHTML(c, term)
	rows := []any{}
	for _, hit := range parseDDG(html, 5) {
		row := newObj()
		row.set("k", hit.title)
		row.set("v", truncateRunes(hit.snippet, 80))
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("web", rows, len(rows))}, nil
}

// fetchDDGHTML 单次检索（失败回空串：结果为空行卡，不抛网络错误）。
func fetchDDGHTML(c *CallContext, term string) string {
	req, err := http.NewRequestWithContext(c.Ctx, http.MethodGet,
		ddgSearchURL+"?"+url.Values{"q": {term}}.Encode(), nil)
	if err != nil {
		return ""
	}
	req.Header.Set("User-Agent", ddgUserAgent)
	resp, err := c.outboundClient().Do(req)
	if err != nil {
		return ""
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return ""
	}
	return string(readAll(resp.Body))
}

// searchSteam Steam 商店全网检索（storesearch 匿名端点；cc=US&l=english；
// 中文词零结果，US 视图可见国区锁区游戏）。
func searchSteam(c *CallContext) (*ToolResult, error) {
	term := truncateRunes(strings.Join(strings.Fields(pyStrOr(c.Args["term"])), " "), 80)
	if term == "" {
		return emptyResult("bad_term"), nil
	}
	data := fetchStoreSearch(c, term)
	if data == nil {
		return emptyResult("search_failed"), nil
	}
	out := newObj()
	out.set("kind", "games")
	out.set("items", steamSearchItems(data, steamSearchLimit))
	return &ToolResult{Data: out}, nil
}

// steamSearchItems storesearch 响应 → 候选条目（cnyFen 仅币种恰为 CNY 才进、
// discount 由 final/initial 推算，取前 limit 条；纯函数便于单测）。
func steamSearchItems(data map[string]any, limit int) []any {
	rawItems, _ := data["items"].([]any)
	items := []any{}
	for _, raw := range rawItems {
		if len(items) >= limit {
			break
		}
		it, ok := raw.(map[string]any)
		if !ok {
			continue
		}
		appid, err := pyIntOr(it["id"])
		if err != nil || appid <= 0 {
			continue
		}
		price, _ := it["price"].(map[string]any)
		var cnyFen any
		final := priceInt(price, "final")
		initial := priceInt(price, "initial")
		if pyStrOr(price["currency"]) == "CNY" && final != nil && *final > 0 {
			cnyFen = *final
		}
		discount := int64(0)
		if final != nil && initial != nil && *initial > *final && *final > 0 {
			discount = roundPy((1 - float64(*final)/float64(*initial)) * 100)
		}
		name := pyStrOr(it["name"])
		if name == "" {
			name = fmt.Sprintf("AppID %d", appid)
		}
		item := newObj()
		item.set("appid", appid)
		item.set("name", name)
		item.set("cnyFen", cnyFen)
		item.set("discount", discount)
		items = append(items, item)
	}
	return items
}

// priceInt 价格字段 → int64 指针（缺失/非整数回 nil）。
func priceInt(price map[string]any, key string) *int64 {
	if price == nil {
		return nil
	}
	n, err := pyIntOr(price[key])
	if err != nil {
		return nil
	}
	return &n
}

// roundPy Python round() 银行家舍入。
func roundPy(f float64) int64 {
	return int64(math.RoundToEven(f))
}

// fetchStoreSearch storesearch 单次检索（走代理出口；失败回 nil 不抛）。
func fetchStoreSearch(c *CallContext, term string) map[string]any {
	q := url.Values{"term": {term}, "cc": {"US"}, "l": {"english"}}
	req, err := http.NewRequestWithContext(c.Ctx, http.MethodGet, steamSearchURL+"?"+q.Encode(), nil)
	if err != nil {
		return nil
	}
	for key, value := range steamHTTPHeaders {
		req.Header.Set(key, value)
	}
	resp, err := c.outboundClient().Do(req)
	if err != nil {
		return nil
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil
	}
	var out map[string]any
	if err := jsonDecoder(readAll(resp.Body)).Decode(&out); err != nil {
		return nil
	}
	return out
}

// readAll 有限读取响应体（8MB 上限防异常大响应）。
func readAll(body interface{ Read([]byte) (int, error) }) []byte {
	buf := make([]byte, 32*1024)
	out := make([]byte, 0, 1<<20)
	for {
		n, err := body.Read(buf)
		out = append(out, buf[:n]...)
		if err != nil || len(out) > 8<<20 {
			break
		}
	}
	return out
}

// ─── 目录导入 / 价格补抓 / 测试邮件 / 更新状态 ─────────────────────────────

// ingestAppids AppID 入库：目录导入分类 + 新导入首爬排队（kind=import 同一
// 执行入口）。只进目录与价格，不建立关注。
func ingestAppids(c *CallContext) (*ToolResult, error) {
	if c.deps == nil || c.deps.ImportAppids == nil {
		return notAvailable(), nil
	}
	raw, _ := c.Args["appids"].([]any)
	seen := map[int64]bool{}
	clean := []int64{}
	for _, item := range raw {
		appid, err := pyIntOr(item)
		if err != nil || appid <= 0 || seen[appid] {
			continue
		}
		seen[appid] = true
		clean = append(clean, appid)
	}
	if len(clean) == 0 {
		return emptyResult("no_target"), nil
	}
	if len(clean) > ingestMaxAppids {
		obj := newObj()
		obj.set("kind", "empty")
		obj.set("note", "too_many")
		data := newObj()
		data.set("max", ingestMaxAppids)
		obj.set("data", data)
		return &ToolResult{Status: StatusEmpty, Data: obj}, nil
	}
	result, err := c.deps.ImportAppids(c.Ctx, raw)
	if err != nil {
		return nil, err
	}
	per := map[int64]string{}
	if list, ok := result["results"].([]map[string]any); ok {
		for _, r := range list {
			if appid, err := pyIntOr(r["appid"]); err == nil {
				per[appid] = pyStrOr(r["status"])
			}
		}
	} else if list, ok := result["results"].([]any); ok {
		for _, item := range list {
			m, isMap := item.(map[string]any)
			if !isMap {
				continue
			}
			if appid, err := pyIntOr(m["appid"]); err == nil {
				per[appid] = pyStrOr(m["status"])
			}
		}
	}
	fresh := []int64{}
	for _, appid := range clean {
		if per[appid] == "ok" {
			fresh = append(fresh, appid)
		}
	}
	started := false
	if len(fresh) > 0 {
		status, _, runErr := c.postRaw("/api/v1/crawl/run", map[string]any{
			"scope": "appids", "appids": fresh, "kind": "import",
		})
		if runErr != nil {
			return nil, runErr
		}
		switch {
		case status == http.StatusOK:
			started = true
		case status == http.StatusConflict:
			started = false // 已有任务在跑：目录已导入，首爬让位现役任务
		default:
			return nil, fmt.Errorf("首爬排队被拒（%d）", status)
		}
	}
	rows := make([]any, 0, len(clean))
	for _, appid := range clean {
		name, nameErr := c.gameName(appid)
		if nameErr != nil {
			return nil, nameErr
		}
		if name == "" {
			name = fmt.Sprintf("AppID %d", appid)
		}
		st := per[appid]
		row := newObj()
		row.set("k", name)
		switch st {
		case "ok":
			if started {
				row.set("vKey", "ingestQueued")
			} else {
				row.set("vKey", "ingestImported")
			}
		case "own":
			row.set("vKey", "ingestOwned")
		default:
			row.set("vKey", "ingestFail")
		}
		row.set("v", "#"+strconv.FormatInt(appid, 10))
		if st == "ok" || st == "own" {
			row.set("tone", "ok")
		} else {
			row.set("tone", "bad")
		}
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("ingest", rows, len(rows))}, nil
}

// refreshGamePrice 单游戏价格补抓（手动 job 挂后台执行，完成自动入库）。
func refreshGamePrice(c *CallContext) (*ToolResult, error) {
	appid, err := toolAppid(c.Args)
	if err != nil {
		return nil, err
	}
	name := ""
	if appid > 0 {
		if name, err = c.gameName(appid); err != nil {
			return nil, err
		}
	}
	if name == "" {
		name = fmt.Sprintf("AppID %d", appid)
	}
	status, _, runErr := c.postRaw("/api/v1/crawl/run", map[string]any{
		"scope": "appids", "appids": []int64{appid}, "kind": "manual",
	})
	if runErr != nil {
		return nil, runErr
	}
	row := newObj()
	row.set("k", name)
	switch status {
	case http.StatusOK:
		row.set("vKey", "refreshStarted")
		row.set("tone", "ok")
	case http.StatusConflict:
		row.set("vKey", "refreshBusy")
		row.set("tone", "warn")
	default:
		row.set("vKey", "refreshRejected")
		row.set("tone", "warn")
	}
	return &ToolResult{Data: rowsCard("refresh", []any{row}, nil)}, nil
}

// notifyTest 发一封连通性测试邮件（只走一次 SMTP，不碰业务数据）。
func notifyTest(c *CallContext) (*ToolResult, error) {
	cfg := fetchMap(c, "/api/v1/alerts/smtp")
	if cfg == nil {
		return nil, fmt.Errorf("内部读取失败：邮件配置不可用")
	}
	complete := pyStrOr(cfg["host"]) != "" && pyStrOr(cfg["user"]) != "" &&
		pyBool(cfg["hasPassword"]) && pyStrOr(cfg["toAddr"]) != ""
	if !complete {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "notifyNoConfig")
		row.set("tone", "warn")
		return &ToolResult{Data: rowsCard("notify", []any{row}, nil)}, nil
	}
	// 密码面绝不回传：TestMail 对空授权码自行读库取真值。
	status, body, err := c.postRaw("/api/v1/alerts/smtp/test", map[string]any{
		"host": pyStrOr(cfg["host"]), "port": statInt(cfg, "port"),
		"user": pyStrOr(cfg["user"]), "password": "",
		"toAddr": pyStrOr(cfg["toAddr"]), "useSsl": pyBool(cfg["useSsl"]),
	})
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "notifyFailed")
		row.set("v", truncateRunes(detailText(body), 80))
		row.set("tone", "bad")
		return &ToolResult{Data: rowsCard("notify", []any{row}, nil)}, nil
	}
	row := newObj()
	row.set("k", "")
	row.set("vKey", "notifySent")
	row.set("tone", "ok")
	return &ToolResult{Data: rowsCard("notify", []any{row}, nil)}, nil
}

// updateStatus 应用更新状态（只读：当前版本 / 下载进度 / 暂存包，绝不触发
// 检查或下载）。
func updateStatus(c *CallContext) (*ToolResult, error) {
	info := fetchMap(c, "/api/v1/info")
	current := ""
	if info != nil {
		current = pyStrOr(info["version"])
	}
	progress := fetchMap(c, "/api/v1/system/update-progress")
	pending := fetchMap(c, "/api/v1/system/update-pending")
	rows := []any{}
	cur := newObj()
	cur.set("k", "")
	cur.set("vKey", "updateCurrent")
	cur.set("v", current)
	cur.set("tone", "ok")
	rows = append(rows, cur)
	var downloadRunning bool
	var downloadPercent any
	var progressPhase, progressError any
	var progressReceived int64
	var progressTotal any
	if progress != nil {
		downloadRunning = pyBool(progress["running"])
		downloadPercent = progress["percent"]
		progressPhase = progress["phase"]
		progressError = progress["error"]
		progressReceived = statInt(progress, "received")
		progressTotal = progress["total"]
	}
	if downloadRunning || downloadPercent != nil {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "updateDownloading")
		data := newObj()
		data.set("phase", progressPhase)
		data.set("percent", downloadPercent)
		data.set("received", progressReceived)
		data.set("total", progressTotal)
		errText := pyStrOr(progressError)
		if errText == "" {
			data.set("error", nil)
		} else {
			data.set("error", truncateRunes(errText, 120))
		}
		row.set("data", data)
		if errText != "" {
			row.set("tone", "bad")
		} else {
			row.set("tone", nil)
		}
		rows = append(rows, row)
	}
	pendingFlag := pending != nil && pyBool(pending["pending"])
	if pendingFlag {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "updatePending")
		row.set("v", pyStrOr(pending["tag"]))
		data := newObj()
		data.set("swapFailed", pyBool(pending["swapFailed"]))
		row.set("data", data)
		row.set("tone", "warn")
		rows = append(rows, row)
	}
	out := rowsCard("updateStatus", rows, len(rows))
	out.set("currentVersion", current)
	out.set("checkedAt", nil)
	out.set("pending", pendingFlag)
	pendingTag := any(nil)
	swapFailed := false
	var justUpdated any
	installing := false
	if pending != nil {
		if pyBool(pending["tag"]) {
			pendingTag = pending["tag"]
		}
		swapFailed = pyBool(pending["swapFailed"])
		justUpdated = pending["justUpdated"]
		installing = pyBool(pending["installing"])
	}
	out.set("pendingTag", pendingTag)
	out.set("swapFailed", swapFailed)
	out.set("justUpdated", pyBool(justUpdated))
	out.set("updateReport", justUpdated)
	out.set("installing", installing)
	download := newObj()
	download.set("running", downloadRunning)
	download.set("phase", progressPhase)
	download.set("percent", downloadPercent)
	download.set("received", progressReceived)
	download.set("total", progressTotal)
	errText := pyStrOr(progressError)
	if errText == "" {
		download.set("error", nil)
	} else {
		download.set("error", truncateRunes(errText, 120))
	}
	out.set("download", download)
	return &ToolResult{Data: out}, nil
}

// ─── 运行时任务（list_tasks / cancel_task：三源合并运行账）────────────────

// taskRunView 三源合并后的运行视图（Python _agent_view/_domain_view 子集，
// 只装任务行投影与取消定位所需字段）。
type taskRunView struct {
	runID         string
	status        string
	task          string
	domainStatus  string
	progressDone  any
	progressTotal any
	createdAt     string
}

// listTaskRuns 三源合并读（price_tasks_only 口径）：dev_pytest 账本行 ∪
// price_cycles ∪ 无轮次挂靠的 repair/missing 抓取任务；created_at 降序 +
// run_id 降序，截 100。
func listTaskRuns(c *CallContext) ([]taskRunView, error) {
	const mergeLimit = 100
	runs, err := scanAgentRuns(c, mergeLimit)
	if err != nil {
		return nil, err
	}
	cycleRows, err := c.db.QueryContext(c.Ctx,
		`SELECT id,status,batches_done,batches_expected,COALESCE(started_at,''),COALESCE(finished_at,'')
		 FROM price_cycles ORDER BY started_at DESC, id DESC LIMIT ?`, mergeLimit)
	if err != nil {
		return nil, err
	}
	for cycleRows.Next() {
		var id int64
		var status, startedAt, finishedAt string
		var done, expected any
		if err := cycleRows.Scan(&id, &status, &done, &expected, &startedAt, &finishedAt); err != nil {
			cycleRows.Close()
			return nil, err
		}
		runs = append(runs, taskRunView{
			runID:         "cycle:" + strconv.FormatInt(id, 10),
			status:        mapDomainStatus(status),
			task:          "price_refresh", domainStatus: status,
			progressDone:  done, progressTotal: expected,
			createdAt:     startedAt,
		})
	}
	if err := cycleRows.Err(); err != nil {
		cycleRows.Close()
		return nil, err
	}
	cycleRows.Close()
	crawlRows, err := c.db.QueryContext(c.Ctx,
		`SELECT id,kind,status,COALESCE(stats_json,''),COALESCE(started_at,'')
		 FROM crawl_jobs WHERE cycle_id IS NULL AND kind IN ('repair','missing')
		 ORDER BY started_at DESC, id DESC LIMIT ?`, mergeLimit)
	if err != nil {
		return nil, err
	}
	for crawlRows.Next() {
		var id int64
		var kind, status, statsRaw, startedAt string
		if err := crawlRows.Scan(&id, &kind, &status, &statsRaw, &startedAt); err != nil {
			crawlRows.Close()
			return nil, err
		}
		var stats map[string]any
		if statsRaw != "" {
			_ = jsonDecoder([]byte(statsRaw)).Decode(&stats)
		}
		runs = append(runs, taskRunView{
			runID:         "crawl:" + strconv.FormatInt(id, 10),
			status:        mapDomainStatus(status),
			task:          mapCrawlTask(kind), domainStatus: status,
			progressDone:  stats["processed"], progressTotal: stats["total"],
			createdAt:     startedAt,
		})
	}
	if err := crawlRows.Err(); err != nil {
		crawlRows.Close()
		return nil, err
	}
	crawlRows.Close()
	sort.SliceStable(runs, func(i, j int) bool {
		ti, tj := taskTimeKey(runs[i].createdAt), taskTimeKey(runs[j].createdAt)
		if !ti.Equal(tj) {
			return ti.After(tj)
		}
		return runs[i].runID > runs[j].runID
	})
	if len(runs) > mergeLimit {
		runs = runs[:mergeLimit]
	}
	return runs, nil
}

// taskTimeKey 运行时刻排序键（兼容 ISO「T」与空格两种时刻文本——账本行与域行
// 存储格式不同，字符串直比会错序；解析失败按零时刻排最后，等价 Python
// created_at or datetime.min）。
func taskTimeKey(s string) time.Time {
	if t, err := time.Parse("2006-01-02T15:04:05", s); err == nil {
		return t
	}
	if t, err := time.Parse("2006-01-02 15:04:05", s); err == nil {
		return t
	}
	return time.Time{}
}

// scanAgentRuns dev_pytest 账本行（runner='task'）。
func scanAgentRuns(c *CallContext, limit int) ([]taskRunView, error) {
	rows, err := c.db.QueryContext(c.Ctx,
		`SELECT id,status,CAST(meta AS TEXT),COALESCE(created_at,'') FROM agent_runs
		 WHERE runner='task' AND json_extract(meta,'$.task')='dev_pytest'
		 ORDER BY created_at DESC, id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []taskRunView{}
	for rows.Next() {
		var id, status, metaRaw, createdAt string
		if err := rows.Scan(&id, &status, &metaRaw, &createdAt); err != nil {
			return nil, err
		}
		var meta map[string]any
		if metaRaw != "" {
			_ = jsonDecoder([]byte(metaRaw)).Decode(&meta)
		}
		done, total := agentProgress(meta)
		out = append(out, taskRunView{
			runID:         id,
			status:        status,
			task:          pyStrOr(meta["task"]),
			progressDone:  done,
			progressTotal: total,
			createdAt:     createdAt,
		})
	}
	return out, rows.Err()
}

// mapDomainStatus 域状态 → run 状态（未知态按 failed 兜底）。
func mapDomainStatus(status string) string {
	if mapped, ok := domainStatusToRun[status]; ok {
		return mapped
	}
	return "failed"
}

// mapCrawlTask 抓取任务类型 → 任务名（repair/missing 之外无任务名）。
func mapCrawlTask(kind string) string {
	switch kind {
	case "repair":
		return "price_repair"
	case "missing":
		return "price_missing"
	}
	return ""
}

// taskRowView 任务行（Python _task_row 同形）。
func taskRowView(r taskRunView) *Obj {
	vKey, tone := "taskQueued", "warn"
	if m, ok := taskStatusRow[r.status]; ok {
		vKey, tone = m[0], m[1]
	}
	if r.domainStatus == "partial" {
		vKey, tone = "taskPartial", "warn"
	}
	v := ""
	if r.progressDone != nil && pyBool(r.progressTotal) {
		v = pyFStr(r.progressDone) + "/" + pyFStr(r.progressTotal)
	}
	row := newObj()
	row.set("k", "")
	row.set("kindKey", "pilot.task."+orStr(taskLabels[r.task], r.task))
	row.set("vKey", vKey)
	row.set("tone", tone)
	row.set("v", v)
	row.set("taskKind", r.task)
	row.set("run_id", r.runID)
	return row
}

// listTasks 运行中的价格任务与最近终态（live 在前、终态取 3）。
func listTasks(c *CallContext) (*ToolResult, error) {
	runs, err := listTaskRuns(c)
	if err != nil {
		return nil, err
	}
	live := []taskRunView{}
	terminal := []taskRunView{}
	for _, r := range runs {
		if _, ok := taskLabels[r.task]; !ok {
			continue
		}
		if isTerminalRunStatus(r.status) {
			terminal = append(terminal, r)
		} else {
			live = append(live, r)
		}
	}
	rows := []any{}
	for _, r := range live {
		rows = append(rows, taskRowView(r))
	}
	for i, r := range terminal {
		if i >= 3 {
			break
		}
		rows = append(rows, taskRowView(r))
	}
	if len(rows) == 0 {
		row := newObj()
		row.set("k", "")
		row.set("vKey", "taskNoRunning")
		row.set("tone", "warn")
		row.set("v", "")
		rows = append(rows, row)
	}
	return &ToolResult{Data: rowsCard("tasks", rows, len(live))}, nil
}

// getTaskRun 按运行标识直读（Python get_run：域键读域行，其余读账本）。
func getTaskRun(c *CallContext, runID string) (*taskRunView, error) {
	if source, id, ok := splitDomainRunID(runID); ok {
		if source == "cycle" {
			var status string
			var done, expected any
			var startedAt string
			err := c.db.QueryRowContext(c.Ctx,
				`SELECT status,batches_done,batches_expected,COALESCE(started_at,'')
				 FROM price_cycles WHERE id=?`, id).Scan(&status, &done, &expected, &startedAt)
			if err != nil {
				return nil, nil
			}
			return &taskRunView{
				runID:         runID,
				status:        mapDomainStatus(status),
				task:          "price_refresh", domainStatus: status,
				progressDone:  done, progressTotal: expected,
				createdAt:     startedAt,
			}, nil
		}
		var kind, status, statsRaw, startedAt string
		err := c.db.QueryRowContext(c.Ctx,
			`SELECT kind,status,COALESCE(stats_json,''),COALESCE(started_at,'')
			 FROM crawl_jobs WHERE id=? AND cycle_id IS NULL`, id).
			Scan(&kind, &status, &statsRaw, &startedAt)
		if err != nil {
			return nil, nil
		}
		var stats map[string]any
		if statsRaw != "" {
			_ = jsonDecoder([]byte(statsRaw)).Decode(&stats)
		}
		return &taskRunView{
			runID:         runID,
			status:        mapDomainStatus(status),
			task:          mapCrawlTask(kind), domainStatus: status,
			progressDone:  stats["processed"], progressTotal: stats["total"],
			createdAt:     startedAt,
		}, nil
	}
	var id, status, metaRaw string
	err := c.db.QueryRowContext(c.Ctx,
		`SELECT id,status,COALESCE(CAST(meta AS TEXT),'') FROM agent_runs WHERE id=?`, runID).
		Scan(&id, &status, &metaRaw)
	if err != nil {
		return nil, nil
	}
	var meta map[string]any
	if metaRaw != "" {
		_ = jsonDecoder([]byte(metaRaw)).Decode(&meta)
	}
	done, total := agentProgress(meta)
	return &taskRunView{
		runID:         id,
		status:        status,
		task:          pyStrOr(meta["task"]),
		progressDone:  done,
		progressTotal: total,
		createdAt:     "",
	}, nil
}

// agentProgress 账本任务进度（meta.progress.done/total，Python meta.progress 同形）。
func agentProgress(meta map[string]any) (any, any) {
	progress, _ := meta["progress"].(map[string]any)
	if progress == nil {
		return nil, nil
	}
	return progress["done"], progress["total"]
}

// splitDomainRunID cycle:N / crawl:N 域键解析（N 必为正整数）。
func splitDomainRunID(runID string) (string, int64, bool) {
	source, num, found := strings.Cut(runID, ":")
	if !found || (source != "cycle" && source != "crawl") {
		return "", 0, false
	}
	id, err := strconv.ParseInt(num, 10, 64)
	if err != nil || id <= 0 {
		return "", 0, false
	}
	return source, id, true
}

// liveCycleID 当前在跑的价格轮（执行态近似：最新一个执行中的轮）。
func liveCycleID(c *CallContext) int64 {
	var id int64
	if err := c.db.QueryRowContext(c.Ctx,
		`SELECT COALESCE(MAX(id),0) FROM price_cycles
		 WHERE status IN ('planning','running','repairing','finalizing')`).Scan(&id); err != nil {
		return 0
	}
	return id
}

// liveJobID 当前在跑的抓取任务（/crawl/active 的运行时事实）。
func liveJobID(c *CallContext) int64 {
	snap := fetchMap(c, "/api/v1/crawl/active")
	if snap == nil || !pyBool(snap["activeJobId"]) {
		return 0
	}
	id, err := pyIntOr(snap["activeJobId"])
	if err != nil {
		return 0
	}
	return id
}

// cancelTask 请求停止运行中的长任务（首版支持 cycle/crawl 两支路；
// Go 侧无 task-runner 协作取消，账本任务回不可停形状）。
func cancelTask(c *CallContext) (*ToolResult, error) {
	runs, err := listTaskRuns(c)
	if err != nil {
		return nil, err
	}
	live := []taskRunView{}
	for _, r := range runs {
		if _, ok := taskLabels[r.task]; ok && !isTerminalRunStatus(r.status) {
			live = append(live, r)
		}
	}
	runID := pyStrOr(c.Args["run_id"])
	kind := pyStrOr(c.Args["kind"])
	var target *taskRunView
	if runID != "" {
		candidate, candErr := getTaskRun(c, runID)
		if candErr != nil {
			return nil, candErr
		}
		if candidate != nil {
			if _, ok := taskLabels[candidate.task]; ok {
				target = candidate
			}
		}
	} else if kind != "" {
		for i := range live {
			if live[i].task == kind {
				target = &live[i]
				break
			}
		}
	} else if len(live) == 1 {
		target = &live[0]
	}
	if target == nil {
		key := "taskAmbiguous"
		if len(live) == 0 {
			key = "taskNoRunning"
		}
		row := newObj()
		row.set("k", "")
		row.set("vKey", key)
		row.set("tone", "warn")
		row.set("v", "")
		return &ToolResult{Data: rowsCard("tasks", []any{row}, nil)}, nil
	}
	if isTerminalRunStatus(target.status) {
		return &ToolResult{Data: rowsCard("tasks", []any{taskRowView(*target)}, nil)}, nil
	}
	if source, id, ok := splitDomainRunID(target.runID); ok {
		expected := liveJobID(c)
		chain := "false"
		if source == "cycle" {
			expected = liveCycleID(c)
			chain = "true"
		}
		if id != expected {
			return &ToolResult{Data: rowsCard("tasks",
				[]any{taskRowViewFlag(*target, "taskStopUnavailable", "warn")}, nil)}, nil
		}
		path := "/api/v1/crawl/stop?chain=" + chain
		if source == "crawl" {
			path = "/api/v1/crawl/stop?jobId=" + strconv.FormatInt(id, 10) + "&chain=false"
		}
		var out map[string]any
		if err := c.postJSON(path, nil, &out); err != nil {
			return nil, err
		}
		if pyBool(out["stopped"]) {
			return &ToolResult{Data: rowsCard("tasks",
				[]any{taskRowViewFlag(*target, "taskStopRequested", "")}, nil)}, nil
		}
		return &ToolResult{Data: rowsCard("tasks", []any{taskRowView(*target)}, nil)}, nil
	}
	// 账本任务（runner=task）：Go 无协作取消通道 → 不可停形状。
	return &ToolResult{Data: rowsCard("tasks",
		[]any{taskRowViewFlag(*target, "taskStopUnavailable", "warn")}, nil)}, nil
}

// taskRowViewFlag 任务行（覆盖 vKey/tone；tone 空串按状态默认色）。
func taskRowViewFlag(r taskRunView, vKey, tone string) *Obj {
	row := taskRowView(r)
	row.set("vKey", vKey)
	if tone != "" {
		row.set("tone", tone)
	}
	return row
}

// ─── 注册 ───────────────────────────────────────────────────────────────────

// RegisterOpsRuntimeTools 批次三 25 工具登记（重名即错；预算取
// budgetFor 分档：任务面 60 / 出网 40 / 全量出口检测 180 / 同步类 240 /
// 其余 20）。
func RegisterOpsRuntimeTools(r *ToolRegistry) error {
	for _, spec := range []*ToolSpec{
		{
			Name: "crawl_active", Group: GroupRead, Risk: "low",
			Description: "查看当前是否有抓取任务在跑、代理池体检是否进行中、" +
				"有多少请求在限流排队。「价格怎么不更新」「任务卡了吗」的第一入口",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    crawlActive,
		},
		{
			Name: "price_freshness", Group: GroupRead, Risk: "low",
			Description: "查游戏价格数据有多新：每个对象最后观察时刻与档位" +
				"（fresh/lagging/stale），并附全库最近成功观察时刻。" +
				"「这游戏价格是多久前的」用它",
			Parameters: `{"type": "object", "properties": {
				"appids": {"type": "array", "items": {"type": "integer"},
					"description": "游戏 appid 列表（一次最多 200 个）"}
			}, "required": ["appids"]}`,
			Handler: priceFreshness,
		},
		{
			Name: "recent_cycles", Group: GroupRead, Risk: "low",
			Description: "最近几轮价格刷新的状态（完成/部分/失败）与批次进度。" +
				"「上一轮刷得怎么样」用它",
			Parameters: `{"type": "object", "properties": {
				"limit": {"type": "integer", "description": "返回条数，默认 5，最多 20"}
			}}`,
			Handler: recentCycles,
		},
		{
			Name: "price_events", Group: GroupRead, Risk: "low",
			Description: "最近的价格事实事件（新低 / 平史低 / 促销免费 / 下架等，新→旧）。" +
				"可按事件类型 / 游戏 / 轮次过滤",
			Parameters: `{"type": "object", "properties": {
				"event_type": {"type": "string", "description": "事件类型过滤（如 new_low）"},
				"appid": {"type": "integer", "description": "按游戏过滤"},
				"cycle_id": {"type": "integer", "description": "按刷新轮次过滤"},
				"limit": {"type": "integer", "description": "返回条数，默认 20，最多 100"}
			}}`,
			Handler: priceEvents,
		},
		{
			Name: "crawl_stop", Group: GroupRead, Risk: "high",
			Description: "停止当前正在跑的抓取任务。用户说「别刷了」「停下来」且" +
				"cancel_task 找不到登记任务时用它",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    crawlStop,
		},
		{
			Name: "recent_job_failures", Group: GroupRead, Risk: "low",
			Description: "列出最近失败的抓取任务与原因。用户问『抓取为什么失败』『最近有什么报错』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     recentJobFailures,
		},
		{
			Name: "write_gate_status", Group: GroupRead, Risk: "low",
			Description: "查看写入调度状态。用户说『操作一直不生效/卡住了』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     writeGateStatus,
		},
		{
			Name: "read_logs", Group: GroupRead, Risk: "low",
			Description: "读取应用运行日志的最近片段（与日志页同源，已脱敏）。" +
				"排查任何异常（抓取失败/同步出错/代理异常/界面报错）时先读这里：" +
				"可按关键词过滤（如游戏名、mihomo、wallet），或按级别只看 warning/error，" +
				"并结合级别计数判断问题规模。返回行数有限时可用更大的 tail",
			Parameters: `{"type": "object", "properties": {
				"keyword": {"type": "string", "description": "可选：只保留包含该关键词的行"},
				"level": {"type": "string", "enum": ["info", "warning", "error"],
					"description": "可选：按级别过滤"},
				"tail": {"type": "integer", "description": "取尾部多少行，默认 120，最大 400"}
			}}`,
			Handler: readLogs,
		},
		{
			Name: "system_diagnostics", Group: GroupRead, Risk: "low",
			Description: "读取实例诊断证据面快照：版本/策略/出口概况、最近价格轮状态，" +
				"以及机器检出的异常信号清单（如价格轮失败、写库竞争、代理池不可用）。" +
				"用户问『现在系统什么状况/有没有什么问题/为什么跑不动』时优先调用，" +
				"再按信号用 read_logs 深挖细节",
			Parameters: `{"type": "object", "properties": {}}`,
			Handler:    systemDiagnostics,
		},
		{
			Name: "subscriptions_list", Group: GroupRead, Risk: "low",
			Description: "查看代理订阅清单：名称、来源域名、节点存活与上次拉取时间（不含订阅链接）。" +
				"用户问『我有哪些订阅/订阅还剩多少节点』，或重拉单条订阅前确认 sub_id 时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     subscriptionsList,
		},
		{
			Name: "subscription_sync", Group: GroupRead, Risk: "high",
			Description: "重拉指定的一条代理订阅（下载新配置，内核按需自动重启，约需十几秒）。" +
				"用户说『重拉/更新这一个订阅』时调用；sub_id 先用订阅清单确认，全部重拉用 sync_subscriptions",
			Parameters: `{"type": "object", "properties": {
				"sub_id": {"type": "integer", "description": "订阅 ID（subscriptions_list 返回的 id）"}
			}, "required": ["sub_id"]}`,
			Handler: subscriptionSync,
		},
		{
			Name: "pool_health_history", Group: GroupRead, Risk: "low",
			Description: "查看代理池最近的体检台账：每轮时间、覆盖节点数、可用/失败数与收尾原因。" +
				"用户问『最近体检结果怎么样/节点为什么被换掉』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     poolHealthHistory,
		},
		{
			Name: "pool_job_runs", Group: GroupRead, Risk: "low",
			Description: "查看代理生产作业台账：最近每轮抓取作业的时间、成功/计划数与状态，附今日概览。" +
				"用户问『代理作业跑得怎么样/今天抓了几轮』时调用",
			Parameters: `{"type": "object", "properties": {
				"limit": {"type": "integer", "description": "返回最近多少条，默认 10"}
			}}`,
			Handler: poolJobRuns,
		},
		{
			Name: "proxy_pool_status", Group: GroupRead, Risk: "low",
			Description: "查看代理通道可用状态（价格抓取依赖它）。诊断『价格不更新/抓取不动』类网络问题时与任务失败清单配合调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     proxyPoolStatus,
		},
		{
			Name: "proxy_exit_detail", Group: GroupRead, Risk: "low",
			Description: "查看代理出口节点明细：逐节点的状态、延迟与出口 IP（最近一次检测的账本事实，不发起新探测）。" +
				"用户问『出口 IP 是什么/有哪些节点/哪个节点延迟最低』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     proxyExitDetail,
		},
		{
			Name: "proxy_latency_test", Group: GroupRead, Risk: "medium",
			Description: "对全部代理出口节点发起一次真实延迟与连通性检测（耗时约 1~3 分钟，等它跑完再回答）。" +
				"用户说『测一下节点延迟/测试连通性/体检代理』时调用；只想看最近一次结果用 proxy_exit_detail",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     proxyLatencyTest,
		},
		{
			Name: "sync_subscriptions", Group: GroupRead, Risk: "medium",
			Description: "重新拉取全部启用的代理订阅（下载新配置，内核按需自动重启；约需十几秒）。" +
				"用户说『同步订阅/更新订阅/重新拉一下订阅』时调用。" +
				"只想看订阅状态用 proxy_pool_status；回执不含订阅链接",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     syncSubscriptions,
		},
		{
			Name: "web_search", Group: GroupRead, Risk: "medium",
			Description: "联网搜索（全互联网，不只 Steam）。用户问『最新的 XX 消息/网上怎么说/帮我查一下 XX』" +
				"且本地工具答不了时调用；只把问题关键词作为搜索词",
			Parameters: `{"type": "object", "properties": {
				"query": {"type": "string", "description": "搜索关键词"}
			}, "required": ["query"]}`,
			Handler: webSearch,
		},
		{
			Name: "refresh_game_price", Group: GroupRead, Risk: "medium",
			Description: "补抓某款已入库游戏的当前价格（后台任务，完成后自动入库）。" +
				"用户说『刷新 XX 的价格』『XX 价格怎么还是旧的』时调用；appid 用检索工具确认",
			Parameters: `{"type": "object", "properties": {
				"appid": {"type": "integer", "description": "游戏 AppID"}
			}, "required": ["appid"]}`,
			Handler: refreshGamePrice,
		},
		{
			Name: "notify_test", Group: GroupRead, Risk: "medium",
			Description: "发一封连通性测试邮件（不碰业务数据）。用户说『发个测试通知/试试邮件』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     notifyTest,
		},
		{
			Name: "update_status", Group: GroupRead, Risk: "low",
			Description: "查看应用更新状态（当前版本 / 是否在下载 / 是否有待重启安装的更新包）。" +
				"用户问『现在是什么版本/有没有更新』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     updateStatus,
		},
		{
			Name: "list_tasks", Group: GroupRead, Risk: "low",
			Description: "查看正在运行的耗时任务（价格刷新 / 价格补抓）与最近结束的几次。" +
				"用户问『刷新到哪了』『还在跑吗』『跑完了吗』时调用",
			Parameters:  `{"type": "object", "properties": {}}`,
			Handler:     listTasks,
		},
		{
			Name: "cancel_task", Group: GroupRead, Risk: "high",
			Description: "停止正在运行的耗时任务。用户说『停下来』『别刷了』『取消』时调用；" +
				"只有一个任务在跑时可只给 kind 或都不给",
			Parameters: `{"type": "object", "properties": {
				"run_id": {"type": "string", "description": "运行标识（来自 list_tasks 结果）"},
				"kind": {"type": "string",
					"enum": ["price_refresh", "price_repair", "price_missing", "dev_pytest"]}
			}}`,
			Handler: cancelTask,
		},
		{
			Name: "search_steam", Group: GroupRead, Risk: "medium",
			Description: "在 Steam 商店全网搜索游戏（不限于本地目录，国区锁区游戏也搜得到），" +
				"返回候选的 appid 与名称。注意：该检索只匹配英文名——中文词搜不到时" +
				"改用英文名重试（如 卡赞→Khazan）。本地目录检索不到某款游戏、或用户明确要求" +
				"『去 Steam 搜』时调用；拿到 appid 后可用 ingest_appids 让它入库，之后即可查价格",
			Parameters: `{"type": "object", "properties": {
				"term": {"type": "string", "description": "搜索词（优先用游戏英文名）"}
			}, "required": ["term"]}`,
			Handler: searchSteam,
		},
		{
			Name: "ingest_appids", Group: GroupRead, Risk: "medium",
			Description: "把指定 AppID 的游戏导入本地目录并立即首爬价格（与任务页『添加游戏』是同一动作），" +
				"导入后即可查价格、史低、设提醒。用户要求『让某游戏入库/抓一下某游戏/收录某游戏』时调用；" +
				"appid 来自 search_steam 结果或用户提供。一次最多 20 款；" +
				"导入不等于关注——要持续追踪降价需另调 add_follow",
			Parameters: `{"type": "object", "properties": {
				"appids": {"type": "array", "items": {"type": "integer"},
					"description": "要导入的游戏 AppID 列表（一次最多 20 个）"}
			}, "required": ["appids"]}`,
			Handler: ingestAppids,
		},
	} {
		spec.TimeoutS = budgetFor(spec.Name)
		if err := r.register(spec); err != nil {
			return err
		}
	}
	return nil
}
