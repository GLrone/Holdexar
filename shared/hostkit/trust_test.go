package hostkit

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
	"runtime"
	"strings"
	"testing"
)

func installTestKey(t *testing.T, keyID string) ed25519.PrivateKey {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	restore := SetTrustedPubkey(keyID, hex.EncodeToString(pub))
	t.Cleanup(restore)
	return priv
}

func signManifestForTest(t *testing.T, priv ed25519.PrivateKey, keyID string, manifest map[string]any) map[string]any {
	t.Helper()
	payload, err := CanonicalManifestBytes(manifest)
	if err != nil {
		t.Fatalf("CanonicalManifestBytes: %v", err)
	}
	signed := make(map[string]any, len(manifest)+1)
	for k, v := range manifest {
		signed[k] = v
	}
	signed[SignatureField] = map[string]any{
		"alg":   "ed25519",
		"keyId": keyID,
		"sig":   base64.StdEncoding.EncodeToString(ed25519.Sign(priv, payload)),
	}
	return signed
}

func hexSHA256(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func makeDirLink(t *testing.T, link, target string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		if out, err := exec.Command("cmd.exe", "/c", "mklink", "/J", link, target).CombinedOutput(); err != nil {
			t.Fatalf("mklink /J failed: %v (%s)", err, out)
		}
	} else {
		if err := os.Symlink(target, link); err != nil {
			t.Fatalf("symlink failed: %v", err)
		}
	}
	t.Cleanup(func() { _ = os.Remove(link) })
}

