package migration

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"testing"
)

func lateMatrixCount(t *testing.T, conn *sql.Conn, query string, want int, args ...any) {
	t.Helper()
	var got int
	if err := conn.QueryRowContext(context.Background(), query, args...).Scan(&got); err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("%s = %d, want %d", query, got, want)
	}
}

func lateMatrixColumns(t *testing.T, conn *sql.Conn, table string, present bool, names ...string) {
	t.Helper()
	columns, err := migrationColumns(context.Background(), conn, table)
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range names {
		if columns[name] != present {
			t.Fatalf("%s.%s presence = %v, want %v", table, name, columns[name], present)
		}
	}
}

const lateMatrixTimingSchema = `CREATE TABLE price_cycles(id INTEGER PRIMARY KEY,units_expected INTEGER,
	started_at TEXT,running_at TEXT,repairing_at TEXT,finalizing_at TEXT,finished_at TEXT,duration_seconds FLOAT,stage_ms_json JSON)`

func TestLateMatrixTimingBoundaries(t *testing.T) {
	cases := []struct {
		name     string
		times    [5]any
		duration any
		stages   string
	}{
		{"microseconds_and_float_truncation", [5]any{"2026-01-01 00:00:00.000001", "2026-01-01 00:00:00.001002", "2026-01-01 00:00:01.002002", "2026-01-01 00:00:01.003003", "2026-01-01 00:00:01.153003"}, 1.2, `{"planning":1,"running":1000,"repairing":1,"finalizing":150}`},
		{"negative_duration_clamped_stages", [5]any{"2026-01-01 00:00:01.000000", "2026-01-01 00:00:00.900000", nil, nil, "2026-01-01 00:00:00.850000"}, -0.1, `{"planning":0,"running":0,"repairing":0,"finalizing":0}`},
		{"skipped_repair_and_finalization", [5]any{"2026-01-01 00:00:00.000000", "2026-01-01 00:00:00.001000", nil, nil, "2026-01-01 00:00:00.251000"}, 0.3, `{"planning":1,"running":250,"repairing":0,"finalizing":0}`},
		{"null_outer_boundaries", [5]any{nil, "2026-01-01 00:00:00.000000", nil, "2026-01-01 00:00:00.100000", nil}, nil, `{"planning":0,"running":100,"repairing":0,"finalizing":0}`},
		{"decimal_tie", [5]any{"2026-01-01T00:00:00", nil, nil, nil, "2026-01-01T00:00:00.250000"}, 0.2, `{"planning":0,"running":0,"repairing":0,"finalizing":0}`},
	}
	for _, check := range cases {
		t.Run(check.name, func(t *testing.T) {
			_, conn := middleTestConn(t, lateMatrixTimingSchema)
			args := []any{1, 1}
			args = append(args, check.times[:]...)
			args = append(args, check.duration, check.stages)
			if _, err := conn.ExecContext(context.Background(), "INSERT INTO price_cycles VALUES(?,?,?,?,?,?,?,?,?)", args...); err != nil {
				t.Fatal(err)
			}
			if err := lateTestStep(t, conn, 27); err != nil {
				t.Fatal(err)
			}
			lateMatrixColumns(t, conn, "price_cycles", false, "duration_seconds", "stage_ms_json")
			lateMatrixColumns(t, conn, "price_cycles", true, "started_at", "finished_at", "units_expected")
			lateMatrixCount(t, conn, "SELECT count(*) FROM price_cycles WHERE id=1", 1)
		})
	}
}

