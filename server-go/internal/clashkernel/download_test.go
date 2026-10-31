package clashkernel

import (
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// 测试用本地 httptest 假镜像链，不触真实 GitHub。

func buildZip(t *testing.T, entries ...struct{ name, data string }) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for _, e := range entries {
		w, err := zw.Create(e.name)
		if err != nil {
			t.Fatalf("zip 条目构造失败：%v", err)
		}
		if _, err := w.Write([]byte(e.data)); err != nil {
			t.Fatalf("zip 条目写入失败：%v", err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatalf("zip 收尾失败：%v", err)
	}
	return buf.Bytes()
}

func TestMihomoURLConstruction(t *testing.T) {
	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = w.Write(buildZip(t, struct{ name, data string }{name: "mihomo.exe", data: "bin"}))
	}))
	defer srv.Close()

	res := DownloadKernel(context.Background(), t.TempDir(), DownloadOptions{Mirrors: []string{srv.URL + "/"}})
	if !res.OK {
		t.Fatalf("单镜像成功链路应 OK=true，实际 %+v", res)
	}
	asset, _ := MihomoAsset()
	// 镜像代理形状：完整 GitHub 地址作为请求路径后缀（ghfast.top 等镜像同款）
	want := "/" + MihomoURL()
	if gotPath != want || !strings.Contains(gotPath, "/releases/download/"+MIHOMOVersion+"/"+asset) {
		t.Fatalf("发布资产地址不符：got %s want %s", gotPath, want)
	}
	if res.Source != srv.URL+"/" || res.Via != "直连" {
		t.Fatalf("命中来源/通道不符：%+v", res)
	}
}

func TestDownloadSwitchesMirrorOnFailure(t *testing.T) {
	dir := t.TempDir()
	payload := "mihomo-binary"
	srvA := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srvA.Close()
	srvB := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(buildZip(t,
			struct{ name, data string }{name: "README.txt", data: "readme"},
			struct{ name, data string }{name: "mihomo.exe", data: payload}))
	}))
	defer srvB.Close()

	res := DownloadKernel(context.Background(), dir, DownloadOptions{Mirrors: []string{srvA.URL + "/", srvB.URL + "/"}})
	if !res.OK || res.Source != srvB.URL+"/" {
		t.Fatalf("首镜像失败应换道至次镜像： %+v", res)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "clash", KernelFilename()))
	if err != nil || string(raw) != payload {
		t.Fatalf("内核文件未按 zip 内容落盘：err=%v len=%d", err, len(raw))
	}
}

func TestDownloadAllMirrorsFail(t *testing.T) {
	var aHits, bHits int
	srvA := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		aHits++
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srvA.Close()
	srvB := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		bHits++
		_, _ = w.Write(buildZip(t, struct{ name, data string }{name: "README.txt", data: "no exe here"}))
	}))
	defer srvB.Close()

	res := DownloadKernel(context.Background(), t.TempDir(), DownloadOptions{Mirrors: []string{srvA.URL + "/", srvB.URL + "/"}})
	if res.OK {
		t.Fatalf("全部源失败应 OK=false")
	}
	if res.Error != "所有下载源均失败，可手动放置内核到 data/clash/" {
		t.Fatalf("终态文案不符：%q", res.Error)
	}
	if aHits != 1 || bHits != 1 {
		t.Fatalf("校验失败也应逐镜像换道：a=%d b=%d", aHits, bHits)
	}
}

func TestExeFromZip(t *testing.T) {
	raw, err := exeFromZip(buildZip(t,
		struct{ name, data string }{name: "docs/readme.txt", data: "x"},
		struct{ name, data string }{name: "dist/mihomo.exe", data: "bin"}))
	if err != nil || string(raw) != "bin" {
		t.Fatalf("应取第一个 .exe 条目：err=%v", err)
	}
	if _, err := exeFromZip(buildZip(t, struct{ name, data string }{name: "readme.txt", data: "x"})); err == nil ||
		err.Error() != "压缩包中未找到 exe" {
		t.Fatalf("缺 exe 校验文案不符：%v", err)
	}
	if _, err := exeFromZip([]byte("not a zip")); err == nil || !strings.Contains(err.Error(), "压缩包解析失败") {
		t.Fatalf("非 zip 输入应报解析失败：%v", err)
	}
}

func TestKernelFromGz(t *testing.T) {
	var buf bytes.Buffer
	zw := gzip.NewWriter(&buf)
	if _, err := zw.Write([]byte("kernel-bytes")); err != nil {
		t.Fatalf("gzip 构造失败：%v", err)
	}
	_ = zw.Close()
	raw, err := kernelFromGz(buf.Bytes())
	if err != nil || string(raw) != "kernel-bytes" {
		t.Fatalf("gz 解压不符：err=%v raw=%q", err, raw)
	}
	if _, err := kernelFromGz([]byte("not gzip")); err == nil || !strings.Contains(err.Error(), "gzip 解析失败") {
		t.Fatalf("非 gzip 输入应报解析失败：%v", err)
	}
}
