//go:build windows

package main

import (
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

func installTestTrustKeyUH(t *testing.T, keyID string) ed25519.PrivateKey {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	restore := hostkit.SetTrustedPubkey(keyID, hex.EncodeToString(pub))
	t.Cleanup(restore)
	return priv
}

func signReleaseManifestUH(t *testing.T, priv ed25519.PrivateKey, keyID string, manifest map[string]any) map[string]any {
	t.Helper()
	payload, err := hostkit.CanonicalManifestBytes(manifest)
	if err != nil {
		t.Fatalf("CanonicalManifestBytes: %v", err)
	}
	signed := make(map[string]any, len(manifest)+1)
	for k, v := range manifest {
		signed[k] = v
	}
	signed[hostkit.SignatureField] = map[string]any{
		"alg":   "ed25519",
		"keyId": keyID,
		"sig":   base64.StdEncoding.EncodeToString(ed25519.Sign(priv, payload)),
	}
	return signed
}

func sha256BytesHex(data []byte) string {
	h := sha256.Sum256(data)
	return hex.EncodeToString(h[:])
}

func writeStagingFixtureUH(t *testing.T, priv ed25519.PrivateKey, keyID, version, sourceVersion string) (string, string, map[string]any) {
	t.Helper()
	stagingDir := t.TempDir()
	dataDir := t.TempDir()
	payloadDir := filepath.Join(stagingDir, appName)
	if err := os.MkdirAll(filepath.Join(payloadDir, "_internal"), 0o755); err != nil {
		t.Fatal(err)
	}
	exeBytes := []byte("new-payload-exe")
	helperBytes := []byte("data")
	if err := os.WriteFile(filepath.Join(payloadDir, appName+".exe"), exeBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(payloadDir, "_internal", "a.txt"), helperBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	filesMap := map[string]any{
		appName + ".exe":  sha256BytesHex(exeBytes),
		"_internal/a.txt": sha256BytesHex(helperBytes),
	}
	release := signReleaseManifestUH(t, priv, keyID, map[string]any{
		"schema":  json.Number("2"),
		"app":     appName,
		"version": version,
		"tag":     "v" + version,
		"assets": []any{map[string]any{
			"role":   "app",
			"name":   appName + "-win64-v" + version + ".zip",
			"size":   json.Number("35441043"),
			"sha256": strings.Repeat("a", 64),
			"files":  filesMap,
		}},
	})
	manifest := map[string]any{
		"tag":              "v" + version,
		"ready":            true,
		"attempt_id":       "att-sec",
		"source_version":   sourceVersion,
		"files":            filesMap,
		"release_manifest": release,
	}
	writeManifestFileUH(t, stagingDir, manifest)
	return stagingDir, dataDir, manifest
}

func writeManifestFileUH(t *testing.T, stagingDir string, manifest map[string]any) {
	t.Helper()
	raw, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stagingDir, "manifest.json"), raw, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestVerifyStagedTrustSecurityUH(t *testing.T) {
	priv := installTestTrustKeyUH(t, "uh-k")
	version := "0.3.7"

	t.Run("valid signed manifest passes without update-trust.json", func(t *testing.T) {
		stagingDir, dataDir, _ := writeStagingFixtureUH(t, priv, "uh-k", version, "0.3.6")
		if err := verifyStagedTrust(stagingDir, dataDir, version); err != nil {
			t.Fatalf("expected valid staged trust to pass, got: %v", err)
		}
	})

	t.Run("rejects attacker forged update-trust.json when release_manifest absent", func(t *testing.T) {
		stagingDir, dataDir, manifest := writeStagingFixtureUH(t, priv, "uh-k", version, "0.3.6")
		forged, _ := json.Marshal(map[string]any{
			"version":    "v" + version,
			"zip_sha256": strings.Repeat("a", 64),
			"files":      manifest["files"],
		})
		if err := os.WriteFile(filepath.Join(dataDir, "update-trust.json"), forged, 0o644); err != nil {
			t.Fatal(err)
		}
		delete(manifest, "release_manifest")
		writeManifestFileUH(t, stagingDir, manifest)

		err := verifyStagedTrust(stagingDir, dataDir, version)
		if err == nil || !strings.Contains(err.Error(), "发布清单缺失") {
			t.Fatalf("expected missing release_manifest error, got: %v", err)
		}
	})

	t.Run("rejects tampered signature, wrong app, wrong schema, and version mismatch", func(t *testing.T) {
		stagingDir, dataDir, manifest := writeStagingFixtureUH(t, priv, "uh-k", version, "0.3.6")
		rel := manifest["release_manifest"].(map[string]any)
		rel["assets"].([]any)[0].(map[string]any)["files"].(map[string]any)["different.exe"] = strings.Repeat("0", 64)
		writeManifestFileUH(t, stagingDir, manifest)
		if err := verifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "签名") {
			t.Fatalf("expected signature error, got: %v", err)
		}

		stagingDir, dataDir, manifest = writeStagingFixtureUH(t, priv, "uh-k", version, "0.3.6")
		rel = manifest["release_manifest"].(map[string]any)
		delete(rel, hostkit.SignatureField)
		rel["app"] = "Other"
		rel["schema"] = json.Number("1")
		manifest["release_manifest"] = signReleaseManifestUH(t, priv, "uh-k", rel)
		writeManifestFileUH(t, stagingDir, manifest)
		if err := verifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "不受支持") {
			t.Fatalf("expected unsupported app/schema error, got: %v", err)
		}

		stagingDir, dataDir, manifest = writeStagingFixtureUH(t, priv, "uh-k", version, "0.3.6")
		manifest["tag"] = "v0.3.8"
		writeManifestFileUH(t, stagingDir, manifest)
		if err := verifyStagedTrust(stagingDir, dataDir, "0.3.8"); err == nil || !strings.Contains(err.Error(), "版本不符") {
			t.Fatalf("expected version mismatch error, got: %v", err)
		}
	})

	t.Run("rejects extra unlisted file, tampered file, traversal files, and reparse junction", func(t *testing.T) {
		stagingDir, dataDir, _ := writeStagingFixtureUH(t, priv, "uh-k", version, "0.3.6")
		extra := filepath.Join(stagingDir, appName, "unexpected.dll")
		if err := os.WriteFile(extra, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := verifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "发布清单之外") {
			t.Fatalf("expected extra file error, got: %v", err)
		}
		_ = os.Remove(extra)

		if err := os.WriteFile(filepath.Join(stagingDir, appName, "_internal", "a.txt"), []byte("evil"), 0o644); err != nil {
			t.Fatal(err)
		}
		if err := verifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "校验不符") {
			t.Fatalf("expected hash mismatch error, got: %v", err)
		}

		stagingDir, dataDir, manifest := writeStagingFixtureUH(t, priv, "uh-k", version, "0.3.6")
		rel := manifest["release_manifest"].(map[string]any)
		delete(rel, hostkit.SignatureField)
		rel["assets"].([]any)[0].(map[string]any)["files"] = map[string]any{"../evil.exe": strings.Repeat("b", 64)}
		manifest["release_manifest"] = signReleaseManifestUH(t, priv, "uh-k", rel)
		writeManifestFileUH(t, stagingDir, manifest)
		if err := verifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "校验表非法") {
			t.Fatalf("expected traversal rejection, got: %v", err)
		}
	})
}