func TestLateMatrixTimingMismatchPreserved(t *testing.T) {
	for _, check := range []struct {
		name, stages string
		duration     float64
	}{
		{"duration", `{"planning":0,"running":100,"repairing":0,"finalizing":0}`, 0.10000000001},
		{"stage", `{"planning":0,"running":101,"repairing":0,"finalizing":0}`, 0.1},
		{"unknown_stage", `{"planning":0,"running":100,"repairing":0,"finalizing":0,"other":0}`, 0.1},
	} {
		t.Run(check.name, func(t *testing.T) {
			_, conn := middleTestConn(t, lateMatrixTimingSchema)
			if _, err := conn.ExecContext(context.Background(), `INSERT INTO price_cycles VALUES(1,1,'2026-01-01 00:00:00','2026-01-01 00:00:00',NULL,NULL,'2026-01-01 00:00:00.100000',?,?)`, check.duration, check.stages); err != nil {
				t.Fatal(err)
			}
			if err := lateTestStep(t, conn, 27); err == nil {
				t.Fatal("discarded inconsistent timing projection")
			}
			lateMatrixColumns(t, conn, "price_cycles", true, "duration_seconds", "stage_ms_json")
			var duration float64
			var raw string
			if err := conn.QueryRowContext(context.Background(), "SELECT duration_seconds,stage_ms_json FROM price_cycles").Scan(&duration, &raw); err != nil {
				t.Fatal(err)
			}
			if duration != check.duration || raw != check.stages {
				t.Fatalf("historical timing changed to %v / %s", duration, raw)
			}
		})
	}
	_, conn := middleTestConn(t, lateMatrixTimingSchema, "INSERT INTO price_cycles VALUES(1,NULL,'bad-time',NULL,NULL,NULL,NULL,999,'bad-json')")
	if err := lateTestStep(t, conn, 27); err != nil {
		t.Fatalf("unfrozen cycle must not be checked against frozen timing: %v", err)
	}
}

const lateMatrixMonitoringSchema = `CREATE TABLE monitor_targets(id INTEGER PRIMARY KEY,target_type TEXT,target_id INTEGER,state TEXT,priority INTEGER);
	CREATE TABLE monitor_sources(id INTEGER PRIMARY KEY,target_type TEXT,target_id INTEGER,priority INTEGER,active INTEGER);
	CREATE TABLE monitor_exclusions(id INTEGER PRIMARY KEY,target_type TEXT,target_id INTEGER,active INTEGER);
	CREATE INDEX ix_monitor_target_state ON monitor_targets(target_type,state);
	CREATE INDEX ix_monitor_target_order ON monitor_targets(state,priority,target_id);`

func TestLateMatrixMonitoringDerivedQualification(t *testing.T) {
	_, conn := middleTestConn(t, lateMatrixMonitoringSchema, `INSERT INTO monitor_targets VALUES
		(1,'game',100,'active',95),(2,'game',200,'excluded',100),(3,'game',300,'released',0),(4,'game',400,'active',0),(5,'bundle',500,'active',60);
		INSERT INTO monitor_sources VALUES
		(1,'game',100,100,0),(2,'game',100,95,1),(3,'game',100,60,1),(4,'game',200,100,1),
		(5,'game',300,100,0),(6,'game',300,100,2),(7,'game',300,100,NULL),(8,'game',400,NULL,1),
		(9,'bundle',500,60,1),(10,'game',500,100,1);
		INSERT INTO monitor_exclusions VALUES(1,'game',100,0),(2,'game',200,1),(3,'game',300,NULL);`)
	if err := lateTestStep(t, conn, 28); err != nil {
		t.Fatal(err)
	}
	lateMatrixColumns(t, conn, "monitor_targets", false, "state", "priority")
	lateMatrixCount(t, conn, "SELECT count(*) FROM monitor_targets", 5)
	lateMatrixCount(t, conn, "SELECT count(*) FROM monitor_sources", 10)
	lateMatrixCount(t, conn, "SELECT count(*) FROM monitor_exclusions", 3)
	lateMatrixCount(t, conn, "SELECT count(*) FROM sqlite_schema WHERE name IN ('ix_monitor_target_state','ix_monitor_target_order')", 0)
}

