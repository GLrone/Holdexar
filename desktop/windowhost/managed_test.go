//go:build windows

package main

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
	"holdexar/hostkit"
)

func put(t *testing.T, path string, content []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, content, 0o600); err != nil {
		t.Fatal(err)
	}
}

func makeSQLitePage1(pageSize int, userVersion int32) []byte {
	page := make([]byte, pageSize)
	copy(page[0:16], []byte("SQLite format 3\x00"))
	binary.BigEndian.PutUint16(page[16:18], uint16(pageSize))
	page[18] = 2
	page[19] = 2
	page[21] = 64
	page[22] = 32
	page[23] = 32
	binary.BigEndian.PutUint32(page[28:32], 1)
	binary.BigEndian.PutUint32(page[60:64], uint32(userVersion))
	return page
}

func dbFixture(t *testing.T, path string) {
	t.Helper()
	put(t, path, makeSQLitePage1(4096, 32))
}

func linkDir(t *testing.T, link, target string) {
	t.Helper()
	if out, err := exec.Command("cmd.exe", "/c", "mklink", "/J", link, target).CombinedOutput(); err != nil {
		t.Fatalf("junction: %v %s", err, out)
	}
	t.Cleanup(func() { _ = os.Remove(link) })
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

func TestMain(m *testing.M) {
	if handled, err := reportProtocol(os.Args[1:]); handled {
		if err != nil {
			os.Exit(1)
		}
		os.Exit(0)
	}
	if mode := os.Getenv("HOLDEXAR_GO_TEST_FIXTURE"); mode != "" {
		if mode == "fatal" {
			showFatal("fixture startup failure")
			os.Exit(1)
		}
		if mode == "noop" {
			os.Exit(0)
		}
		cwd, _ := os.Getwd()
		payload, _ := json.Marshal(map[string]any{
			"args":      os.Args[1:],
			"root":      os.Getenv("HOLDEXAR_INSTALL_ROOT"),
			"data":      os.Getenv("HOLDEXAR_DATA_DIR"),
			"cwd":       cwd,
			"stale":     os.Getenv("HOLDEXAR_DEV_URL"),
			"inherited": os.Getenv("HOLDEXAR_TEST_INHERITED"),
		})
		if err := os.WriteFile(os.Getenv("HOLDEXAR_GO_TEST_OBSERVED"), payload, 0o600); err != nil {
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

func TestVersionRootRedirect(t *testing.T) {
	root, out := t.TempDir(), t.TempDir()
	put(t, filepath.Join(out, "0.3.8", "Holdexar.exe"), []byte("fixture"))
	linkDir(t, filepath.Join(root, "versions"), out)
	if hostkit.IsVersionValid(root, "0.3.8") {
		t.Fatal("redirected versions root accepted")
	}
}

func TestManagedNameRejectsWhitespace(t *testing.T) {
	if hostkit.IsManagedVersionName("0.3.8 ") {
		t.Fatal("trailing space admitted")
	}
}

func TestCompatibilityShape(t *testing.T) {
	for _, raw := range []string{
		`null`,
		`{"dataDir":"E:\\data","versions":["0.3.8"]}`,
		`{"dataDir":"E:\\data","schemaVersion":false,"versions":["0.3.8"]}`,
	} {
		t.Run(raw, func(t *testing.T) {
			root := t.TempDir()
			put(t, filepath.Join(root, "state", "current.json"), []byte(`{"version":"0.3.8","dataCompatibility":`+raw+`}`))
			if _, err := hostkit.ReadDataCompatibility(root); err == nil {
				t.Fatal("invalid compatibility accepted")
			}
		})
	}
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
	if !hostkit.SamePath(root, filepath.Dir(hostkit.CanonicalPath(exe))) {
		t.Fatal(root)
	}
}

func TestRecoveryDataFactsRequireExistingHost(t *testing.T) {
	for _, tx := range []map[string]any{
		{"dataPrepared": true},
		{"dataAccepted": true, "stage": "committed"},
		{"dataRollbackReleased": true, "stage": "rolled_back"},
		{"dataDir": `E:\\sandbox`},
	} {
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
	t.Setenv("HOLDEXAR_DATA_DIR", "")
	t.Setenv("HOLDEXAR_TEST_INHERITED", "中文 preserved")
	args := []string{"--server", "", "two words", "中文", `a\"b`, `C:\space path\`}
	code, err := launchVersion(root, "0.3.8", args)
	if err != nil || code != 37 {
		t.Fatal(code, err)
	}
	b, _ := os.ReadFile(observed)
	var record map[string]any
	_ = json.Unmarshal(b, &record)
	if record["stale"] != "" || !hostkit.SamePath(record["root"].(string), root) ||
		!hostkit.SamePath(record["data"].(string), filepath.Join(root, "data")) ||
		!hostkit.SamePath(record["cwd"].(string), directory) ||
		record["inherited"] != "中文 preserved" {
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

func TestManagedSteadyStateRunsInPlaceAndMismatchChainExecs(t *testing.T) {
	root := t.TempDir()
	data := filepath.Join(root, "data")
	dbFixture(t, filepath.Join(data, dbName))
	verDir := filepath.Join(root, "versions", "0.4.0")
	executableFixture(t, filepath.Join(verDir, "Holdexar.exe"))
	put(t, filepath.Join(verDir, "serverd.exe"), []byte("MZ-serverd"))
	state := map[string]any{
		"version": "0.4.0",
		"dataCompatibility": map[string]any{
			"dataDir":       data,
			"schemaVersion": 32,
			"versions":      []string{"0.4.0"},
		},
	}
	raw, _ := json.Marshal(state)
	put(t, filepath.Join(root, "state", "current.json"), raw)

	observed := filepath.Join(root, "observed.json")
	t.Setenv("HOLDEXAR_GO_TEST_FIXTURE", "version")
	t.Setenv("HOLDEXAR_GO_TEST_OBSERVED", observed)

	gotVerDir, gotVer, gotData, _, chainExited, err := prepareManagedRoot(root, "0.4.0", []string{"--server"})
	if err != nil {
		t.Fatalf("steady-state prepareManagedRoot failed: %v", err)
	}
	if chainExited {
		t.Fatal("steady-state matching buildVersion must run in-place without chain-exec")
	}
	if _, statErr := os.Stat(observed); !os.IsNotExist(statErr) {
		t.Fatal("in-place steady state must not spawn child Holdexar.exe")
	}
	if !hostkit.SamePath(gotVerDir, verDir) || gotVer != "0.4.0" || !hostkit.SamePath(gotData, data) {
		t.Fatalf("unexpected in-place resolution: verDir=%q ver=%q data=%q", gotVerDir, gotVer, gotData)
	}

	_, _, _, code, chainExited, err := prepareManagedRoot(root, "0.4.1", []string{"--server"})
	if err != nil || !chainExited || code != 37 {
		t.Fatalf("mismatch must chain-exec target version: exited=%v code=%d err=%v", chainExited, code, err)
	}
	if _, statErr := os.Stat(observed); statErr != nil {
		t.Fatalf("expected child Holdexar.exe to write observed.json on mismatch: %v", statErr)
	}
}

func TestExistingInvalidPointerNeverFallsThrough(t *testing.T) {
	for _, raw := range []string{`{}`, `null`, `[]`, `{"version":""}`, `{"version":32}`, `{"version":false}`, `{"version":"0.3.8"} {}`} {
		t.Run(raw, func(t *testing.T) {
			root := t.TempDir()
			put(t, filepath.Join(root, "state", "current.json"), []byte(raw))
			put(t, filepath.Join(root, "versions", "0.3.8", "Holdexar.exe"), []byte("fixture"))
			if _, err := hostkit.ReadDataCompatibility(root); err == nil {
				t.Fatal("existing invalid pointer accepted")
			}
		})
	}
}

func TestCompatibilityNumberLexemes(t *testing.T) {
	root := t.TempDir()
	put(t, filepath.Join(root, "state", "current.json"), []byte(`{"version":"0.3.8","dataCompatibility":{"dataDir":"E:\\data","schemaVersion":32.0,"versions":["0.3.8"]}}`))
	if _, err := hostkit.ReadDataCompatibility(root); err == nil {
		t.Fatal("schema32.0 accepted as integer")
	}
}

func TestFallbackPreservesNestedLargeInteger(t *testing.T) {
	root := t.TempDir()
	path := filepath.Join(root, "state", "current.json")
	put(t, path, []byte(`{"version":"0.3.8","dataCompatibility":{"dataDir":"E:\\data","schemaVersion":32,"versions":["0.3.8","0.3.7"],"extension":{"number":9007199254740993}}}`))
	if _, err := hostkit.WriteCurrentState(root, "0.3.7", nil); err != nil {
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
	env := []string{}
	for _, entry := range os.Environ() {
		key, _, _ := strings.Cut(entry, "=")
		if !strings.EqualFold(key, "CI") && !strings.EqualFold(key, "HOLDEXAR_NO_DIALOG") {
			env = append(env, entry)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, path, "--server")
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
	t.Setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", "")
	if showFatalDialog([]string{"--server"}) {
		t.Fatal("headless error allowed modal")
	}
}

func TestCompatibilityAliasesCannotChangeFacts(t *testing.T) {
	for _, aliases := range []string{
		`"datadir":"E:\\unaccepted","schemaversion":99,"VERSIONS":["0.3.7"]`,
		`"DataDir":"E:\\unaccepted","SchemaVersion":99,"Versions":["0.3.7"]`,
		`"DATADIR":"E:\\unaccepted","SCHEMAVERSION":99,"VERSIONS":["0.3.7"]`,
	} {
		root := t.TempDir()
		raw := `{"version":"0.3.8","dataCompatibility":{"dataDir":"E:\\accepted","schemaVersion":32,"versions":["0.3.8"],` + aliases + `}}`
		put(t, filepath.Join(root, "state", "current.json"), []byte(raw))
		compat, err := hostkit.ReadDataCompatibility(root)
		if err != nil {
			t.Fatal(err)
		}
		if !hostkit.SamePath(compat.DataDir, `E:\accepted`) || compat.SchemaVersion != 32 || len(compat.Versions) != 1 || compat.Versions[0] != "0.3.8" {
			t.Fatalf("aliases changed accepted facts: %+v", compat)
		}
		if len(compat.Extra) != 3 {
			t.Fatalf("case-sensitive extensions lost: %+v", compat.Extra)
		}
	}
}

func completedTransaction(t *testing.T, root string) map[string]any {
	t.Helper()
	data := filepath.Join(root, "data")
	dbFixture(t, filepath.Join(data, dbName))
	state := map[string]any{
		"version":   "0.3.8",
		"attemptId": "terminal-attempt",
		"dataCompatibility": map[string]any{
			"dataDir":       data,
			"schemaVersion": 32,
			"versions":      []string{"0.3.8"},
		},
	}
	b, _ := json.Marshal(state)
	put(t, filepath.Join(root, "state", "current.json"), b)
	return map[string]any{
		"stage":             "committed",
		"attemptId":         "terminal-attempt",
		"targetVersion":     "0.3.8",
		"sourceVersion":     "0.3.7",
		"currentVersion":    "0.3.8",
		"dataPrepared":      true,
		"dataAccepted":      true,
		"dataRecoveryPhase": "accepted",
		"dataSchemaVersion": 32,
		"launchPending":     false,
		"dataDir":           data,
		"dataDbPath":        filepath.Join(data, dbName),
	}
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
				state, _ := hostkit.ReadCurrentState(root)
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
				completedTransaction(t, root)
				tx := map[string]any{
					"attemptId":     "new-aborted-attempt",
					"stage":         stage,
					"sourceVersion": "0.3.8",
					"targetVersion": "0.3.9",
					"dataDir":       filepath.Join(root, "data"),
				}
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
	for _, change := range []string{
		"restore-marker", "data-dir", "database-path", "pending-version", "launch-pending",
		"phase", "not-prepared", "schema", "schema-lexeme", "current-version",
		"compatibility", "attempt", "release-type", "rollback-marker", "rollback-phase",
		"rollback-accepted", "rollback-accepted-type", "rollback-external-no-compat",
	} {
		t.Run(change, func(t *testing.T) {
			root := t.TempDir()
			tx := completedTransaction(t, root)
			state, _ := hostkit.ReadCurrentState(root)
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
	put(t, filepath.Join(root, "data", dbName), makeSQLitePage1(4096, 33))
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
	state, _ := hostkit.ReadCurrentState(root)
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

func TestAttachChildJobKillsTargetWithoutPlacingSelfInJob(t *testing.T) {
	cmdPath := filepath.Join(os.Getenv("SystemRoot"), "System32", "cmd.exe")
	srvCmd := exec.Command(cmdPath, "/c", "ping", "-n", "30", "127.0.0.1")
	if err := srvCmd.Start(); err != nil {
		t.Fatal(err)
	}
	srvDone := make(chan struct{})
	go func() { _ = srvCmd.Wait(); close(srvDone) }()

	job := attachChildJob(srvCmd.Process.Pid)
	if job == 0 {
		_ = srvCmd.Process.Kill()
		t.Fatal("attachChildJob returned 0")
	}

	peerCmd := exec.Command(cmdPath, "/c", "ping", "-n", "30", "127.0.0.1")
	if err := peerCmd.Start(); err != nil {
		_ = windows.CloseHandle(job)
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = peerCmd.Process.Kill(); _ = peerCmd.Wait() })

	_ = windows.CloseHandle(job)
	select {
	case <-srvDone:
	case <-time.After(5 * time.Second):
		_ = srvCmd.Process.Kill()
		t.Fatal("closing srvJob did not terminate child process")
	}

	handle, err := windows.OpenProcess(windows.SYNCHRONIZE, false, uint32(peerCmd.Process.Pid))
	if err != nil {
		t.Fatal(err)
	}
	defer windows.CloseHandle(handle)
	ev, _ := windows.WaitForSingleObject(handle, 100)
	if ev != uint32(windows.WAIT_TIMEOUT) {
		t.Fatal("sibling process died when srvJob closed; windowhost must not place itself in srvJob")
	}
}

func TestPerformRestartPostsWmAppQuitToUIThread(t *testing.T) {
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()

	t.Setenv("HOLDEXAR_GO_TEST_FIXTURE", "noop")
	dir := t.TempDir()
	log := openLog(dir)
	defer log.close()
	cfg := &options{appName: "Holdexar", dataDir: dir, serverPort: 29999, portFree: portFree}
	a := newApp(cfg, log, nil)

	registerDialogClass()
	hwnd := call(procCreateWindowExW, uintptr(wsExToolWindow), utf16Ptr(dialogClassName), utf16Ptr("restart-quit-test"),
		uintptr(wsPopup), 0, 0, 100, 100, 0, 0, 0, 0)
	if hwnd == 0 {
		t.Skip("window creation unavailable")
	}
	defer call(procDestroyWindow, hwnd)
	a.hwnd = hwnd

	done := make(chan struct{})
	go func() {
		a.performRestart()
		close(done)
	}()
	<-done

	if !a.restartPending.Load() {
		t.Fatal("restartPending should be set atomically")
	}
	if a.quitting.Load() {
		t.Fatal("performRestart must not call performQuit directly off the UI thread")
	}

	var m msg
	const pmRemove = 0x0001
	procPeekMessageW := modUser32.NewProc("PeekMessageW")
	found := false
	for i := 0; i < 16; i++ {
		if call(procPeekMessageW, uintptr(unsafe.Pointer(&m)), hwnd, wmAppQuit, wmAppQuit, pmRemove) != 0 {
			found = true
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !found {
		t.Fatal("expected wmAppQuit posted to UI thread hwnd")
	}
}
