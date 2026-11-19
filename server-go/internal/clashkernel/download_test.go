package clashkernel

import (
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

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

func serveReleaseDigest(t *testing.T, assetName, digest string) string {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		entry := map[string]any{"name": assetName, "size": 10}
		if digest != "" {
			entry["digest"] = digest
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"assets": []any{entry}})
	}))
	t.Cleanup(srv.Close)
	return srv.URL
}

func TestMihomoURLConstruction(t *testing.T) {
	zipBytes := buildZip(t, struct{ name, data string }{name: "mihomo.exe", data: "bin"})
	sum := sha256.Sum256(zipBytes)
	asset, _ := MihomoAsset()
	apiURL := serveReleaseDigest(t, asset, "sha256:"+hex.EncodeToString(sum[:]))

	var gotPath string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		_, _ = w.Write(zipBytes)
	}))
	defer srv.Close()

	res := DownloadKernel(context.Background(), t.TempDir(), DownloadOptions{
		Mirrors:       []string{srv.URL + "/"},
		ReleaseAPIURL: apiURL,
	})
	if !res.OK {
		t.Fatalf("单镜像成功链路应 OK=true，实际 %+v", res)
	}
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
	zipBytes := buildZip(t,
		struct{ name, data string }{name: "README.txt", data: "readme"},
		struct{ name, data string }{name: "mihomo.exe", data: payload})
	sum := sha256.Sum256(zipBytes)
	asset, _ := MihomoAsset()
	apiURL := serveReleaseDigest(t, asset, "sha256:"+hex.EncodeToString(sum[:]))

	srvA := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusInternalServerError)
	}))
	defer srvA.Close()
	srvB := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(zipBytes)
	}))
	defer srvB.Close()

	res := DownloadKernel(context.Background(), dir, DownloadOptions{
		Mirrors:       []string{srvA.URL + "/", srvB.URL + "/"},
		ReleaseAPIURL: apiURL,
	})
	if !res.OK || res.Source != srvB.URL+"/" {
		t.Fatalf("首镜像失败应换道至次镜像： %+v", res)
	}
	raw, err := os.ReadFile(filepath.Join(dir, "clash", KernelFilename()))
	if err != nil || string(raw) != payload {
		t.Fatalf("内核文件未按 zip 内容落盘：err=%v len=%d", err, len(raw))
	}
}

func TestDownloadAllMirrorsFail(t *testing.T) {
	badZip := buildZip(t, struct{ name, data string }{name: "README.txt", data: "no exe here"})
	sum := sha256.Sum256(badZip)
	asset, _ := MihomoAsset()
	apiURL := serveReleaseDigest(t, asset, "sha256:"+hex.EncodeToString(sum[:]))

	var aHits, bHits int
	srvA := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		aHits++
		w.WriteHeader(http.StatusNotFound)
	}))
	defer srvA.Close()
	srvB := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		bHits++
		_, _ = w.Write(badZip)
	}))
	defer srvB.Close()

	res := DownloadKernel(context.Background(), t.TempDir(), DownloadOptions{
		Mirrors:       []string{srvA.URL + "/", srvB.URL + "/"},
		ReleaseAPIURL: apiURL,
	})
	if res.OK {
		t.Fatalf("全部源失败应 OK=false")
	}
	if res.Error != "所有下载源均失败，可手动放置内核到 data/clash/（文件名 holdexar-mihomo.exe）" {
		t.Fatalf("终态文案不符：%q", res.Error)
	}
	if aHits != 1 || bHits != 1 {
		t.Fatalf("校验失败也应逐镜像换道：a=%d b=%d", aHits, bHits)
	}
}

