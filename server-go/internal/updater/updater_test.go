//go:build windows

package updater

import (
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
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
		{"1.0.0", "1.0.0-beta", true}, // 正式版 > 预发布
		{"0.3.8-w5", "0.3.7-w4", true},
	}
	for _, tc := range cases {
		if got := versionGT(tc.a, tc.b); got != tc.want {
			t.Fatalf("versionGT(%q,%q)=%v 期望 %v", tc.a, tc.b, got, tc.want)
		}
	}
}

// TestManifestSignatureRoundTrip 验签正反两向：发布端同构签名可通过，
// 改动任一载荷字段后必失败。
func TestManifestSignatureRoundTrip(t *testing.T) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	// 用测试公钥替换信任表（进程内测试面）
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
		"version": "0.3.9",
		"tag":     "v0.3.9",
		"notes":   "测试说明",
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
	// 篡改载荷字段 → 验签必败
	manifest["version"] = "9.9.9"
	if ok, _ := verifyManifest(manifest); ok {
		t.Fatal("篡改后的清单通过验签")
	}
	// 未签名清单必败
	if ok, reason := verifyManifest(map[string]any{"version": "0.3.9"}); ok {
		t.Fatalf("无签名清单通过验签：%s", reason)
	}
	// 信任表外的 keyId 必败
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
	// 构造带越界条目的 zip
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
	// 无暂存无事务：pending=false、swapFailed=false、justUpdated=nil
	status := service.PendingStatus()
	if status["pending"] != false || status["swapFailed"] != false {
		t.Fatalf("空现场投影错误：%v", status)
	}
	if service.JustUpdated() != nil {
		t.Fatal("空现场不应有 justUpdated")
	}
	// 换装失败标记投影
	if err := os.WriteFile(filepath.Join(service.DataDir, SwapFailedFlag), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if status = service.PendingStatus(); status["swapFailed"] != true {
		t.Fatal("swap-failed 未投影")
	}
	_ = os.Remove(filepath.Join(service.DataDir, SwapFailedFlag))
	// 已提交事务 + 成功诊断 → justUpdated + ack 闭合
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
	// 本地测试源：HOLDEXAR_UPDATE_URL 豁免验签，返回的载荷直接进检查投影
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
		"version": "0.4.0",
		"tag":     "v0.4.0",
		"assets": []any{map[string]any{
			"name": "Holdexar-win64-v0.4.0.zip", "role": "app",
			"sha256": strings.Repeat("cd", 32), "size": json.Number("1000"),
		}},
	}
	payload, _ := canonicalManifestBytes(manifest)
	manifest["signature"] = map[string]any{"alg": "ed25519", "keyId": keyID,
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
	if result["sha256"] != strings.Repeat("cd", 32) || result["asset"] != "Holdexar-win64-v0.4.0.zip" {
		t.Fatalf("校验值/资产名投影错误：%v", result)
	}
}
