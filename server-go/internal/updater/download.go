//go:build windows

// download.go 下载暂存状态机：并发探测（Range 0-0 判存在+测延迟）→ 按延迟
// 升序断点续传下载（停滞/速率双闸换道）→ 分片边界自查取消 → sha256 双闸 →
// 解包 → 逐文件校验值入 staging 清单。失败归因机器可读（code 字段）。
package updater

import (
	"archive/zip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"crypto/rand"
	"net/url"
	"sort"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// 进度形态对齐 Python _PROGRESS：phase/received/total/percent/speed/channel/
// code/error/ok/running。Go 侧以互斥保护（Python 靠 dict 原子替换，Go 需显式锁）。
type progressState struct {
	mu   sync.Mutex
	data progressData
}

// progressData 进度字段集；重置只换 data，绝不替换持有 mu 的外层结构。
type progressData struct {
	running  bool
	phase    string
	percent  any // float64 或 nil
	received int64
	total    any // int64 或 nil
	error    string
	ok       bool
	code     string
	speed    int64
	channel  string
	cancel   bool
}

func (p *progressState) snapshot() map[string]any {
	p.mu.Lock()
	defer p.mu.Unlock()
	return map[string]any{
		"running":  p.data.running,
		"phase":    phaseOrNil(p.data.phase),
		"percent":  p.data.percent,
		"received": p.data.received,
		"total":    p.data.total,
		"error":    errOrNil(p.data.error),
		"ok":       p.data.ok,
		"code":     codeOrNil(p.data.code),
		"speed":    p.data.speed,
		"channel":  channelOrNil(p.data.channel),
	}
}

func (p *progressState) set(mutate func()) {
	p.mu.Lock()
	defer p.mu.Unlock()
	mutate()
}

func phaseOrNil(phase string) any {
	if phase == "" {
		return nil
	}
	return phase
}

func errOrNil(text string) any {
	if text == "" {
		return nil
	}
	return text
}

func codeOrNil(code string) any {
	if code == "" {
		return nil
	}
	return code
}

func channelOrNil(label string) any {
	if label == "" {
		return nil
	}
	return label
}

var ErrCancelled = errors.New("已取消下载")

// AssetMissing 发布资产不存在（404/体积不符）：版本尚未真正发布或被撤下。
// 与「网络不通」分开——前者重试无意义，前端据此给不同文案。
type AssetMissing struct{ Message string }

func (e *AssetMissing) Error() string { return e.Message }

// Service 更新链运行态：进度单例 + 环境依赖（路径/版本/通道提供方）。
type Service struct {
	mu          sync.Mutex
	progress    progressState
	DataDir     string
	InstallRoot string
	Version     string
	// Channels 候选下载通道（内核代理/本地混合端口/直连），每次下载现取。
	Channels func() []Channel
	// Log 关键事件一行式记录（可空）。
	Log func(string, ...any)
}

func (s *Service) stagingDir() string {
	dir := filepath.Join(s.InstallRoot, StagingDirName)
	_ = os.MkdirAll(dir, 0o755)
	return dir
}

func (s *Service) dataDir() string {
	if s.DataDir != "" {
		return s.DataDir
	}
	return filepath.Join(s.InstallRoot, "data")
}

// CheckUpdate 对比最新版本与当前版本：清单优先（一次请求拿全），GitHub API
// 兜底；两者皆不可达返回 available=False，绝不抛——检查失败≠更新失败。
func (s *Service) CheckUpdate(client *http.Client) map[string]any {
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	if data := fetchManifest(client); data != nil {
		return s.payloadFromManifest(data)
	}
	return checkViaAPI(client)
}

func (s *Service) payloadFromManifest(data map[string]any) map[string]any {
	latest := strings.TrimLeft(stringOf(data["version"]), "vV")
	if latest == "" {
		return map[string]any{"available": false, "reason": "bad_manifest"}
	}
	assets, _ := data["assets"].([]any)
	asset := pickAsset(assets, "")
	if asset == nil {
		return map[string]any{"available": false, "reason": "no_asset"}
	}
	// 资产缺 sha256 = 发布侧残缺：有更新也不能进「无校验下载」的口子。
	digest := strings.TrimSpace(stringOf(asset["sha256"]))
	if digest == "" {
		return map[string]any{"available": false, "reason": "bad_manifest"}
	}
	tag := stringOf(data["tag"])
	if tag == "" {
		tag = "v" + latest
	}
	notes, _ := data["notes"].(string)
	published, _ := data["publishedAt"].(string)
	size := 0
	if number, ok := asset["size"].(json.Number); ok {
		fmt.Sscanf(number.String(), "%d", &size)
	}
	return map[string]any{
		"available":   versionGT(latest, s.Version),
		"current":     s.Version,
		"latest":      latest,
		"tag":         tag,
		"notes":       plainNotes(notes),
		"sizeBytes":   size,
		"sha256":      asset["sha256"],
		"asset":       stringOf(asset["name"]),
		"publishedAt": published,
		"source":      "manifest",
	}
}

// Progress 下载/校验/解包进度（轮询端点读）。
func (s *Service) Progress() map[string]any {
	s.progress.mu.Lock()
	empty := s.progress.data.phase == "" && !s.progress.data.running && s.progress.data.received == 0 &&
		s.progress.data.total == nil && s.progress.data.error == "" && !s.progress.data.ok
	s.progress.mu.Unlock()
	if empty {
		s.progress.set(func() {})
	}
	return s.progress.snapshot()
}

// MarkDownloadStarting 原子置位下载中状态，防并发竞争。
func (s *Service) MarkDownloadStarting(tag string, expectedSize int64) bool {
	s.progress.mu.Lock()
	defer s.progress.mu.Unlock()
	if s.progress.data.running {
		return false
	}
	s.progress.data = progressData{running: true, phase: "probe"}
	if expectedSize > 0 {
		s.progress.data.total = expectedSize
		s.progress.data.percent = float64(0)
	}
	_ = tag
	return true
}

// CancelDownload 置位取消旗标，下载循环在分片边界自查退出（.part 保留续传）。
func (s *Service) CancelDownload() map[string]any {
	s.progress.set(func() { s.progress.data.cancel = true })
	return map[string]any{"cancelled": true}
}

// ClearStaging 放弃本次更新：清暂存目录；下载中先置取消旗标再删。
func (s *Service) ClearStaging() map[string]any {
	s.progress.set(func() {
		if s.progress.data.running {
			s.progress.data.cancel = true
		}
	})
	_ = os.RemoveAll(s.stagingDir())
	s.progress.set(func() { s.progress.data = progressData{} })
	return map[string]any{"cleared": true}
}

// Download 下载 release zip 到 staging 并解包校验（fire-and-forget 的执行体；
// 调用方先 MarkDownloadStarting）。返回值结构与 Python download_update 一致。
func (s *Service) Download(ctx context.Context, tag string, expectedSHA256, assetName string, expectedSize int64, notes string) map[string]any {
	if strings.TrimSpace(expectedSHA256) == "" {
		s.finishProgress("verify_failed", "更新源未提供完整性校验值，已拒绝下载（更新源不可信或残缺）")
		return map[string]any{"ok": false, "error": "更新源未提供完整性校验值"}
	}
	staging := s.stagingDir()
	part := filepath.Join(staging, partName)
	metaPath := filepath.Join(staging, downloadMetaName)
	_ = os.Remove(filepath.Join(staging, StagingManifest))

	// 续传基线与资产绑定：元数据（tag/asset/size/sha256）完全一致且未超目标
	// 大小才保留 .part 接着下；任一不符视为其他版本或损坏残留，从头开始。
	metaValid := false
	if info, err := os.Stat(part); err == nil {
		if raw, rerr := os.ReadFile(metaPath); rerr == nil {
			var oldMeta map[string]any
			if json.Unmarshal(raw, &oldMeta) == nil &&
				stringOf(oldMeta["tag"]) == tag &&
				stringOf(oldMeta["asset"]) == assetName &&
				numberEquals(oldMeta["size"], expectedSize) &&
				stringOf(oldMeta["sha256"]) == expectedSHA256 &&
				(expectedSize <= 0 || info.Size() <= expectedSize) {
				metaValid = true
			}
		}
		if !metaValid || (expectedSize > 0 && fileSizeOrZero(part) > expectedSize) {
			_ = os.Remove(part)
		}
	}
	_ = os.WriteFile(metaPath, []byte(fmt.Sprintf(
		`{"tag":%q,"asset":%q,"size":%d,"sha256":%q}`, tag, assetName, expectedSize, expectedSHA256)), 0o644)

	s.progress.set(func() {
		if info, err := os.Stat(part); err == nil {
			s.progress.data.received = info.Size()
		}
	})

	result := s.runDownload(ctx, staging, part, metaPath, tag, expectedSHA256, assetName, expectedSize, notes)
	s.progress.set(func() { s.progress.data.running = false })
	return result
}

func (s *Service) finishProgress(code, message string) {
	s.progress.set(func() {
		s.progress.data.running = false
		s.progress.data.code = code
		s.progress.data.error = message
	})
}

func (s *Service) runDownload(ctx context.Context, staging, part, metaPath, tag, expectedSHA256, assetName string, expectedSize int64, notes string) map[string]any {
	defer func() {
		s.progress.set(func() { s.progress.data.running = false })
	}()
	assetURL, urlIsLocal := s.resolveAssetURL(tag, assetName)
	channels := s.channelPlan(assetURL, urlIsLocal)
	if len(channels) == 0 {
		channels = []probeChannel{{Channel: Channel{Label: "直连"}}}
	}
	probes := probeChannels(ctx, channels, expectedSize)
	if len(probes) == 0 {
		s.finishProgress("network", "所有下载通道均失败：无可用通道")
		return map[string]any{"ok": false, "error": "所有下载通道均失败"}
	}
	// 「资产不存在」双成因：GitHub 直连明确 404 = 真没发；镜像 404 不作数
	// （可能只是没同步）。全部通道 404/体积不符也按缺资产处理。
	for _, probe := range probes {
		if probe.direct && probe.missing {
			message := fmt.Sprintf("版本 %s 的发布资产不存在（GitHub 返回 404）", tag)
			s.finishProgress("asset_missing", message)
			return map[string]any{"ok": false, "error": message}
		}
	}
	unusable := 0
	for _, probe := range probes {
		if probe.missing || probe.bogus {
			unusable++
		}
	}
	if unusable == len(probes) {
		message := fmt.Sprintf("版本 %s 的发布资产不可用（各通道 404 或体积不符）", tag)
		s.finishProgress("asset_missing", message)
		return map[string]any{"ok": false, "error": message}
	}
	usable := make([]probeResult, 0, len(probes))
	for _, probe := range probes {
		if !probe.missing && !probe.bogus {
			usable = append(usable, probe)
		}
	}
	if len(usable) == 0 {
		usable = probes
	}
	total := expectedSize
	if total <= 0 {
		for _, probe := range usable {
			if probe.total > 0 {
				total = probe.total
				break
			}
		}
	}
	s.progress.set(func() {
		s.progress.data.phase = "download"
		if total > 0 {
			s.progress.data.total = total
			if info, err := os.Stat(part); err == nil {
				s.progress.data.percent = float64(info.Size()) * 100 / float64(total)
			} else {
				s.progress.data.percent = float64(0)
			}
		}
	})

	var lastErr error
	for idx, probe := range usable {
		baseline := fileSizeOrZero(part)
		minRate := int64(minChannelRate)
		if idx == len(usable)-1 {
			minRate = 0 // 最后一个可用通道不考核：全网都慢也得让它下完
		}
		err := s.streamToFile(ctx, probe.URL, probe.Proxy, part, total, minRate, probe.Label)
		if err == nil {
			// 完整性闸一：字节数对不上当这条通道没干成活——不进校验阶段，
			// 否则用户看到的是「校验失败」，真因（通道返回垃圾）被盖住。
			got := fileSizeOrZero(part)
			if got < minPackageBytes || (expectedSize > 0 && got != expectedSize) {
				truncateTo(part, baseline)
				lastErr = fmt.Errorf("通道返回内容不完整（%d 字节），换下一通道", got)
				continue
			}
			// 完整性闸二：镜像可能完整返回错误内容，立即校验换道，不清文件
			// 前先比对（失败清 .part 从头再来）。
			actual, derr := sha256OfFile(part)
			if derr != nil || !strings.EqualFold(actual, strings.TrimSpace(expectedSHA256)) {
				_ = os.Remove(part)
				lastErr = fmt.Errorf("SHA256 校验失败（实际 %s…），换下一通道", clip(actual, 16))
				continue
			}
			break
		}
		if errors.Is(err, ErrCancelled) {
			s.finishProgress("cancelled", "已取消下载")
			return map[string]any{"ok": false, "cancelled": true}
		}
		if info, serr := os.Stat(part); serr == nil && info.Size() != baseline {
			truncateTo(part, baseline)
		}
		if s.Log != nil {
			s.Log("更新下载通道不可用，换下一通道 │ 通道=%s 原因=%v", probe.Label, err)
		}
		lastErr = err
	}
	if fileSizeOrZero(part) == 0 {
		message := "所有下载通道均失败"
		if lastErr != nil {
			message += "：" + lastErr.Error()
		}
		s.finishProgress("network", message)
		return map[string]any{"ok": false, "error": message}
	}

	zipPath := filepath.Join(staging, zipName)
	if err := os.Rename(part, zipPath); err != nil {
		s.finishProgress("error", err.Error())
		return map[string]any{"ok": false, "error": err.Error()}
	}
	// 终验（安全兜底防线）：失败清 staging，绝不进换装链。
	s.progress.set(func() { s.progress.data.phase = "verify" })
	if actual, err := sha256OfFile(zipPath); err != nil || !strings.EqualFold(actual, strings.TrimSpace(expectedSHA256)) {
		_ = os.RemoveAll(staging)
		message := fmt.Sprintf("SHA256 校验失败：期望 %s… 实得 %s…", clip(expectedSHA256, 16), clip(actual, 16))
		s.finishProgress("verify_failed", message)
		return map[string]any{"ok": false, "error": message}
	}
	// 解包到 staging/Holdexar/（zip 内根目录即 Holdexar/）。
	s.progress.set(func() { s.progress.data.phase = "extract" })
	if err := extractZip(zipPath, staging); err != nil {
		_ = os.RemoveAll(staging)
		s.finishProgress("error", "解包失败："+err.Error())
		return map[string]any{"ok": false, "error": "解包失败：" + err.Error()}
	}
	_ = os.Remove(metaPath)

	// 逐文件校验值入清单：换装宿主据此核验暂存载荷（解包后到换装前的窗口
	// 期内文件缺失/被改，含杀软隔离，都能在换装前被拒）。
	payloadRoot := filepath.Join(staging, appPayloadDir)
	files, err := payloadHashes(payloadRoot)
	if err != nil {
		_ = os.RemoveAll(staging)
		s.finishProgress("error", "载荷校验值计算失败："+err.Error())
		return map[string]any{"ok": false, "error": "载荷校验值计算失败：" + err.Error()}
	}
	manifest := map[string]any{
		"tag": tag, "sha256": expectedSHA256, "ready": true,
		"attempt_id": randomHex16(), "source_version": s.Version,
		"files": files, "notes": notes,
	}
	payload, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		_ = os.RemoveAll(staging)
		s.finishProgress("error", err.Error())
		return map[string]any{"ok": false, "error": err.Error()}
	}
	if err := os.WriteFile(filepath.Join(staging, StagingManifest), payload, 0o644); err != nil {
		_ = os.RemoveAll(staging)
		s.finishProgress("error", err.Error())
		return map[string]any{"ok": false, "error": err.Error()}
	}
	// 新暂存包重新具备交接资格：上一包的不支持标记作废，上次失败的安装
	// 状态标记与交接账本一并解除（重新下载同一版本不被旧账挡住）。
	_ = os.Remove(filepath.Join(staging, ".handoff-unsupported"))
	_ = os.Remove(filepath.Join(s.dataDir(), SwapFailedFlag))
	_ = os.Remove(filepath.Join(s.dataDir(), HandoffLedgerName))
	s.progress.set(func() {
		s.progress.data.ok = true
		s.progress.data.phase = "done"
		s.progress.data.percent = float64(100)
	})
	if s.Log != nil {
		s.Log("更新包已下载暂存，重启后换装 │ 版本=%s", tag)
	}
	return map[string]any{"ok": true, "staging": staging, "tag": tag}
}

