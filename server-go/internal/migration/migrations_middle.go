package migration

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strconv"
	"strings"

	"holdexar/server-go/internal/proxypool"
	"holdexar/server-go/internal/secretbox"
)

var middleMigrationSteps = map[int]migrationStep{
	14: migrateDropRedundantHistoryIndexes,
	15: func(ctx context.Context, conn *sql.Conn) error {
		return migrationExec(ctx, conn,
			"CREATE TABLE IF NOT EXISTS redeem_activations (id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, created_at DATETIME NOT NULL, kind VARCHAR(10) NOT NULL, status VARCHAR(10) NOT NULL, product_key VARCHAR(120) NOT NULL, subid VARCHAR(40) NOT NULL, sub_name VARCHAR(300) NOT NULL, detail VARCHAR(300) NOT NULL, raw TEXT NOT NULL, steamid VARCHAR(32) NOT NULL)",
			"CREATE INDEX IF NOT EXISTS ix_ra_created ON redeem_activations (created_at)")
	},
	16: func(ctx context.Context, conn *sql.Conn) error {
		return migrationExec(ctx, conn,
			"DROP TRIGGER IF EXISTS games_name_bi_au",
			"CREATE TRIGGER games_name_bi_au AFTER UPDATE OF name, name_en ON games WHEN old.name IS NOT new.name OR old.name_en IS NOT new.name_en BEGIN DELETE FROM games_name_fts WHERE appid = old.appid; INSERT INTO games_name_fts(appid, name_bi) VALUES (new.appid, name_bigram(new.name, new.name_en)); END")
	},
	17: migrateProxySubscriptionIdentity,
	18: migrateHistorySnapshotUnique,
	19: migrateNotificationIdentity,
	20: func(ctx context.Context, conn *sql.Conn) error {
		return migrationExec(ctx, conn, "UPDATE game_current_prices SET attempt_outcome = CASE WHEN price_status IN ('ok', 'locked') THEN 'success' ELSE 'failed' END, steam_answer = CASE price_status WHEN 'ok' THEN 'ok' WHEN 'locked' THEN 'locked' ELSE NULL END, last_success_at = CASE WHEN price_status IN ('ok', 'locked') THEN updated_at ELSE NULL END WHERE attempt_outcome IS NULL")
	},
	21: migratePriceSnapshot,
	22: migrateRetireOrchestration,
	23: func(ctx context.Context, conn *sql.Conn) error {
		columns, err := migrationColumns(ctx, conn, "price_cycles")
		if err != nil || !columns["coverage_json"] {
			return err
		}
		return migrationExec(ctx, conn, "ALTER TABLE price_cycles DROP COLUMN coverage_json")
	},
	24: migrateProxyLedgerColumns,
}

func migrateDropRedundantHistoryIndexes(ctx context.Context, conn *sql.Conn) error {
	var found int
	if err := conn.QueryRowContext(ctx, "SELECT count(*) FROM sqlite_schema WHERE type='index' AND tbl_name='game_price_history' AND name='ux_gph_snapshot'").Scan(&found); err != nil || found == 0 {
		return err
	}
	return migrationExec(ctx, conn, "DROP INDEX IF EXISTS ix_gph_appid_region_snapshot", "DROP INDEX IF EXISTS ix_gph_sub_id")
}

