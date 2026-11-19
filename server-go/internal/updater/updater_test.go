package updater

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"
	"unicode/utf8"
)

func TestVersionGTSemantics(t *testing.T) {
	cases := []struct {
		a, b string
		want bool
	}{
		{"0.3.9", "0.3.8", true},
		{"0.3.8", "0.3.9", false},
		{"0.3.10", "0.3.9", true},
		{"0.4.0", "0.3.99", true},
		{"0.3.8", "0.3.8", false},
		{"1.0.0-beta", "1.0.0-alpha", true},
		{"1.0.0", "1.0.0-beta", true},
		{"0.3.8-w5", "0.3.7-w4", true},
	}
	for _, tc := range cases {
		if got := versionGT(tc.a, tc.b); got != tc.want {
			t.Fatalf("versionGT(%q,%q)=%v 期望 %v", tc.a, tc.b, got, tc.want)
		}
	}
}

func TestManifestSignatureRoundTrip(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	const keyID = "test"
	backup := trustedPubkeys[keyID]
	trustedPubkeys[keyID] = hex.EncodeToString(pub)
	defer func() {
		if backup == "" {
			delete(trustedPubkeys, keyID)
		} else {
			trustedPubkeys[keyID] = backup
		}
	}()

	manifest := map[string]any{
		"version":     "0.3.9",
		"tag":         "v0.3.9",
		"notes":       "测试说明",
		"publishedAt": "2026-11-11T00:00:00Z",
		"assets": []any{
			map[string]any{
				"name":   "Holdexar-win64-v0.3.9.zip",
				"role":   "app",
				"sha256": strings.Repeat("ab", 32),
				"size":   json.Number("35441043"),
			},
		},
	}
	payload, err := canonicalManifestBytes(manifest)
	if err != nil {
		t.Fatal(err)
	}
	signature := ed25519.Sign(priv, payload)
	manifest["signature"] = map[string]any{
		"alg":   "ed25519",
		"keyId": keyID,
		"sig":   base64.StdEncoding.EncodeToString(signature),
	}
	if ok, reason := verifyManifest(manifest); !ok {
		t.Fatalf("合法签名被拒：%s", reason)
	}
	manifest["version"] = "9.9.9"
	if ok, _ := verifyManifest(manifest); ok {
		t.Fatal("篡改后的清单通过验签")
	}
	if ok, reason := verifyManifest(map[string]any{"version": "0.3.9"}); ok {
		t.Fatalf("无签名清单通过验签：%s", reason)
	}
	manifest["version"] = "0.3.9"
	manifest["signature"] = map[string]any{"alg": "ed25519", "keyId": "unknown", "sig": "AAA"}
	if ok, _ := verifyManifest(manifest); ok {
		t.Fatal("信任表外公钥通过验签")
	}
}

func TestPickAssetAndPlainNotes(t *testing.T) {
	assets := []any{
		map[string]any{"name": "latest.json", "size": json.Number("900")},
		map[string]any{"name": "Holdexar-win64-v0.3.9.zip", "sha256": "x", "size": json.Number("35441043")},
	}
	asset := pickAsset(assets, "")
	if asset == nil || stringOf(asset["name"]) != "Holdexar-win64-v0.3.9.zip" {
		t.Fatalf("命名模式未命中：%v", asset)
	}
	if asset = pickAsset(assets, "latest.json"); asset == nil {
		t.Fatal("显式名未命中")
	}
	notes := plainNotes("# 标题\n\n**加粗** 与 [链接](https://x)\n\n```go\ncode()\n```")
	if strings.Contains(notes, "#") || strings.Contains(notes, "**") ||
		strings.Contains(notes, "https://x") || strings.Contains(notes, "code()") {
		t.Fatalf("markdown 噪音未清理：%q", notes)
	}
}

func TestTotalFromHeaders(t *testing.T) {
	build := func(headers map[string]string) http.Header {
		header := http.Header{}
		for key, value := range headers {
			header.Set(key, value)
		}
		return header
	}
	if got := totalFromHeaders(build(map[string]string{"Content-Range": "bytes 1/35441043"}), 206); got != 35441043 {
		t.Fatalf("206 总量解析错误：%d", got)
	}
	if got := totalFromHeaders(build(map[string]string{"Content-Length": "35441043"}), 200); got != 35441043 {
		t.Fatalf("200 总量解析错误：%d", got)
	}
	if got := totalFromHeaders(build(map[string]string{"Content-Length": "1"}), 206); got != 0 {
		t.Fatalf("206 不应读 content-length：%d", got)
	}
}

