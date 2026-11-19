// 内核直连下载兜底：随包资产缺失时按镜像链直连下载（不经代理）。
package clashkernel

import (
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const (
	mihomoRepo            = "https://github.com/MetaCubeX/mihomo"
	githubAPIBase         = "https://api.github.com/repos/MetaCubeX/mihomo/releases/tags/"
	kernelVerifyFailedMsg = "内核下载校验未通过，请稍后重试"

	maxReleaseAPIBytes int64 = 4 << 20
	maxArchiveBytes    int64 = 64 << 20
	maxKernelBytes     int64 = 128 << 20
)

var (
	releaseAPISizeLimit = maxReleaseAPIBytes
	archiveSizeLimit    = maxArchiveBytes
	kernelSizeLimit     = maxKernelBytes
)

func readBounded(r io.Reader, limit int64, label string) ([]byte, error) {
	raw, err := io.ReadAll(io.LimitReader(r, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(raw)) > limit {
		return nil, fmt.Errorf("%s体积超出上限（%d 字节）", label, limit)
	}
	return raw, nil
}

func ReleaseAPIURL() string {
	return githubAPIBase + MIHOMOVersion
}

var DefaultMirrors = []string{"https://ghfast.top/", "https://gh-proxy.com/", ""}

func MihomoAsset() (asset, shape string) {
	if isWindows() {
		return fmt.Sprintf("mihomo-windows-amd64-compatible-%s.zip", MIHOMOVersion), "zip"
	}
	return fmt.Sprintf("mihomo-linux-amd64-compatible-%s.gz", MIHOMOVersion), "gz"
}

func MihomoURL() string {
	asset, _ := MihomoAsset()
	return fmt.Sprintf("%s/releases/download/%s/%s", mihomoRepo, MIHOMOVersion, asset)
}

type DownloadResult struct {
	OK     bool
	Path   string
	Source string
	Via    string
	Error  string
}

type DownloadOptions struct {
	Mirrors       []string
	ReleaseAPIURL string
	Log           func(format string, args ...any)
	Progress      func(Progress)
}

type Progress struct {
	Running  bool
	Phase    string
	Received int64
	Total    int64
	Percent  float64
	Source   string
	Via      string
	OK       bool
	Error    string
}

func assetDigestSHA256(digest string) (string, bool) {
	if !strings.HasPrefix(digest, "sha256:") {
		return "", false
	}
	hexPart := strings.ToLower(strings.TrimPrefix(digest, "sha256:"))
	if len(hexPart) != 64 {
		return "", false
	}
	if _, err := hex.DecodeString(hexPart); err != nil {
		return "", false
	}
	return hexPart, true
}

func fetchReleaseDigest(ctx context.Context, apiURL, asset string) (string, error) {
	if apiURL == "" {
		apiURL = ReleaseAPIURL()
	}
	client := newDownloadClient()
	client.Timeout = 20 * time.Second
	req := newGet(ctx, apiURL)
	req.Header.Set("Accept", "application/vnd.github+json")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	if resp.ContentLength > releaseAPISizeLimit {
		return "", fmt.Errorf("release 元数据体积超出上限（%d 字节）", releaseAPISizeLimit)
	}
	raw, err := readBounded(resp.Body, releaseAPISizeLimit, "release 元数据")
	if err != nil {
		return "", err
	}
	var release struct {
		Assets []struct {
			Name   string `json:"name"`
			Digest string `json:"digest"`
		} `json:"assets"`
	}
	if err := json.Unmarshal(raw, &release); err != nil {
		return "", err
	}
	for _, entry := range release.Assets {
		if entry.Name != asset {
			continue
		}
		sha, ok := assetDigestSHA256(entry.Digest)
		if !ok {
			return "", fmt.Errorf("release 资产 %s 缺少有效 sha256 digest", asset)
		}
		return sha, nil
	}
	return "", fmt.Errorf("release 缺少当前平台资产 %s", asset)
}

func sha256HexBytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

// DownloadKernel 先取 release sha256 摘要，再逐镜像下载并校验后写盘；OK=false 不视为异常。
func DownloadKernel(ctx context.Context, dataDir string, opts DownloadOptions) DownloadResult {
	if opts.Mirrors == nil {
		opts.Mirrors = DefaultMirrors
	}
	asset, shape := MihomoAsset()
	base := MihomoURL()
	target := KernelExe(dataDir)
	opts.emit(Progress{Running: true})

	digest, err := fetchReleaseDigest(ctx, opts.ReleaseAPIURL, asset)
	if err != nil || digest == "" {
		opts.logf("内核校验锚获取失败：%v", err)
		return failed(opts, kernelVerifyFailedMsg)
	}

	hashMismatch := false
	for _, mirror := range opts.Mirrors {
		url := mirror + base
		source := mirror
		if source == "" {
			source = "github"
		}
		var data []byte
		if shape == "zip" {
			opts.logf("下载 Clash 内核：尝试镜像 %s、通道 直连（%s）", source, url)
			opts.emit(Progress{Running: true, Phase: "连接下载源", Source: source, Via: "直连"})
			data, err = httpGetStreaming(ctx, url, func(received, total int64) {
				opts.emit(Progress{Running: true, Phase: "下载内核", Received: received, Total: total,
					Percent: percent(received, total), Source: source, Via: "直连"})
			})
		} else {
			data, err = httpGetWhole(ctx, url)
		}
		if err == nil {
			if sha256HexBytes(data) != digest {
				hashMismatch = true
				opts.logf("内核哈希与上游 digest 不符，拒绝该下载源：%s", source)
				continue
			}
			var raw []byte
			if shape == "zip" {
				raw, err = exeFromZip(data)
			} else {
				raw, err = kernelFromGz(data)
			}
			if err == nil {
				err = writeKernelAtomic(target, raw)
			}
		}
		if err == nil {
			opts.emit(Progress{OK: true, Percent: 100})
			return DownloadResult{OK: true, Path: target, Source: source, Via: "直连"}
		}
		opts.logf("内核下载失败：镜像 %s 经 直连 未成功，换下一通道（%v）", source, err)
	}
	if hashMismatch {
		return failed(opts, kernelVerifyFailedMsg)
	}
	if shape == "zip" {
		return failed(opts, fmt.Sprintf(
			"所有下载源均失败，可手动放置内核到 data/clash/（文件名 %s）", KernelFilename()))
	}
	return failed(opts, "所有下载源均失败")
}

func failed(opts DownloadOptions, msg string) DownloadResult {
	opts.emit(Progress{Error: msg})
	return DownloadResult{Error: msg}
}

func writeKernelAtomic(target string, raw []byte) (err error) {
	dir := filepath.Dir(target)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".mihomo-*.tmp")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	closed := false
	defer func() {
		if !closed {
			_ = tmp.Close()
		}
		if err != nil {
			_ = os.Remove(tmpPath)
		}
	}()
	if _, err = tmp.Write(raw); err != nil {
		return err
	}
	if !isWindows() {
		if err = tmp.Chmod(0o755); err != nil {
			return err
		}
	} else {
		_ = tmp.Chmod(0o755)
	}
	if err = tmp.Close(); err != nil {
		return err
	}
	closed = true
	if err = os.Rename(tmpPath, target); err != nil {
		return err
	}
	return nil
}

// httpGetStreaming 传输层不设 Proxy（不读环境变量代理）。
// shortcut: 读体无逐块超时，慢速传输靠 ctx 取消兜底。
func httpGetStreaming(ctx context.Context, url string, onChunk func(received, total int64)) ([]byte, error) {
	resp, err := newDownloadClient().Do(newGet(ctx, url))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	if resp.ContentLength > archiveSizeLimit {
		return nil, fmt.Errorf("内核压缩包体积超出上限（%d 字节）", archiveSizeLimit)
	}
	total := resp.ContentLength
	limited := io.LimitReader(resp.Body, archiveSizeLimit+1)
	var buf bytes.Buffer
	chunk := make([]byte, 64*1024)
	for {
		n, rerr := limited.Read(chunk)
		if n > 0 {
			buf.Write(chunk[:n])
			if int64(buf.Len()) > archiveSizeLimit {
				return nil, fmt.Errorf("内核压缩包体积超出上限（%d 字节）", archiveSizeLimit)
			}
			if onChunk != nil {
				onChunk(int64(buf.Len()), total)
			}
		}
		if rerr == io.EOF {
			return buf.Bytes(), nil
		}
		if rerr != nil {
			return nil, rerr
		}
	}
}

func httpGetWhole(ctx context.Context, url string) ([]byte, error) {
	client := newDownloadClient()
	client.Timeout = 120 * time.Second
	resp, err := client.Do(newGet(ctx, url))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	if resp.ContentLength > archiveSizeLimit {
		return nil, fmt.Errorf("内核压缩包体积超出上限（%d 字节）", archiveSizeLimit)
	}
	return readBounded(resp.Body, archiveSizeLimit, "内核压缩包")
}

func newDownloadClient() *http.Client {
	return &http.Client{
		Transport: &http.Transport{
			DialContext:           (&net.Dialer{Timeout: 10 * time.Second}).DialContext,
			TLSHandshakeTimeout:   10 * time.Second,
			ResponseHeaderTimeout: 120 * time.Second,
			IdleConnTimeout:       90 * time.Second,
		},
	}
}

func newGet(ctx context.Context, url string) *http.Request {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		panic(fmt.Sprintf("下载地址构造失败：%v", err))
	}
	return req
}

