//go:build windows

// recover_test.go 恢复器残壳区分、终态即清事务、升级资格裁决与迁移分阶段计时的语义测试。
package main

import (
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// seedUserVersion 建立带指定结构版本的占位数据库（迁移信号与相容记录用）。
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

// seedVersionDir 建立可被 isVersionValid 认可的版本目录。
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

// TestHuskResidueWithoutMarkerClearsDespitePointerDrift 无准备标记的残壳事务
// （数据从未进入保护）+指针漂移=直接清场，不走 fail-closed 中止。
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

// TestResidueWithLiveMarkerKeepsFailClosedOnPointerDrift 有准备标记且指针漂移=
// 保护状态存疑：维持阻断，保留现场。
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

// TestReleasedResidueWithDriftedPointerClearsAndProceeds 终态已释放+指针漂移=
// 事务已完结，清场放行（指针归属由启动链相容校验管）。
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

// TestReleasedResidueWithIncompletePhaseStaysFailClosed 相位无法证明恢复完整
// 时保持阻断（保留现场）。
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

// TestAcceptedRecoveryClearsTransaction 已验收恢复收尾（指针与相容记录落
// current.json）后必须清事务。
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

// TestUnpreparedResidueRecoveryClearsAndAllowsUpgrade 未准备残留恢复清场后
// 升级资格按最新事务裁决（事务已清场即放行），完整换装走到提交终态并清事务。
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
	for _, name := range []string{appName + ".exe", filepath.Join("_internal", "payload.txt")} {
		body := []byte("binary:" + name)
		if err := os.WriteFile(filepath.Join(payload, filepath.FromSlash(name)), body, 0o644); err != nil {
			t.Fatal(err)
		}
		sum := sha256.Sum256(body)
		files[name] = hex.EncodeToString(sum[:])
	}
	manifest, _ := json.MarshalIndent(map[string]any{
		"tag": "v9.9.9", "ready": true, "source_version": "0.3.5", "files": files,
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

// TestReadMigrationSignalReportsVersionAndSnapshot 迁移存活信号读数：
// 库结构版本 + 迁移前快照字节数；读不到按无信号处理。
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

// TestMigrationWatchExtendsDeadlineOnSignalAdvance 迁移信号推进刷新空闲窗
// （只延长不缩短），总上限兜底裁决回滚。
func TestMigrationWatchExtendsDeadlineOnSignalAdvance(t *testing.T) {
	base := t.TempDir()
	root := filepath.Join(base, "app")
	data := filepath.Join(base, "data")
	if err := os.MkdirAll(data, 0o755); err != nil {
		t.Fatal(err)
	}
	t0 := time.Now()
	watch := newMigrationWatch(t0, 400*time.Millisecond)
	// 首轮无信号：空闲窗维持原验收窗
	if watch.observe(root, data, t0) {
		t.Fatal("无信号推进不得触发总上限")
	}
	if watch.deadline.After(t0.Add(400 * time.Millisecond)) {
		t.Fatal("无信号推进不得续期")
	}
	// 快照出现=信号推进（信号读取受轮询间隔节流，合成时钟按节流窗排布）：
	// 空闲窗延至 now+迁移空闲窗
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
	// 持续推进：空闲窗随最新信号继续延长
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
	// 越过迁移总上限：裁决回滚
	if !watch.observe(root, data, t0.Add(migrationMaxDuration+time.Second)) {
		t.Fatal("总上限耗尽应裁决回滚")
	}
}

// TestStalledMigrationWatchKeepsOriginalDeadline 无迁移信号推进时不得凭进程
// 存在续期：验收窗维持原值。
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