func TestDownloadReleaseDigestVerification(t *testing.T) {
	goodZip := buildZip(t, struct{ name, data string }{name: "mihomo.exe", data: "authentic"})
	goodSum := sha256.Sum256(goodZip)
	goodHex := hex.EncodeToString(goodSum[:])
	tamperedZip := buildZip(t, struct{ name, data string }{name: "mihomo.exe", data: "tampered"})
	asset, _ := MihomoAsset()

	if got, ok := assetDigestSHA256("sha256:" + strings.ToUpper(goodHex)); !ok || got != goodHex {
		t.Fatalf("valid digest failed parse: (%q, %v)", got, ok)
	}
	for _, bad := range []string{"", "md5:" + goodHex, "sha256:abc", "sha256:" + strings.Repeat("z", 64)} {
		if _, ok := assetDigestSHA256(bad); ok {
			t.Fatalf("invalid digest %q accepted", bad)
		}
	}

	apiErr := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer apiErr.Close()
	mirrorHits := 0
	mirrorGood := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		mirrorHits++
		_, _ = w.Write(goodZip)
	}))
	defer mirrorGood.Close()

	for _, tc := range []struct {
		name   string
		apiURL string
	}{
		{"api 502", apiErr.URL},
		{"missing digest", serveReleaseDigest(t, asset, "")},
		{"non-sha256 digest", serveReleaseDigest(t, asset, "md5:"+goodHex)},
		{"wrong asset name", serveReleaseDigest(t, "other.zip", "sha256:"+goodHex)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			res := DownloadKernel(context.Background(), dir, DownloadOptions{
				Mirrors:       []string{mirrorGood.URL + "/"},
				ReleaseAPIURL: tc.apiURL,
			})
			if res.OK || res.Error != kernelVerifyFailedMsg {
				t.Fatalf("%s: res = %+v, want fail-closed verify error", tc.name, res)
			}
			if _, err := os.Stat(KernelExe(dir)); !os.IsNotExist(err) {
				t.Fatalf("%s: kernel binary must not exist on disk", tc.name)
			}
		})
	}
	if mirrorHits != 0 {
		t.Fatalf("mirrors hit %d times even though release API anchor failed", mirrorHits)
	}

	mirrorTampered := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(tamperedZip)
	}))
	defer mirrorTampered.Close()
	apiGood := serveReleaseDigest(t, asset, "sha256:"+goodHex)

	dir := t.TempDir()
	res := DownloadKernel(context.Background(), dir, DownloadOptions{
		Mirrors:       []string{mirrorTampered.URL + "/"},
		ReleaseAPIURL: apiGood,
	})
	if res.OK || res.Error != kernelVerifyFailedMsg {
		t.Fatalf("tampered mirror res = %+v, want %q", res, kernelVerifyFailedMsg)
	}
	if _, err := os.Stat(KernelExe(dir)); !os.IsNotExist(err) {
		t.Fatalf("tampered binary must not be written to disk")
	}

	dir2 := t.TempDir()
	res = DownloadKernel(context.Background(), dir2, DownloadOptions{
		Mirrors:       []string{mirrorTampered.URL + "/", mirrorGood.URL + "/"},
		ReleaseAPIURL: apiGood,
	})
	if !res.OK || res.Source != mirrorGood.URL+"/" {
		t.Fatalf("expected fallback from tampered mirror to good mirror, got %+v", res)
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

func TestDownloadSizeCapsAndAtomicOverwrite(t *testing.T) {
	origAPI, origArchive, origKernel := releaseAPISizeLimit, archiveSizeLimit, kernelSizeLimit
	t.Cleanup(func() {
		releaseAPISizeLimit = origAPI
		archiveSizeLimit = origArchive
		kernelSizeLimit = origKernel
	})

	// 1. Oversize archive rejected in both streaming and whole download helpers, and via DownloadKernel.
	archiveSizeLimit = 1024
	oversizeSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(bytes.Repeat([]byte("a"), 2048))
	}))
	defer oversizeSrv.Close()

	if _, err := httpGetStreaming(context.Background(), oversizeSrv.URL, nil); err == nil || !strings.Contains(err.Error(), "超出上限") {
		t.Fatalf("httpGetStreaming oversize expected error, got %v", err)
	}
	if _, err := httpGetWhole(context.Background(), oversizeSrv.URL); err == nil || !strings.Contains(err.Error(), "超出上限") {
		t.Fatalf("httpGetWhole oversize expected error, got %v", err)
	}

	bigZip := buildZip(t, struct{ name, data string }{name: "mihomo.exe", data: "compressible-content"})
	archiveSizeLimit = int64(len(bigZip) - 1)
	bigSum := sha256.Sum256(bigZip)
	asset, _ := MihomoAsset()
	apiBig := serveReleaseDigest(t, asset, "sha256:"+hex.EncodeToString(bigSum[:]))
	zipSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/octet-stream")
		_, _ = w.Write(bigZip)
	}))
	defer zipSrv.Close()

	dir := t.TempDir()
	res := DownloadKernel(context.Background(), dir, DownloadOptions{
		Mirrors:       []string{zipSrv.URL + "/"},
		ReleaseAPIURL: apiBig,
	})
	if res.OK {
		t.Fatal("oversize archive must be rejected by DownloadKernel")
	}

	// 2. Decompressed kernel size cap (zip bomb / gz bomb defense).
	archiveSizeLimit = origArchive
	kernelSizeLimit = 64
	bombZip := buildZip(t, struct{ name, data string }{name: "mihomo.exe", data: strings.Repeat("K", 256)})
	if _, err := exeFromZip(bombZip); err == nil || !strings.Contains(err.Error(), "超出上限") {
		t.Fatalf("exeFromZip oversize expected error, got %v", err)
	}
	var gzBuf bytes.Buffer
	gzW := gzip.NewWriter(&gzBuf)
	_, _ = gzW.Write(bytes.Repeat([]byte("G"), 256))
	_ = gzW.Close()
	if _, err := kernelFromGz(gzBuf.Bytes()); err == nil || !strings.Contains(err.Error(), "超出上限") {
		t.Fatalf("kernelFromGz oversize expected error, got %v", err)
	}

	// 3. Atomic temp-file write replaces existing binary and leaves zero .tmp files on success or failure.
	kernelSizeLimit = origKernel
	target := KernelExe(dir)
	if err := os.WriteFile(target, []byte("old-binary"), 0o755); err != nil {
		t.Fatal(err)
	}

	validZip := buildZip(t, struct{ name, data string }{name: "mihomo.exe", data: "new-atomic-binary"})
	validSum := sha256.Sum256(validZip)
	apiValid := serveReleaseDigest(t, asset, "sha256:"+hex.EncodeToString(validSum[:]))
	validSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		_, _ = w.Write(validZip)
	}))
	defer validSrv.Close()

	res = DownloadKernel(context.Background(), dir, DownloadOptions{
		Mirrors:       []string{validSrv.URL + "/"},
		ReleaseAPIURL: apiValid,
	})
	if !res.OK {
		t.Fatalf("atomic overwrite DownloadKernel failed: %+v", res)
	}
	gotBytes, err := os.ReadFile(target)
	if err != nil || string(gotBytes) != "new-atomic-binary" {
		t.Fatalf("atomic overwrite content=%q err=%v", gotBytes, err)
	}
	entries, err := os.ReadDir(filepath.Dir(target))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".mihomo-") {
			t.Fatalf("leftover temp file after success: %s", entry.Name())
		}
	}

	// Force rename failure by making target path a non-empty directory and verify temp cleanup.
	failDir := t.TempDir()
	blockedTarget := filepath.Join(failDir, "clash", KernelFilename())
	if err := os.MkdirAll(filepath.Join(blockedTarget, "nested"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeKernelAtomic(blockedTarget, []byte("payload")); err == nil {
		t.Fatal("expected writeKernelAtomic error when target is a non-empty directory")
	}
	failEntries, err := os.ReadDir(filepath.Join(failDir, "clash"))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range failEntries {
		if strings.HasPrefix(entry.Name(), ".mihomo-") {
			t.Fatalf("leftover temp file after failed rename: %s", entry.Name())
		}
	}
}
