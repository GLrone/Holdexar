package updater

import (
	"archive/zip"
	"bytes"
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"holdexar/hostkit"
)

func installTestTrustKey(t *testing.T, keyID string) ed25519.PrivateKey {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(hostkit.SetTrustedPubkey(keyID, hex.EncodeToString(pub)))
	return priv
}

func signReleaseManifestForTest(t *testing.T, priv ed25519.PrivateKey, keyID string, manifest map[string]any) map[string]any {
	t.Helper()
	payload, err := canonicalManifestBytes(manifest)
	if err != nil {
		t.Fatalf("canonicalManifestBytes: %v", err)
	}
	signed := make(map[string]any, len(manifest)+1)
	for k, v := range manifest {
		signed[k] = v
	}
	signed[signatureField] = map[string]any{
		"alg":   "ed25519",
		"keyId": keyID,
		"sig":   base64.StdEncoding.EncodeToString(ed25519.Sign(priv, payload)),
	}
	return signed
}

func writeStagedPayloadFixture(t *testing.T, priv ed25519.PrivateKey, keyID, version string) (string, string, map[string]any) {
	t.Helper()
	stagingDir := t.TempDir()
	dataDir := t.TempDir()
	payloadDir := filepath.Join(stagingDir, appPayloadDir)
	if err := os.MkdirAll(filepath.Join(payloadDir, "_internal"), 0o755); err != nil {
		t.Fatal(err)
	}
	exeBytes := []byte("fake holdexar executable content for test")
	helperBytes := []byte("data")
	if err := os.WriteFile(filepath.Join(payloadDir, "Holdexar.exe"), exeBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(payloadDir, "_internal", "a.txt"), helperBytes, 0o644); err != nil {
		t.Fatal(err)
	}

	filesMap := map[string]any{
		"Holdexar.exe":    sha256Hex(exeBytes),
		"_internal/a.txt": sha256Hex(helperBytes),
	}
	release := signReleaseManifestForTest(t, priv, keyID, map[string]any{
		"schema":  json.Number("2"),
		"app":     AppName,
		"version": version,
		"tag":     "v" + version,
		"notes":   "test notes",
		"assets": []any{
			map[string]any{
				"role":   "app",
				"name":   "Holdexar-win64-v" + version + ".zip",
				"size":   json.Number("35441043"),
				"sha256": strings.Repeat("a", 64),
				"files":  filesMap,
			},
		},
	})

	manifest := map[string]any{
		"tag":              "v" + version,
		"ready":            true,
		"attempt_id":       "att-sec",
		"files":            filesMap,
		"release_manifest": release,
	}
	writeManifestJSON(t, stagingDir, manifest)
	return stagingDir, dataDir, manifest
}

