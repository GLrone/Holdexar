//go:build windows

package main

import (
	"crypto/ed25519"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"

	"holdexar/hostkit"
)

func seedUserVersion(t *testing.T, path string, version int) {
	t.Helper()
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec("PRAGMA user_version=" + strconv.Itoa(version)); err != nil {
		t.Fatal(err)
	}
}

func seedVersionDir(t *testing.T, root, version string) {
	t.Helper()
	dir := filepath.Join(root, versionsDir, version)
	if err := os.MkdirAll(filepath.Join(dir, "_internal"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, appName+".exe"), []byte("binary"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "_internal", "payload.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func mustWriteMarker(t *testing.T, dataDir string, marker map[string]any) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(markerPath(dataDir)), 0o700); err != nil {
		t.Fatal(err)
	}
	payload, _ := json.Marshal(marker)
	if err := os.WriteFile(markerPath(dataDir), payload, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestHuskResidueWithoutMarkerClearsDespitePointerDrift(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeCurrentState(root, "0.3.8-beta-rc2", "", nil); err != nil {
		t.Fatal(err)
	}
	if err := startTransaction(root, "0.3.8-beta-rc2", "0.3.8-beta-rc1", "husk"); err != nil {
		t.Fatal(err)
	}
	if err := updateTransaction(root, stageRolledBack, map[string]any{"dataDir": data}); err != nil {
		t.Fatal(err)
	}
	if err := recoverInterruptedUpdate(root); err != nil {
		t.Fatalf("残壳事务应直接清场：%v", err)
	}
	if tx := readTransaction(root); tx != nil {
		t.Fatal("残壳事务未被清场")
	}
}

func TestResidueWithLiveMarkerKeepsFailClosedOnPointerDrift(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeCurrentState(root, "0.3.8-beta-rc2", "", nil); err != nil {
		t.Fatal(err)
	}
	if err := startTransaction(root, "0.3.8-beta-rc2", "0.3.8-beta-rc1", "guarded"); err != nil {
		t.Fatal(err)
	}
	if err := updateTransaction(root, stageRolledBack, map[string]any{"dataDir": data}); err != nil {
		t.Fatal(err)
	}
	mustWriteMarker(t, data, map[string]any{
		"token": "t", "purpose": "update", "attemptId": "guarded",
		"installRoot": absPath(root), "dbPath": dbPathOf(data),
	})
	err := recoverInterruptedUpdate(root)
	if err == nil || !strings.Contains(err.Error(), "升级准备状态无法确认安全") {
		t.Fatalf("存疑事务应维持 fail-closed：%v", err)
	}
	tx := readTransaction(root)
	if tx == nil || txString(tx, "attemptId") != "guarded" {
		t.Fatal("存疑事务现场被误清")
	}
}

func TestReleasedResidueWithDriftedPointerClearsAndProceeds(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := writeCurrentState(root, "0.3.6", "", nil); err != nil {
		t.Fatal(err)
	}
	if err := startTransaction(root, "0.3.6", "0.3.5", "previous"); err != nil {
		t.Fatal(err)
	}
	if err := updateTransaction(root, stageRolledBack, map[string]any{
		"dataRollbackReleased": true, "dataRecoveryPhase": "restored",
		"dataDir": data, "dataDbPath": dbPathOf(data), "dataToken": "tok",
		"dataSchemaVersion": 17,
	}); err != nil {
		t.Fatal(err)
	}
	if err := recoverInterruptedUpdate(root); err != nil {
		t.Fatalf("已释放终态应清场放行：%v", err)
	}
	if tx := readTransaction(root); tx != nil {
		t.Fatal("已释放事务未被清场")
	}
}

func TestReleasedResidueWithIncompletePhaseStaysFailClosed(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := startTransaction(root, "0.3.6", "0.3.5", "previous"); err != nil {
		t.Fatal(err)
	}
	if err := updateTransaction(root, stageRolledBack, map[string]any{
		"dataRollbackReleased": true, "dataRecoveryPhase": "restoring",
		"dataDir": data, "dataDbPath": dbPathOf(data), "dataToken": "tok",
		"dataSchemaVersion": 17,
	}); err != nil {
		t.Fatal(err)
	}
	if err := recoverInterruptedUpdate(root); err == nil ||
		!strings.Contains(err.Error(), "更新恢复相位无法证明完成") {
		t.Fatalf("相位不完备应保持阻断：%v", err)
	}
	tx := readTransaction(root)
	if tx == nil || txString(tx, "attemptId") != "previous" {
		t.Fatal("阻断现场被误清")
	}
}

func TestAcceptedRecoveryClearsTransaction(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	seedUserVersion(t, dbPathOf(data), 17)
	seedVersionDir(t, root, "9.9.9")
	if err := startTransaction(root, "9.9.9", "0.3.5", "accepted"); err != nil {
		t.Fatal(err)
	}
	if err := updateTransaction(root, stageVerifying, map[string]any{
		"dataPrepared": true, "dataAccepted": true, "dataRecoveryPhase": "accepted",
		"dataDir": data, "dataDbPath": dbPathOf(data), "dataToken": "tok",
		"dataSchemaVersion": 17,
	}); err != nil {
		t.Fatal(err)
	}
	if err := recoverInterruptedUpdate(root); err != nil {
		t.Fatalf("已验收恢复应成功收尾：%v", err)
	}
	if tx := readTransaction(root); tx != nil {
		t.Fatal("已验收恢复收尾后事务未被清场")
	}
	state, err := readCurrentState(root)
	if err != nil || stateString(state, "version") != "9.9.9" {
		t.Fatalf("指针未落相容版本：%v %+v", err, state)
	}
}

func TestUnpreparedResidueRecoveryClearsAndAllowsUpgrade(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "app")
	staging := filepath.Join(base, "staging")
	data := filepath.Join(root, "data")
	payload := filepath.Join(staging, appName)
	if err := os.MkdirAll(filepath.Join(payload, "_internal"), 0o755); err != nil {
		t.Fatal(err)
	}
	// 安装根的 versions/ 目录由发行布局与启动链保证存在（换装安装的前提）
	if err := os.MkdirAll(filepath.Join(root, versionsDir), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	files := map[string]any{}
	for _, name := range []string{appName + ".exe", "_internal/payload.txt"} {
		body := []byte("binary:" + name)
		if err := os.WriteFile(filepath.Join(payload, filepath.FromSlash(name)), body, 0o644); err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(body)
		files[name] = hex.EncodeToString(sum[:])
	}
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	const keyID = "recover-test"
	restore := hostkit.SetTrustedPubkey(keyID, hex.EncodeToString(pub))
	t.Cleanup(restore)
	release := map[string]any{
		"schema":  json.Number("2"),
		"app":     appName,
		"version": "9.9.9",
		"tag":     "v9.9.9",
		"assets": []any{map[string]any{
			"role": "app", "name": appName + "-win64-v9.9.9.zip",
			"size": json.Number("1024"), "sha256": strings.Repeat("a", 64), "files": files,
		}},
	}
	sigPayload, _ := hostkit.CanonicalManifestBytes(release)
	release[hostkit.SignatureField] = map[string]any{
		"alg": "ed25519", "keyId": keyID,
		"sig": base64.StdEncoding.EncodeToString(ed25519.Sign(priv, sigPayload)),
	}
	manifest, _ := json.MarshalIndent(map[string]any{
		"tag": "v9.9.9", "ready": true, "source_version": "0.3.5", "files": files, "release_manifest": release,
	}, "", "  ")
	if err := os.WriteFile(filepath.Join(staging, "manifest.json"), manifest, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := writeCurrentState(root, "0.3.5", "", nil); err != nil {
		t.Fatal(err)
	}
	if err := startTransaction(root, "9.9.9", "0.3.5", "previous"); err != nil {
		t.Fatal(err)
	}
	if err := updateTransaction(root, stageSwitching, map[string]any{"dataDir": data}); err != nil {
		t.Fatal(err)
	}
	if !executeUpdateLocked(root, &options{staging: staging, skipHealthCheck: true, noLaunch: true}) {
		t.Fatal("残留恢复清场后升级被拦截")
	}
	if tx := readTransaction(root); tx != nil {
		t.Fatalf("提交终态后事务未被清场：%+v", tx)
	}
	state, err := readCurrentState(root)
	if err != nil || stateString(state, "version") != "9.9.9" {
		t.Fatalf("换装未完成指针提交：%v %+v", err, state)
	}
	if _, err := os.Stat(staging); !os.IsNotExist(err) {
		t.Fatal("暂存包未随提交清理")
	}
}

func TestReadMigrationSignalReportsVersionAndSnapshot(t *testing.T) {
	base := t.TempDir()
	empty := readMigrationSignal(filepath.Join(base, "no-data"))
	if empty.schemaKnown || empty.snapshot != -1 {
		t.Fatalf("空目录应无信号：%+v", empty)
	}
	data := filepath.Join(base, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	seedUserVersion(t, dbPathOf(data), 30)
	bak := filepath.Join(data, "holdexar.db.pre-migration.bak")
	if err := os.WriteFile(bak, bytesOfLength(128), 0o644); err != nil {
		t.Fatal(err)
	}
	signal := readMigrationSignal(data)
	if !signal.schemaKnown || signal.schema != 30 || signal.snapshot != 128 {
		t.Fatalf("信号读数不符：%+v", signal)
	}
}

func bytesOfLength(n int) []byte {
	body := make([]byte, n)
	for i := range body {
		body[i] = 'x'
	}
	return body
}

func TestMigrationWatchExtendsDeadlineOnSignalAdvance(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "app")
	data := filepath.Join(base, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	t0 := time.Now()
	watch := newMigrationWatch(t0, 400*time.Millisecond)
	if watch.observe(root, data, t0) {
		t.Fatal("无信号推进不得触发总上限")
	}
	if watch.deadline.After(t0.Add(400 * time.Millisecond)) {
		t.Fatal("无信号推进不得续期")
	}
	// 信号读取受 2s 轮询节流，合成时钟需跨节流窗才续期空闲窗。
	bak := filepath.Join(data, "holdexar.db.pre-migration.bak")
	if err := os.WriteFile(bak, bytesOfLength(8), 0o644); err != nil {
		t.Fatal(err)
	}
	firstAdvance := t0.Add(2 * time.Second)
	if watch.observe(root, data, firstAdvance) {
		t.Fatal("信号推进未超总预算时不得裁决回滚")
	}
	if !watch.deadline.After(t0.Add(migrationIdleWindow)) {
		t.Fatalf("信号推进未刷新空闲窗：%v", watch.deadline.Sub(t0))
	}
	if err := os.WriteFile(bak, bytesOfLength(16), 0o644); err != nil {
		t.Fatal(err)
	}
	secondAdvance := firstAdvance.Add(2 * time.Second)
	if watch.observe(root, data, secondAdvance) {
		t.Fatal("信号推进未超总预算时不得裁决回滚")
	}
	if watch.deadline.Before(secondAdvance.Add(migrationIdleWindow)) {
		t.Fatalf("空闲窗未随最新信号延长：%v", watch.deadline.Sub(t0))
	}
	if !watch.observe(root, data, t0.Add(migrationMaxDuration+time.Second)) {
		t.Fatal("总上限耗尽应裁决回滚")
	}
}

func TestStalledMigrationWatchKeepsOriginalDeadline(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "app")
	data := filepath.Join(base, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	t0 := time.Now()
	watch := newMigrationWatch(t0, 400*time.Millisecond)
	for _, step := range []time.Duration{0, time.Second, 10 * time.Second} {
		if watch.observe(root, data, t0.Add(step)) {
			t.Fatal("无信号推进不得触发总上限")
		}
	}
	if watch.deadline.After(t0.Add(400 * time.Millisecond)) {
		t.Fatalf("无信号推进不得延长验收窗：%v", watch.deadline.Sub(t0))
	}
}

func TestFailedProbeLeavesRootBinariesUntouchedAndCommitRefreshesRoot(t *testing.T) {
	priv := installTestTrustKeyUH(t, "commit-root-k")
	selfExe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	probeBytes, err := os.ReadFile(selfExe)
	if err != nil {
		t.Fatal(err)
	}

	root := t.TempDir()
	data := filepath.Join(root, "data")
	if err := os.MkdirAll(filepath.Join(root, "updater"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "web", "dist"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	seedVersionDir(t, root, "0.3.5")
	seedUserVersion(t, dbPathOf(data), 17)
	if err := writeCurrentState(root, "0.3.5", "0.3.5", nil); err != nil {
		t.Fatal(err)
	}
	origRootExe := []byte("original-root-launcher")
	origUpdaterExe := []byte("original-updater-host")
	if err := os.WriteFile(filepath.Join(root, appName+".exe"), origRootExe, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "updater", "UpdateHost.exe"), origUpdaterExe, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "web", "dist", "index.html"), []byte("legacy-root-dist"), 0o644); err != nil {
		t.Fatal(err)
	}

	staging := t.TempDir()
	payload := filepath.Join(staging, appName)
	newRootExe := []byte("new-root-launcher-v036")
	newUpdaterExe := []byte("new-updater-host-v036")
	newClashLicense := []byte("GPL-3.0-license-v036")
	filesToWrite := map[string][]byte{
		appName + ".exe":                     newRootExe,
		"updater/UpdateHost.exe":             newUpdaterExe,
		"assets/clash/LICENSE-mihomo.txt":    newClashLicense,
		"versions/0.3.6/" + appName + ".exe": probeBytes,
		"versions/0.3.6/serverd.exe":         []byte("MZ-serverd"),
		"versions/0.3.6/web/dist/index.html": []byte("<html>v036</html>"),
	}
	filesHashes := map[string]any{}
	for rel, content := range filesToWrite {
		full := filepath.Join(payload, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(full, content, 0o755); err != nil {
			t.Fatal(err)
		}
		filesHashes[rel] = sha256BytesHex(content)
	}
	release := signReleaseManifestUH(t, priv, "commit-root-k", map[string]any{
		"schema":  json.Number("2"),
		"app":     appName,
		"version": "0.3.6",
		"tag":     "v0.3.6",
		"assets": []any{map[string]any{
			"role":   "app",
			"name":   appName + "-win64-v0.3.6.zip",
			"size":   json.Number("4096"),
			"sha256": strings.Repeat("b", 64),
			"files":  filesHashes,
		}},
	})
	writeManifestFileUH(t, staging, map[string]any{
		"tag":              "v0.3.6",
		"ready":            true,
		"attempt_id":       "att-probe-fail",
		"source_version":   "0.3.5",
		"files":            filesHashes,
		"release_manifest": release,
	})

	t.Setenv("HOLDEXAR_UH_TEST_PROBE_FAIL", "1")
	ok := executeUpdateLocked(root, &options{
		staging:       staging,
		targetVersion: "0.3.6",
		sourceVersion: "0.3.5",
		dataDir:       data,
		noLaunch:      true,
	})
	if ok {
		t.Fatal("expected executeUpdateLocked to fail when probeVersionHealth fails")
	}

	gotRootExe, err := os.ReadFile(filepath.Join(root, appName+".exe"))
	if err != nil || string(gotRootExe) != string(origRootExe) {
		t.Fatalf("failed probeVersionHealth overwrote <root>/Holdexar.exe: got %q, want %q", string(gotRootExe), string(origRootExe))
	}
	gotUpdaterExe, err := os.ReadFile(filepath.Join(root, "updater", "UpdateHost.exe"))
	if err != nil || string(gotUpdaterExe) != string(origUpdaterExe) {
		t.Fatalf("failed probeVersionHealth overwrote <root>/updater/UpdateHost.exe: got %q, want %q", string(gotUpdaterExe), string(origUpdaterExe))
	}
	if _, err := os.Stat(filepath.Join(root, "web", "dist", "index.html")); err != nil {
		t.Fatalf("failed probeVersionHealth deleted legacy <root>/web/dist before commit: %v", err)
	}

	// Now verify commit() refreshes root binaries, clash assets, and removes legacy <root>/web/dist.
	_ = os.RemoveAll(data)
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	_ = writeCurrentState(root, "0.3.5", "0.3.5", nil)
	_ = os.RemoveAll(filepath.Join(root, versionsDir, "0.3.6"))
	clearTransaction(root)
	if !executeUpdateLocked(root, &options{
		staging:         staging,
		targetVersion:   "0.3.6",
		sourceVersion:   "0.3.5",
		dataDir:         data,
		skipHealthCheck: true,
		noLaunch:        true,
	}) {
		t.Fatal("expected commit path to succeed on clean DB-less upgrade")
	}
	gotRootExe, err = os.ReadFile(filepath.Join(root, appName+".exe"))
	if err != nil || string(gotRootExe) != string(newRootExe) {
		t.Fatalf("commit did not update <root>/Holdexar.exe: got %q, want %q", string(gotRootExe), string(newRootExe))
	}
	gotUpdaterExe, err = os.ReadFile(filepath.Join(root, "updater", "UpdateHost.exe"))
	if err != nil || string(gotUpdaterExe) != string(newUpdaterExe) {
		t.Fatalf("commit did not update <root>/updater/UpdateHost.exe: got %q, want %q", string(gotUpdaterExe), string(newUpdaterExe))
	}
	gotClash, err := os.ReadFile(filepath.Join(root, "assets", "clash", "LICENSE-mihomo.txt"))
	if err != nil || string(gotClash) != string(newClashLicense) {
		t.Fatalf("commit did not update <root>/assets/clash: got %q, err %v", string(gotClash), err)
	}
	if _, err := os.Stat(filepath.Join(root, "web", "dist")); !os.IsNotExist(err) {
		t.Fatal("commit did not remove legacy <root>/web/dist directory")
	}
}
