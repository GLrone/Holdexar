package hostkit

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func seedDBWithVersion(t *testing.T, dbPath string, userVersion int32) {
	t.Helper()
	writeTestFile(t, dbPath, buildSQLiteDBPage1(4096, userVersion))
}

func TestCompatibilityStrictJSONShapes(t *testing.T) {
	absData := filepath.Join(CanonicalPath(t.TempDir()), "data")
	absDataJSON, _ := json.Marshal(absData)

	for _, raw := range []string{
		`null`,
		fmt.Sprintf(`{"dataDir":%s,"versions":["0.3.8"]}`, absDataJSON),
		fmt.Sprintf(`{"dataDir":%s,"schemaVersion":false,"versions":["0.3.8"]}`, absDataJSON),
		fmt.Sprintf(`{"dataDir":%s,"schemaVersion":32.0,"versions":["0.3.8"]}`, absDataJSON),
		fmt.Sprintf(`{"dataDir":%s,"schemaVersion":"32","versions":["0.3.8"]}`, absDataJSON),
		fmt.Sprintf(`{"dataDir":%s,"schemaVersion":32,"versions":[null]}`, absDataJSON),
		fmt.Sprintf(`{"dataDir":%s,"schemaVersion":32,"versions":[]}`, absDataJSON),
	} {
		t.Run(raw, func(t *testing.T) {
			root := t.TempDir()
			writeTestFile(t, filepath.Join(root, "state", "current.json"),
				[]byte(`{"version":"0.3.8","dataCompatibility":`+raw+`}`))
			if _, err := ReadDataCompatibility(root); err == nil {
				t.Fatalf("invalid compatibility accepted: %s", raw)
			}
		})
	}
}

func TestExistingInvalidPointerNeverFallsThrough(t *testing.T) {
	for _, raw := range []string{
		`{}`,
		`null`,
		`[]`,
		`{"version":""}`,
		`{"version":32}`,
		`{"version":false}`,
		`{"version":"0.3.8"} {}`,
	} {
		t.Run(raw, func(t *testing.T) {
			root := t.TempDir()
			writeTestFile(t, filepath.Join(root, "state", "current.json"), []byte(raw))
			writeTestFile(t, filepath.Join(root, "versions", "0.3.8", "Holdexar.exe"), []byte("MZ"))
			if _, err := ReadCurrentState(root); err == nil {
				t.Fatalf("invalid pointer accepted by ReadCurrentState: %s", raw)
			}
			if _, err := SelectTargetVersion(root, nil); err == nil {
				t.Fatalf("invalid pointer fell through in SelectTargetVersion: %s", raw)
			}
		})
	}
}

func TestFallbackPreservesNestedLargeInteger(t *testing.T) {
	root := t.TempDir()
	absData := filepath.Join(CanonicalPath(root), "data")
	absDataJSON, _ := json.Marshal(absData)
	path := filepath.Join(root, "state", "current.json")
	writeTestFile(t, path, []byte(fmt.Sprintf(
		`{"version":"0.3.8","pendingVersion":"0.3.8","attemptId":"att","dataCompatibility":{"dataDir":%s,"schemaVersion":32,"versions":["0.3.8","0.3.7"],"extension":{"number":9007199254740993}}}`,
		absDataJSON,
	)))
	if _, err := WriteCurrentState(root, "0.3.7", nil); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(path)
	if err != nil || !strings.Contains(string(raw), "9007199254740993") {
		t.Fatalf("large integer lost: %s (%v)", string(raw), err)
	}
	if strings.Contains(string(raw), "pendingVersion") || strings.Contains(string(raw), "attemptId") {
		t.Fatalf("stale switching fields not cleared: %s", string(raw))
	}
}

func TestCompatibilityAliasesCannotChangeFacts(t *testing.T) {
	accepted := filepath.Join(CanonicalPath(t.TempDir()), "accepted")
	unaccepted := filepath.Join(CanonicalPath(t.TempDir()), "unaccepted")
	accJSON, _ := json.Marshal(accepted)
	unaccJSON, _ := json.Marshal(unaccepted)

	for _, aliases := range []string{
		fmt.Sprintf(`"datadir":%s,"schemaversion":99,"VERSIONS":["0.3.7"]`, unaccJSON),
		fmt.Sprintf(`"DataDir":%s,"SchemaVersion":99,"Versions":["0.3.7"]`, unaccJSON),
		fmt.Sprintf(`"DATADIR":%s,"SCHEMAVERSION":99,"VERSIONS":["0.3.7"]`, unaccJSON),
	} {
		root := t.TempDir()
		raw := fmt.Sprintf(`{"version":"0.3.8","dataCompatibility":{"dataDir":%s,"schemaVersion":32,"versions":["0.3.8"],%s}}`, accJSON, aliases)
		writeTestFile(t, filepath.Join(root, "state", "current.json"), []byte(raw))
		compat, err := ReadDataCompatibility(root)
		if err != nil {
			t.Fatal(err)
		}
		if !SamePath(compat.DataDir, accepted) || compat.SchemaVersion != 32 || len(compat.Versions) != 1 || compat.Versions[0] != "0.3.8" {
			t.Fatalf("aliases changed accepted facts: %+v", compat)
		}
		if len(compat.Extra) != 3 {
			t.Fatalf("case-sensitive extensions lost: %+v", compat.Extra)
		}
	}
}