func TestLateMatrixMonitoringMismatchesPreserved(t *testing.T) {
	for _, check := range []struct {
		name, state string
		priority    int
		excluded    bool
	}{
		{"state", "released", 95, false}, {"priority", "active", 94, false}, {"exclusion_precedence", "active", 95, true},
	} {
		t.Run(check.name, func(t *testing.T) {
			_, conn := middleTestConn(t, lateMatrixMonitoringSchema, "INSERT INTO monitor_sources VALUES(1,'game',100,95,1)")
			if _, err := conn.ExecContext(context.Background(), "INSERT INTO monitor_targets VALUES(1,'game',100,?,?)", check.state, check.priority); err != nil {
				t.Fatal(err)
			}
			if check.excluded {
				if err := migrationExec(context.Background(), conn, "INSERT INTO monitor_exclusions VALUES(1,'game',100,1)"); err != nil {
					t.Fatal(err)
				}
			}
			if err := lateTestStep(t, conn, 28); err == nil {
				t.Fatal("discarded unmatched monitoring projection")
			}
			lateMatrixColumns(t, conn, "monitor_targets", true, "state", "priority")
			lateMatrixCount(t, conn, "SELECT count(*) FROM monitor_targets WHERE state=? AND priority=?", 1, check.state, check.priority)
			lateMatrixCount(t, conn, "SELECT count(*) FROM sqlite_schema WHERE name IN ('ix_monitor_target_state','ix_monitor_target_order')", 2)
		})
	}
}

func TestLateMatrixMonitoringUnknownIndexRestoresPriorDrop(t *testing.T) {
	_, conn := middleTestConn(t, lateMatrixMonitoringSchema,
		"INSERT INTO monitor_targets VALUES(1,'game',100,'released',0)",
		"DROP INDEX ix_monitor_target_order;CREATE INDEX ix_monitor_target_order ON monitor_targets(state DESC,priority,target_id)")
	if err := lateTestStep(t, conn, 28); err == nil {
		t.Fatal("accepted unsupported descending historical index")
	}
	lateMatrixColumns(t, conn, "monitor_targets", true, "state", "priority")
	lateMatrixCount(t, conn, "SELECT count(*) FROM sqlite_schema WHERE name IN ('ix_monitor_target_state','ix_monitor_target_order')", 2)
	lateMatrixCount(t, conn, `SELECT count(*) FROM pragma_index_xinfo('ix_monitor_target_order') WHERE name='state' AND "desc"=1`, 1)
}

const lateMatrixAchievementSchema = `CREATE TABLE achievement_games(steamid TEXT,appid INTEGER,total_achievements INTEGER,unlocked INTEGER,platinum BOOLEAN);
	CREATE TABLE challenge_progress(steamid TEXT,challenge_id TEXT,target INTEGER,progress INTEGER);`

func TestLateMatrixAchievementFacts(t *testing.T) {
	_, conn := middleTestConn(t, lateMatrixAchievementSchema, `INSERT INTO achievement_games VALUES
		('a',1,5,5,1),('a',2,5,6,1),('a',3,5,4,0),('a',4,0,0,0),('a',5,NULL,NULL,0),('a',6,1,NULL,0);
		INSERT INTO challenge_progress VALUES('a','first_trophy',1,1),('a','hunter_twelve',12,4),('a','hours_thousand',60000,123);`)
	if err := lateTestStep(t, conn, 29); err != nil {
		t.Fatal(err)
	}
	lateMatrixColumns(t, conn, "achievement_games", false, "platinum")
	lateMatrixColumns(t, conn, "challenge_progress", false, "target")
	lateMatrixCount(t, conn, "SELECT count(*) FROM achievement_games", 6)
	lateMatrixCount(t, conn, "SELECT count(*) FROM challenge_progress WHERE challenge_id='hours_thousand' AND progress=123", 1)
}

func TestLateMatrixAchievementMismatchesPreserved(t *testing.T) {
	for _, check := range []struct{ name, facts string }{
		{"platinum", "INSERT INTO achievement_games VALUES('a',1,5,4,1);INSERT INTO challenge_progress VALUES('a','first_trophy',1,0)"},
		{"unknown_challenge", "INSERT INTO achievement_games VALUES('a',1,5,5,1);INSERT INTO challenge_progress VALUES('a','unknown',1,0)"},
		{"wrong_target", "INSERT INTO achievement_games VALUES('a',1,5,5,1);INSERT INTO challenge_progress VALUES('a','hunter_twelve',11,0)"},
	} {
		t.Run(check.name, func(t *testing.T) {
			_, conn := middleTestConn(t, lateMatrixAchievementSchema, check.facts)
			if err := lateTestStep(t, conn, 29); err == nil {
				t.Fatal("discarded unmatched achievement projection")
			}
			lateMatrixColumns(t, conn, "achievement_games", true, "platinum")
			lateMatrixColumns(t, conn, "challenge_progress", true, "target")
			lateMatrixCount(t, conn, "SELECT count(*) FROM achievement_games", 1)
			lateMatrixCount(t, conn, "SELECT count(*) FROM challenge_progress", 1)
		})
	}
}

