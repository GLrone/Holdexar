package migration

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"math"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/proxypool"
)

var lateMigrationSteps = map[int]migrationStep{
	25: migrateProviderConfig,
	26: func(ctx context.Context, conn *sql.Conn) error {
		return migrationDropProjections(ctx, conn, map[string][]string{
			"bill_imports":     {"game_net_fen", "game_spend_fen", "game_refund_fen", "orders", "fx_missing"},
			"tracked_accounts": {"item_count"},
		})
	},
	27: migrateCycleTimingProjection,
	28: migrateMonitoringProjection,
	29: migrateAchievementProjections,
	30: migrateUnusedSessionState,
	31: migrateNodeCooldown,
	32: migrateCycleCoverageProjection,
	33: func(ctx context.Context, conn *sql.Conn) error {
		columns, err := migrationColumns(ctx, conn, "bill_imports")
		if err != nil {
			return err
		}
		if !columns["steam_id"] {
			if err := migrationExec(ctx, conn, "ALTER TABLE bill_imports ADD COLUMN steam_id VARCHAR(20)"); err != nil {
				return err
			}
		}
		return migrationExec(ctx, conn, "CREATE INDEX IF NOT EXISTS ix_bill_imports_steam_id ON bill_imports(steam_id)")
	},
	34: func(ctx context.Context, conn *sql.Conn) error {
		return migrationExec(ctx, conn, "DROP INDEX IF EXISTS ux_notification_candidate",
			"CREATE UNIQUE INDEX ux_notification_candidate ON notification_candidates(source_kind,event_id,recipient)")
	},
}