func exeFromZip(data []byte) ([]byte, error) {
	r, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fmt.Errorf("压缩包解析失败：%w", err)
	}
	for _, f := range r.File {
		if !f.FileInfo().IsDir() && len(f.Name) >= 4 && f.Name[len(f.Name)-4:] == ".exe" {
			if f.UncompressedSize64 > uint64(kernelSizeLimit) {
				return nil, fmt.Errorf("内核可执行文件体积超出上限（%d 字节）", kernelSizeLimit)
			}
			rc, err := f.Open()
			if err != nil {
				return nil, err
			}
			raw, err := readBounded(rc, kernelSizeLimit, "内核可执行文件")
			_ = rc.Close()
			return raw, err
		}
	}
	return nil, fmt.Errorf("压缩包中未找到 exe")
}

func kernelFromGz(data []byte) ([]byte, error) {
	zr, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("gzip 解析失败：%w", err)
	}
	defer zr.Close()
	return readBounded(zr, kernelSizeLimit, "内核可执行文件")
}

func percent(received, total int64) float64 {
	if total <= 0 {
		return 0
	}
	return float64(int64(float64(received)*1000/float64(total))) / 10
}

func (o DownloadOptions) emit(p Progress) {
	if o.Progress != nil {
		o.Progress(p)
	}
}

func (o DownloadOptions) logf(format string, args ...any) {
	if o.Log != nil {
		o.Log(format, args...)
	}
}
