package updater

import (
	"archive/zip"
	"bytes"
	"cmp"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type progressState struct {
	mu   sync.Mutex
	data progressData
}

// progressData 进度字段集；重置只换 data，绝不替换持有 mu 的外层结构。
type progressData struct {
	running  bool
	phase    string
	percent  any
	received int64
	total    any
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

type AssetMissing struct{ Message string }

func (e *AssetMissing) Error() string { return e.Message }

type Service struct {
	mu          sync.Mutex
	progress    progressState
	DataDir     string
	InstallRoot string
	Version     string
	HTTPClient  *http.Client
	Channels    func() []Channel
	Log         func(string, ...any)
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

// CheckUpdate 优先校验签名清单，不可达时回退 GitHub API；均失败返回 available=false。
func (s *Service) CheckUpdate(client *http.Client) map[string]any {
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	if data := fetchManifest(client); data != nil {
		return s.payloadFromManifest(data)
	}
	return checkViaAPI(client, s.Version)
}

func (s *Service) payloadFromManifest(data map[string]any) map[string]any {
	latest := versionFromTag(stringOf(data["version"]))
	if latest == "" {
		return map[string]any{"available": false, "reason": "bad_manifest"}
	}
	assets, _ := data["assets"].([]any)
	asset := pickAsset(assets, "")
	if asset == nil {
		return map[string]any{"available": false, "reason": "no_asset"}
	}
	// 资产缺 sha256 或载荷校验表 = 发布侧残缺：有更新也不能进无校验下载。
	digest := strings.TrimSpace(stringOf(asset["sha256"]))
	if !hex64Re.MatchString(digest) {
		return map[string]any{"available": false, "reason": "bad_manifest"}
	}
	if _, ok := validPayloadFiles(asset["files"]); !ok {
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

func (s *Service) CancelDownload() map[string]any {
	s.progress.set(func() { s.progress.data.cancel = true })
	return map[string]any{"cancelled": true}
}

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

func (s *Service) Download(ctx context.Context, tag string, expectedSHA256, assetName string, expectedSize int64, notes string) map[string]any {
	client := s.HTTPClient
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	release := fetchManifest(client)
	if release == nil {
		s.finishProgress("verify_failed", "签名更新清单不可用，已拒绝下载")
		return map[string]any{"ok": false, "error": "签名更新清单不可用，已拒绝下载"}
	}
	releaseTag := stringOf(release["tag"])
	if releaseTag == "" {
		if ver := versionFromTag(stringOf(release["version"])); ver != "" {
			releaseTag = "v" + ver
		}
	}
	if tag != releaseTag {
		s.finishProgress("verify_failed", "请求的更新版本与签名清单不一致")
		return map[string]any{"ok": false, "error": "请求的更新版本与签名清单不一致"}
	}
	if !versionGT(versionFromTag(tag), versionFromTag(s.Version)) {
		s.finishProgress("verify_failed", "目标版本不高于当前版本，已拒绝下载")
		return map[string]any{"ok": false, "error": "目标版本不高于当前版本，已拒绝下载"}
	}
	assets, _ := release["assets"].([]any)
	asset := pickAsset(assets, "")
	if asset == nil {
		s.finishProgress("verify_failed", "签名清单缺少应用安装包")
		return map[string]any{"ok": false, "error": "签名清单缺少应用安装包"}
	}
	signedAsset := stringOf(asset["name"])
	if !assetPattern.MatchString(signedAsset) {
		s.finishProgress("verify_failed", "签名清单应用安装包名称非法")
		return map[string]any{"ok": false, "error": "签名清单应用安装包名称非法"}
	}
	if _, ok := validPayloadFiles(asset["files"]); !ok {
		s.finishProgress("verify_failed", "签名清单缺少有效的载荷校验表")
		return map[string]any{"ok": false, "error": "签名清单缺少有效的载荷校验表"}
	}
	signedSHA256 := strings.ToLower(strings.TrimSpace(stringOf(asset["sha256"])))
	signedSize, sizeOK := int64Of(asset["size"])
	if !hex64Re.MatchString(signedSHA256) || !sizeOK || signedSize < 0 {
		s.finishProgress("verify_failed", "签名清单缺少有效的安装包校验值")
		return map[string]any{"ok": false, "error": "签名清单缺少有效的安装包校验值"}
	}
	if (strings.TrimSpace(expectedSHA256) != "" && strings.ToLower(strings.TrimSpace(expectedSHA256)) != signedSHA256) ||
		(strings.TrimSpace(assetName) != "" && assetName != signedAsset) ||
		(expectedSize > 0 && expectedSize != signedSize) {
		s.finishProgress("verify_failed", "下载参数与签名清单不一致")
		return map[string]any{"ok": false, "error": "下载参数与签名清单不一致"}
	}
	expectedSHA256 = signedSHA256
	assetName = signedAsset
	expectedSize = signedSize
	notes = stringOf(release["notes"])

	staging := s.stagingDir()
	part := filepath.Join(staging, partName)
	metaPath := filepath.Join(staging, downloadMetaName)
	_ = os.Remove(filepath.Join(staging, StagingManifest))

	// 仅当 .part 元数据与签名清单完全一致且未超大小时断点续传，否则清除重下。
	metaValid := false
	if info, err := os.Stat(part); err == nil {
		if raw, rerr := os.ReadFile(metaPath); rerr == nil {
			dec := json.NewDecoder(bytes.NewReader(raw))
			dec.UseNumber()
			var oldMeta map[string]any
			if dec.Decode(&oldMeta) == nil &&
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
		s.progress.data.running = true
		s.progress.data.phase = "probe"
		if expectedSize > 0 {
			s.progress.data.total = expectedSize
			s.progress.data.percent = float64(0)
		}
		if info, err := os.Stat(part); err == nil {
			s.progress.data.received = info.Size()
		}
	})

	result := s.runDownload(ctx, staging, part, metaPath, tag, expectedSHA256, assetName, expectedSize, notes, release)
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

func (s *Service) runDownload(ctx context.Context, staging, part, metaPath, tag, expectedSHA256, assetName string, expectedSize int64, notes string, release map[string]any) map[string]any {
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
	// 直连返回 404/410 或全部通道 404/体积异常时判定资产缺失（单镜像 404 不早退）。
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
			got := fileSizeOrZero(part)
			if got < minPackageBytes || (expectedSize > 0 && got != expectedSize) {
				truncateTo(part, baseline)
				lastErr = fmt.Errorf("通道返回内容不完整（%d 字节），换下一通道", got)
				continue
			}
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
	s.progress.set(func() { s.progress.data.phase = "extract" })
	if err := extractZip(zipPath, staging); err != nil {
		_ = os.RemoveAll(staging)
		s.finishProgress("error", "解包失败："+err.Error())
		return map[string]any{"ok": false, "error": "解包失败：" + err.Error()}
	}
	_ = os.Remove(metaPath)

	// 落盘逐文件 SHA-256 供换装前复核载荷未被篡改或隔离。
	payloadRoot := filepath.Join(staging, appPayloadDir)
	files, err := payloadHashes(payloadRoot)
	if err != nil {
		_ = os.RemoveAll(staging)
		s.finishProgress("error", "载荷校验值计算失败："+err.Error())
		return map[string]any{"ok": false, "error": "载荷校验值计算失败：" + err.Error()}
	}
	manifest := map[string]any{
		"tag":              tag,
		"sha256":           expectedSHA256,
		"ready":            true,
		"attempt_id":       randomHex16(),
		"source_version":   s.Version,
		"files":            files,
		"notes":            notes,
		"release_manifest": release,
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
	if err := VerifyStagedTrust(staging, s.dataDir(), versionFromTag(tag)); err != nil {
		_ = os.RemoveAll(staging)
		s.finishProgress("verify_failed", err.Error())
		return map[string]any{"ok": false, "error": err.Error()}
	}
	// 暂存验签通过后清理换装失败与交接状态标记，允许新包进入换装。
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

func (s *Service) resolveAssetURL(tag, assetName string) (string, bool) {
	if base := UpdateURLFromEnv(); base != "" && assetName != "" {
		return base + "/" + assetName, true
	}
	if assetName != "" {
		return fmt.Sprintf("https://github.com/%s/releases/download/%s/%s", GitHubRepo, tag, assetName), false
	}
	guessed := fmt.Sprintf("https://github.com/%s/releases/download/%s/%s-win64-v%s.zip",
		GitHubRepo, tag, appPayloadDir, versionFromTag(tag))
	return guessed, false
}

// probeChannels 并发发送 Range 0-0 GET 探测通道并按延迟升序返回，未探通置后。
func probeChannels(ctx context.Context, channels []probeChannel, fallbackTotal int64) []probeResult {
	results := make([]probeResult, len(channels))
	var wg sync.WaitGroup
	for i := range channels {
		channel := channels[i]
		wg.Go(func() {
			started := time.Now()
			probe := probeResult{probeChannel: channel, latency: time.Hour}
			request, err := http.NewRequestWithContext(ctx, http.MethodGet, channel.URL, nil)
			if err != nil {
				results[i] = probe
				return
			}
			request.Header.Set("Range", "bytes=0-0")
			resp, perr := doProbe(ctx, channel, request)
			probe = probeWithResponse(resp, perr, channel, fallbackTotal, started)
			results[i] = probe
		})
	}
	wg.Wait()
	slices.SortStableFunc(results, func(a, b probeResult) int { return cmp.Compare(a.latency, b.latency) })
	return results
}

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

// totalFromHeaders 206 取 Content-Range 总量，200 取 Content-Length，防把分片长度当整包大小。
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

// streamToFile 支持断点续传与停滞/速率双闸；服务端忽略 Range 回 200 时写 .full 完成后原子替换。
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
	readCtx, cancelRead := context.WithCancel(ctx)
	defer cancelRead()
	request, err := http.NewRequestWithContext(readCtx, http.MethodGet, rawURL, nil)
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
		resumed = 0
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
	flags := os.O_CREATE | os.O_TRUNC | os.O_WRONLY
	if useSidecar {
		target = dest + ".full"
	} else if received > 0 {
		flags = os.O_WRONLY | os.O_APPEND
	}
	file, err := os.OpenFile(target, flags, 0o644)
	if err != nil {
		return err
	}
	writer := file
	started := time.Now()
	windowAt := started
	var windowBytes int64
	stall := stallDuration
	var timedOut atomic.Bool
	stallTimer := time.AfterFunc(stall, func() {
		timedOut.Store(true)
		cancelRead()
	})
	defer stallTimer.Stop()
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
		if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) <= 0 {
			file.Close()
			return fmt.Errorf("下载总时长超限，换下一通道")
		}
		if ctx.Err() != nil {
			file.Close()
			return ErrCancelled
		}
		stallTimer.Reset(stall)
		n, rerr := resp.Body.Read(buf)
		stallTimer.Reset(stall)
		if n > 0 {
			if _, werr := writer.Write(buf[:n]); werr != nil {
				file.Close()
				return werr
			}
			received += int64(n)
			windowBytes += int64(n)
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
			if minRateBps > 0 {
				elapsed := now.Sub(started).Seconds()
				if elapsed >= trialSeconds && float64(received-resumed)/elapsed < float64(minRateBps) {
					file.Close()
					return fmt.Errorf("通道过慢（%.0f KB/s < %.0f KB/s），换下一通道",
						float64(received-resumed)/elapsed/1024, float64(minRateBps)/1024)
				}
			}
		}
		if rerr != nil {
			if errors.Is(rerr, io.EOF) {
				break
			}
			file.Close()
			s.progress.mu.Lock()
			wasCancelled := s.progress.data.cancel
			s.progress.mu.Unlock()
			if wasCancelled {
				if useSidecar {
					_ = os.Remove(target)
				}
				return ErrCancelled
			}
			if ctx.Err() != nil {
				if deadline, ok := ctx.Deadline(); ok && time.Until(deadline) <= 0 {
					return fmt.Errorf("下载总时长超限，换下一通道")
				}
				return ErrCancelled
			}
			if timedOut.Load() {
				return fmt.Errorf("通道停滞（%ds 无数据），换下一通道", stallTimeout)
			}
			return rerr
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

// truncateTo 将超出基线的 .part 截回基线（截断失败忽略）；短于基线时删除以防补零。
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
		_ = os.Remove(path)
	}
}

func extractZip(zipPath, staging string) error {
	return extractZipWithLimit(zipPath, staging, maxExtractBytes)
}

func extractZipWithLimit(zipPath, staging string, limit int64) (retErr error) {
	reader, err := zip.OpenReader(zipPath)
	if err != nil {
		return err
	}
	defer func() {
		_ = reader.Close()
		if retErr != nil {
			_ = os.RemoveAll(staging)
		}
	}()
	var totalWritten int64
	for _, entry := range reader.File {
		// 归一化 zip 条目反斜杠路径，避免目录条目被误判或越界校验错乱。
		entry.Name = strings.ReplaceAll(entry.Name, "\\", "/")
		if entry.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("压缩包包含符号链接：%s", entry.Name)
		}
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
		written, copyErr := io.Copy(out, io.LimitReader(source, limit-totalWritten+1))
		closeErr := out.Close()
		source.Close()
		if copyErr != nil {
			return fmt.Errorf("条目 %q 写入失败：%w", entry.Name, copyErr)
		}
		totalWritten += written
		if totalWritten > limit {
			return fmt.Errorf("压缩包解压体积超出上限（>%d 字节）", limit)
		}
		if closeErr != nil {
			return fmt.Errorf("条目 %q 收尾失败：%w", entry.Name, closeErr)
		}
	}
	_ = reader.Close()
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