// channelPlan 候选通道 = 通道提供方（内核代理/本地口/直连）× 镜像前缀；
// 本地测试源（127.0.0.1/localhost）只走直连单通道。
func (s *Service) channelPlan(assetURL string, local bool) []probeChannel {
	if local {
		return []probeChannel{{Channel: Channel{Label: "本地测试更新源"}, URL: assetURL, direct: true}}
	}
	var plan []probeChannel
	for _, channel := range s.Channels() {
		for _, mirror := range MirrorList() {
			plan = append(plan, probeChannel{
				Channel: Channel{Proxy: channel.Proxy, Label: channel.Label + mirrorLabel(mirror)},
				URL:     mirror + assetURL,
				direct:  mirror == "" && channel.Proxy == "",
			})
		}
	}
	return plan
}

func mirrorLabel(mirror string) string {
	if mirror == "" {
		return ""
	}
	return "·镜像"
}

// resolveAssetURL 确定资产下载地址：清单给了确切名就直连，否则按命名规则。
// （Python 的 API 反查兜底保留在探测失败后的重试路径——命名规则猜不出带时间
// 戳的资产名，但发行包命名是固定的，可先按规则探测。）
func (s *Service) resolveAssetURL(tag, assetName string) (string, bool) {
	if base := UpdateURLFromEnv(); base != "" && assetName != "" {
		return base + "/" + assetName, strings.Contains(base, "127.0.0.1") || strings.Contains(base, "localhost")
	}
	if assetName != "" {
		return fmt.Sprintf("https://github.com/%s/releases/download/%s/%s", GitHubRepo, tag, assetName), false
	}
	guessed := fmt.Sprintf("https://github.com/%s/releases/download/%s/%s-win64-v%s.zip",
		GitHubRepo, tag, appPayloadDir, strings.TrimLeft(tag, "vV"))
	return guessed, false
}