func migrationTableDeclarations(ddl, table string) ([]string, string, error) {
	invalid := func() ([]string, string, error) {
		return nil, "", fmt.Errorf("无法安全迁移 %s 的整数身份结构", table)
	}
	start, end, depth, offset := -1, -1, 0, 0
	var quote byte
	lineComment, blockComment := false, false
	var parts []string
	for i := 0; i < len(ddl); i++ {
		c := ddl[i]
		if lineComment {
			if c == '\n' {
				lineComment = false
			}
			continue
		}
		if blockComment {
			if c == '*' && i+1 < len(ddl) && ddl[i+1] == '/' {
				blockComment = false
				i++
			}
			continue
		}
		if quote != 0 {
			if c == quote {
				if i+1 < len(ddl) && ddl[i+1] == quote {
					i++
				} else {
					quote = 0
				}
			}
			continue
		}
		if c == '-' && i+1 < len(ddl) && ddl[i+1] == '-' {
			lineComment = true
			i++
			continue
		}
		if c == '/' && i+1 < len(ddl) && ddl[i+1] == '*' {
			blockComment = true
			i++
			continue
		}
		if c == '\'' || c == '"' || c == '`' || c == '[' {
			quote = c
			if c == '[' {
				quote = ']'
			}
			continue
		}
		if c == '(' {
			if start < 0 {
				start = i
				offset = i + 1
			}
			depth++
		} else if c == ')' {
			depth--
			if depth < 0 {
				return invalid()
			}
			if depth == 0 {
				end = i
				parts = append(parts, ddl[offset:i])
				break
			}
		} else if c == ',' && depth == 1 {
			parts = append(parts, ddl[offset:i])
			offset = i + 1
		}
	}
	if start < 0 || end < start || depth != 0 || quote != 0 || blockComment {
		return invalid()
	}
	if regexp.MustCompile(`(?i)\bWITHOUT\s+ROWID\b`).MatchString(ddl[end+1:]) {
		return invalid()
	}
	for _, part := range parts {
		if strings.TrimSpace(part) == "" {
			return invalid()
		}
	}
	return parts, ddl[end:], nil
}

var migrationSQLLiterals = regexp.MustCompile(`(?s)'(?:''|[^'])*'|"(?:""|[^"])*"|` + "`(?:``|[^`])*`" + `|\[[^\]]*\]|/\*.*?\*/|--[^\n]*`)

func migrationSQLSyntax(ddl string) string {
	return migrationSQLLiterals.ReplaceAllStringFunc(ddl, func(value string) string { return strings.Repeat(" ", len(value)) })
}

func migrationIdentityTableDDL(ddl, table string) (string, error) {
	parts, suffix, err := migrationTableDeclarations(ddl, table)
	if err != nil {
		return "", err
	}
	identifier := `(?:id|"id"|` + "`id`" + `|\[id\])`
	primaryKey := regexp.MustCompile(`(?i)^\s*PRIMARY\s+KEY\s*\(\s*` + identifier + `\s*\)\s*$`)
	column := regexp.MustCompile(`(?is)^(\s*` + identifier + `\s+INTEGER)\b(.*)$`)
	inlinePrimary := regexp.MustCompile(`(?i)\bPRIMARY\s+KEY\b`)
	alreadyAuto := regexp.MustCompile(`(?i)\bPRIMARY\s+KEY\s+AUTOINCREMENT\b`)
	desc := regexp.MustCompile(`(?i)\bDESC\b`)
	count, columns, auto := 0, 0, false
	var converted []string
	for _, part := range parts {
		if primaryKey.MatchString(part) {
			count++
			continue
		}
		match := column.FindStringSubmatch(part)
		if match == nil {
			converted = append(converted, part)
			continue
		}
		columns++
		rest := match[2]
		syntax := migrationSQLSyntax(rest)
		primary := inlinePrimary.FindStringIndex(syntax)
		if primary != nil {
			if desc.MatchString(syntax) {
				return "", fmt.Errorf("无法安全迁移 %s 的整数身份约束", table)
			}
			if alreadyAuto.MatchString(syntax) {
				auto = true
			} else {
				rest = rest[:primary[1]] + " AUTOINCREMENT" + rest[primary[1]:]
			}
			count++
		} else {
			rest = " PRIMARY KEY AUTOINCREMENT" + rest
		}
		converted = append(converted, match[1]+rest)
	}
	if count != 1 || columns != 1 {
		return "", fmt.Errorf("无法安全迁移 %s 的主键定义", table)
	}
	if auto {
		return "", nil
	}
	return "CREATE TABLE " + migrationQuoteIdent(table+"_identity_new") + " (" + strings.Join(converted, ",") + suffix, nil
}