func TestVerifyStagedTrustReparsePointUH(t *testing.T) {
	priv := installTestTrustKeyUH(t, "uh-junc")
	stagingDir, dataDir, _ := writeStagingFixtureUH(t, priv, "uh-junc", "0.3.7", "0.3.6")
	ext := t.TempDir()
	_ = os.WriteFile(filepath.Join(ext, "x.txt"), []byte("x"), 0o644)
	junc := filepath.Join(stagingDir, appName, "junc")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", junc, ext).CombinedOutput(); err != nil {
		t.Fatalf("mklink /J failed: %v (%s)", err, string(out))
	}
	if err := verifyStagedTrust(stagingDir, dataDir, "0.3.7"); err == nil || !strings.Contains(err.Error(), "重解析路径") {
		t.Fatalf("expected reparse point rejection, got: %v", err)
	}
}

func TestTrustedPathRejectsJunctionUH(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	junc := filepath.Join(root, "versions")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", junc, outside).CombinedOutput(); err != nil {
		t.Skipf("mklink /J failed: %v (%s)", err, string(out))
	}
	t.Cleanup(func() { _ = os.Remove(junc) })
	if trustedPath(filepath.Join(junc, "0.4.0")) {
		t.Fatal("trustedPath accepted NTFS junction path")
	}
}

func TestServerdExeOfPrefersFlatLayoutUH(t *testing.T) {
	verDir := t.TempDir()
	hostExe := filepath.Join(verDir, "Holdexar.exe")
	legacy := filepath.Join(verDir, "_internal", "serverd.exe")
	if err := os.MkdirAll(filepath.Dir(legacy), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(legacy, []byte("MZ-legacy"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := serverdExeOf(hostExe); !samePath(got, legacy) {
		t.Fatalf("expected fallback %q, got %q", legacy, got)
	}
	flat := filepath.Join(verDir, "serverd.exe")
	if err := os.WriteFile(flat, []byte("MZ-flat"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := serverdExeOf(hostExe); !samePath(got, flat) {
		t.Fatalf("expected flat %q preferred over _internal, got %q", flat, got)
	}
}

func TestExecuteUpdateLockedRejectsDowngradeAndUntrustedStaging(t *testing.T) {
	priv := installTestTrustKeyUH(t, "exec-k")

	for _, tc := range []struct {
		name          string
		activeVersion string
		targetVersion string
		dropManifest  bool
	}{
		{"downgrade blocked even if signed", "0.3.8", "0.3.7", false},
		{"equal version blocked even if signed", "0.3.7", "0.3.7", false},
		{"missing release_manifest blocked", "0.3.6", "0.3.7", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			if err := os.MkdirAll(filepath.Join(root, versionsDir), 0o755); err != nil {
				t.Fatal(err)
			}
			seedVersionDir(t, root, tc.activeVersion)
			if err := writeCurrentState(root, tc.activeVersion, "", nil); err != nil {
				t.Fatal(err)
			}
			stagingDir, _, manifest := writeStagingFixtureUH(t, priv, "exec-k", tc.targetVersion, tc.activeVersion)
			if tc.dropManifest {
				delete(manifest, "release_manifest")
				writeManifestFileUH(t, stagingDir, manifest)
			}
			ok := executeUpdateLocked(root, &options{
				staging:         stagingDir,
				targetVersion:   tc.targetVersion,
				sourceVersion:   tc.activeVersion,
				skipHealthCheck: true,
				noLaunch:        true,
			})
			if ok {
				t.Fatalf("expected executeUpdateLocked to reject %s", tc.name)
			}
			state, err := readCurrentState(root)
			if err != nil || stateString(state, "version") != tc.activeVersion {
				t.Fatalf("expected active version %s preserved, got (%+v, %v)", tc.activeVersion, state, err)
			}
		})
	}
}
