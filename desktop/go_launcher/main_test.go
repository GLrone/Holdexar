package main

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func put(t *testing.T, path string, content []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, content, 0600); err != nil {
		t.Fatal(err)
	}
}
func dbFixture(t *testing.T, path string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	db, err := sql.Open("sqlite", path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err = db.Exec("PRAGMA user_version=32; CREATE TABLE facts(value TEXT); INSERT INTO facts VALUES ('preserved')"); err != nil {
		t.Fatal(err)
	}
}
func linkDir(t *testing.T, link, target string) {
	t.Helper()
	if out, err := exec.Command("cmd.exe", "/c", "mklink", "/J", link, target).CombinedOutput(); err != nil {
		t.Fatalf("junction: %v %s", err, out)
	}
	t.Cleanup(func() { os.Remove(link) })
}
func TestVersionRootRedirect(t *testing.T) {
	root, out := t.TempDir(), t.TempDir()
	put(t, filepath.Join(out, "0.3.8", "Holdexar.exe"), []byte("fixture"))
	linkDir(t, filepath.Join(root, "versions"), out)
	if isVersionValid(root, "0.3.8") {
		t.Fatal("redirected versions root accepted")
	}
}
func TestManagedNameRejectsWhitespace(t *testing.T) {
	if isManagedVersionName("0.3.8 ") {
		t.Fatal("trailing space admitted")
	}
}
func TestCompatibilityShape(t *testing.T) {
	for _, raw := range []string{`null`, `{"dataDir":"E:\\data","versions":["0.3.8"]}`, `{"dataDir":"E:\\data","schemaVersion":false,"versions":["0.3.8"]}`} {
		t.Run(raw, func(t *testing.T) {
			root := t.TempDir()
			put(t, filepath.Join(root, "state", "current.json"), []byte(`{"version":"0.3.8","dataCompatibility":`+raw+`}`))
			if _, err := readDataCompatibility(root); err == nil {
				t.Fatal("invalid compatibility accepted")
			}
		})
	}
}
func TestMigrationPartialIsNotSuccess(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	dbFixture(t, filepath.Join(src, dbName))
	put(t, filepath.Join(src, "secret.master"), []byte("source key"))
	if err := os.Mkdir(filepath.Join(dst, "secret.master"), 0755); err != nil {
		t.Fatal(err)
	}
	ok, _, err := adoptSourceDatabase(src, dst)
	if ok || err == nil {
		t.Fatal("partial migration reported successful")
	}
	if readMigrationMeta(dst)["completed"] != false {
		t.Fatal("incomplete fact not recorded")
	}
}
func TestMigrationMixedCaseKeys(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	dbFixture(t, filepath.Join(src, dbName))
	put(t, filepath.Join(src, "SECRET.MASTER"), []byte("source key"))
	ok, _, err := adoptSourceDatabase(src, dst)
	if err != nil || !ok {
		t.Fatalf("migration: %v", err)
	}
	b, err := os.ReadFile(filepath.Join(dst, "secret.master"))
	if err != nil || string(b) != "source key" {
		t.Fatal("mixed case key missing")
	}
}
func TestMigrationResumeRequiredMissing(t *testing.T) {
	src, dst := t.TempDir(), t.TempDir()
	dbFixture(t, filepath.Join(src, dbName))
	dbFixture(t, filepath.Join(dst, dbName))
	meta := map[string]any{"status": "incomplete", "completed": false, "sourceDir": resolvePath(src), "targetDir": resolvePath(dst), "database": dbName, "requiredKeys": []string{"secret.master"}, "requiredDirs": []string{"config"}}
	b, _ := json.Marshal(meta)
	put(t, filepath.Join(dst, "migration.json"), b)
	ok, err := resolveDataDirConflict(dst, src)
	if ok || err == nil {
		t.Fatal("required files washed into completed")
	}
}
func TestMigrationJunctionAttachment(t *testing.T) {
	src, dst, out := t.TempDir(), t.TempDir(), t.TempDir()
	dbFixture(t, filepath.Join(src, dbName))
	put(t, filepath.Join(out, "settings.json"), []byte("external"))
	linkDir(t, filepath.Join(src, "config"), out)
	ok, _, err := adoptSourceDatabase(src, dst)
	if ok || err == nil {
		t.Fatal("junction attachment migration succeeded")
	}
}

func TestMain(m *testing.M) {
	if mode := os.Getenv("HOLDEXAR_GO_TEST_FIXTURE"); mode != "" {
		if mode == "fatal" {
			showFatal("fixture startup failure")
			os.Exit(1)
		}
		cwd, _ := os.Getwd()
		payload, _ := json.Marshal(map[string]any{"args": os.Args[1:], "root": os.Getenv("HOLDEXAR_INSTALL_ROOT"), "data": os.Getenv("HOLDEXAR_DATA_DIR"), "cwd": cwd, "stale": os.Getenv("HOLDEXAR_DEV_URL"), "inherited": os.Getenv("HOLDEXAR_TEST_INHERITED")})
		if err := os.WriteFile(os.Getenv("HOLDEXAR_GO_TEST_OBSERVED"), payload, 0600); err != nil {
			os.Exit(98)
		}
		if mode == "recovery" {
			if os.Getenv("HOLDEXAR_GO_TEST_FAIL") == "1" {
				os.Exit(1)
			}
			os.Exit(0)
		}
		os.Exit(37)
	}
	os.Exit(m.Run())
}
func executableFixture(t *testing.T, path string) {
	t.Helper()
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(exe)
	if err != nil {
		t.Fatal(err)
	}
	put(t, path, b)
}
func TestProtocolHasNoStartupSideEffects(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "protocol.json")
	t.Setenv("HOLDEXAR_UPDATE_PROTOCOL_RESULT", path)
	handled, err := reportProtocol([]string{"--update-protocol"})
	if !handled || err != nil {
		t.Fatal(handled, err)
	}
	b, err := os.ReadFile(path)
	if err != nil || string(b) != `{"updateDataProtocol":1}` {
		t.Fatal(string(b), err)
	}
}
func TestRootIgnoresInheritedOverride(t *testing.T) {
	t.Setenv("HOLDEXAR_INSTALL_ROOT", t.TempDir())
	root, err := launcherRoot()
	if err != nil {
		t.Fatal(err)
	}
	exe, _ := os.Executable()
	if !samePath(root, filepath.Dir(resolvePath(exe))) {
		t.Fatal(root)
	}
}
func TestRecoveryDataFactsRequireExistingHost(t *testing.T) {
	for _, tx := range []map[string]any{{"dataPrepared": true}, {"dataAccepted": true, "stage": "committed"}, {"dataRollbackReleased": true, "stage": "rolled_back"}, {"dataDir": `E:\\sandbox`}} {
		root := t.TempDir()
		tx["attemptId"] = "fixture-recovery"
		b, _ := json.Marshal(tx)
		put(t, filepath.Join(root, "state", "transaction.json"), b)
		if err := recoverInterruptedUpdate(root); err == nil {
			t.Fatalf("missing host accepted: %v", tx)
		}
	}
}
func TestRecoveryHandoff(t *testing.T) {
	root := t.TempDir()
	executableFixture(t, filepath.Join(root, "updater", "UpdateHost.exe"))
	observed := filepath.Join(root, "observed.json")
	t.Setenv("HOLDEXAR_GO_TEST_FIXTURE", "recovery")
	t.Setenv("HOLDEXAR_GO_TEST_OBSERVED", observed)
	put(t, filepath.Join(root, "state", "transaction.json"), []byte(`{"attemptId":"fixture-recovery","dataAccepted":true,"stage":"committed"}`))
	if err := recoverInterruptedUpdate(root); err != nil {
		t.Fatal(err)
	}
	b, _ := os.ReadFile(observed)
	var record map[string]any
	if err := json.Unmarshal(b, &record); err != nil {
		t.Fatal(err)
	}
	args := record["args"].([]any)
	if len(args) != 3 || args[0] != "--recover-only" || args[1] != "--target" || args[2] != root {
		t.Fatal(args)
	}
	t.Setenv("HOLDEXAR_GO_TEST_FAIL", "1")
	if err := recoverInterruptedUpdate(root); err == nil {
		t.Fatal("recovery failure ignored")
	}
}
func TestRecoveryRejectsRedirectedHost(t *testing.T) {
	root, outside := t.TempDir(), t.TempDir()
	executableFixture(t, filepath.Join(outside, "UpdateHost.exe"))
	linkDir(t, filepath.Join(root, "updater"), outside)
	put(t, filepath.Join(root, "state", "transaction.json"), []byte(`{"attemptId":"fixture-recovery","dataPrepared":true}`))
	if err := recoverInterruptedUpdate(root); err == nil {
		t.Fatal("redirected updater accepted")
	}
}
func TestVersionEnvironmentAndExit(t *testing.T) {
	root := t.TempDir()
	directory := filepath.Join(root, "versions", "0.3.8")
	executableFixture(t, filepath.Join(directory, "Holdexar.exe"))
	observed := filepath.Join(root, "observed.json")
	t.Setenv("HOLDEXAR_GO_TEST_FIXTURE", "version")
	t.Setenv("HOLDEXAR_GO_TEST_OBSERVED", observed)
	t.Setenv("HOLDEXAR_DEV_URL", "untrusted")
	t.Setenv("HOLDEXAR_INSTALL_ROOT", "untrusted")
	t.Setenv("HOLDEXAR_TEST_INHERITED", "中文 preserved")
	args := []string{"--server", "", "two words", "中文", `a\"b`, `C:\space path\`}
	code, err := launchVersion(root, "0.3.8", args)
	if err != nil || code != 37 {
		t.Fatal(code, err)
	}
	b, _ := os.ReadFile(observed)
	var record map[string]any
	json.Unmarshal(b, &record)
	if record["stale"] != "" || record["root"] != root || record["data"] != filepath.Join(root, "data") || !samePath(record["cwd"].(string), directory) || record["inherited"] != "中文 preserved" {
		t.Fatal(record)
	}
	got := record["args"].([]any)
	if len(got) != len(args) {
		t.Fatal(got)
	}
	for i, arg := range args {
		if got[i] != arg {
			t.Fatal(got)
		}
	}
}

func TestExistingInvalidPointerNeverFallsThrough(t *testing.T) {
	for _, raw := range []string{`{}`, `null`, `[]`, `{"version":""}`, `{"version":32}`, `{"version":false}`, `{"version":"0.3.8"} {}`} {
		t.Run(raw, func(t *testing.T) {
			root := t.TempDir()
			put(t, filepath.Join(root, "state", "current.json"), []byte(raw))
			put(t, filepath.Join(root, "versions", "0.3.8", "Holdexar.exe"), []byte("fixture"))
			if _, err := readDataCompatibility(root); err == nil {
				t.Fatal("existing invalid pointer accepted")
			}
		})
	}
}

func TestCompatibilityNumberLexemes(t *testing.T) {
	root := t.TempDir()
	put(t, filepath.Join(root, "state", "current.json"), []byte(`{"version":"0.3.8","dataCompatibility":{"dataDir":"E:\\data","schemaVersion":32.0,"versions":["0.3.8"]}}`))
	if _, err := readDataCompatibility(root); err == nil {
		t.Fatal("schema32.0 accepted as integer")
	}
}
func TestFallbackPreservesNestedLargeInteger(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "state", "current.json")
	put(t, path, []byte(`{"version":"0.3.8","dataCompatibility":{"dataDir":"E:\\data","schemaVersion":32,"versions":["0.3.8","0.3.7"],"extension":{"number":9007199254740993}}}`))
	if _, err := writeCurrentState(root, "0.3.7", nil); err != nil {
		t.Fatal(err)
	}
	b, err := os.ReadFile(path)
	if err != nil || !strings.Contains(string(b), "9007199254740993") {
		t.Fatal(string(b), err)
	}
}
func TestServerFailureWithoutDialogEnvironment(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "Holdexar.exe")
	executableFixture(t, path)
	cmd := exec.Command(path, "--server")
	env := []string{}
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		if !strings.EqualFold(key, "CI") && !strings.EqualFold(key, "HOLDEXAR_NO_DIALOG") {
			env = append(env, entry)
		}
	}
	cmd.Env = append(env, "HOLDEXAR_GO_TEST_FIXTURE=fatal")
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	cmd = exec.CommandContext(ctx, path, "--server")
	cmd.Env = append(env, "HOLDEXAR_GO_TEST_FIXTURE=fatal")
	out, err := cmd.CombinedOutput()
	if ctx.Err() != nil {
		t.Fatal("headless error attempted a blocking dialog")
	}
	if err == nil || !strings.Contains(string(out), "fixture startup failure") {
		t.Fatal(string(out), err)
	}
}

func TestHeadlessFatalDecision(t *testing.T) {
	t.Setenv("CI", "")
	t.Setenv("HOLDEXAR_NO_DIALOG", "")
	if showFatalDialog([]string{"--server"}) {
		t.Fatal("headless error allowed modal")
	}
}

func TestCompatibilityAliasesCannotChangeFacts(t *testing.T) {
	for _, aliases := range []string{`"datadir":"E:\\unaccepted","schemaversion":99,"VERSIONS":["0.3.7"]`, `"DataDir":"E:\\unaccepted","SchemaVersion":99,"Versions":["0.3.7"]`, `"DATADIR":"E:\\unaccepted","SCHEMAVERSION":99,"VERSIONS":["0.3.7"]`} {
		root := t.TempDir()
		raw := `{"version":"0.3.8","dataCompatibility":{"dataDir":"E:\\accepted","schemaVersion":32,"versions":["0.3.8"],` + aliases + `}}`
		put(t, filepath.Join(root, "state", "current.json"), []byte(raw))
		compat, err := readDataCompatibility(root)
		if err != nil {
			t.Fatal(err)
		}
		if !samePath(compat.DataDir, `E:\accepted`) || compat.SchemaVersion != 32 || len(compat.Versions) != 1 || compat.Versions[0] != "0.3.8" {
			t.Fatalf("aliases changed accepted facts: %+v", compat)
		}
		if len(compat.Extra) != 3 {
			t.Fatalf("case-sensitive extensions lost: %+v", compat.Extra)
		}
	}
}

func completedTransaction(t *testing.T, root string) map[string]any {
	data := filepath.Join(root, "data")
	dbFixture(t, filepath.Join(data, dbName))
	state := map[string]any{"version": "0.3.8", "attemptId": "terminal-attempt", "dataCompatibility": map[string]any{"dataDir": data, "schemaVersion": 32, "versions": []string{"0.3.8"}}}
	b, _ := json.Marshal(state)
	put(t, filepath.Join(root, "state", "current.json"), b)
	return map[string]any{"stage": "committed", "attemptId": "terminal-attempt", "targetVersion": "0.3.8", "sourceVersion": "0.3.7", "currentVersion": "0.3.8", "dataPrepared": true, "dataAccepted": true, "dataRecoveryPhase": "accepted", "dataSchemaVersion": 32, "launchPending": false, "dataDir": data, "dataDbPath": filepath.Join(data, dbName)}
}
func TestTerminalTransactionsNeedNoHost(t *testing.T) {
	for _, rollback := range []bool{false, true} {
		root := t.TempDir()
		tx := completedTransaction(t, root)
		if rollback {
			tx["stage"] = "rolled_back"
			tx["dataAccepted"] = false
			tx["dataRollbackReleased"] = true
			tx["dataRecoveryPhase"] = "restored"
			tx["sourceVersion"] = "0.3.8"
			tx["targetVersion"] = "0.3.9"
		}
		b, _ := json.Marshal(tx)
		put(t, filepath.Join(root, "state", "transaction.json"), b)
		if err := recoverInterruptedUpdate(root); err != nil {
			t.Fatalf("completed transaction launched missing host: %v", err)
		}
	}
}

func TestCompletedUpdateOptionalFlags(t *testing.T) {
	for _, flag := range []string{"missing", "null", "false", "true", "string"} {
		for _, rollback := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/rollback-%t", flag, rollback), func(t *testing.T) {
				root := t.TempDir()
				tx := completedTransaction(t, root)
				switch flag {
				case "missing":
					delete(tx, "launchPending")
				case "null":
					tx["launchPending"] = nil
				case "true":
					tx["launchPending"] = true
				case "string":
					tx["launchPending"] = "false"
				}
				if rollback {
					tx["stage"], tx["dataAccepted"], tx["dataRollbackReleased"] = "rolled_back", nil, true
					tx["dataRecoveryPhase"], tx["sourceVersion"] = "restored", "0.3.8"
				}
				state, _ := readCurrentState(root)
				state.Raw["pendingVersion"] = nil
				state.Raw["attemptId"] = nil
				b, _ := json.Marshal(state.Raw)
				put(t, filepath.Join(root, "state", "current.json"), b)
				b, _ = json.Marshal(tx)
				put(t, filepath.Join(root, "state", "transaction.json"), b)
				err := recoverInterruptedUpdate(root)
				wantHost := flag == "true" || flag == "string"
				if (err != nil) != wantHost {
					t.Fatalf("host needed=%t, got %v", wantHost, err)
				}
			})
		}
	}
}

func TestPreparationAbortNeedsNoRepeatedHost(t *testing.T) {
	for _, stage := range []string{"switching", "rolled_back"} {
		for _, fault := range []string{"clean", "marker", "prepared", "accepted", "launch", "children", "owner", "source", "target"} {
			t.Run(stage+"/"+fault, func(t *testing.T) {
				root := t.TempDir()
				completedTransaction(t, root) // The pointer still records the preceding successful attempt.
				tx := map[string]any{"attemptId": "new-aborted-attempt", "stage": stage,
					"sourceVersion": "0.3.8", "targetVersion": "0.3.9", "dataDir": filepath.Join(root, "data")}
				switch fault {
				case "marker":
					put(t, filepath.Join(root, "data", "backups", ".restore.lock"), []byte(`{"purpose":"update"}`))
				case "prepared":
					tx["dataPrepared"] = true
				case "accepted":
					tx["dataAccepted"] = true
				case "launch":
					tx["launchPending"] = true
				case "children":
					tx["children"] = []any{map[string]any{"pid": 123, "stopped": true}}
				case "owner":
					tx["dataDir"] = t.TempDir()
				case "source":
					tx["sourceVersion"] = "0.3.7"
				case "target":
					tx["targetVersion"] = "0.3.8"
				}
				b, _ := json.Marshal(tx)
				put(t, filepath.Join(root, "state", "transaction.json"), b)
				err := recoverInterruptedUpdate(root)
				if (err != nil) != (fault != "clean") {
					t.Fatalf("fault=%s, got %v", fault, err)
				}
			})
		}
	}
}

func TestTerminalTransactionsWithUnfinishedProtectionNeedHost(t *testing.T) {
	for _, change := range []string{"restore-marker", "data-dir", "database-path", "pending-version", "launch-pending", "phase", "not-prepared", "schema", "schema-lexeme", "current-version", "compatibility", "attempt", "release-type", "rollback-marker", "rollback-phase", "rollback-accepted", "rollback-accepted-type", "rollback-external-no-compat"} {
		t.Run(change, func(t *testing.T) {
			root := t.TempDir()
			tx := completedTransaction(t, root)
			state, _ := readCurrentState(root)
			if strings.HasPrefix(change, "rollback-") {
				tx["stage"], tx["dataAccepted"], tx["dataRollbackReleased"] = "rolled_back", false, true
				tx["dataRecoveryPhase"], tx["sourceVersion"] = "restored", state.Version
			}
			switch change {
			case "restore-marker", "rollback-marker":
				put(t, filepath.Join(root, "data", "backups", ".restore.lock"), []byte(`{"purpose":"update"}`))
			case "data-dir":
				tx["dataDir"] = filepath.Join(root, "other-data")
			case "database-path":
				tx["dataDbPath"] = filepath.Join(root, "data", "other.db")
			case "pending-version":
				pending := state.Version
				state.PendingVersion = &pending
			case "launch-pending":
				tx["launchPending"] = true
			case "phase", "rollback-phase":
				tx["dataRecoveryPhase"] = "prepared"
			case "not-prepared":
				tx["dataPrepared"] = false
			case "schema":
				tx["dataSchemaVersion"] = 33
			case "schema-lexeme":
				tx["dataSchemaVersion"] = json.RawMessage(`32.0`)
			case "current-version":
				tx["currentVersion"] = "0.3.7"
			case "compatibility":
				state.DataCompatibility.Versions = []string{"0.3.7"}
			case "attempt":
				other := "other-attempt"
				state.AttemptID = &other
			case "release-type":
				tx["dataRollbackReleased"] = "false"
			case "rollback-accepted":
				tx["dataAccepted"] = true
			case "rollback-accepted-type":
				tx["dataAccepted"] = "false"
			case "rollback-external-no-compat":
				state.DataCompatibility = nil
				delete(state.Raw, "dataCompatibility")
				tx["dataDir"] = t.TempDir()
				tx["dataDbPath"] = filepath.Join(tx["dataDir"].(string), dbName)
			}
			b, _ := json.Marshal(state)
			put(t, filepath.Join(root, "state", "current.json"), b)
			b, _ = json.Marshal(tx)
			put(t, filepath.Join(root, "state", "transaction.json"), b)
			if err := recoverInterruptedUpdate(root); err == nil {
				t.Fatal("unfinished or conflicting terminal state bypassed the missing recovery host")
			}
		})
	}
}

func TestUnreadableTransactionCannotBecomeNoRecovery(t *testing.T) {
	for _, raw := range []string{`null`, `[]`, `{}`, `{"attemptId":false}`, `{"attemptId":""}`, `{"attemptId":"a"} trailing`} {
		root := t.TempDir()
		put(t, filepath.Join(root, "state", "transaction.json"), []byte(raw))
		if err := recoverInterruptedUpdate(root); err == nil {
			t.Fatalf("invalid update record bypassed protection: %s", raw)
		}
	}
}

func TestCompletedAcceptedUpdateStillChecksLiveSchema(t *testing.T) {
	root := t.TempDir()
	tx := completedTransaction(t, root)
	b, _ := json.Marshal(tx)
	put(t, filepath.Join(root, "state", "transaction.json"), b)
	db, err := sql.Open("sqlite", filepath.Join(root, "data", dbName))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec("PRAGMA user_version=33"); err != nil {
		t.Fatal(err)
	}
	db.Close()
	if _, err := runLauncher([]string{"--server"}, root); err == nil || !strings.Contains(err.Error(), "数据结构已变化") {
		t.Fatalf("completed update bypassed live schema check: %v", err)
	}
}

func TestTerminalRollbackAllowsLegacyCompatibility(t *testing.T) {
	root := t.TempDir()
	tx := completedTransaction(t, root)
	tx["stage"], tx["dataAccepted"], tx["dataRollbackReleased"] = "rolled_back", false, true
	tx["dataRecoveryPhase"], tx["sourceVersion"] = "restored", "0.3.8"
	put(t, filepath.Join(root, "state", "current.json"), []byte(`{"version":"0.3.8"}`))
	b, _ := json.Marshal(tx)
	put(t, filepath.Join(root, "state", "transaction.json"), b)
	if err := recoverInterruptedUpdate(root); err != nil {
		t.Fatal(err)
	}
}

func TestCommittedCompatibilityFallbackNeedsNoHost(t *testing.T) {
	root := t.TempDir()
	tx := completedTransaction(t, root)
	state, _ := readCurrentState(root)
	state.Version = "0.3.7"
	state.DataCompatibility.Versions = append(state.DataCompatibility.Versions, state.Version)
	tx["currentVersion"] = state.Version
	b, _ := json.Marshal(state)
	put(t, filepath.Join(root, "state", "current.json"), b)
	b, _ = json.Marshal(tx)
	put(t, filepath.Join(root, "state", "transaction.json"), b)
	if err := recoverInterruptedUpdate(root); err != nil {
		t.Fatalf("already confirmed compatible fallback launched recovery host: %v", err)
	}
}