// probeChannels 并发探测全部通道：Range 0-0 判存在+量延迟；可用者按延迟
// 升序返回；探不到的排最后（全探不到时总得有条路可试）。
func probeChannels(ctx context.Context, channels []probeChannel, fallbackTotal int64) []probeResult {
	results := make([]probeResult, len(channels))
	var wg sync.WaitGroup
	for i := range channels {
		channel := channels[i]
		wg.Add(1)
		go func(slot int, channel probeChannel) {
			defer wg.Done()
			started := time.Now()
			probe := probeResult{probeChannel: channel, latency: time.Hour}
			request, err := http.NewRequestWithContext(ctx, http.MethodGet, channel.URL, nil)
			if err != nil {
				results[slot] = probe
				return
			}
			resp, perr := doProbe(ctx, channel, request)
			probe = probeWithResponse(resp, perr, channel, fallbackTotal, started)
			results[slot] = probe
		}(i, channel)
	}
	wg.Wait()
	sort.SliceStable(results, func(i, j int) bool { return results[i].latency < results[j].latency })
	return results
}

// doProbe 单通道探测请求（代理走独立 Transport）。
func doProbe(ctx context.Context, channel probeChannel, request *http.Request) (*http.Response, error) {
	if channel.Proxy == "" {
		return probeClientGlobal.Do(request.WithContext(ctx))
	}
	transport := http.DefaultTransport.(*http.Transport).Clone()
	proxyURL, err := url.Parse(channel.Proxy)
	if err != nil {
		return nil, err
	}
	transport.Proxy = func(*http.Request) (*url.URL, error) { return proxyURL, nil }
	client := &http.Client{Timeout: time.Duration(probeTimeoutSec) * time.Second, Transport: transport}
	return client.Do(request.WithContext(ctx))
}