const lateMatrixCooldownSchema = `CREATE TABLE proxy_nodes(node_id TEXT PRIMARY KEY,runtime_name TEXT,subscription_id INTEGER,original_name TEXT,cooldown_until TEXT);
	CREATE TABLE clash_nodes(id INTEGER PRIMARY KEY,subscription_id INTEGER,name TEXT,cooldown_until TEXT);
	CREATE INDEX ix_clash_nodes_sub_name ON clash_nodes(subscription_id,name);`

func TestLateMatrixCooldownUnownedAndAmbiguous(t *testing.T) {
	for _, check := range []struct{ name, nodes, oldName string }{
		{"ambiguous_source", "INSERT INTO proxy_nodes VALUES('a','1|same',1,'same','2998-01-01'),('b','1|same #2',1,'same','2998-01-01')", "same"},
		{"foreign_subscription_runtime", "INSERT INTO proxy_nodes VALUES('a','1|name',2,'name','2998-01-01')", "name"},
		{"foreign_subscription_exact_name", "INSERT INTO proxy_nodes VALUES('a','2|name',2,'name','2998-01-01')", "2|name"},
		{"source_less_foreign_runtime", "INSERT INTO proxy_nodes VALUES('a','2|name',NULL,'name','2998-01-01')", "2|name"},
		{"no_match", "INSERT INTO proxy_nodes VALUES('a','1|other',1,'other','2998-01-01')", "missing"},
	} {
		for _, expired := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/expired=%v", check.name, expired), func(t *testing.T) {
				_, conn := middleTestConn(t, lateMatrixCooldownSchema, check.nodes)
				deadline := "2999-01-01 00:00:00"
				if expired {
					deadline = "2000-01-01 00:00:00"
				}
				if _, err := conn.ExecContext(context.Background(), "INSERT INTO clash_nodes VALUES(1,1,?,?)", check.oldName, deadline); err != nil {
					t.Fatal(err)
				}
				err := lateTestStep(t, conn, 31)
				if expired && err != nil || !expired && err == nil {
					t.Fatalf("expired=%v migration error=%v", expired, err)
				}
				exists, err := migrationTableExists(context.Background(), conn, "clash_nodes")
				if err != nil || exists == expired {
					t.Fatalf("legacy table exists=%v, error=%v", exists, err)
				}
				lateMatrixCount(t, conn, "SELECT count(*) FROM proxy_nodes WHERE cooldown_until!='2998-01-01'", 0)
				if !expired {
					lateMatrixCount(t, conn, "SELECT count(*) FROM clash_nodes WHERE cooldown_until=?", 1, deadline)
				}
			})
		}
	}
}

func TestLateMatrixCooldownSourceLessOwnershipAndMaximum(t *testing.T) {
	_, conn := middleTestConn(t, lateMatrixCooldownSchema,
		"INSERT INTO proxy_nodes VALUES('a','1|name',NULL,'name','2998-01-01 00:00:00')",
		"INSERT INTO clash_nodes VALUES(1,1,'name #2','2997-01-01'),(2,1,'1|name','2999-01-01'),(3,1,'name','2996-01-01')")
	if err := lateTestStep(t, conn, 31); err != nil {
		t.Fatal(err)
	}
	lateMatrixCount(t, conn, "SELECT count(*) FROM proxy_nodes WHERE node_id='a' AND subscription_id IS NULL AND cooldown_until='2999-01-01 00:00:00.000000'", 1)
	if exists, err := migrationTableExists(context.Background(), conn, "clash_nodes"); err != nil || exists {
		t.Fatalf("legacy cooldown table remains: %v,%v", exists, err)
	}
}

