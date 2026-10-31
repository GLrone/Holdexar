package proxypool

// Registry 与订阅快照账本的建表 DDL：逐字对齐 Python 模型 create_all 产物
// （列序/类型名/UNIQUE 表级约束/索引名），缺表即建、不迁移不抬结构版本。

import (
	"context"
	"database/sql"
)

const ddlProxyNodes = `CREATE TABLE IF NOT EXISTS proxy_nodes (
	id INTEGER NOT NULL,
	node_id VARCHAR(64) NOT NULL,
	runtime_name VARCHAR(400) NOT NULL,
	proxy_type VARCHAR(32) NOT NULL,
	server VARCHAR(255),
	normalized_config JSON,
	state VARCHAR(16) NOT NULL,
	first_seen DATETIME,
	subscription_id INTEGER,
	original_name VARCHAR(255),
	last_seen DATETIME,
	last_source_seen DATETIME,
	last_l0_at DATETIME,
	l0_delay_ms INTEGER,
	last_l1_at DATETIME,
	last_l2_at DATETIME,
	exit_ip VARCHAR(64),
	consecutive_failures INTEGER NOT NULL,
	cooldown_until DATETIME,
	PRIMARY KEY (id),
	UNIQUE (node_id),
	UNIQUE (runtime_name)
)`

const ddlSubscriptionSnapshots = `CREATE TABLE IF NOT EXISTS subscription_snapshots (
	id INTEGER NOT NULL,
	subscription_id INTEGER NOT NULL,
	sha256 VARCHAR(64) NOT NULL,
	format VARCHAR(16) NOT NULL,
	raw_path VARCHAR(500),
	url VARCHAR(500),
	node_count INTEGER NOT NULL,
	status VARCHAR(16) NOT NULL,
	http_status INTEGER,
	content_type VARCHAR(100),
	source_channel VARCHAR(32),
	source_egress VARCHAR(100),
	profile_title VARCHAR(100),
	content_disposition VARCHAR(255),
	userinfo VARCHAR(500),
	duration_ms INTEGER,
	attempts_json TEXT,
	provider_nodes_json TEXT,
	error TEXT,
	fetched_at DATETIME,
	PRIMARY KEY (id)
)`

var schemaIndexes = []string{
	"CREATE INDEX IF NOT EXISTS ix_proxy_nodes_subscription_id ON proxy_nodes (subscription_id)",
	"CREATE INDEX IF NOT EXISTS ix_proxy_nodes_exit_ip ON proxy_nodes (exit_ip)",
	"CREATE INDEX IF NOT EXISTS ix_subscription_snapshots_subscription_id ON subscription_snapshots (subscription_id)",
}

// EnsureSchema 幂等建两表与索引；ctx 由写闸租约的事务传入。
func EnsureSchema(ctx context.Context, exec interface {
	ExecContext(ctx context.Context, query string, args ...any) (sql.Result, error)
}) error {
	for _, ddl := range []string{ddlProxyNodes, ddlSubscriptionSnapshots} {
		if _, err := exec.ExecContext(ctx, ddl); err != nil {
			return err
		}
	}
	for _, ddl := range schemaIndexes {
		if _, err := exec.ExecContext(ctx, ddl); err != nil {
			return err
		}
	}
	return nil
}