func migrationTableNames(ctx context.Context, conn *sql.Conn) ([]string, error) {
	rows, err := conn.QueryContext(ctx, "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var names []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, err
		}
		names = append(names, name)
	}
	return names, rows.Err()
}

func migrationForeignTargets(ctx context.Context, conn *sql.Conn, table string) ([]string, error) {
	rows, err := conn.QueryContext(ctx, "SELECT \"table\" FROM pragma_foreign_key_list(?)", table)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var targets []string
	for rows.Next() {
		var target string
		if err := rows.Scan(&target); err != nil {
			return nil, err
		}
		targets = append(targets, strings.ToLower(target))
	}
	return targets, rows.Err()
}

type migrationSchemaObject struct{ kind, ddl string }

func migrationSchemaObjects(ctx context.Context, conn *sql.Conn, table string) ([]migrationSchemaObject, error) {
	rows, err := conn.QueryContext(ctx, "SELECT type,sql FROM sqlite_schema WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name", table)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var objects []migrationSchemaObject
	for rows.Next() {
		var obj migrationSchemaObject
		if err := rows.Scan(&obj.kind, &obj.ddl); err != nil {
			return nil, err
		}
		objects = append(objects, obj)
	}
	return objects, rows.Err()
}

func migrationStoredColumns(ctx context.Context, conn *sql.Conn, table, exclude string) (string, error) {
	rows, err := conn.QueryContext(ctx, "SELECT name FROM pragma_table_xinfo(?) WHERE hidden=0 ORDER BY cid", table)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	var columns []string
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return "", err
		}
		if name != exclude {
			columns = append(columns, migrationQuoteIdent(name))
		}
	}
	return strings.Join(columns, ","), rows.Err()
}

func migrationPreserveSequence(ctx context.Context, conn *sql.Conn, table string, highwater int64) error {
	if _, err := conn.ExecContext(ctx, "INSERT INTO sqlite_sequence(name,seq) SELECT ?,? WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name=?)", table, highwater, table); err != nil {
		return err
	}
	_, err := conn.ExecContext(ctx, "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?", highwater, table)
	return err
}

func migrateNotificationIdentity(ctx context.Context, conn *sql.Conn) error {
	references := map[string]string{
		"price_events": "SELECT event_id AS value FROM notification_candidates",
		"price_cycles": "SELECT cycle_id AS value FROM price_events WHERE cycle_id>0 UNION ALL SELECT cycle_id FROM notification_candidates WHERE cycle_id>0 UNION ALL SELECT cycle_id FROM crawl_jobs WHERE cycle_id>0",
		"crawl_jobs":   "SELECT -cycle_id AS value FROM price_events WHERE cycle_id<0 UNION ALL SELECT -cycle_id FROM notification_candidates WHERE cycle_id<0",
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
		_, ownerIdentity := references[strings.ToLower(table)]
		for _, target := range targets {
			if _, targetIdentity := references[target]; ownerIdentity || targetIdentity {
				return fmt.Errorf("价格身份迁移发现未知外键关联，已保留原结构与数据并停止升级")
			}
		}
	}
	for _, table := range []string{"price_events", "price_cycles", "crawl_jobs"} {
		var ddl string
		if err := conn.QueryRowContext(ctx, "SELECT sql FROM sqlite_schema WHERE type='table' AND name=?", table).Scan(&ddl); err != nil {
			return err
		}
		var highwater int64
		if err := conn.QueryRowContext(ctx, "SELECT COALESCE(MAX(value),0) FROM (SELECT id AS value FROM "+migrationQuoteIdent(table)+" UNION ALL "+references[table]+")").Scan(&highwater); err != nil {
			return err
		}
		rebuilt, err := migrationIdentityTableDDL(ddl, table)
		if err != nil {
			return err
		}
		if rebuilt != "" {
			objects, err := migrationSchemaObjects(ctx, conn, table)
			if err != nil {
				return err
			}
			columns, err := migrationStoredColumns(ctx, conn, table, "")
			if err != nil {
				return err
			}
			target := migrationQuoteIdent(table + "_identity_new")
			if err := migrationExec(ctx, conn, rebuilt, "INSERT INTO "+target+" ("+columns+") SELECT "+columns+" FROM "+migrationQuoteIdent(table), "DROP TABLE "+migrationQuoteIdent(table), "ALTER TABLE "+target+" RENAME TO "+migrationQuoteIdent(table)); err != nil {
				return err
			}
			for _, object := range objects {
				if err := migrationExec(ctx, conn, object.ddl); err != nil {
					return err
				}
			}
		}
		if err := migrationPreserveSequence(ctx, conn, table, highwater); err != nil {
			return err
		}
	}
	return migrateHistorySnapshotUnique(ctx, conn)
}

