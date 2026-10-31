// 内核网络下载兜底：随包资产缺位（源码 clone 未补资产、用户手工删除 exe）时
// 才走本链。镜像优先、直连兜底；通道恒为直连——下载触发时内核必然没在跑，
// 无经代理通道可用（对齐现网 download_kernel* 的通道构造）。
package clashkernel

import (
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"time"
)

// 内核来源（对齐 kernel_release.py）：镜像前缀拼在 GitHub 发布地址之前，
// 空串即直连 GitHub。
const mihomoRepo = "https://github.com/MetaCubeX/mihomo"

// DefaultMirrors 内置镜像链：国内直连 GitHub 发布资产基本不可用，镜像优先、
// 直连兜底（末位空串）。改链只改这里。
var DefaultMirrors = []string{"https://ghfast.top/", "https://gh-proxy.com/", ""}

// MihomoAsset 当前平台的 mihomo 发布资产：返回 (资产名, 打包形状)。
// Windows=zip（内含 exe）、Linux=gz 单文件；上游仅发 amd64-compatible。
func MihomoAsset() (asset, shape string) {
	if isWindows() {
		return fmt.Sprintf("mihomo-windows-amd64-compatible-%s.zip", MIHOMOVersion), "zip"
	}
	return fmt.Sprintf("mihomo-linux-amd64-compatible-%s.gz", MIHOMOVersion), "gz"
}

// MihomoURL 当前平台发布资产完整地址（随包与网络下载共用同一版本源）。
func MihomoURL() string {
	asset, _ := MihomoAsset()
	return fmt.Sprintf("%s/releases/download/%s/%s", mihomoRepo, MIHOMOVersion, asset)
}

// DownloadResult 下载结果（对齐 download_kernel 返回形状）。
type DownloadResult struct {
	OK     bool
	Path   string
	Source string // 命中的镜像（直连 GitHub 时为 "github"）
	Via    string // 命中的通道标签（下载链恒为「直连」）
	Error  string
}

// DownloadOptions 注入项。Mirrors nil=内置链（测试注入本地桩地址）；Log/Progress
// nil 丢弃；Progress 在下载 goroutine 内同步调用，应快速返回。
type DownloadOptions struct {
	Mirrors  []string
	Log      func(format string, args ...any)
	Progress func(Progress)
}

// Progress 下载进度快照。Total/Percent 零值=未知（响应缺 Content-Length）。
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

// DownloadKernel 下载内核到 data/clash/（镜像优先，逐镜像过直连通道）。
// 返回 OK=false 不视为程序错误——调用方据此提示用户手工放置内核。
func DownloadKernel(ctx context.Context, dataDir string, opts DownloadOptions) DownloadResult {
	if opts.Mirrors == nil {
		opts.Mirrors = DefaultMirrors
	}
	base := MihomoURL()
	if isWindows() {
		return downloadKernelZip(ctx, dataDir, base, opts)
	}
	return downloadKernelGz(ctx, dataDir, base, opts)
}

func downloadKernelZip(ctx context.Context, dataDir, base string, opts DownloadOptions) DownloadResult {
	target := KernelExe(dataDir)
	opts.emit(Progress{Running: true})
	for _, mirror := range opts.Mirrors {
		url := mirror + base
		source := mirror
		if source == "" {
			source = "github"
		}
		opts.logf("下载 Clash 内核：尝试镜像 %s、通道 直连（%s）", source, url)
		opts.emit(Progress{Running: true, Phase: "连接下载源", Source: source, Via: "直连"})
		data, err := httpGetStreaming(ctx, url, func(received, total int64) {
			opts.emit(Progress{Running: true, Phase: "下载内核", Received: received, Total: total,
				Percent: percent(received, total), Source: source, Via: "直连"})
		})
		if err == nil {
			var exe []byte
			exe, err = exeFromZip(data)
			if err == nil {
				err = os.WriteFile(target, exe, 0o755)
			}
		}
		if err == nil {
			opts.emit(Progress{OK: true, Percent: 100})
			return DownloadResult{OK: true, Path: target, Source: source, Via: "直连"}
		}
		opts.logf("内核下载失败：镜像 %s 经 直连 未成功，换下一通道（%v）", source, err)
	}
	return failed(opts, "所有下载源均失败，可手动放置内核到 data/clash/")
}

func downloadKernelGz(ctx context.Context, dataDir, base string, opts DownloadOptions) DownloadResult {
	target := KernelExe(dataDir)
	opts.emit(Progress{Running: true})
	for _, mirror := range opts.Mirrors {
		url := mirror + base
		source := mirror
		if source == "" {
			source = "github"
		}
		data, err := httpGetWhole(ctx, url)
		if err == nil {
			var raw []byte
			raw, err = kernelFromGz(data)
			if err == nil {
				err = os.WriteFile(target, raw, 0o755)
				if err == nil && !isWindows() {
					_ = os.Chmod(target, 0o755)
				}
			}
		}
		if err == nil {
			opts.emit(Progress{OK: true, Percent: 100})
			return DownloadResult{OK: true, Path: target, Source: source, Via: "直连"}
		}
		opts.logf("镜像下载内核失败，换下一个镜像（%v）", err)
	}
	return failed(opts, "所有下载源均失败")
}

func failed(opts DownloadOptions, msg string) DownloadResult {
	opts.emit(Progress{Error: msg})
	return DownloadResult{Error: msg}
}

// httpGetStreaming 流式下载：连接超时收紧到 10s（被墙通道快速轮转），响应头
// 等待 120s 容慢速源站；传输层不设 Proxy（也不读环境变量代理）。shortcut:
// 读体无逐块超时（net/http 无直连口），慢速传输靠 ctx 取消兜底。
func httpGetStreaming(ctx context.Context, url string, onChunk func(received, total int64)) ([]byte, error) {
	resp, err := newDownloadClient().Do(newGet(ctx, url))
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return nil, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	total := resp.ContentLength
	var buf bytes.Buffer
	chunk := make([]byte, 64*1024)
	for {
		n, rerr := resp.Body.Read(chunk)
		if n > 0 {
			buf.Write(chunk[:n])
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

// httpGetWhole 一次性下载（gz 分支同现网：整包 120s 总超时）。
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
	return io.ReadAll(resp.Body)
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
		// URL 由本包常量拼接，构造失败只可能来自注入的非法镜像串
		panic(fmt.Sprintf("下载地址构造失败：%v", err))
	}
	return req
}

// exeFromZip zip 内第一个 .exe 条目的字节（形状校验：找不到即无效下载）。
func exeFromZip(data []byte) ([]byte, error) {
	r, err := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if err != nil {
		return nil, fmt.Errorf("压缩包解析失败：%w", err)
	}
	for _, f := range r.File {
		if !f.FileInfo().IsDir() && len(f.Name) >= 4 && f.Name[len(f.Name)-4:] == ".exe" {
			rc, err := f.Open()
			if err != nil {
				return nil, err
			}
			raw, err := io.ReadAll(rc)
			_ = rc.Close()
			return raw, err
		}
	}
	return nil, fmt.Errorf("压缩包中未找到 exe")
}

// kernelFromGz gz 单文件解压（Linux 分支形状）。
func kernelFromGz(data []byte) ([]byte, error) {
	zr, err := gzip.NewReader(bytes.NewReader(data))
	if err != nil {
		return nil, fmt.Errorf("gzip 解析失败：%w", err)
	}
	defer zr.Close()
	return io.ReadAll(zr)
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