var probeClientGlobal = &http.Client{Timeout: time.Duration(probeTimeoutSec) * time.Second}

func probeWithResponse(resp *http.Response, err error, channel probeChannel, fallbackTotal int64, started time.Time) probeResult {
	probe := probeResult{probeChannel: channel, latency: time.Hour, total: fallbackTotal}
	if err != nil {
		return probe
	}
	defer func() {
		_, _ = io.Copy(io.Discard, io.LimitReader(resp.Body, 64))
		_ = resp.Body.Close()
	}()
	total := totalFromHeaders(resp.Header, resp.StatusCode)
	if total <= 0 {
		total = fallbackTotal
	}
	bogus := (total > 0 && total < minPackageBytes) ||
		(fallbackTotal > 0 && total > 0 && total != fallbackTotal)
	probe.status = resp.StatusCode
	probe.total = total
	probe.missing = resp.StatusCode == http.StatusNotFound || resp.StatusCode == http.StatusGone
	probe.bogus = bogus
	probe.latency = time.Since(started)
	return probe
}

// totalFromHeaders 取整包体积：206 的总量在 content-range 尾段，200 才看
// content-length——两者混用会把 1 字节探测响应当成整包体积。
func totalFromHeaders(header http.Header, status int) int64 {
	if contentRange := header.Get("Content-Range"); contentRange != "" {
		if idx := strings.LastIndex(contentRange, "/"); idx >= 0 {
			tail := contentRange[idx+1:]
			if n, err := strconv.ParseInt(tail, 10, 64); err == nil && n > 0 {
				return n
			}
		}
		return 0
	}
	if status == http.StatusPartialContent {
		return 0
	}
	if raw := header.Get("Content-Length"); raw != "" {
		if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
			return n
		}
	}
	return 0
}