func migrateRetireOrchestration(ctx context.Context, conn *sql.Conn) error {
	rows, err := conn.QueryContext(ctx, "SELECT sql FROM sqlite_schema WHERE type IN ('view','trigger') AND sql IS NOT NULL")
	if err != nil {
		return err
	}
	ref := regexp.MustCompile(`(?i)\borchestration_events\b`)
	for rows.Next() {
		var ddl string
		if err := rows.Scan(&ddl); err != nil {
			rows.Close()
			return err
		}
		if ref.MatchString(ddl) {
			rows.Close()
			return fmt.Errorf("运行记录仍被扩展视图或触发器引用，已保留原结构与数据并停止升级")
		}
	}
	err = rows.Err()
	rows.Close()
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
			if target == "orchestration_events" {
				return fmt.Errorf("运行记录迁移发现未知外键关联，已保留原结构与数据并停止升级")
			}
		}
	}
	return migrationExec(ctx, conn, "DROP TABLE IF EXISTS orchestration_events")
}

func migrateProxyLedgerColumns(ctx context.Context, conn *sql.Conn) error {
	columns, err := migrationColumns(ctx, conn, "proxy_nodes")
	if err != nil {
		return err
	}
	if columns["fingerprint"] {
		var mismatch int
		if err := conn.QueryRowContext(ctx, "SELECT count(*) FROM (SELECT 1 FROM proxy_nodes WHERE node_id IS NOT fingerprint LIMIT 1)").Scan(&mismatch); err != nil {
			return err
		}
		if mismatch != 0 {
			return fmt.Errorf("节点身份不一致，已保留原结构与数据并停止升级")
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
				if target == "proxy_nodes" {
					return fmt.Errorf("节点身份迁移发现未知外键关联，已保留原结构与数据并停止升级")
				}
			}
		}
		objects, err := migrationSchemaObjects(ctx, conn, "proxy_nodes")
		if err != nil {
			return err
		}
		fingerprint := regexp.MustCompile(`(?i)\bfingerprint\b`)
		for _, object := range objects {
			if object.kind == "trigger" && fingerprint.MatchString(object.ddl) {
				return fmt.Errorf("节点身份仍被扩展触发器引用，已停止升级")
			}
		}
		var ddl string
		if err := conn.QueryRowContext(ctx, "SELECT sql FROM sqlite_schema WHERE type='table' AND name='proxy_nodes'").Scan(&ddl); err != nil {
			return err
		}
		parts, suffix, err := migrationTableDeclarations(ddl, "proxy_nodes")
		if err != nil {
			return err
		}
		identifier := `(?:fingerprint|"fingerprint"|` + "`fingerprint`" + `|\[fingerprint\])`
		column := regexp.MustCompile(`(?i)^\s*` + identifier + `\s`)
		unique := regexp.MustCompile(`(?i)^\s*UNIQUE\s*\(\s*` + identifier + `\s*\)\s*$`)
		var kept []string
		for _, part := range parts {
			if !column.MatchString(part) && !unique.MatchString(part) {
				kept = append(kept, part)
			}
		}
		var sequence int64
		if exists, err := migrationTableExists(ctx, conn, "sqlite_sequence"); err != nil {
			return err
		} else if exists {
			if err := conn.QueryRowContext(ctx, "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='proxy_nodes'),0)").Scan(&sequence); err != nil {
				return err
			}
		}
		names, err := migrationStoredColumns(ctx, conn, "proxy_nodes", "fingerprint")
		if err != nil {
			return err
		}
		if err := migrationExec(ctx, conn, "CREATE TABLE proxy_nodes_identity_new ("+strings.Join(kept, ",")+suffix, "INSERT INTO proxy_nodes_identity_new ("+names+") SELECT "+names+" FROM proxy_nodes", "DROP TABLE proxy_nodes", "ALTER TABLE proxy_nodes_identity_new RENAME TO proxy_nodes"); err != nil {
			return err
		}
		oldIndex := regexp.MustCompile(`(?i)^CREATE\s+UNIQUE\s+INDEX\s+ux_proxy_nodes_fingerprint\s+ON\s+proxy_nodes\s*\(\s*fingerprint\s*\)$`)
		for _, object := range objects {
			if !oldIndex.MatchString(object.ddl) {
				if err := migrationExec(ctx, conn, object.ddl); err != nil {
					return err
				}
			}
		}
		if sequence != 0 {
			if err := migrationPreserveSequence(ctx, conn, "proxy_nodes", sequence); err != nil {
				return err
			}
		}
	}
	cooldown, err := migrationColumns(ctx, conn, "clash_nodes")
	if err != nil {
		return err
	}
	for _, name := range []string{"exit_ip", "status", "latency_ms", "fail_count", "last_checked_at", "created_at", "revive_passes"} {
		if cooldown[name] {
			if err := migrationExec(ctx, conn, "ALTER TABLE clash_nodes DROP COLUMN "+migrationQuoteIdent(name)); err != nil {
				return err
			}
		}
	}
	return nil
}