func TestReadTransactionRejectsCorruptRecords(t *testing.T) {
	rootEmpty := t.TempDir()
	if tx, err := ReadTransaction(rootEmpty); err != nil || tx != nil {
		t.Fatalf("missing transaction should return (nil, nil), got (%v, %v)", tx, err)
	}
	for _, bad := range []string{`null`, `[]`, `{`, `{"attemptId":"a"} trailing`} {
		root := t.TempDir()
		writeTestFile(t, filepath.Join(root, "state", "transaction.json"), []byte(bad))
		if _, err := ReadTransaction(root); err == nil {
			t.Fatalf("corrupt transaction accepted: %s", bad)
		}
	}
}

func completedTxFixture(t *testing.T, root string) map[string]any {
	t.Helper()
	data := filepath.Join(CanonicalPath(root), "data")
	seedDBWithVersion(t, filepath.Join(data, DBFileName), 32)
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
	writeTestFile(t, filepath.Join(root, "state", "current.json"), b)
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
		"dataDbPath":        filepath.Join(data, DBFileName),
	}
}

func TestUpdateRecoveryCompleteAllInvariants(t *testing.T) {
	for _, rollback := range []bool{false, true} {
		root := CanonicalPath(t.TempDir())
		tx := completedTxFixture(t, root)
		if rollback {
			tx["stage"] = "rolled_back"
			tx["dataAccepted"] = false
			tx["dataRollbackReleased"] = true
			tx["dataRecoveryPhase"] = "restored"
			tx["sourceVersion"] = "0.3.8"
			tx["targetVersion"] = "0.3.9"
		}
		b, _ := json.Marshal(tx)
		writeTestFile(t, filepath.Join(root, "state", "transaction.json"), b)
		loadedTx, err := ReadTransaction(root)
		if err != nil {
			t.Fatal(err)
		}
		state, err := ReadCurrentState(root)
		if err != nil {
			t.Fatal(err)
		}
		if !UpdateRecoveryComplete(loadedTx) || !UpdateRecoveryCompleteForRoot(root, loadedTx, state) {
			t.Fatalf("expected terminal tx complete (rollback=%v)", rollback)
		}
	}

	for _, change := range []string{
		"restore-marker", "data-dir", "database-path", "pending-version",
		"launch-pending", "phase", "not-prepared", "schema", "schema-lexeme",
		"current-version", "compatibility", "attempt", "release-type",
		"rollback-marker", "rollback-phase", "rollback-accepted",
		"rollback-accepted-type", "rollback-external-no-compat",
	} {
		t.Run(change, func(t *testing.T) {
			root := CanonicalPath(t.TempDir())
			tx := completedTxFixture(t, root)
			state, err := ReadCurrentState(root)
			if err != nil {
				t.Fatal(err)
			}
			if strings.HasPrefix(change, "rollback-") {
				tx["stage"], tx["dataAccepted"], tx["dataRollbackReleased"] = "rolled_back", false, true
				tx["dataRecoveryPhase"], tx["sourceVersion"] = "restored", state.Version
			}
			switch change {
			case "restore-marker", "rollback-marker":
				writeTestFile(t, filepath.Join(root, "data", "backups", ".restore.lock"), []byte(`{"purpose":"update"}`))
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
				extDir := CanonicalPath(t.TempDir())
				tx["dataDir"] = extDir
				tx["dataDbPath"] = filepath.Join(extDir, DBFileName)
			}
			sb, _ := json.Marshal(state)
			writeTestFile(t, filepath.Join(root, "state", "current.json"), sb)
			tb, _ := json.Marshal(tx)
			writeTestFile(t, filepath.Join(root, "state", "transaction.json"), tb)

			loadedTx, _ := ReadTransaction(root)
			loadedState, _ := ReadCurrentState(root)
			if UpdateRecoveryCompleteForRoot(root, loadedTx, loadedState) {
				t.Fatalf("unfinished/conflicting terminal state accepted: %s", change)
			}
		})
	}
}