func TestExtractZipRejectsTraversal(t *testing.T) {
	root := t.TempDir()
	zipPath := filepath.Join(root, "bad.zip")
	archive, err := os.Create(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	writer := newZipWriter(archive)
	if err := writer.add("../evil.txt", []byte("x")); err != nil {
		t.Fatal(err)
	}
	writer.close()
	archive.Close()
	staging := filepath.Join(root, "staging")
	if err := os.MkdirAll(staging, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := extractZip(zipPath, staging); err == nil {
		t.Fatal("越界条目未被拒绝")
	}
	if _, err := os.Stat(filepath.Join(root, "evil.txt")); err == nil {
		t.Fatal("越界文件被写出")
	}
}

func TestPendingAndAcknowledgeProjection(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "state"), 0o755); err != nil {
		t.Fatal(err)
	}
	service := &Service{InstallRoot: root, DataDir: filepath.Join(root, "data"), Version: "0.3.9"}
	_ = os.MkdirAll(service.DataDir, 0o755)
	if err := os.WriteFile(filepath.Join(root, "state", "current.json"),
		[]byte(`{"version":"0.3.9","lastKnownGood":"0.3.8"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	status := service.PendingStatus()
	if status["pending"] != false || status["swapFailed"] != false {
		t.Fatalf("空现场投影错误：%v", status)
	}
	if service.JustUpdated() != nil {
		t.Fatal("空现场不应有 justUpdated")
	}
	if err := os.WriteFile(filepath.Join(service.DataDir, SwapFailedFlag), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if status = service.PendingStatus(); status["swapFailed"] != true {
		t.Fatal("swap-failed 未投影")
	}
	_ = os.Remove(filepath.Join(service.DataDir, SwapFailedFlag))
	if err := os.WriteFile(filepath.Join(root, "state", "transaction.json"),
		[]byte(`{"attemptId":"a1","stage":"committed","targetVersion":"0.3.9","currentVersion":"0.3.9","sourceVersion":"0.3.8","dataAccepted":true}`), 0o644); err != nil {
		t.Fatal(err)
	}
	logs := filepath.Join(root, "logs")
	if err := os.MkdirAll(logs, 0o755); err != nil {
		t.Fatal(err)
	}
	diagnosis := `{"attemptId":"a1","targetVersion":"0.3.9","sourceVersion":"0.3.8","stage":"committed","success":true,"notes":"成功说明"}`
	if err := os.WriteFile(filepath.Join(logs, "update-diagnosis.json"), []byte(diagnosis), 0o644); err != nil {
		t.Fatal(err)
	}
	just := service.JustUpdated()
	if just == nil || just["previousVersion"] != "0.3.8" || just["notes"] != "成功说明" {
		t.Fatalf("justUpdated 投影错误：%v", just)
	}
	if result := service.AcknowledgeUpdate(); result["acknowledged"] != true || result["updated"] != true {
		t.Fatalf("确认闭合失败：%v", result)
	}
	if service.JustUpdated() != nil {
		t.Fatal("确认后仍投影 justUpdated")
	}
}

func TestCheckUpdateManifestPath(t *testing.T) {
	priv := installTestTrustKey(t, "test")
	manifest := map[string]any{
		"schema":  json.Number("2"),
		"app":     AppName,
		"version": "0.4.0",
		"tag":     "v0.4.0",
		"assets": []any{map[string]any{
			"name": "Holdexar-win64-v0.4.0.zip", "role": "app",
			"sha256": strings.Repeat("cd", 32), "size": json.Number("35441043"),
			"files": map[string]any{"Holdexar.exe": strings.Repeat("12", 32)},
		}},
	}
	payload, _ := canonicalManifestBytes(manifest)
	manifest["signature"] = map[string]any{"alg": "ed25519", "keyId": "test",
		"sig": base64.StdEncoding.EncodeToString(ed25519.Sign(priv, payload))}
	raw, _ := json.Marshal(manifest)
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/latest.json" {
			w.WriteHeader(http.StatusNotFound)
			return
		}
		_, _ = w.Write(raw)
	}))
	defer server.Close()
	t.Setenv("HOLDEXAR_UPDATE_URL", server.URL)
	service := &Service{Version: "0.3.9"}
	result := service.CheckUpdate(nil)
	if result["available"] != true || result["latest"] != "0.4.0" || result["source"] != "manifest" {
		t.Fatalf("清单检查投影错误：%v", result)
	}
	if result["sizeBytes"] != 35441043 {
		t.Fatalf("大整数体积未保真：%v (%T)", result["sizeBytes"], result["sizeBytes"])
	}
	if result["sha256"] != strings.Repeat("cd", 32) || result["asset"] != "Holdexar-win64-v0.4.0.zip" {
		t.Fatalf("校验值/资产名投影错误：%v", result)
	}
}

func TestLoopbackAndNonLoopbackEnvUpdateURLSecurity(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte(`{"schema":2,"app":"Holdexar","version":"0.9.0","tag":"v0.9.0","assets":[{"role":"app","name":"Holdexar-win64-v0.9.0.zip","sha256":"` + strings.Repeat("a", 64) + `","size":100,"files":{"Holdexar.exe":"` + strings.Repeat("b", 64) + `"}}]}`))
	}))
	defer server.Close()

	t.Setenv("HOLDEXAR_UPDATE_URL", server.URL)
	if got := fetchManifest(server.Client()); got != nil {
		t.Fatalf("expected unsigned loopback manifest to be rejected, got: %v", got)
	}

	t.Setenv("HOLDEXAR_UPDATE_URL", "http://evil.example.com:9999")
	if got := UpdateURLFromEnv(); got != "" {
		t.Fatalf("expected non-loopback env url to be ignored, got: %q", got)
	}
	svc := &Service{Version: "0.3.9"}
	if assetURL, isLocal := svc.resolveAssetURL("v0.4.0", "Holdexar-win64-v0.4.0.zip"); isLocal || !strings.Contains(assetURL, "github.com") {
		t.Fatalf("expected github.com asset url when env non-loopback, got (%q, %v)", assetURL, isLocal)
	}
}

func TestUpdateDownloadRouteParameterValidation(t *testing.T) {
	mux := http.NewServeMux()
	Register(mux, Deps{DataDir: t.TempDir(), InstallRoot: t.TempDir(), Version: "0.3.9"})

	for _, tc := range []struct {
		name       string
		body       string
		wantDetail string
	}{
		{"invalid json", "{", "请求体无效"},
		{"empty tag", `{"tag":""}`, "更新标识包含非法字符"},
		{"dot tag", `{"tag":"."}`, "更新标识包含非法字符"},
		{"dotdot tag", `{"tag":".."}`, "更新标识包含非法字符"},
		{"traversal tag", `{"tag":"../v0.4.0"}`, "更新标识包含非法字符"},
		{"query injection tag", `{"tag":"v0.4.0?x=1"}`, "更新标识包含非法字符"},
		{"slash in asset", `{"tag":"v0.4.0","asset":"dir/Holdexar.zip"}`, "更新标识包含非法字符"},
		{"traversal asset", `{"tag":"v0.4.0","asset":".."}`, "更新标识包含非法字符"},
		{"bad sha256", `{"tag":"v0.4.0","sha256":"not-a-sha256"}`, "校验值格式无效"},
		{"whitespace sha256", `{"tag":"v0.4.0","sha256":"   "}`, "校验值格式无效"},
		{"same version", `{"tag":"v0.3.9"}`, "目标版本不高于当前版本"},
		{"downgrade version", `{"tag":"v0.3.8"}`, "目标版本不高于当前版本"},
		{"non-digit v tag", `{"tag":"vbeta"}`, "目标版本不高于当前版本"},
		{"double v tag", `{"tag":"vv0.4.0"}`, "目标版本不高于当前版本"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest(http.MethodPost, "/api/v1/system/update-download", strings.NewReader(tc.body))
			rec := httptest.NewRecorder()
			mux.ServeHTTP(rec, req)
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400; body=%s", rec.Code, rec.Body.String())
			}
			var payload map[string]string
			_ = json.Unmarshal(rec.Body.Bytes(), &payload)
			if payload["detail"] != tc.wantDetail {
				t.Fatalf("detail = %q, want %q", payload["detail"], tc.wantDetail)
			}
		})
	}
}

func TestServiceDownloadEmbedsSignedReleaseManifest(t *testing.T) {
	priv := installTestTrustKey(t, "dl-k")
	oldMin := minPackageBytes
	minPackageBytes = 0
	t.Cleanup(func() { minPackageBytes = oldMin })

	var zipBuf bytes.Buffer
	zw := newZipWriter(&zipBuf)
	exeBytes := []byte("MZ-fake-payload")
	if err := zw.add("Holdexar/Holdexar.exe", exeBytes); err != nil {
		t.Fatal(err)
	}
	zw.close()
	zipBytes := zipBuf.Bytes()
	zipHash := sha256Hex(zipBytes)
	assetName := "Holdexar-win64-v0.4.0.zip"

	release := signReleaseManifestForTest(t, priv, "dl-k", map[string]any{
		"schema":  json.Number("2"),
		"app":     AppName,
		"version": "0.4.0",
		"tag":     "v0.4.0",
		"notes":   "signed release notes",
		"assets": []any{map[string]any{
			"role":   "app",
			"name":   assetName,
			"size":   json.Number(jsonNumberFromInt(len(zipBytes))),
			"sha256": zipHash,
			"files":  map[string]any{"Holdexar.exe": sha256Hex(exeBytes)},
		}},
	})
	releaseRaw, _ := json.Marshal(release)

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/latest.json":
			_, _ = w.Write(releaseRaw)
		case "/" + assetName:
			if r.Header.Get("Range") == "bytes=0-0" {
				w.Header().Set("Content-Range", "bytes 0-0/"+jsonNumberFromInt(len(zipBytes)))
				w.WriteHeader(http.StatusPartialContent)
				_, _ = w.Write(zipBytes[:1])
				return
			}
			w.Header().Set("Content-Length", jsonNumberFromInt(len(zipBytes)))
			_, _ = w.Write(zipBytes)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	t.Setenv("HOLDEXAR_UPDATE_URL", server.URL)

	root := t.TempDir()
	dataDir := filepath.Join(root, "data")
	svc := &Service{
		InstallRoot: root,
		DataDir:     dataDir,
		Version:     "0.3.9",
		HTTPClient:  server.Client(),
	}

	if res := svc.Download(context.Background(), "v0.4.1", "", "", 0, ""); res["ok"] != false {
		t.Fatalf("expected tag mismatch rejection, got %v", res)
	}
	if res := svc.Download(context.Background(), "v0.4.0", strings.Repeat("0", 64), "", 0, ""); res["ok"] != false {
		t.Fatalf("expected sha mismatch rejection, got %v", res)
	}

	res := svc.Download(context.Background(), "v0.4.0", "", "", 0, "spoofed notes")
	if res["ok"] != true {
		t.Fatalf("Download failed: %v", res)
	}

	staging := svc.stagingDir()
	if err := VerifyStagedTrust(staging, dataDir, "0.4.0"); err != nil {
		t.Fatalf("VerifyStagedTrust failed after Download: %v", err)
	}
	if _, err := os.Stat(filepath.Join(dataDir, "update-trust.json")); !os.IsNotExist(err) {
		t.Fatalf("update-trust.json should no longer be written")
	}
	rawManifest, err := os.ReadFile(filepath.Join(staging, StagingManifest))
	if err != nil {
		t.Fatal(err)
	}
	var staged map[string]any
	if err := json.Unmarshal(rawManifest, &staged); err != nil {
		t.Fatal(err)
	}
	if staged["notes"] != "signed release notes" {
		t.Fatalf("notes = %v, want signed manifest notes", staged["notes"])
	}
}

func jsonNumberFromInt(n int) string {
	return json.Number(strings.TrimSpace(
		func() string {
			b, _ := json.Marshal(n)
			return string(b)
		}(),
	)).String()
}

func TestVersionFromTagAndUTF8Clipping(t *testing.T) {
	for _, tc := range []struct {
		in, want string
	}{
		{"v0.4.0", "0.4.0"},
		{"V1.2.3-beta", "1.2.3-beta"},
		{"0.4.0", "0.4.0"},
		{"vbeta", "vbeta"},
		{"Vrelease", "Vrelease"},
		{"vv1.0.0", "vv1.0.0"},
		{"v", "v"},
		{"", ""},
	} {
		if got := versionFromTag(tc.in); got != tc.want {
			t.Fatalf("versionFromTag(%q)=%q, want %q", tc.in, got, tc.want)
		}
	}

	clipped := clip("中文错误说明不会被截出半个UTF8字节", 6)
	if !utf8.ValidString(clipped) || clipped != "中文错误说明" {
		t.Fatalf("clip UTF-8 结果异常：%q", clipped)
	}
	longCN := strings.Repeat("更", notesLimit+25)
	notes := plainNotes(longCN)
	if !utf8.ValidString(notes) || utf8.RuneCountInString(notes) != notesLimit {
		t.Fatalf("plainNotes UTF-8 截断异常：valid=%v runes=%d", utf8.ValidString(notes), utf8.RuneCountInString(notes))
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func TestCheckViaAPIComputesVersionAvailability(t *testing.T) {
	body := `{"tag_name":"v0.3.9","body":"更新说明","published_at":"2026-11-11T00:00:00Z","assets":[{"name":"Holdexar-win64-v0.3.9.zip","size":1048576,"digest":"sha256:` + strings.Repeat("ab", 32) + `"}]}`
	client := &http.Client{Transport: roundTripFunc(func(r *http.Request) (*http.Response, error) {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(body)),
		}, nil
	})}

	if got := checkViaAPI(client, "0.3.8"); got["available"] != true || got["current"] != "0.3.8" {
		t.Fatalf("expected newer version available, got %v", got)
	}
	if got := checkViaAPI(client, "0.3.9"); got["available"] != false {
		t.Fatalf("expected same version unavailable, got %v", got)
	}
	if got := checkViaAPI(client, "0.4.0"); got["available"] != false {
		t.Fatalf("expected downgrade unavailable, got %v", got)
	}
}

func TestDownloadHTTP206ResumePreservesPartAndRejectsDowngrade(t *testing.T) {
	priv := installTestTrustKey(t, "resume-k")
	oldMin := minPackageBytes
	minPackageBytes = 0
	t.Cleanup(func() { minPackageBytes = oldMin })

	var zipBuf bytes.Buffer
	zw := newZipWriter(&zipBuf)
	exeBytes := []byte(strings.Repeat("MZ-resume-payload-", 64))
	if err := zw.add("Holdexar/Holdexar.exe", exeBytes); err != nil {
		t.Fatal(err)
	}
	_ = zw.close()
	zipBytes := zipBuf.Bytes()
	zipHash := sha256Hex(zipBytes)
	assetName := "Holdexar-win64-v0.4.0.zip"
	split := len(zipBytes) / 2

	release := signReleaseManifestForTest(t, priv, "resume-k", map[string]any{
		"schema":  json.Number("2"),
		"app":     AppName,
		"version": "0.4.0",
		"tag":     "v0.4.0",
		"notes":   "续传说明",
		"assets": []any{map[string]any{
			"role":   "app",
			"name":   assetName,
			"size":   json.Number(jsonNumberFromInt(len(zipBytes))),
			"sha256": zipHash,
			"files":  map[string]any{"Holdexar.exe": sha256Hex(exeBytes)},
		}},
	})
	releaseRaw, _ := json.Marshal(release)

	var sawProbeRange atomic.Bool
	var sawResumeRange atomic.Bool
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/latest.json":
			_, _ = w.Write(releaseRaw)
		case "/" + assetName:
			rng := r.Header.Get("Range")
			if rng == "bytes=0-0" {
				sawProbeRange.Store(true)
				w.Header().Set("Content-Range", fmt.Sprintf("bytes 0-0/%d", len(zipBytes)))
				w.WriteHeader(http.StatusPartialContent)
				_, _ = w.Write(zipBytes[:1])
				return
			}
			wantRange := fmt.Sprintf("bytes=%d-", split)
			if rng != wantRange {
				w.WriteHeader(http.StatusRequestedRangeNotSatisfiable)
				return
			}
			sawResumeRange.Store(true)
			w.Header().Set("Content-Range", fmt.Sprintf("bytes %d-%d/%d", split, len(zipBytes)-1, len(zipBytes)))
			w.WriteHeader(http.StatusPartialContent)
			_, _ = w.Write(zipBytes[split:])
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer server.Close()
	t.Setenv("HOLDEXAR_UPDATE_URL", server.URL)

	root := t.TempDir()
	dataDir := filepath.Join(root, "data")
	svc := &Service{
		InstallRoot: root,
		DataDir:     dataDir,
		Version:     "0.4.0",
		HTTPClient:  server.Client(),
	}
	if res := svc.Download(context.Background(), "v0.4.0", "", "", 0, ""); res["ok"] != false {
		t.Fatalf("same-version download must be rejected: %v", res)
	}
	svc.Version = "0.5.0"
	if res := svc.Download(context.Background(), "v0.4.0", "", "", 0, ""); res["ok"] != false {
		t.Fatalf("downgrade download must be rejected: %v", res)
	}

	svc.Version = "0.3.9"
	staging := svc.stagingDir()
	if err := os.WriteFile(filepath.Join(staging, partName), zipBytes[:split], 0o644); err != nil {
		t.Fatal(err)
	}
	meta := fmt.Sprintf(`{"tag":"v0.4.0","asset":%q,"size":%d,"sha256":%q}`, assetName, len(zipBytes), zipHash)
	if err := os.WriteFile(filepath.Join(staging, downloadMetaName), []byte(meta), 0o644); err != nil {
		t.Fatal(err)
	}

	res := svc.Download(context.Background(), "v0.4.0", "", "", 0, "")
	if res["ok"] != true {
		t.Fatalf("HTTP 206 resume Download failed: %v", res)
	}
	if !sawProbeRange.Load() || !sawResumeRange.Load() {
		t.Fatalf("missing Range headers: probe=%v resume=%v", sawProbeRange.Load(), sawResumeRange.Load())
	}
	if err := VerifyStagedTrust(staging, dataDir, "0.4.0"); err != nil {
		t.Fatalf("VerifyStagedTrust after 206 resume failed: %v", err)
	}
}

func TestStreamToFileStallTimeoutCancelsRead(t *testing.T) {
	oldStall := stallDuration
	stallDuration = 50 * time.Millisecond
	t.Cleanup(func() { stallDuration = oldStall })

	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Length", "1024")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("head"))
		if f, ok := w.(http.Flusher); ok {
			f.Flush()
		}
		<-r.Context().Done()
	}))
	defer server.Close()

	svc := &Service{InstallRoot: t.TempDir(), Version: "0.3.9"}
	dest := filepath.Join(t.TempDir(), partName)
	err := svc.streamToFile(context.Background(), server.URL+"/pkg.zip", "", dest, 1024, 0, "stalled")
	if err == nil || !strings.Contains(err.Error(), "通道停滞") {
		t.Fatalf("expected stall error, got %v", err)
	}
}

func TestExtractZipRejectsZipBombAndCleansStaging(t *testing.T) {
	root := t.TempDir()
	zipPath := filepath.Join(root, "bomb.zip")
	archive, err := os.Create(zipPath)
	if err != nil {
		t.Fatal(err)
	}
	zw := newZipWriter(archive)
	if err := zw.add("Holdexar/a.bin", bytes.Repeat([]byte("A"), 1024)); err != nil {
		t.Fatal(err)
	}
	if err := zw.add("Holdexar/b.bin", bytes.Repeat([]byte("B"), 1025)); err != nil {
		t.Fatal(err)
	}
	_ = zw.close()
	_ = archive.Close()

	staging := filepath.Join(root, "staging")
	if err := os.MkdirAll(staging, 0o755); err != nil {
		t.Fatal(err)
	}
	err = extractZipWithLimit(zipPath, staging, 2048)
	if err == nil || !strings.Contains(err.Error(), "超出上限") {
		t.Fatalf("expected zip bomb limit error, got %v", err)
	}
	if _, statErr := os.Stat(staging); !os.IsNotExist(statErr) {
		t.Fatalf("expected staging directory removed on zip bomb, statErr=%v", statErr)
	}
}