// streamToFile 流式下载+断点续传+停滞/速率双闸。失败抛错（调用方换通道）。
// 服务端不认 Range 回 200 时下载到 .full 副产物，全包完整后原子替换——
// 避免中途失败抹掉已有续传进度。
func (s *Service) streamToFile(ctx context.Context, rawURL, proxy, dest string, expectedTotal int64, minRateBps int64, label string) error {
	resumed := fileSizeOrZero(dest)
	transport := http.DefaultTransport.(*http.Transport).Clone()
	if proxy != "" {
		proxyURL, perr := url.Parse(proxy)
		if perr != nil {
			return perr
		}
		transport.Proxy = func(*http.Request) (*url.URL, error) { return proxyURL, nil }
	}
	client := &http.Client{
		Timeout:   time.Duration(downloadTimeoutSec) * time.Second,
		Transport: transport,
	}
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return err
	}
	if resumed > 0 {
		request.Header.Set("Range", fmt.Sprintf("bytes=%d-", resumed))
	}
	resp, err := client.Do(request)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusPartialContent {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	useSidecar := resumed > 0 && resp.StatusCode == http.StatusOK
	if resp.StatusCode == http.StatusOK {
		resumed = 0 // 服务端不认 Range：必须从头下
	}
	total := totalFromHeaders(resp.Header, resp.StatusCode)
	if total <= 0 {
		total = expectedTotal
	}
	received := resumed
	s.progress.set(func() {
		if total > 0 {
			s.progress.data.total = total
		}
		s.progress.data.channel = label
	})

	target := dest
	if useSidecar {
		target = dest + ".full"
	}
	file, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	writer := file
	if !useSidecar && received > 0 {
		file.Close()
		file, err = os.OpenFile(dest, os.O_APPEND|os.O_WRONLY, 0o644)
		if err != nil {
			return err
		}
		writer = file
	}
	started := time.Now()
	windowAt := started
	var windowBytes int64
	stall := time.Duration(stallTimeout) * time.Second
	buf := make([]byte, 1<<20)
	for {
		s.progress.mu.Lock()
		cancelled := s.progress.data.cancel
		s.progress.mu.Unlock()
		if cancelled {
			file.Close()
			if useSidecar {
				_ = os.Remove(target)
			}
			return ErrCancelled
		}
		// 停滞闸：读超时=连上了但不吐数据的半死通道，不让进度条干等。
		if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) <= 0 {
			file.Close()
			return fmt.Errorf("下载总时长超限，换下一通道")
		}
		type result struct {
			n   int
			err error
		}
		done := make(chan result, 1)
		go func() {
			n, rerr := resp.Body.Read(buf)
			done <- result{n, rerr}
		}()
		var read result
		select {
		case read = <-done:
		case <-time.After(stall):
			file.Close()
			return fmt.Errorf("通道停滞（%ds 无数据），换下一通道", stallTimeout)
		case <-ctx.Done():
			file.Close()
			return ErrCancelled
		}
		if read.n > 0 {
			if _, werr := writer.Write(buf[:read.n]); werr != nil {
				file.Close()
				return werr
			}
			received += int64(read.n)
			windowBytes += int64(read.n)
			now := time.Now()
			if now.Sub(windowAt) >= time.Second {
				speed := int64(float64(windowBytes) / now.Sub(windowAt).Seconds())
				percent := any(nil)
				if total > 0 {
					percent = float64(received) * 100 / float64(total)
				}
				s.progress.set(func() {
					s.progress.data.speed = speed
					s.progress.data.received = received
					s.progress.data.percent = percent
				})
				windowAt, windowBytes = now, 0
			}
			// 速率闸：试跑期后平均速率低于下限判「过慢」——停滞闸抓不住
			// 「慢但在动」的通道（曾有镜像 40KB/s 整包要一小时）。
			if minRateBps > 0 {
				elapsed := now.Sub(started).Seconds()
				if elapsed >= trialSeconds && float64(received-resumed)/elapsed < float64(minRateBps) {
					file.Close()
					return fmt.Errorf("通道过慢（%.0f KB/s < %.0f KB/s），换下一通道",
						float64(received-resumed)/elapsed/1024, float64(minRateBps)/1024)
				}
			}
		}
		if read.err != nil {
			if errors.Is(read.err, io.EOF) {
				break // 正常结束：交给函数尾部唯一的 Close
			}
			file.Close()
			return read.err
		}
	}
	if err := file.Close(); err != nil {
		return err
	}
	if useSidecar {
		if err := os.Rename(target, dest); err != nil {
			_ = os.Remove(target)
			return err
		}
	}
	return nil
}