func writeManifestJSON(t *testing.T, stagingDir string, manifest map[string]any) {
	t.Helper()
	raw, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stagingDir, StagingManifest), raw, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestVerifyStagedTrustSignedManifestSecurity(t *testing.T) {
	priv := installTestTrustKey(t, "test-k")
	version := "0.4.0"

	t.Run("valid signed manifest with large int size passes without update-trust.json", func(t *testing.T) {
		stagingDir, dataDir, _ := writeStagedPayloadFixture(t, priv, "test-k", version)
		if err := VerifyStagedTrust(stagingDir, dataDir, version); err != nil {
			t.Fatalf("expected valid staged trust to pass, got: %v", err)
		}
	})

	t.Run("rejects forged update-trust.json when release_manifest absent", func(t *testing.T) {
		stagingDir, dataDir, manifest := writeStagedPayloadFixture(t, priv, "test-k", version)
		forgedTrust, _ := json.Marshal(map[string]any{
			"version":    "v" + version,
			"zip_sha256": strings.Repeat("a", 64),
			"files":      manifest["files"],
		})
		if err := os.WriteFile(filepath.Join(dataDir, "update-trust.json"), forgedTrust, 0o644); err != nil {
			t.Fatal(err)
		}
		delete(manifest, "release_manifest")
		writeManifestJSON(t, stagingDir, manifest)

		err := VerifyStagedTrust(stagingDir, dataDir, version)
		if err == nil || !strings.Contains(err.Error(), "发布清单缺失") {
			t.Fatalf("expected missing release_manifest error, got: %v", err)
		}
	})

	t.Run("rejects tampered release_manifest signature", func(t *testing.T) {
		stagingDir, dataDir, manifest := writeStagedPayloadFixture(t, priv, "test-k", version)
		rel := manifest["release_manifest"].(map[string]any)
		assets := rel["assets"].([]any)
		asset := assets[0].(map[string]any)
		asset["files"].(map[string]any)["different.exe"] = strings.Repeat("0", 64)
		writeManifestJSON(t, stagingDir, manifest)

		err := VerifyStagedTrust(stagingDir, dataDir, version)
		if err == nil || !strings.Contains(err.Error(), "签名") {
			t.Fatalf("expected signature verification error, got: %v", err)
		}
	})

	t.Run("rejects wrong app or wrong schema even when validly signed", func(t *testing.T) {
		for _, tc := range []struct {
			name   string
			app    string
			schema any
		}{
			{"wrong app", "EvilApp", json.Number("2")},
			{"schema 1", AppName, json.Number("1")},
			{"schema float", AppName, json.Number("2.0")},
			{"schema string", AppName, "2"},
		} {
			t.Run(tc.name, func(t *testing.T) {
				stagingDir, dataDir, manifest := writeStagedPayloadFixture(t, priv, "test-k", version)
				rel := manifest["release_manifest"].(map[string]any)
				delete(rel, signatureField)
				rel["app"] = tc.app
				rel["schema"] = tc.schema
				manifest["release_manifest"] = signReleaseManifestForTest(t, priv, "test-k", rel)
				writeManifestJSON(t, stagingDir, manifest)

				err := VerifyStagedTrust(stagingDir, dataDir, version)
				if err == nil || !strings.Contains(err.Error(), "不受支持") {
					t.Fatalf("expected unsupported app/schema error, got: %v", err)
				}
			})
		}
	})

	t.Run("rejects version mismatches", func(t *testing.T) {
		stagingDir, dataDir, manifest := writeStagedPayloadFixture(t, priv, "test-k", version)
		if err := VerifyStagedTrust(stagingDir, dataDir, "0.4.1"); err == nil || !strings.Contains(err.Error(), "版本不符") {
			t.Fatalf("expected target version mismatch error, got: %v", err)
		}
		manifest["tag"] = "v0.4.1"
		writeManifestJSON(t, stagingDir, manifest)
		if err := VerifyStagedTrust(stagingDir, dataDir, "0.4.1"); err == nil || !strings.Contains(err.Error(), "版本不符") {
			t.Fatalf("expected staging tag mismatch error, got: %v", err)
		}
	})

	t.Run("rejects files traversal entries even when signed", func(t *testing.T) {
		for _, badPath := range []string{"../evil.exe", "/abs/Holdexar.exe", `dir\Holdexar.exe`, "C:/Holdexar.exe", "./Holdexar.exe"} {
			t.Run(badPath, func(t *testing.T) {
				stagingDir, dataDir, manifest := writeStagedPayloadFixture(t, priv, "test-k", version)
				rel := manifest["release_manifest"].(map[string]any)
				delete(rel, signatureField)
				asset := rel["assets"].([]any)[0].(map[string]any)
				asset["files"] = map[string]any{badPath: strings.Repeat("b", 64)}
				manifest["release_manifest"] = signReleaseManifestForTest(t, priv, "test-k", rel)
				writeManifestJSON(t, stagingDir, manifest)

				err := VerifyStagedTrust(stagingDir, dataDir, version)
				if err == nil || !strings.Contains(err.Error(), "校验表非法") {
					t.Fatalf("expected bad files table error for %q, got: %v", badPath, err)
				}
			})
		}
	})

	t.Run("rejects tampered, missing, and extra planted files", func(t *testing.T) {
		stagingDir, dataDir, _ := writeStagedPayloadFixture(t, priv, "test-k", version)
		extraPath := filepath.Join(stagingDir, appPayloadDir, "unexpected.dll")
		if err := os.WriteFile(extraPath, []byte("untrusted"), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := VerifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "发布清单之外") {
			t.Fatalf("expected extra file error, got: %v", err)
		}
		_ = os.Remove(extraPath)

		exePath := filepath.Join(stagingDir, appPayloadDir, "Holdexar.exe")
		if err := os.WriteFile(exePath, []byte("tampered!"), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := VerifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "校验不符") {
			t.Fatalf("expected hash mismatch error, got: %v", err)
		}

		_ = os.Remove(exePath)
		if err := VerifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "载荷缺失") {
			t.Fatalf("expected missing file error, got: %v", err)
		}
	})

	t.Run("rejects directory junction / reparse point inside payload", func(t *testing.T) {
		stagingDir, dataDir, _ := writeStagedPayloadFixture(t, priv, "test-k", version)
		externalDir := t.TempDir()
		if err := os.WriteFile(filepath.Join(externalDir, "secret.txt"), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		junction := filepath.Join(stagingDir, appPayloadDir, "junc")
		cmd := exec.Command("cmd", "/c", "mklink", "/J", junction, externalDir)
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Skipf("mklink /J unavailable: %v (%s)", err, string(out))
		}
		err := VerifyStagedTrust(stagingDir, dataDir, version)
		if err == nil || !strings.Contains(err.Error(), "重解析路径") {
			t.Fatalf("expected reparse point rejection, got: %v", err)
		}
	})
}

func TestExtractZipRejectsSymlinkEntry(t *testing.T) {
	root := t.TempDir()
	zipPath := filepath.Join(root, "symlink.zip")
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	hdr := &zip.FileHeader{Name: "Holdexar/link.exe", Method: zip.Deflate}
	hdr.SetMode(os.ModeSymlink | 0o777)
	w, err := zw.CreateHeader(hdr)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := w.Write([]byte(`C:\Windows\System32\cmd.exe`)); err != nil {
		t.Fatal(err)
	}
	if err := zw.Close(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(zipPath, buf.Bytes(), 0o644); err != nil {
		t.Fatal(err)
	}

	staging := filepath.Join(root, "staging")
	if err := os.MkdirAll(staging, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := extractZip(zipPath, staging); err == nil || !strings.Contains(err.Error(), "符号链接") {
		t.Fatalf("expected symlink rejection from extractZip, got: %v", err)
	}
}

func sha256Hex(data []byte) string {
	h := sha256.Sum256(data)
	return hex.EncodeToString(h[:])
}
