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
	"runtime"
	"strings"
	"testing"
	"unsafe"

	"holdexar/hostkit"
)

func installTestTrustKeyHost(t *testing.T, keyID string) ed25519.PrivateKey {
	t.Helper()
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	restore := hostkit.SetTrustedPubkey(keyID, hex.EncodeToString(pub))
	t.Cleanup(restore)
	return priv
}

func signReleaseManifestHost(t *testing.T, priv ed25519.PrivateKey, keyID string, manifest map[string]any) map[string]any {
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

func sha256HostHex(data []byte) string {
	h := sha256.Sum256(data)
	return hex.EncodeToString(h[:])
}

func writeStagingFixtureHost(t *testing.T, priv ed25519.PrivateKey, keyID, version string) (string, string, map[string]any) {
	t.Helper()
	stagingDir := t.TempDir()
	dataDir := t.TempDir()
	payloadDir := filepath.Join(stagingDir, hostkit.PayloadDirName)
	if err := os.MkdirAll(filepath.Join(payloadDir, "versions", version), 0o755); err != nil {
		t.Fatal(err)
	}
	exeBytes := []byte("host-exe")
	helperBytes := []byte("server-exe")
	if err := os.WriteFile(filepath.Join(payloadDir, "Holdexar.exe"), exeBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(payloadDir, "versions", version, "serverd.exe"), helperBytes, 0o644); err != nil {
		t.Fatal(err)
	}
	filesMap := map[string]any{
		"Holdexar.exe":                         sha256HostHex(exeBytes),
		"versions/" + version + "/serverd.exe": sha256HostHex(helperBytes),
	}
	release := signReleaseManifestHost(t, priv, keyID, map[string]any{
		"schema":  json.Number("2"),
		"app":     hostkit.AppName,
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
		"files":            filesMap,
		"release_manifest": release,
	}
	raw, _ := json.MarshalIndent(manifest, "", "  ")
	if err := os.WriteFile(filepath.Join(stagingDir, hostkit.StagingManifestName), raw, 0o644); err != nil {
		t.Fatal(err)
	}
	return stagingDir, dataDir, manifest
}

func TestVerifyStagedTrustHostSecurity(t *testing.T) {
	priv := installTestTrustKeyHost(t, "wh-k")
	version := "0.4.0"

	stagingDir, dataDir, _ := writeStagingFixtureHost(t, priv, "wh-k", version)
	if err := verifyStagedTrust(stagingDir, dataDir, version); err != nil {
		t.Fatalf("expected valid signed staging to pass, got: %v", err)
	}

	stagingDir, dataDir, manifest := writeStagingFixtureHost(t, priv, "wh-k", version)
	forged, _ := json.Marshal(map[string]any{"version": "v" + version, "zip_sha256": strings.Repeat("a", 64), "files": manifest["files"]})
	_ = os.WriteFile(filepath.Join(dataDir, "update-trust.json"), forged, 0o644)
	delete(manifest, "release_manifest")
	raw, _ := json.Marshal(manifest)
	_ = os.WriteFile(filepath.Join(stagingDir, hostkit.StagingManifestName), raw, 0o644)
	if err := verifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "发布清单缺失") {
		t.Fatalf("expected missing release_manifest error, got: %v", err)
	}

	stagingDir, dataDir, _ = writeStagingFixtureHost(t, priv, "wh-k", version)
	_ = os.WriteFile(filepath.Join(stagingDir, hostkit.PayloadDirName, "extra.dll"), []byte("x"), 0o644)
	if err := verifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "发布清单之外") {
		t.Fatalf("expected extra file rejection, got: %v", err)
	}

	stagingDir, dataDir, _ = writeStagingFixtureHost(t, priv, "wh-k", version)
	ext := t.TempDir()
	_ = os.WriteFile(filepath.Join(ext, "x.txt"), []byte("x"), 0o644)
	junc := filepath.Join(stagingDir, hostkit.PayloadDirName, "junc")
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", junc, ext).CombinedOutput(); err != nil {
		t.Skipf("mklink /J unavailable: %v (%s)", err, string(out))
	}
	if err := verifyStagedTrust(stagingDir, dataDir, version); err == nil || !strings.Contains(err.Error(), "重解析路径") {
		t.Fatalf("expected reparse rejection, got: %v", err)
	}
}