func TestLateMatrixCooldownFailurePreservesEarlierMatchedDeadline(t *testing.T) {
	_, conn := middleTestConn(t, lateMatrixCooldownSchema,
		"INSERT INTO proxy_nodes VALUES('a','1|name',1,'name','2998-01-01')",
		"INSERT INTO clash_nodes VALUES(1,1,'name','2999-01-01'),(2,1,'unknown','2999-01-01')")
	if err := lateTestStep(t, conn, 31); err == nil {
		t.Fatal("accepted an unowned future cooldown")
	}
	lateMatrixCount(t, conn, "SELECT count(*) FROM clash_nodes", 2)
	lateMatrixCount(t, conn, "SELECT count(*) FROM proxy_nodes WHERE cooldown_until='2998-01-01'", 1)
}

func TestLateMatrixCooldownUnknownDependenciesPreserved(t *testing.T) {
	for _, check := range []struct{ name, dependency string }{
		{"view", "CREATE VIEW cooldown_extension AS SELECT name FROM clash_nodes"},
		{"foreign_key", "CREATE TABLE cooldown_extension(id INTEGER REFERENCES clash_nodes(id))"},
		{"unknown_index", "CREATE INDEX cooldown_extension ON clash_nodes(name)"},
		{"descending_index", "DROP INDEX ix_clash_nodes_sub_name;CREATE INDEX ix_clash_nodes_sub_name ON clash_nodes(subscription_id,name DESC)"},
	} {
		t.Run(check.name, func(t *testing.T) {
			_, conn := middleTestConn(t, lateMatrixCooldownSchema, "INSERT INTO clash_nodes VALUES(1,1,'old','2000-01-01')", check.dependency)
			if err := lateTestStep(t, conn, 31); err == nil {
				t.Fatal("discarded cooldown table with unsupported dependency")
			}
			lateMatrixCount(t, conn, "SELECT count(*) FROM clash_nodes", 1)
		})
	}
}

const lateMatrixCoverageSchema = `CREATE TABLE price_cycles(id INTEGER PRIMARY KEY,units_expected INTEGER,units_ok INTEGER,units_locked INTEGER,coverage FLOAT,coverage_confirmed FLOAT)`

func TestLateMatrixCoverageNullZeroAndRounding(t *testing.T) {
	cases := []struct {
		name   string
		counts [3]any
		ratios [2]any
	}{
		{"unfrozen", [3]any{nil, nil, nil}, [2]any{nil, nil}},
		{"unfrozen_other_counts_not_validated", [3]any{nil, "unknown", -1}, [2]any{nil, nil}},
		{"zero", [3]any{0, nil, nil}, [2]any{nil, nil}},
		{"zero_other_counts_not_validated", [3]any{0, -1, "unknown"}, [2]any{nil, nil}},
		{"no_success", [3]any{1, 0, 0}, [2]any{0.0, 0.0}},
		{"ordinary_ratio", [3]any{64, 15, 1}, [2]any{0.2344, 0.25}},
		{"binary_float_tie_above", [3]any{20000, 1, 1}, [2]any{0.0001, 0.0001}},
		{"binary_float_tie_below", [3]any{20000, 3, 2}, [2]any{0.0001, 0.0003}},
		{"binary_float_tie_next", [3]any{20000, 5, 2}, [2]any{0.0003, 0.0003}},
	}
	for _, check := range cases {
		t.Run(check.name, func(t *testing.T) {
			_, conn := middleTestConn(t, lateMatrixCoverageSchema)
			if _, err := conn.ExecContext(context.Background(), "INSERT INTO price_cycles VALUES(1,?,?,?,?,?)", check.counts[0], check.counts[1], check.counts[2], check.ratios[0], check.ratios[1]); err != nil {
				t.Fatal(err)
			}
			if err := lateTestStep(t, conn, 32); err != nil {
				t.Fatal(err)
			}
			lateMatrixColumns(t, conn, "price_cycles", false, "coverage", "coverage_confirmed")
			lateMatrixCount(t, conn, "SELECT count(*) FROM price_cycles", 1)
		})
	}
}