func migrationDropProjections(ctx context.Context, conn *sql.Conn, retired map[string][]string) error {
	extensions, err := conn.QueryContext(ctx, "SELECT sql FROM sqlite_schema WHERE type IN ('view','trigger') AND sql IS NOT NULL")
	if err != nil {
		return err
	}
	var definitions []string
	for extensions.Next() {
		var definition string
		if err := extensions.Scan(&definition); err != nil {
			extensions.Close()
			return err
		}
		definitions = append(definitions, definition)
	}
	err = extensions.Err()
	extensions.Close()
	if err != nil {
		return err
	}
	tables, err := migrationTableNames(ctx, conn)
	if err != nil {
		return err
	}
	var owners []string
	for table := range retired {
		owners = append(owners, table)
	}
	sort.Strings(owners)
	for _, table := range owners {
		columns, err := migrationColumns(ctx, conn, table)
		if err != nil {
			return err
		}
		var dropped []string
		for _, name := range retired[table] {
			if columns[name] {
				dropped = append(dropped, name)
			}
		}
		if len(dropped) == 0 {
			continue
		}
		mentions := regexp.MustCompile(`(?i)\b` + regexp.QuoteMeta(table) + `\b`)
		for _, definition := range definitions {
			if mentions.MatchString(definition) {
				return fmt.Errorf("%s仍被扩展视图或触发器引用，已停止汇总列退役", table)
			}
		}
		for _, owner := range tables {
			rows, err := conn.QueryContext(ctx, `SELECT "table","to" FROM pragma_foreign_key_list(?)`, owner)
			if err != nil {
				return err
			}
			for rows.Next() {
				var target string
				var column sql.NullString
				if err := rows.Scan(&target, &column); err != nil {
					rows.Close()
					return err
				}
				if strings.EqualFold(target, table) {
					for _, name := range retired[table] {
						if strings.EqualFold(column.String, name) {
							rows.Close()
							return fmt.Errorf("%s汇总列仍被外键引用，已停止升级", table)
						}
					}
				}
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
		}
		sort.Strings(dropped)
		for _, name := range dropped {
			if err := migrationExec(ctx, conn, "ALTER TABLE "+migrationQuoteIdent(table)+" DROP COLUMN "+migrationQuoteIdent(name)); err != nil {
				return err
			}
		}
	}
	return nil
}

func migrationCheckIndex(ctx context.Context, conn *sql.Conn, table, name string, expected []string) error {
	var owner string
	err := conn.QueryRowContext(ctx, "SELECT tbl_name FROM sqlite_schema WHERE type='index' AND name=? COLLATE NOCASE", name).Scan(&owner)
	if err == sql.ErrNoRows {
		return nil
	}
	if err != nil {
		return err
	}
	if !strings.EqualFold(owner, table) {
		return fmt.Errorf("旧索引%s结构未知，已停止升级", name)
	}
	var unique, partial int
	if err := conn.QueryRowContext(ctx, `SELECT "unique",partial FROM pragma_index_list(?) WHERE name=? COLLATE NOCASE`, table, name).Scan(&unique, &partial); err != nil {
		return err
	}
	if unique != 0 || partial != 0 {
		return fmt.Errorf("旧索引%s结构未知，已停止升级", name)
	}
	rows, err := conn.QueryContext(ctx, `SELECT name,"desc",coll FROM pragma_index_xinfo(?) WHERE key=1 ORDER BY seqno`, name)
	if err != nil {
		return err
	}
	defer rows.Close()
	i := 0
	for rows.Next() {
		var column sql.NullString
		var descending int
		var collation string
		if err := rows.Scan(&column, &descending, &collation); err != nil {
			return err
		}
		if i >= len(expected) || column.String != expected[i] || descending != 0 || collation != "BINARY" {
			return fmt.Errorf("旧索引%s结构未知，已停止升级", name)
		}
		i++
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if i != len(expected) {
		return fmt.Errorf("旧索引%s结构未知，已停止升级", name)
	}
	return nil
}

func migrateMonitoringProjection(ctx context.Context, conn *sql.Conn) error {
	columns, err := migrationColumns(ctx, conn, "monitor_targets")
	if err != nil {
		return err
	}
	if columns["state"] != columns["priority"] {
		return fmt.Errorf("监控资格旧列不完整，已保留原结构并停止升级")
	}
	if columns["state"] {
		var identity int64
		err := conn.QueryRowContext(ctx, `SELECT id FROM monitor_targets t WHERE
state IS NOT CASE WHEN EXISTS(SELECT 1 FROM monitor_exclusions e WHERE e.target_type=t.target_type AND e.target_id=t.target_id AND e.active IS 1) THEN 'excluded'
WHEN EXISTS(SELECT 1 FROM monitor_sources s WHERE s.target_type=t.target_type AND s.target_id=t.target_id AND s.active IS 1) THEN 'active' ELSE 'released' END
OR priority IS NOT COALESCE((SELECT MAX(s.priority) FROM monitor_sources s WHERE s.target_type=t.target_type AND s.target_id=t.target_id AND s.active IS 1),0) LIMIT 1`).Scan(&identity)
		if err == nil {
			return fmt.Errorf("监控对象%d资格与来源或排除不一致，已保留原值并停止升级", identity)
		}
		if err != sql.ErrNoRows {
			return err
		}
	}
	for _, index := range []struct {
		name    string
		columns []string
	}{
		{"ix_monitor_target_state", []string{"target_type", "state"}},
		{"ix_monitor_target_order", []string{"state", "priority", "target_id"}},
	} {
		if err := migrationCheckIndex(ctx, conn, "monitor_targets", index.name, index.columns); err != nil {
			return err
		}
		if err := migrationExec(ctx, conn, "DROP INDEX IF EXISTS "+migrationQuoteIdent(index.name)); err != nil {
			return err
		}
	}
	return migrationDropProjections(ctx, conn, map[string][]string{"monitor_targets": {"state", "priority"}})
}

func migrateAchievementProjections(ctx context.Context, conn *sql.Conn) error {
	columns, err := migrationColumns(ctx, conn, "achievement_games")
	if err != nil {
		return err
	}
	if columns["platinum"] {
		var steamID any
		var appID int64
		err := conn.QueryRowContext(ctx, "SELECT steamid,appid FROM achievement_games WHERE platinum IS NOT (COALESCE(total_achievements,0)>0 AND COALESCE(unlocked,0)>=total_achievements) LIMIT 1").Scan(&steamID, &appID)
		if err == nil {
			return fmt.Errorf("成就%v/%d的白金状态与计数不一致，已保留原值并停止升级", steamID, appID)
		}
		if err != sql.ErrNoRows {
			return err
		}
	}
	columns, err = migrationColumns(ctx, conn, "challenge_progress")
	if err != nil {
		return err
	}
	if columns["target"] {
		targets := map[string]int64{"first_trophy": 1, "hunter_twelve": 12, "hunter_fifty": 50, "hunter_hundred": 100, "streak_three": 3, "streak_seven": 7, "streak_thirty": 30, "platinum_first": 1, "platinum_five": 5, "platinum_twelve": 12, "breadth_twenty": 20, "full_clear_first": 1, "full_clear_triple": 3, "hours_hundred": 6000, "hours_thousand": 60000, "buckets_ten": 10, "note_first": 1, "note_gallery": 25, "social_duo": 2, "social_trio": 3}
		rows, err := conn.QueryContext(ctx, "SELECT challenge_id,target FROM challenge_progress")
		if err != nil {
			return err
		}
		for rows.Next() {
			var id string
			var target any
			if err := rows.Scan(&id, &target); err != nil {
				rows.Close()
				return err
			}
			want, exists := targets[id]
			actual, integer := target.(int64)
			if !exists || !integer || actual != want {
				rows.Close()
				return fmt.Errorf("挑战目标与现有目录不一致，已保留原值并停止升级")
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
	}
	return migrationDropProjections(ctx, conn, map[string][]string{"achievement_games": {"platinum"}, "challenge_progress": {"target"}})
}

func migrateUnusedSessionState(ctx context.Context, conn *sql.Conn) error {
	names := []string{"title", "context_snapshot", "context_version", "context_updated_at"}
	columns, err := migrationColumns(ctx, conn, "agent_sessions")
	if err != nil {
		return err
	}
	count := 0
	for _, name := range names {
		if columns[name] {
			count++
		}
	}
	if count != 0 && count != len(names) {
		return fmt.Errorf("执行会话旧扩展列不完整，已保留原结构并停止升级")
	}
	if count != 0 {
		var present int
		err := conn.QueryRowContext(ctx, "SELECT 1 FROM agent_sessions WHERE COALESCE(title,'')!='' OR (context_snapshot IS NOT NULL AND context_snapshot!='null') OR COALESCE(context_version,0)!=0 OR context_updated_at IS NOT NULL LIMIT 1").Scan(&present)
		if err == nil {
			return fmt.Errorf("执行会话含未知上下文或标题，已保留原值并停止升级")
		}
		if err != sql.ErrNoRows {
			return err
		}
	}
	if err := migrationDropProjections(ctx, conn, map[string][]string{"agent_sessions": names}); err != nil {
		return err
	}
	return migrationExec(ctx, conn, "DELETE FROM app_settings WHERE key='bills.probe_skipped'")
}

func migrationRound(value float64, digits int) float64 {
	result, _ := strconv.ParseFloat(strconv.FormatFloat(value, 'f', digits, 64), 64)
	return result
}

func migrateCycleCoverageProjection(ctx context.Context, conn *sql.Conn) error {
	columns, err := migrationColumns(ctx, conn, "price_cycles")
	if err != nil {
		return err
	}
	if columns["coverage"] != columns["coverage_confirmed"] {
		return fmt.Errorf("周期覆盖比率列不完整，已保留原结构并停止升级")
	}
	if columns["coverage"] {
		rows, err := conn.QueryContext(ctx, "SELECT id,units_expected,units_ok,units_locked,coverage,coverage_confirmed FROM price_cycles")
		if err != nil {
			return err
		}
		for rows.Next() {
			var id int64
			var expected, ok, locked any
			var coverage, confirmed sql.NullFloat64
			if err := rows.Scan(&id, &expected, &ok, &locked, &coverage, &confirmed); err != nil {
				rows.Close()
				return err
			}
			wantCoverage, wantConfirmed := sql.NullFloat64{}, sql.NullFloat64{}
			if expected != nil {
				e, valid := expected.(int64)
				if !valid || e < 0 {
					rows.Close()
					return fmt.Errorf("周期%d的冻结计数不完整，已保留原值并停止升级", id)
				}
				if e > 0 {
					o, validOK := ok.(int64)
					l, validLocked := locked.(int64)
					if !validOK || !validLocked || o < 0 || l < 0 {
						rows.Close()
						return fmt.Errorf("周期%d的冻结计数不完整，已保留原值并停止升级", id)
					}
					wantCoverage = sql.NullFloat64{Float64: migrationRound(float64(o)/float64(e), 4), Valid: true}
					wantConfirmed = sql.NullFloat64{Float64: migrationRound((float64(o)+float64(l))/float64(e), 4), Valid: true}
				}
			}
			if coverage != wantCoverage || confirmed != wantConfirmed {
				rows.Close()
				return fmt.Errorf("周期%d的历史覆盖比率与冻结计数不一致，已保留原值并停止升级", id)
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
	}
	return migrationDropProjections(ctx, conn, map[string][]string{"price_cycles": {"coverage", "coverage_confirmed"}})
}

func migrationParseTime(value sql.NullString) (*time.Time, error) {
	if !value.Valid {
		return nil, nil
	}
	for _, layout := range []string{"2006-01-02 15:04:05.999999999", "2006-01-02T15:04:05.999999999", "2006-01-02"} {
		if result, err := time.Parse(layout, value.String); err == nil {
			return &result, nil
		}
	}
	return nil, fmt.Errorf("历史时间无法解析，已保留原值")
}

func migrateCycleTimingProjection(ctx context.Context, conn *sql.Conn) error {
	columns, err := migrationColumns(ctx, conn, "price_cycles")
	if err != nil {
		return err
	}
	if columns["duration_seconds"] != columns["stage_ms_json"] {
		return fmt.Errorf("周期历史耗时列不完整，已保留原结构并停止升级")
	}
	if columns["duration_seconds"] {
		rows, err := conn.QueryContext(ctx, `SELECT id,CAST(started_at AS TEXT),CAST(running_at AS TEXT),CAST(repairing_at AS TEXT),CAST(finalizing_at AS TEXT),CAST(finished_at AS TEXT),duration_seconds,stage_ms_json FROM price_cycles WHERE units_expected IS NOT NULL`)
		if err != nil {
			return err
		}
		for rows.Next() {
			var id int64
			var raw [5]sql.NullString
			var duration sql.NullFloat64
			var stages sql.NullString
			if err := rows.Scan(&id, &raw[0], &raw[1], &raw[2], &raw[3], &raw[4], &duration, &stages); err != nil {
				rows.Close()
				return err
			}
			var times [5]*time.Time
			for i, value := range raw {
				times[i], err = migrationParseTime(value)
				if err != nil {
					rows.Close()
					return err
				}
			}
			want := sql.NullFloat64{}
			if times[0] != nil && times[4] != nil {
				want = sql.NullFloat64{Float64: migrationRound(float64(times[4].Sub(*times[0])/time.Microsecond)/1e6, 1), Valid: true}
			}
			elapsed := func(a, b *time.Time) float64 {
				if a == nil || b == nil {
					return 0
				}
				return math.Max(0, math.Trunc(float64(b.Sub(*a)/time.Microsecond)/1e6*1000))
			}
			runningEnd := times[2]
			if runningEnd == nil {
				runningEnd = times[3]
			}
			if runningEnd == nil {
				runningEnd = times[4]
			}
			expected := map[string]any{"planning": elapsed(times[0], times[1]), "running": elapsed(times[1], runningEnd), "repairing": elapsed(times[2], times[3]), "finalizing": elapsed(times[3], times[4])}
			var actual any
			if stages.Valid {
				if err := json.Unmarshal([]byte(stages.String), &actual); err != nil {
					rows.Close()
					return err
				}
			}
			if duration != want || !reflect.DeepEqual(actual, expected) {
				rows.Close()
				return fmt.Errorf("周期%d的历史耗时与时间边界不一致，已保留原值并停止升级", id)
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
	}
	return migrationDropProjections(ctx, conn, map[string][]string{"price_cycles": {"duration_seconds", "stage_ms_json"}})
}

func migrateProviderConfig(ctx context.Context, conn *sql.Conn) error {
	legacy := []string{"protocol", "base_url", "models", "models_disabled", "api_key", "context_window"}
	rows, err := conn.QueryContext(ctx, "SELECT key,CAST(value_json AS TEXT) FROM app_settings WHERE key IN ('pilot.llm.protocol','pilot.llm.base_url','pilot.llm.models','pilot.llm.models_disabled','pilot.llm.api_key','pilot.llm.context_window','pilot.llm.providers','pilot.llm.provider_key.legacy-v1')")
	if err != nil {
		return err
	}
	values := map[string]any{}
	rawValues := map[string]string{}
	for rows.Next() {
		var key string
		var stored sql.NullString
		if err := rows.Scan(&key, &stored); err != nil {
			rows.Close()
			return err
		}
		raw := "null"
		if stored.Valid {
			raw = stored.String
		}
		value, err := migrationDecodeJSON(raw)
		if err != nil {
			rows.Close()
			return err
		}
		values[strings.TrimPrefix(key, "pilot.llm.")] = value
		rawValues[strings.TrimPrefix(key, "pilot.llm.")] = raw
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	present := false
	for _, key := range legacy {
		if _, ok := values[key]; ok {
			present = true
			break
		}
	}
	if !present {
		return nil
	}
	if _, ok := values["providers"].([]any); !ok {
		if _, exists := values["provider_key.legacy-v1"]; exists {
			return fmt.Errorf("旧供应商迁移身份已被占用，已保留原配置并停止升级")
		}
		cleanString := func(value any, fallback string) string {
			if !migrationTruthy(value) {
				return fallback
			}
			return strings.TrimSpace(migrationString(value))
		}
		protocol := cleanString(values["protocol"], "openai")
		if protocol != "openai" && protocol != "openai-responses" && protocol != "anthropic" && protocol != "ollama" {
			protocol = "openai"
		}
		models := func(raw any) []string {
			out := []string{}
			if items, ok := raw.([]any); ok {
				for _, item := range items {
					s := migrationString(item)
					if strings.TrimSpace(s) != "" {
						out = append(out, s)
						if len(out) == 50 {
							break
						}
					}
				}
			}
			return out
		}
		var window any
		if raw := values["context_window"]; raw != nil {
			switch value := raw.(type) {
			case bool:
				if value {
					window = int64(1)
				}
			case json.Number:
				if f, err := strconv.ParseFloat(value.String(), 64); err == nil && f >= 1 && f < 2000001 {
					window = int64(f)
				}
			case string:
				if n, err := strconv.ParseInt(strings.TrimSpace(value), 10, 64); err == nil && n > 0 && n <= 2000000 {
					window = n
				}
			}
		}
		provider := map[string]any{"id": "legacy-v1", "name": "默认接入", "protocol": protocol, "base_url": cleanString(values["base_url"], ""), "models": models(values["models"]), "models_disabled": models(values["models_disabled"]), "context_window": window}
		encoded, err := json.Marshal([]any{provider})
		if err != nil {
			return err
		}
		now := time.Now().In(time.FixedZone("Asia/Shanghai", 8*3600)).Format("2006-01-02 15:04:05.000000")
		pairs := [][2]string{{"pilot.llm.providers", string(encoded)}, {"pilot.llm.active", `"legacy-v1"`}}
		if key, exists := values["api_key"]; exists && migrationTruthy(key) {
			pairs = append(pairs, [2]string{"pilot.llm.provider_key.legacy-v1", rawValues["api_key"]})
		}
		encodedPairs, err := json.Marshal(pairs)
		if err != nil {
			return err
		}
		if _, err := conn.ExecContext(ctx, `INSERT INTO app_settings(key,value_json,updated_at) SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]'),? FROM json_each(?) WHERE 1 ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at`, now, string(encodedPairs)); err != nil {
			return err
		}
	}
	return migrationExec(ctx, conn, "DELETE FROM app_settings WHERE key IN ('pilot.llm.protocol','pilot.llm.base_url','pilot.llm.models','pilot.llm.models_disabled','pilot.llm.api_key','pilot.llm.context_window')")
}

type migrationCooldownNode struct {
	id, runtime  string
	original     sql.NullString
	subscription sql.NullInt64
	until        *time.Time
	ambiguous    bool
}

func migrateNodeCooldown(ctx context.Context, conn *sql.Conn) error {
	columns, err := migrationColumns(ctx, conn, "clash_nodes")
	if err != nil {
		return err
	}
	if len(columns) == 0 {
		return nil
	}
	if len(columns) != 4 || !columns["id"] || !columns["subscription_id"] || !columns["name"] || !columns["cooldown_until"] {
		return fmt.Errorf("旧节点冷却结构未知，已保留原数据并停止升级")
	}
	extensions, err := conn.QueryContext(ctx, "SELECT sql FROM sqlite_schema WHERE type IN ('view','trigger') AND sql IS NOT NULL")
	if err != nil {
		return err
	}
	mentions := regexp.MustCompile(`(?i)\bclash_nodes\b`)
	for extensions.Next() {
		var ddl string
		if err := extensions.Scan(&ddl); err != nil {
			extensions.Close()
			return err
		}
		if mentions.MatchString(ddl) {
			extensions.Close()
			return fmt.Errorf("旧节点冷却仍被扩展视图或触发器引用，已停止升级")
		}
	}
	err = extensions.Err()
	extensions.Close()
	if err != nil {
		return err
	}
	tables, err := migrationTableNames(ctx, conn)
	if err != nil {
		return err
	}
	for _, table := range tables {
		targets, err := migrationForeignTargets(ctx, conn, table)
		if err != nil {
			return err
		}
		for _, target := range targets {
			if strings.EqualFold(target, "clash_nodes") {
				return fmt.Errorf("旧节点冷却仍被外键引用，已停止升级")
			}
		}
	}
	indexes, err := conn.QueryContext(ctx, "SELECT name FROM pragma_index_list('clash_nodes')")
	if err != nil {
		return err
	}
	var names []string
	for indexes.Next() {
		var name string
		if err := indexes.Scan(&name); err != nil {
			indexes.Close()
			return err
		}
		names = append(names, name)
	}
	err = indexes.Err()
	indexes.Close()
	if err != nil {
		return err
	}
	for _, name := range names {
		if name != "ix_clash_nodes_sub_name" {
			return fmt.Errorf("旧节点冷却索引未知，已停止升级")
		}
		if err := migrationCheckIndex(ctx, conn, "clash_nodes", name, []string{"subscription_id", "name"}); err != nil {
			return err
		}
	}
	rows, err := conn.QueryContext(ctx, "SELECT node_id,runtime_name,subscription_id,original_name,CAST(cooldown_until AS TEXT) FROM proxy_nodes")
	if err != nil {
		return err
	}
	byRuntime := map[string]*migrationCooldownNode{}
	bySource := map[string]*migrationCooldownNode{}
	sourceKey := func(sub int64, name string) string { return strconv.FormatInt(sub, 10) + "\x00" + name }
	for rows.Next() {
		node := &migrationCooldownNode{}
		var raw sql.NullString
		if err := rows.Scan(&node.id, &node.runtime, &node.subscription, &node.original, &raw); err != nil {
			rows.Close()
			return err
		}
		node.until, err = migrationParseTime(raw)
		if err != nil {
			rows.Close()
			return err
		}
		byRuntime[node.runtime] = node
		if node.subscription.Valid && node.original.Valid {
			key := sourceKey(node.subscription.Int64, node.original.String)
			if _, exists := bySource[key]; exists {
				bySource[key] = &migrationCooldownNode{ambiguous: true}
			} else {
				bySource[key] = node
			}
		}
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	match := func(sub int64, name string) *migrationCooldownNode {
		keys := proxypool.NameKeys(name)
		for _, key := range keys {
			if node := bySource[sourceKey(sub, key)]; node != nil {
				return node
			}
		}
		owned := func(node *migrationCooldownNode) bool {
			return node != nil && (node.subscription.Valid && node.subscription.Int64 == sub || !node.subscription.Valid && strings.HasPrefix(node.runtime, strconv.FormatInt(sub, 10)+"|"))
		}
		for _, key := range keys {
			if node := byRuntime[strconv.FormatInt(sub, 10)+"|"+key]; owned(node) {
				return node
			}
		}
		if node := byRuntime[name]; owned(node) {
			return node
		}
		prefix, rest, has := strings.Cut(name, "|")
		if has && prefix == strconv.FormatInt(sub, 10) {
			for _, key := range proxypool.NameKeys(rest) {
				if node := bySource[sourceKey(sub, key)]; node != nil {
					return node
				}
			}
		}
		return nil
	}
	rows, err = conn.QueryContext(ctx, "SELECT subscription_id,name,CAST(cooldown_until AS TEXT) FROM clash_nodes")
	if err != nil {
		return err
	}
	deadlines := map[string]time.Time{}
	nowLocal := time.Now().In(time.FixedZone("Asia/Shanghai", 8*3600))
	now := time.Date(nowLocal.Year(), nowLocal.Month(), nowLocal.Day(), nowLocal.Hour(), nowLocal.Minute(), nowLocal.Second(), nowLocal.Nanosecond(), time.UTC)
	for rows.Next() {
		var sub int64
		var name string
		var raw sql.NullString
		if err := rows.Scan(&sub, &name, &raw); err != nil {
			rows.Close()
			return err
		}
		until, err := migrationParseTime(raw)
		if err != nil {
			rows.Close()
			return err
		}
		if until == nil {
			continue
		}
		node := match(sub, name)
		if node == nil || node.ambiguous {
			if until.After(now) {
				rows.Close()
				return fmt.Errorf("旧节点存在未能唯一归属的有效冷却，已保留原值并停止升级")
			}
			continue
		}
		deadline := *until
		if node.until != nil && node.until.After(deadline) {
			deadline = *node.until
		}
		if previous, exists := deadlines[node.id]; exists && previous.After(deadline) {
			deadline = previous
		}
		deadlines[node.id] = deadline
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if len(deadlines) > 0 {
		var values [][2]string
		for id, deadline := range deadlines {
			values = append(values, [2]string{id, deadline.Format("2006-01-02 15:04:05.000000")})
		}
		encoded, err := json.Marshal(values)
		if err != nil {
			return err
		}
		if _, err := conn.ExecContext(ctx, `UPDATE proxy_nodes SET cooldown_until=(SELECT json_extract(value,'$[1]') FROM json_each(?) WHERE json_extract(value,'$[0]')=node_id) WHERE node_id IN (SELECT json_extract(value,'$[0]') FROM json_each(?))`, string(encoded), string(encoded)); err != nil {
			return err
		}
	}
	return migrationExec(ctx, conn, "DROP TABLE clash_nodes")
}