func writeStagedFixture(t *testing.T, priv ed25519.PrivateKey, keyID, version string) (string, map[string]any) {
	t.Helper()
	stagingDir := t.TempDir()
	payloadDir := filepath.Join(stagingDir, PayloadDirName)
	if err := os.MkdirAll(filepath.Join(payloadDir, "_internal"), 0o755); err != nil {
		t.Fatal(err)
	}
	exeBytes := []byte("MZ-payload")
	srvBytes := []byte("MZ-serverd")
	if err := os.WriteFile(filepath.Join(payloadDir, "Holdexar.exe"), exeBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(payloadDir, "_internal", "serverd.exe"), srvBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	filesMap := map[string]any{
		"Holdexar.exe":          hexSHA256(exeBytes),
		"_internal/serverd.exe": hexSHA256(srvBytes),
	}
	release := signManifestForTest(t, priv, keyID, map[string]any{
		"schema":  json.Number("2"),
		"app":     AppName,
		"version": version,
		"tag":     "v" + version,
		"assets": []any{map[string]any{
			"role":   "app",
			"name":   "Holdexar-win64-v" + version + ".zip",
			"size":   json.Number("35441043"),
			"sha256": strings.Repeat("a", 64),
			"files":  filesMap,
		}},
	})
	manifest := map[string]any{
		"tag":              "v" + version,
		"ready":            true,
		"attempt_id":       "att-1",
		"files":            filesMap,
		"release_manifest": release,
	}
	if err := AtomicWriteJSON(filepath.Join(stagingDir, StagingManifestName), manifest); err != nil {
		t.Fatal(err)
	}
	return stagingDir, manifest
}

func TestManifestVerificationAndOverrideHook(t *testing.T) {
	priv := installTestKey(t, "unit-k")
	manifest := signManifestForTest(t, priv, "unit-k", map[string]any{
		"schema":  json.Number("2"),
		"app":     AppName,
		"version": "0.4.0",
		"tag":     "v0.4.0",
	})
	if ok, reason := CheckManifestTrusted(manifest); !ok {
		t.Fatalf("valid manifest rejected: %s", reason)
	}
	manifest["version"] = "9.9.9"
	if ok, _ := VerifyManifest(manifest); ok {
		t.Fatal("tampered manifest accepted")
	}
}

func TestValidPayloadRelPathAndFiles(t *testing.T) {
	for _, bad := range []string{"", "../evil.exe", "/abs/a.exe", `dir\a.exe`, "C:/a.exe", "./a.exe", "dir//a.exe", "dir/"} {
		if ValidPayloadRelPath(bad) {
			t.Fatalf("ValidPayloadRelPath(%q) = true, want false", bad)
		}
	}
	for _, good := range []string{"Holdexar.exe", "_internal/serverd.exe", "versions/0.4.0/Holdexar.exe"} {
		if !ValidPayloadRelPath(good) {
			t.Fatalf("ValidPayloadRelPath(%q) = false, want true", good)
		}
	}
	dupCase := map[string]any{
		"Holdexar.exe": strings.Repeat("a", 64),
		"holdexar.exe": strings.Repeat("b", 64),
	}
	if _, ok := ValidPayloadFiles(dupCase); ok {
		t.Fatal("case-colliding files accepted")
	}
}

func TestVerifyStagedPayloadSecurity(t *testing.T) {
	priv := installTestKey(t, "stg-k")
	version := "0.4.0"

	t.Run("valid upgrade passes", func(t *testing.T) {
		stagingDir, _ := writeStagedFixture(t, priv, "stg-k", version)
		gotVer, rel, reason := VerifyStagedPayload(stagingDir, "0.3.9")
		if reason != "" || gotVer != version || rel == nil {
			t.Fatalf("VerifyStagedPayload failed: ver=%q reason=%q", gotVer, reason)
		}
		if err := VerifyStagedTrust(stagingDir, version); err != nil {
			t.Fatalf("VerifyStagedTrust failed: %v", err)
		}
	})

	t.Run("not ready rejected", func(t *testing.T) {
		stagingDir, manifest := writeStagedFixture(t, priv, "stg-k", version)
		manifest["ready"] = false
		_ = AtomicWriteJSON(filepath.Join(stagingDir, StagingManifestName), manifest)
		if _, _, reason := VerifyStagedPayload(stagingDir, "0.3.9"); !strings.Contains(reason, "尚未就绪") {
			t.Fatalf("expected not-ready rejection, got %q", reason)
		}
	})

	t.Run("downgrade and equal version rejected", func(t *testing.T) {
		stagingDir, _ := writeStagedFixture(t, priv, "stg-k", version)
		if _, _, reason := VerifyStagedPayload(stagingDir, "0.4.0"); !strings.Contains(reason, "不新于") {
			t.Fatalf("equal version accepted: %q", reason)
		}
		if _, _, reason := VerifyStagedPayload(stagingDir, "0.4.1"); !strings.Contains(reason, "不新于") {
			t.Fatalf("downgrade accepted: %q", reason)
		}
	})

	t.Run("wrong app or float schema rejected", func(t *testing.T) {
		stagingDir, manifest := writeStagedFixture(t, priv, "stg-k", version)
		rel := manifest["release_manifest"].(map[string]any)
		delete(rel, SignatureField)
		rel["schema"] = json.Number("2.0")
		manifest["release_manifest"] = signManifestForTest(t, priv, "stg-k", rel)
		_ = AtomicWriteJSON(filepath.Join(stagingDir, StagingManifestName), manifest)
		if _, _, reason := VerifyStagedPayload(stagingDir, "0.3.9"); !strings.Contains(reason, "不受支持") {
			t.Fatalf("float schema accepted: %q", reason)
		}
	})

	t.Run("extra tampered missing and reparse directory rejected", func(t *testing.T) {
		stagingDir, _ := writeStagedFixture(t, priv, "stg-k", version)
		extra := filepath.Join(stagingDir, PayloadDirName, "extra.dll")
		_ = os.WriteFile(extra, []byte("unlisted"), 0o644)
		if _, _, reason := VerifyStagedPayload(stagingDir, "0.3.9"); !strings.Contains(reason, "发布清单之外") {
			t.Fatalf("extra file accepted: %q", reason)
		}
		_ = os.Remove(extra)

		target := filepath.Join(stagingDir, PayloadDirName, "_internal", "serverd.exe")
		_ = os.WriteFile(target, []byte("tampered"), 0o644)
		if _, _, reason := VerifyStagedPayload(stagingDir, "0.3.9"); !strings.Contains(reason, "校验不符") {
			t.Fatalf("tampered file accepted: %q", reason)
		}

		_ = os.Remove(target)
		if _, _, reason := VerifyStagedPayload(stagingDir, "0.3.9"); !strings.Contains(reason, "载荷缺失") {
			t.Fatalf("missing file accepted: %q", reason)
		}

		stagingDir2, _ := writeStagedFixture(t, priv, "stg-k", version)
		ext := t.TempDir()
		_ = os.WriteFile(filepath.Join(ext, "a.txt"), []byte("x"), 0o644)
		makeDirLink(t, filepath.Join(stagingDir2, PayloadDirName, "junc"), ext)
		if _, _, reason := VerifyStagedPayload(stagingDir2, "0.3.9"); !strings.Contains(reason, "重解析路径") {
			t.Fatalf("reparse link accepted: %q", reason)
		}
	})
}