func TestLateMatrixCoverageInvalidCountsPreserved(t *testing.T) {
	for _, check := range []struct {
		name   string
		counts [3]any
		ratios [2]any
	}{
		{"negative_expected", [3]any{-1, 0, 0}, [2]any{nil, nil}},
		{"fractional_expected", [3]any{1.5, 0, 0}, [2]any{0.0, 0.0}},
		{"text_expected", [3]any{"unknown", 0, 0}, [2]any{0.0, 0.0}},
		{"missing_ok", [3]any{10, nil, 0}, [2]any{0.0, 0.0}},
		{"missing_locked", [3]any{10, 0, nil}, [2]any{0.0, 0.0}},
		{"negative_ok", [3]any{10, -1, 0}, [2]any{-0.1, -0.1}},
		{"negative_locked", [3]any{10, 0, -1}, [2]any{0.0, -0.1}},
		{"fractional_ok", [3]any{10, 1.5, 0}, [2]any{0.15, 0.15}},
		{"text_locked", [3]any{10, 0, "unknown"}, [2]any{0.0, 0.0}},
		{"mismatched_ratio", [3]any{10, 1, 1}, [2]any{0.2, 0.2}},
		{"unfrozen_nonnull_ratio", [3]any{nil, nil, nil}, [2]any{0.0, nil}},
		{"zero_nonnull_ratio", [3]any{0, nil, nil}, [2]any{nil, 0.0}},
	} {
		t.Run(check.name, func(t *testing.T) {
			_, conn := middleTestConn(t, lateMatrixCoverageSchema)
			if _, err := conn.ExecContext(context.Background(), "INSERT INTO price_cycles VALUES(1,?,?,?,?,?)", check.counts[0], check.counts[1], check.counts[2], check.ratios[0], check.ratios[1]); err != nil {
				t.Fatal(err)
			}
			if err := lateTestStep(t, conn, 32); err == nil {
				t.Fatal("discarded invalid coverage projection")
			}
			lateMatrixColumns(t, conn, "price_cycles", true, "coverage", "coverage_confirmed")
			lateMatrixCount(t, conn, "SELECT count(*) FROM price_cycles", 1)
			var gotCoverage, gotConfirmed sql.NullFloat64
			if err := conn.QueryRowContext(context.Background(), "SELECT coverage,coverage_confirmed FROM price_cycles").Scan(&gotCoverage, &gotConfirmed); err != nil {
				t.Fatal(err)
			}
			for i, got := range []sql.NullFloat64{gotCoverage, gotConfirmed} {
				if check.ratios[i] == nil && got.Valid || check.ratios[i] != nil && (!got.Valid || got.Float64 != check.ratios[i].(float64)) {
					t.Fatalf("historical ratio changed: %v", got)
				}
			}
		})
	}
}

func TestLateMatrixProjectionPartialColumnsPreserved(t *testing.T) {
	for _, check := range []struct {
		name, table, definition, column string
		version                         int
	}{
		{"timing", "price_cycles", "id INTEGER,duration_seconds FLOAT", "duration_seconds", 27},
		{"monitoring", "monitor_targets", "id INTEGER,state TEXT", "state", 28},
		{"coverage", "price_cycles", "id INTEGER,coverage FLOAT", "coverage", 32},
	} {
		t.Run(check.name, func(t *testing.T) {
			_, conn := middleTestConn(t, "CREATE TABLE "+check.table+"("+check.definition+")")
			if err := lateTestStep(t, conn, check.version); err == nil {
				t.Fatal("accepted partial historical projection")
			}
			lateMatrixColumns(t, conn, check.table, true, check.column)
		})
	}
}

func TestLateMatrixTimingExpectedJSONIsNumeric(t *testing.T) {
	var stages map[string]int
	if err := json.Unmarshal([]byte(`{"planning":1,"running":1000,"repairing":1,"finalizing":150}`), &stages); err != nil || stages["running"] != 1000 {
		t.Fatalf("timing fixture invalid: %v,%v", stages, err)
	}
}