func TestHandoffPendingManagedRootMode(t *testing.T) {
	priv := installTestTrustKeyHost(t, "wh-handoff")
	selfExe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	exeBytes, err := os.ReadFile(selfExe)
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	dataDir := filepath.Join(root, "data")
	stagingDir := filepath.Join(root, hostkit.StagingDirName)
	payloadDir := filepath.Join(stagingDir, hostkit.PayloadDirName)
	if err := os.MkdirAll(filepath.Join(payloadDir, "updater"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(payloadDir, "Holdexar.exe"), exeBytes, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(payloadDir, "updater", "UpdateHost.exe"), exeBytes, 0o755); err != nil {
		t.Fatal(err)
	}
	digest := sha256HostHex(exeBytes)
	filesMap := map[string]any{
		"Holdexar.exe":           digest,
		"updater/UpdateHost.exe": digest,
	}
	release := signReleaseManifestHost(t, priv, "wh-handoff", map[string]any{
		"schema":  json.Number("2"),
		"app":     hostkit.AppName,
		"version": "0.5.0",
		"tag":     "v0.5.0",
		"assets": []any{map[string]any{
			"role":   "app",
			"name":   "Holdexar-win64-v0.5.0.zip",
			"size":   json.Number("1024"),
			"sha256": strings.Repeat("a", 64),
			"files":  filesMap,
		}},
	})
	manifest := map[string]any{
		"tag":              "v0.5.0",
		"ready":            true,
		"files":            filesMap,
		"release_manifest": release,
	}
	raw, _ := json.MarshalIndent(manifest, "", "  ")
	if err := os.WriteFile(filepath.Join(stagingDir, hostkit.StagingManifestName), raw, 0o644); err != nil {
		t.Fatal(err)
	}

	cfg := &options{
		installRoot: root,
		dataDir:     dataDir,
		appVersion:  "0.4.0",
		isManaged:   true,
	}
	if !handoffPending(cfg, func(string) {}) {
		t.Fatal("expected handoffPending to succeed in managed root mode")
	}
}

func TestCloseDialogOwnerAnchorableAndCentering(t *testing.T) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	if ownerAnchorable(0) {
		t.Fatal("hwnd 0 must not be anchorable")
	}
	x, y := centeredInRect(rect{Left: 100, Top: 50, Right: 1100, Bottom: 850}, dialogWidth, dialogHeight)
	if x != 340 || y != 330 {
		t.Fatalf("centeredInRect = (%d, %d), want (340, 330)", x, y)
	}

	registerDialogClass()
	owner := call(procCreateWindowExW, uintptr(wsExToolWindow), utf16Ptr(dialogClassName), utf16Ptr("anchor-test"),
		uintptr(wsPopup), 100, 100, 800, 600, 0, 0, 0, 0)
	if owner == 0 {
		t.Skip("desktop window creation unavailable")
	}
	defer call(procDestroyWindow, owner)

	if ownerAnchorable(owner) {
		t.Fatal("hidden owner must not be anchorable")
	}
	call(procShowWindow, owner, swShow)
	if !ownerAnchorable(owner) {
		t.Fatal("visible non-iconic owner should be anchorable")
	}
	call(procShowWindow, owner, swMinimize)
	if ownerAnchorable(owner) {
		t.Fatal("minimized owner must not be anchorable")
	}

	dlgWnd := call(procCreateWindowExW, uintptr(wsExToolWindow), utf16Ptr(dialogClassName), utf16Ptr("dlg-test"),
		uintptr(wsPopup), 0, 0, dialogWidth, dialogHeight, 0, 0, 0, 0)
	if dlgWnd == 0 {
		t.Fatal("failed to create test dialog window")
	}
	defer call(procDestroyWindow, dlgWnd)
	d := &closeDialog{owner: owner, hwnd: dlgWnd}
	d.positionOverOwner()
	var pos rect
	if call(procGetWindowRect, dlgWnd, uintptr(unsafe.Pointer(&pos))) == 0 {
		t.Fatal("GetWindowRect failed")
	}
	if pos.Left < 0 || pos.Top < 0 || pos.Left >= 0x4000 || pos.Top >= 0x4000 {
		t.Fatalf("fallback dialog position escaped screen bounds: %+v", pos)
	}
}