func TestIsVersionValidAndSelectTargetVersion(t *testing.T) {
	if IsManagedVersionName("0.3.8 ") || IsManagedVersionName("data") || IsManagedVersionName("../0.3.8") {
		t.Fatal("invalid managed version name accepted")
	}
	if !IsManagedVersionName("0.3.8") || !IsManagedVersionName("legacy_version") {
		t.Fatal("valid managed version name rejected")
	}

	root := CanonicalPath(t.TempDir())
	// Flat pure-Go version layout (<verDir>/Holdexar.exe + <verDir>/serverd.exe)
	vFlat := filepath.Join(root, "versions", "0.4.0")
	writeTestFile(t, filepath.Join(vFlat, "Holdexar.exe"), []byte("MZ"))
	writeTestFile(t, filepath.Join(vFlat, "serverd.exe"), []byte("MZ"))
	if !IsVersionValid(root, "0.4.0") {
		t.Fatal("flat Go version layout rejected")
	}
	if srv, err := ResolveVersionServerExe(vFlat); err != nil || !SamePath(srv, filepath.Join(vFlat, "serverd.exe")) {
		t.Fatalf("ResolveVersionServerExe flat = (%q, %v)", srv, err)
	}

	// Legacy _internal/serverd.exe layout
	vLegacy := filepath.Join(root, "versions", "0.3.9")
	writeTestFile(t, filepath.Join(vLegacy, "Holdexar.exe"), []byte("MZ"))
	writeTestFile(t, filepath.Join(vLegacy, "_internal", "serverd.exe"), []byte("MZ"))
	if !IsVersionValid(root, "0.3.9") {
		t.Fatal("legacy _internal/serverd.exe layout rejected")
	}
	if srv, err := ResolveVersionServerExe(vLegacy); err != nil || !SamePath(srv, filepath.Join(vLegacy, "_internal", "serverd.exe")) {
		t.Fatalf("ResolveVersionServerExe legacy = (%q, %v)", srv, err)
	}

	// Empty _internal must be rejected
	vEmptyInternal := filepath.Join(root, "versions", "0.3.8")
	writeTestFile(t, filepath.Join(vEmptyInternal, "Holdexar.exe"), []byte("MZ"))
	if err := os.MkdirAll(filepath.Join(vEmptyInternal, "_internal"), 0o755); err != nil {
		t.Fatal(err)
	}
	if IsVersionValid(root, "0.3.8") {
		t.Fatal("empty _internal directory accepted")
	}

	// Redirected versions root must be rejected
	rootRedirect, outside := CanonicalPath(t.TempDir()), CanonicalPath(t.TempDir())
	writeTestFile(t, filepath.Join(outside, "0.4.0", "Holdexar.exe"), []byte("MZ"))
	makeDirLink(t, filepath.Join(rootRedirect, "versions"), outside)
	if IsVersionValid(rootRedirect, "0.4.0") {
		t.Fatal("redirected versions root accepted")
	}

	// SelectTargetVersion fallback to LastKnownGood + live WAL schema mismatch detection
	dataDir := filepath.Join(root, "data")
	seedDBWithVersion(t, filepath.Join(dataDir, DBFileName), 32)
	lkg := "0.3.9"
	rawState, _ := json.Marshal(map[string]any{
		"version":       "0.4.1",
		"lastKnownGood": lkg,
		"dataCompatibility": map[string]any{
			"dataDir":       dataDir,
			"schemaVersion": 32,
			"versions":      []string{"0.4.0", "0.3.9"},
		},
	})
	writeTestFile(t, filepath.Join(root, "state", "current.json"), rawState)
	selected, err := SelectTargetVersion(root, nil)
	if err != nil || selected != "0.3.9" {
		t.Fatalf("SelectTargetVersion fallback = (%q, %v), want (0.3.9, nil)", selected, err)
	}

	// Advance live schema via WAL commit to 33 -> SelectTargetVersion must reject mismatch
	wal33 := buildSQLiteWAL(walMagicLE, 4096, 0x12345678, 0x9abcdef0, []walFrameSpec{
		{pageNumber: 1, dbSize: 1, userVersion: 33},
	})
	writeTestFile(t, filepath.Join(dataDir, DBFileName+"-wal"), wal33)
	if _, err := SelectTargetVersion(root, nil); err == nil || !strings.Contains(err.Error(), "数据结构已变化") {
		t.Fatalf("expected live WAL schema mismatch error, got %v", err)
	}
}