type migrationProxyIdentity struct {
	id               int64
	oldID, fp, state string
}

func migrateProxySubscriptionIdentity(ctx context.Context, conn *sql.Conn) error {
	if exists, err := migrationTableExists(ctx, conn, "proxy_nodes"); err != nil || !exists {
		return err
	}
	columns, err := migrationColumns(ctx, conn, "proxy_nodes")
	if err != nil {
		return err
	}
	for _, column := range []struct{ name, definition string }{{"subscription_id", "INTEGER"}, {"original_name", "VARCHAR(255)"}} {
		if !columns[column.name] {
			if err := migrationExec(ctx, conn, "ALTER TABLE proxy_nodes ADD COLUMN "+column.name+" "+column.definition); err != nil {
				return err
			}
		}
	}
	tags := map[int64]string{}
	rows, err := conn.QueryContext(ctx, "SELECT id,url FROM proxy_subscriptions")
	if err != nil {
		return err
	}
	var subscriptions []struct {
		id  int64
		url string
	}
	for rows.Next() {
		var id int64
		var raw sql.NullString
		if err := rows.Scan(&id, &raw); err != nil {
			rows.Close()
			return err
		}
		subscriptions = append(subscriptions, struct {
			id  int64
			url string
		}{id, raw.String})
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	dataDir, err := migrationDataDir(ctx, conn)
	if err != nil {
		return err
	}
	for _, subscription := range subscriptions {
		raw := subscription.url
		if secretbox.IsEncrypted(raw) {
			if plain, err := secretbox.DecryptSecretIn(dataDir, raw, "proxy-subscription"); err == nil {
				raw = plain
			} else {
				raw = ""
			}
		}
		if strings.TrimSpace(raw) == "" {
			tags[subscription.id] = "sub" + strconv.FormatInt(subscription.id, 10)
		} else {
			tags[subscription.id] = proxypool.SubscriptionTag(raw)
		}
	}
	if exists, err := migrationTableExists(ctx, conn, "proxy_node_sources"); err != nil {
		return err
	} else if exists {
		if err := migrationExec(ctx, conn, "UPDATE proxy_nodes SET subscription_id=(SELECT subscription_id FROM proxy_node_sources WHERE node_id=proxy_nodes.node_id LIMIT 1),original_name=(SELECT original_name FROM proxy_node_sources WHERE node_id=proxy_nodes.node_id LIMIT 1) WHERE subscription_id IS NULL AND EXISTS(SELECT 1 FROM proxy_node_sources WHERE node_id=proxy_nodes.node_id)"); err != nil {
			return err
		}
	}
	if err := migrationExec(ctx, conn, "UPDATE proxy_nodes SET subscription_id=CAST(substr(runtime_name,1,instr(runtime_name,'|')-1) AS INTEGER),original_name=substr(runtime_name,instr(runtime_name,'|')+1) WHERE subscription_id IS NULL AND instr(runtime_name,'|')>1 AND substr(runtime_name,1,instr(runtime_name,'|')-1) NOT GLOB '*[^0-9]*'"); err != nil {
		return err
	}
	rows, err = conn.QueryContext(ctx, "SELECT id,node_id,subscription_id,normalized_config,state FROM proxy_nodes")
	if err != nil {
		return err
	}
	groups := map[string][]migrationProxyIdentity{}
	for rows.Next() {
		var member migrationProxyIdentity
		var sid sql.NullInt64
		var raw, state sql.NullString
		if err := rows.Scan(&member.id, &member.oldID, &sid, &raw, &state); err != nil {
			rows.Close()
			return err
		}
		member.state = state.String
		config := map[string]any{}
		decoder := json.NewDecoder(strings.NewReader(raw.String))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err == nil {
			if _, err := decoder.Token(); err == io.EOF {
				if obj, ok := value.(map[string]any); ok {
					config = obj
				}
			}
		}
		tag := ""
		if sid.Valid {
			tag = tags[sid.Int64]
		}
		fp, err := proxypool.NodeFingerprint(config, tag)
		if err != nil {
			rows.Close()
			return err
		}
		member.fp = fp
		groups[fp] = append(groups[fp], member)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	if err := migrationExec(ctx, conn, "CREATE TEMP TABLE proxy_identity_migration(id INTEGER PRIMARY KEY,old_id TEXT,fp TEXT,keep INTEGER)"); err != nil {
		return err
	}
	defer conn.ExecContext(context.Background(), "DROP TABLE IF EXISTS temp.proxy_identity_migration")
	rank := func(state string) int {
		switch state {
		case "ACTIVE":
			return 0
		case "NEW":
			return 1
		case "STALE":
			return 2
		case "DEAD":
			return 3
		case "RETIRED":
			return 4
		}
		return 9
	}
	var args []any
	var values []string
	flush := func() error {
		if len(values) == 0 {
			return nil
		}
		_, err := conn.ExecContext(ctx, "INSERT INTO temp.proxy_identity_migration VALUES "+strings.Join(values, ","), args...)
		args = nil
		values = nil
		return err
	}
	for _, members := range groups {
		sort.Slice(members, func(i, j int) bool {
			a, b := rank(members[i].state), rank(members[j].state)
			if a != b {
				return a < b
			}
			return members[i].id > members[j].id
		})
		for i, member := range members {
			keep := 0
			if i == 0 {
				keep = 1
			}
			values = append(values, "(?,?,?,?)")
			args = append(args, member.id, member.oldID, member.fp, keep)
			if len(values) == 200 {
				if err := flush(); err != nil {
					return err
				}
			}
		}
	}
	if err := flush(); err != nil {
		return err
	}
	if len(groups) > 0 {
		if err := migrationExec(ctx, conn, "UPDATE proxy_nodes SET node_id=m.fp,fingerprint=m.fp FROM temp.proxy_identity_migration m WHERE proxy_nodes.id=m.id AND m.keep=1"); err != nil {
			return err
		}
	}
	return migrationExec(ctx, conn,
		"DELETE FROM health_observations WHERE node_id IN (SELECT old_id FROM temp.proxy_identity_migration WHERE keep=0)",
		"DELETE FROM proxy_nodes WHERE id IN (SELECT id FROM temp.proxy_identity_migration WHERE keep=0)",
		"DROP TABLE IF EXISTS proxy_node_sources",
		"CREATE INDEX IF NOT EXISTS ix_proxy_nodes_subscription_id ON proxy_nodes(subscription_id)")
}