// truncateTo 把续传文件截回基线（本轮写入的都作废）；截不动就删掉宁可重下。
func truncateTo(path string, size int64) {
	info, err := os.Stat(path)
	if err != nil {
		return
	}
	if info.Size() > size && size >= 0 {
		if file, ferr := os.OpenFile(path, os.O_RDWR, 0); ferr == nil {
			_ = file.Truncate(size)
			_ = file.Close()
		}
		return
	}
	if info.Size() < size {
		_ = os.Remove(path) // 禁止 truncate 补零膨胀
	}
}

func extractZip(zipPath, staging string) error {
	reader, err := zip.OpenReader(zipPath)
	if err != nil {
		return err
	}
	defer reader.Close()
	for _, entry := range reader.File {
		// PowerShell 打包器的 zip 条目用反斜杠分隔（规范要求正斜杠）：目录
		// 条目尾部反斜杠会被当文件、路径逐级错乱——统一归一化后再计算。
		entry.Name = strings.ReplaceAll(entry.Name, "\\", "/")
		target := filepath.Join(staging, filepath.FromSlash(entry.Name))
		if !strings.HasPrefix(filepath.Clean(target), filepath.Clean(staging)+string(os.PathSeparator)) {
			return fmt.Errorf("压缩包内路径越界：%s", entry.Name)
		}
		if entry.FileInfo().IsDir() {
			if err := os.MkdirAll(target, 0o755); err != nil {
				return fmt.Errorf("条目 %q 建目录失败：%w", entry.Name, err)
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return fmt.Errorf("条目 %q 建父目录失败：%w", entry.Name, err)
		}
		source, err := entry.Open()
		if err != nil {
			return fmt.Errorf("条目 %q 打开失败：%w", entry.Name, err)
		}
		out, err := os.OpenFile(target, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o644)
		if err != nil {
			source.Close()
			return fmt.Errorf("条目 %q 建文件失败：%w", entry.Name, err)
		}
		if _, err := io.Copy(out, source); err != nil {
			source.Close()
			out.Close()
			return fmt.Errorf("条目 %q 写入失败：%w", entry.Name, err)
		}
		if err := out.Close(); err != nil {
			source.Close()
			return fmt.Errorf("条目 %q 收尾失败：%w", entry.Name, err)
		}
		source.Close()
	}
	_ = os.Remove(zipPath)
	return nil
}

func fileSizeOrZero(path string) int64 {
	info, err := os.Stat(path)
	if err != nil {
		return 0
	}
	return info.Size()
}

func numberEquals(value any, expected int64) bool {
	number, ok := value.(json.Number)
	if !ok {
		return expected == 0 && value == nil
	}
	parsed, err := strconv.ParseInt(number.String(), 10, 64)
	return err == nil && parsed == expected
}

func randomHex16() string {
	buf := make([]byte, 16)
	if _, err := rand.Read(buf); err != nil {
		return fmt.Sprintf("%x-%d", buf, time.Now().UnixNano())
	}
	return hex.EncodeToString(buf)
}

func probeResultLess(_ *[]probeResult) {}

type probeResult struct {
	probeChannel
	status  int
	total   int64
	missing bool
	bogus   bool
	latency time.Duration
}

var _ = sha256.Sum256
