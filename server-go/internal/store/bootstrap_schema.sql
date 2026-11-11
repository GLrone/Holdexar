CREATE TABLE achievement_defs (
	appid BIGINT NOT NULL, 
	image_name VARCHAR(160) NOT NULL, 
	apiname VARCHAR(160), 
	name VARCHAR(400), 
	description VARCHAR(800), 
	icon_url VARCHAR(500), 
	icon_gray_url VARCHAR(500), 
	global_percent FLOAT, 
	display_order INTEGER NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (appid, image_name)
);

CREATE TABLE achievement_games (
	steamid VARCHAR(20) NOT NULL, 
	appid BIGINT NOT NULL, 
	name VARCHAR(400), 
	playtime_min INTEGER NOT NULL, 
	last_played BIGINT NOT NULL, 
	total_achievements INTEGER NOT NULL, 
	unlocked INTEGER NOT NULL, 
	source VARCHAR(12) NOT NULL, 
	schema_synced_at DATETIME, 
	state_synced_at DATETIME, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (steamid, appid)
);

CREATE TABLE achievement_notes (
	steamid VARCHAR(20) NOT NULL,
	appid BIGINT NOT NULL,
	image_name VARCHAR(160) NOT NULL,
	content VARCHAR(140) NOT NULL,
	mood VARCHAR(12),
	created_at DATETIME NOT NULL,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (steamid, appid, image_name)
);

CREATE TABLE achievement_states (
	steamid VARCHAR(20) NOT NULL, 
	appid BIGINT NOT NULL, 
	image_name VARCHAR(160) NOT NULL, 
	achieved BOOLEAN NOT NULL, 
	unlock_time BIGINT NOT NULL, 
	synced_at DATETIME NOT NULL, 
	PRIMARY KEY (steamid, appid, image_name)
);

CREATE TABLE agent_events (
	id INTEGER NOT NULL, 
	run_id VARCHAR(32) NOT NULL, 
	seq INTEGER NOT NULL, 
	event_type VARCHAR(32) NOT NULL, 
	payload JSON, 
	created_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT ux_agent_event_run_seq UNIQUE (run_id, seq), 
	FOREIGN KEY(run_id) REFERENCES agent_runs (id) ON DELETE CASCADE
);

CREATE TABLE agent_memories (
	id VARCHAR(32) NOT NULL, 
	category VARCHAR(32) NOT NULL, 
	"key" VARCHAR(64) NOT NULL, 
	value JSON NOT NULL, 
	confidence FLOAT NOT NULL, 
	source VARCHAR(64), 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT ux_agent_memory_category_key UNIQUE (category, "key")
);

CREATE TABLE agent_runs (
	id VARCHAR(32) NOT NULL, 
	session_id VARCHAR(32) NOT NULL, 
	runner VARCHAR(16) NOT NULL, 
	"trigger" VARCHAR(16) NOT NULL, 
	meta JSON, 
	status VARCHAR(24) NOT NULL, 
	error_code VARCHAR(64), 
	created_at DATETIME, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES agent_sessions (id) ON DELETE CASCADE
);

CREATE TABLE agent_sessions (
	id VARCHAR(32) NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE alert_events (
	id INTEGER NOT NULL, 
	alert_id INTEGER NOT NULL, 
	appid BIGINT NOT NULL, 
	region VARCHAR(10) NOT NULL, 
	price BIGINT, 
	price_cny BIGINT, 
	triggered_at DATETIME, 
	notified BOOLEAN NOT NULL, 
	details_json JSON, 
	PRIMARY KEY (id)
);

CREATE TABLE app_settings (
	"key" VARCHAR(64) NOT NULL, 
	value_json JSON, 
	updated_at DATETIME, 
	PRIMARY KEY ("key")
);

CREATE TABLE bill_cdk_games (
	id INTEGER NOT NULL, 
	import_id INTEGER NOT NULL, 
	name VARCHAR(300) NOT NULL, 
	appid BIGINT, 
	date VARCHAR(20) NOT NULL, 
	acq VARCHAR(10) NOT NULL, 
	acq_label VARCHAR(40) NOT NULL, 
	manual_fen BIGINT, 
	PRIMARY KEY (id)
);

CREATE TABLE bill_game_txs (
	id INTEGER NOT NULL, 
	import_id INTEGER NOT NULL, 
	date VARCHAR(20) NOT NULL, 
	tx_type VARCHAR(20) NOT NULL, 
	items_json TEXT NOT NULL, 
	currency VARCHAR(10) NOT NULL, 
	amount FLOAT NOT NULL, 
	sign INTEGER NOT NULL, 
	cny_fen BIGINT, 
	fx_rate FLOAT, 
	fx_note VARCHAR(60) NOT NULL, 
	payment VARCHAR(120) NOT NULL, 
	discount_pct VARCHAR(20) NOT NULL, 
	orig_price FLOAT, 
	wallet_balance VARCHAR(60) NOT NULL, 
	base_price VARCHAR(60) NOT NULL, 
	payment_parts_json TEXT, 
	gift_to_json TEXT, 
	is_gift BOOLEAN NOT NULL, 
	is_refund BOOLEAN NOT NULL, 
	orig_is_gift BOOLEAN NOT NULL, 
	raw_idx INTEGER NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE bill_imports (
	id INTEGER NOT NULL, 
	steam_id VARCHAR(20), 
	nickname VARCHAR(120) NOT NULL, 
	avatar_base64 TEXT NOT NULL, 
	source_file VARCHAR(300) NOT NULL, 
	imported_at DATETIME, 
	warnings_json TEXT NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE bill_topup_txs (
	id INTEGER NOT NULL, 
	import_id INTEGER NOT NULL, 
	date VARCHAR(20) NOT NULL, 
	"desc" VARCHAR(300) NOT NULL, 
	currency VARCHAR(10) NOT NULL, 
	amount FLOAT NOT NULL, 
	sign INTEGER NOT NULL, 
	cny_fen BIGINT, 
	fx_rate FLOAT, 
	fx_note VARCHAR(60) NOT NULL, 
	payment VARCHAR(120) NOT NULL, 
	tx_type VARCHAR(20) NOT NULL, 
	is_refund BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE bundle_region_prices (
	id INTEGER NOT NULL, 
	bundle_id INTEGER NOT NULL, 
	region_code VARCHAR(10) NOT NULL, 
	currency VARCHAR(10), 
	price BIGINT, 
	original_price BIGINT, 
	discount_percent INTEGER NOT NULL, 
	bundle_base_discount INTEGER NOT NULL, 
	price_status VARCHAR(20) NOT NULL, 
	cny_fen BIGINT, 
	discount_end_ts INTEGER, 
	app_ids JSON, 
	crawled_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE bundles (
	id INTEGER NOT NULL, 
	bundle_id INTEGER NOT NULL, 
	name VARCHAR(512) NOT NULL, 
	must_purchase_as_set INTEGER NOT NULL, 
	item_kind INTEGER NOT NULL, 
	header_image VARCHAR(1024), 
	is_lowest BOOLEAN NOT NULL, 
	min_cny_fen BIGINT, 
	diff_fen INTEGER DEFAULT '0' NOT NULL, 
	smart_score FLOAT, 
	url VARCHAR(1024), 
	app_ids JSON, 
	view_count INTEGER NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (bundle_id)
);

CREATE TABLE catalog_removals (
	appid BIGINT NOT NULL, 
	reason VARCHAR(200), 
	removed_at DATETIME, 
	PRIMARY KEY (appid)
);

CREATE TABLE challenge_progress (
	steamid VARCHAR(20) NOT NULL,
	challenge_id VARCHAR(40) NOT NULL,
	progress INTEGER NOT NULL,
	status VARCHAR(12) NOT NULL,
	completed_at DATETIME,
	claimed_at DATETIME,
	updated_at DATETIME NOT NULL,
	PRIMARY KEY (steamid, challenge_id)
);

CREATE TABLE crawl_jobs (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	kind VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	mode VARCHAR(10), 
	regions_json JSON, 
	stats_json JSON, 
	started_at DATETIME, 
	finished_at DATETIME, 
	error TEXT, 
	cycle_id INTEGER
);

CREATE TABLE crawl_regions (
	code VARCHAR(10) NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	currency VARCHAR(10) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	sort INTEGER NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (code)
);

CREATE TABLE fact_notices (
	id INTEGER NOT NULL, 
	source VARCHAR(24) NOT NULL, 
	kind VARCHAR(24) NOT NULL, 
	fact_key TEXT NOT NULL, 
	data JSON, 
	occurred_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (fact_key)
);

CREATE TABLE family_groups (
	steamid VARCHAR(20) NOT NULL, 
	family_groupid VARCHAR(40), 
	family_name VARCHAR(120), 
	members_json JSON, 
	fetch_ok BOOLEAN NOT NULL, 
	last_error VARCHAR(300), 
	member_count INTEGER NOT NULL, 
	updated_at DATETIME NOT NULL, 
	play_json JSON, 
	PRIMARY KEY (steamid)
);

CREATE TABLE family_library_snapshots (
	appid BIGINT NOT NULL, 
	family_groupid VARCHAR(40) NOT NULL, 
	name VARCHAR(300), 
	owners_json JSON, 
	time_acquired BIGINT NOT NULL, 
	presence INTEGER NOT NULL, 
	excluded BOOLEAN NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (appid, family_groupid)
);

CREATE TABLE fx_rate_history (
	id INTEGER NOT NULL, 
	currency_code VARCHAR(10) NOT NULL, 
	rate_to_cny FLOAT NOT NULL, 
	source VARCHAR(30), 
	fetched_at DATETIME, 
	rate_date DATE, 
	source_kind VARCHAR(12), 
	observed_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE fx_rates (
	currency_code VARCHAR(10) NOT NULL, 
	rate_to_cny FLOAT NOT NULL, 
	fetched_at DATETIME, 
	PRIMARY KEY (currency_code)
);

CREATE TABLE game_current_prices (
	appid BIGINT NOT NULL, 
	region_code VARCHAR(10) NOT NULL, 
	currency VARCHAR(10), 
	price BIGINT, 
	original_price BIGINT, 
	discount_percent INTEGER NOT NULL, 
	sub_id INTEGER, 
	price_status VARCHAR(20) NOT NULL, 
	fail_count INTEGER NOT NULL, 
	cny_fen BIGINT, 
	discount_end_ts INTEGER, 
	attempt_outcome VARCHAR(10), 
	steam_answer VARCHAR(12), 
	last_success_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (appid, region_code)
);

CREATE TABLE game_price_history (
	id INTEGER NOT NULL, 
	appid BIGINT NOT NULL, 
	region_code VARCHAR(10) NOT NULL, 
	currency VARCHAR(10), 
	price BIGINT, 
	original_price BIGINT, 
	discount_percent INTEGER NOT NULL, 
	sub_id INTEGER, 
	is_gold BOOLEAN NOT NULL, 
	version_suffix VARCHAR(100), 
	is_bundle BOOLEAN NOT NULL, 
	price_status VARCHAR(20) NOT NULL, 
	cny_fen BIGINT, 
	discount_end_ts INTEGER, 
	discount_desc VARCHAR(60), 
	bundle_id INTEGER, 
	bundle_discount_pct INTEGER, 
	steam_event_key VARCHAR(60), 
	source VARCHAR(20) DEFAULT 'steam' NOT NULL, 
	product_unknown BOOLEAN DEFAULT '0' NOT NULL, 
	snapshot_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE game_tags (
	appid BIGINT NOT NULL, 
	tagid INTEGER NOT NULL, 
	weight INTEGER NOT NULL, 
	PRIMARY KEY (appid, tagid)
)
 WITHOUT ROWID

;

CREATE TABLE games (
	appid BIGINT NOT NULL, 
	name VARCHAR(512) NOT NULL, 
	name_en VARCHAR(512), 
	type VARCHAR(20), 
	header_image VARCHAR(1024), 
	store_url VARCHAR(1024), 
	chinese_support VARCHAR(50), 
	family_sharing BOOLEAN NOT NULL, 
	trading_cards BOOLEAN NOT NULL, 
	xgp_tier VARCHAR(50), 
	is_epic BOOLEAN NOT NULL, 
	epic_date VARCHAR(100), 
	is_hb BOOLEAN NOT NULL, 
	hb_data VARCHAR(100), 
	bundle_count INTEGER, 
	series_id VARCHAR(100), 
	hl_flag INTEGER NOT NULL, 
	pp_flag INTEGER NOT NULL, 
	pp_changed_at DATETIME, 
	min_cny_fen BIGINT, 
	diff_fen INTEGER NOT NULL, 
	smart_score FLOAT, 
	is_adult BOOLEAN NOT NULL, 
	is_visual_novel BOOLEAN NOT NULL, 
	release_date VARCHAR(50), 
	removed_at DATETIME, 
	removed_strikes INTEGER NOT NULL, 
	free_kind VARCHAR(10), 
	promo_end_at BIGINT, 
	developers JSON, 
	publishers JSON, 
	positive_rate INTEGER, 
	positive_reviews INTEGER NOT NULL, 
	review_count INTEGER NOT NULL, 
	view_count INTEGER NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (appid)
);

CREATE VIRTUAL TABLE games_name_fts USING fts5(appid UNINDEXED, name_bi);

CREATE TABLE health_observations (
	id INTEGER NOT NULL, 
	node_id VARCHAR(64) NOT NULL, 
	level VARCHAR(2) NOT NULL, 
	ok BOOLEAN NOT NULL, 
	latency_ms INTEGER, 
	detail TEXT, 
	observed_at DATETIME, 
	run_id INTEGER, 
	channel VARCHAR(16), 
	target VARCHAR(500), 
	PRIMARY KEY (id)
);

CREATE TABLE health_runs (
	id INTEGER NOT NULL, 
	channel VARCHAR(16) NOT NULL, 
	subscription_id INTEGER, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	duration_ms INTEGER, 
	total INTEGER NOT NULL, 
	steam_ok INTEGER NOT NULL, 
	ip_known INTEGER NOT NULL, 
	failed INTEGER NOT NULL, 
	end_reason VARCHAR(30), 
	PRIMARY KEY (id)
);

CREATE TABLE history_backfill_state (
	appid BIGINT NOT NULL, 
	region_code VARCHAR(10) NOT NULL, 
	last_ok_at DATETIME, 
	last_empty_at DATETIME, 
	last_error_at DATETIME, 
	last_error VARCHAR(200), 
	points_stored INTEGER NOT NULL, 
	updated_at DATETIME, 
	PRIMARY KEY (appid, region_code)
);

CREATE TABLE monitor_exclusions (
	id INTEGER NOT NULL, 
	target_type VARCHAR(16) NOT NULL, 
	target_id BIGINT NOT NULL, 
	reason VARCHAR(200), 
	active BOOLEAN NOT NULL, 
	created_at DATETIME, 
	cleared_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE monitor_sources (
	id INTEGER NOT NULL, 
	target_type VARCHAR(16) NOT NULL, 
	target_id BIGINT NOT NULL, 
	source VARCHAR(32) NOT NULL, 
	priority INTEGER NOT NULL, 
	active BOOLEAN NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE monitor_targets (
	id INTEGER NOT NULL, 
	target_type VARCHAR(16) NOT NULL, 
	target_id BIGINT NOT NULL, 
	created_at DATETIME, 
	updated_at DATETIME, 
	activated_at DATETIME, 
	released_at DATETIME, 
	excluded_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE notification_candidates (
	id INTEGER NOT NULL, 
	source_kind VARCHAR(16) DEFAULT 'price_event' NOT NULL, 
	event_id INTEGER NOT NULL, 
	cycle_id INTEGER NOT NULL, 
	appid INTEGER NOT NULL, 
	region_code VARCHAR(8), 
	event_type VARCHAR(32) NOT NULL, 
	category VARCHAR(16) NOT NULL, 
	recipient VARCHAR(200) NOT NULL, 
	status VARCHAR(12) NOT NULL, 
	reason TEXT, 
	attempts INTEGER NOT NULL, 
	last_error TEXT, 
	created_at DATETIME, 
	updated_at DATETIME, 
	delivered_at DATETIME, 
	delivery_history_json JSON, 
	payload_json JSON, 
	due_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE preset_games (
	appid BIGINT NOT NULL, 
	name VARCHAR(512), 
	source VARCHAR(80) NOT NULL, 
	added_at DATETIME, 
	PRIMARY KEY (appid)
);

CREATE TABLE price_alerts (
	id INTEGER NOT NULL, 
	appid BIGINT NOT NULL, 
	region VARCHAR(10) NOT NULL, 
	target_type VARCHAR(20) NOT NULL, 
	target_value FLOAT, 
	active BOOLEAN NOT NULL, 
	created_at DATETIME, 
	last_triggered_at DATETIME, 
	repeat_mode VARCHAR(10) NOT NULL, 
	repeat_hours INTEGER NOT NULL, 
	config_json JSON, 
	PRIMARY KEY (id)
);

CREATE TABLE price_cycles (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	kind VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	scope VARCHAR(20) NOT NULL, 
	expected_json JSON, 
	specs_json JSON, 
	notification_scan_json JSON, 
	started_at DATETIME, 
	running_at DATETIME, 
	repairing_at DATETIME, 
	finalizing_at DATETIME, 
	finished_at DATETIME, 
	error TEXT, 
	batches_expected INTEGER, 
	batches_done INTEGER, 
	targets_total INTEGER, 
	targets_done INTEGER, 
	units_expected INTEGER, 
	units_ok INTEGER, 
	units_locked INTEGER, 
	units_failed INTEGER, 
	units_missing INTEGER, 
	units_blocked INTEGER, 
	units_unobserved INTEGER, 
	stale_count INTEGER
);

CREATE TABLE price_events (
	id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, 
	cycle_id INTEGER NOT NULL, 
	appid INTEGER NOT NULL, 
	region_code VARCHAR(8), 
	event_type VARCHAR(32) NOT NULL, 
	previous_json JSON, 
	current_json JSON, 
	occurred_at DATETIME, 
	created_at DATETIME
);

CREATE TABLE proxies (
	id INTEGER NOT NULL, 
	label VARCHAR(100), 
	scheme VARCHAR(10) NOT NULL, 
	host VARCHAR(255) NOT NULL, 
	port INTEGER NOT NULL, 
	username VARCHAR(255), 
	password VARCHAR(255), 
	enabled BOOLEAN NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	latency_ms INTEGER, 
	consecutive_failures INTEGER NOT NULL, 
	last_checked_at DATETIME, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE proxy_events (
	id INTEGER NOT NULL, 
	ts DATETIME, 
	kind VARCHAR(20) NOT NULL, 
	target VARCHAR(255), 
	proxy_label VARCHAR(100), 
	status_code INTEGER, 
	duration_ms INTEGER, 
	error TEXT, 
	PRIMARY KEY (id)
);

CREATE TABLE proxy_job_runs (
	id INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	kind VARCHAR(16) NOT NULL, 
	crawl_job_id INTEGER, 
	started_at DATETIME, 
	finished_at DATETIME, 
	duration_ms INTEGER, 
	regions_json JSON, 
	workers INTEGER, 
	proxy_url VARCHAR(120), 
	pool_sha256 VARCHAR(64), 
	pool_node_count INTEGER, 
	pool_exit_ip_count INTEGER, 
	selected_node VARCHAR(400), 
	node_exit_ip VARCHAR(64), 
	subscription_ids_json JSON, 
	snapshot_ids_json JSON, 
	task_count INTEGER, 
	success_count INTEGER, 
	error_count INTEGER, 
	error_summary JSON, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE proxy_nodes (
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
);

CREATE TABLE proxy_run_exits (
	id INTEGER NOT NULL, 
	run_id INTEGER NOT NULL, 
	exit_ip VARCHAR(64) NOT NULL, 
	endpoint VARCHAR(32) NOT NULL, 
	node VARCHAR(400), 
	requests INTEGER NOT NULL, 
	success INTEGER NOT NULL, 
	e429 INTEGER NOT NULL, 
	e4xx INTEGER NOT NULL, 
	e5xx INTEGER NOT NULL, 
	timeout INTEGER NOT NULL, 
	connect_error INTEGER NOT NULL, 
	other INTEGER NOT NULL, 
	duration_ms INTEGER NOT NULL, 
	created_at DATETIME, 
	PRIMARY KEY (id)
);

CREATE TABLE proxy_subscriptions (
	id INTEGER NOT NULL, 
	kind VARCHAR(10) NOT NULL, 
	url TEXT NOT NULL, 
	label VARCHAR(100), 
	created_at DATETIME, 
	last_imported_at DATETIME, 
	last_stats JSON, 
	deprecated BOOLEAN NOT NULL, 
	deprecated_at DATETIME, 
	deprecated_reason VARCHAR(200), 
	admission_status VARCHAR(16) DEFAULT 'ACTIVE' NOT NULL, 
	last_fetch_at DATETIME, 
	last_fetch_status VARCHAR(32), 
	last_success_at DATETIME, 
	last_error VARCHAR(500), 
	cache_url_identity VARCHAR(64), 
	auto_refresh BOOLEAN DEFAULT '1' NOT NULL, snapshot_sha256 VARCHAR(64), snapshot_version INTEGER DEFAULT 0, 
	PRIMARY KEY (id)
);

CREATE TABLE redeem_activations (
	id INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	kind VARCHAR(10) NOT NULL, 
	status VARCHAR(10) NOT NULL, 
	product_key VARCHAR(120) NOT NULL, 
	subid VARCHAR(40) NOT NULL, 
	sub_name VARCHAR(300) NOT NULL, 
	detail VARCHAR(300) NOT NULL, 
	raw TEXT NOT NULL, 
	steamid VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE steam_accounts (
	steam_id VARCHAR(20) NOT NULL, 
	cookies TEXT NOT NULL, 
	persona_name VARCHAR(100) NOT NULL, 
	avatar_url VARCHAR(300) NOT NULL, 
	wallet_json JSON, 
	is_active BOOLEAN NOT NULL, 
	is_online BOOLEAN NOT NULL, 
	in_game VARCHAR(200) NOT NULL, 
	bound_at DATETIME, 
	wallet_checked_at DATETIME, 
	online_checked_at DATETIME, 
	wallet_error VARCHAR(300) NOT NULL, 
	wallet_backoff_level INTEGER NOT NULL, 
	wallet_fail_streak INTEGER NOT NULL, 
	wallet_frozen BOOLEAN NOT NULL, 
	PRIMARY KEY (steam_id)
);

CREATE TABLE steam_events (
	id INTEGER NOT NULL, 
	event_key VARCHAR(60) NOT NULL, 
	category VARCHAR(20) NOT NULL, 
	name_en VARCHAR(120) NOT NULL, 
	name_zh VARCHAR(120), 
	start_date DATE NOT NULL, 
	end_date DATE NOT NULL, 
	precise_start_ts BIGINT, 
	precise_end_ts BIGINT, 
	store_url VARCHAR(200), 
	source_updated_at DATETIME, 
	PRIMARY KEY (id), 
	UNIQUE (event_key)
);

CREATE TABLE subscription_snapshots (
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
);

CREATE TABLE tags (
	tagid INTEGER NOT NULL, 
	name_zh VARCHAR(100), 
	name_en VARCHAR(100), 
	updated_at DATETIME, 
	PRIMARY KEY (tagid)
);

CREATE TABLE tracked_accounts (
	steamid VARCHAR(20) NOT NULL, 
	label VARCHAR(100), 
	persona_name VARCHAR(100), 
	avatar_url VARCHAR(500), 
	kinds_json JSON, 
	last_sync_at DATETIME, 
	created_at DATETIME, 
	PRIMARY KEY (steamid)
);

CREATE TABLE wishlist_items (
	steamid VARCHAR(20) NOT NULL, 
	appid BIGINT NOT NULL, 
	added_at DATETIME, 
	active BOOLEAN NOT NULL, 
	owned BOOLEAN NOT NULL, 
	manual BOOLEAN NOT NULL, 
	wishlisted BOOLEAN NOT NULL, 
	manual_pool BOOLEAN NOT NULL, 
	excluded BOOLEAN NOT NULL, 
	board_pool BOOLEAN NOT NULL, 
	PRIMARY KEY (steamid, appid)
);

CREATE TABLE xp_events (
	id INTEGER NOT NULL, 
	steamid VARCHAR(20) NOT NULL, 
	source VARCHAR(24) NOT NULL, 
	source_ref VARCHAR(220) NOT NULL, 
	xp INTEGER NOT NULL, 
	multiplier FLOAT NOT NULL, 
	note VARCHAR(220), 
	created_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_xp_events_sid_src_ref UNIQUE (steamid, source, source_ref)
);

CREATE INDEX ix_agent_memories_category ON agent_memories (category);

CREATE INDEX ix_agent_memories_key ON agent_memories ("key");

CREATE INDEX ix_agent_run_session ON agent_runs (session_id, created_at);

CREATE INDEX ix_agent_run_status ON agent_runs (status);

CREATE INDEX ix_bcg_import_acq ON bill_cdk_games (import_id, acq);

CREATE INDEX ix_bgt_import_date ON bill_game_txs (import_id, date);

CREATE INDEX ix_bgt_import_type ON bill_game_txs (import_id, tx_type);

CREATE INDEX ix_bill_cdk_games_import_id ON bill_cdk_games (import_id);

CREATE INDEX ix_bill_game_txs_import_id ON bill_game_txs (import_id);

CREATE INDEX ix_bill_imports_steam_id ON bill_imports (steam_id);

CREATE INDEX ix_bill_topup_txs_import_id ON bill_topup_txs (import_id);

CREATE INDEX ix_brp_bundle_region ON bundle_region_prices (bundle_id, region_code);

CREATE INDEX ix_btt_import_date ON bill_topup_txs (import_id, date);

CREATE INDEX ix_bundles_diff_fen ON bundles(diff_fen DESC);

CREATE INDEX ix_crawl_jobs_cycle ON crawl_jobs (cycle_id);

CREATE INDEX ix_family_groups_family_groupid ON family_groups (family_groupid);

CREATE INDEX ix_family_library_snapshots_family_groupid ON family_library_snapshots (family_groupid);

CREATE INDEX ix_frh_currency_id ON fx_rate_history(currency_code, id);

CREATE INDEX ix_games_diff_fen ON games(diff_fen DESC);

CREATE INDEX ix_games_type ON games (type);

CREATE INDEX ix_games_updated ON games (updated_at);

CREATE INDEX ix_gcp_appid_status_cny ON game_current_prices (appid, price_status, cny_fen);

CREATE INDEX ix_gcp_region_cny ON game_current_prices (region_code, cny_fen);

CREATE INDEX ix_gcp_status ON game_current_prices (price_status);

CREATE INDEX ix_gcp_updated ON game_current_prices (updated_at);

CREATE INDEX ix_gph_snapshot_at ON game_price_history(snapshot_at);

CREATE INDEX ix_health_observations_node_id ON health_observations (node_id);

CREATE INDEX ix_health_observations_run_id ON health_observations (run_id);

CREATE INDEX ix_health_runs_channel ON health_runs (channel);

CREATE INDEX ix_health_runs_subscription_id ON health_runs (subscription_id);

CREATE INDEX ix_monitor_exclusion_target ON monitor_exclusions (target_type, target_id);

CREATE INDEX ix_monitor_source_lookup ON monitor_sources (source, active);

CREATE INDEX ix_monitor_source_target ON monitor_sources (target_type, target_id);

CREATE INDEX ix_notification_cycle ON notification_candidates (cycle_id);

CREATE INDEX ix_notification_status ON notification_candidates (status);

CREATE INDEX ix_pjr_node_started ON proxy_job_runs (selected_node, started_at);

CREATE INDEX ix_pre_run_exit ON proxy_run_exits (run_id, exit_ip);

CREATE INDEX ix_preset_source ON preset_games (source);

CREATE INDEX ix_price_cycle_status ON price_cycles (status, started_at);

CREATE INDEX ix_price_events_appid ON price_events (appid, occurred_at);

CREATE INDEX ix_price_events_cycle ON price_events (cycle_id);

CREATE INDEX ix_proxy_job_runs_started_at ON proxy_job_runs (started_at);

CREATE INDEX ix_proxy_job_runs_status ON proxy_job_runs (status);

CREATE INDEX ix_proxy_nodes_exit_ip ON proxy_nodes (exit_ip);

CREATE INDEX ix_proxy_nodes_subscription_id ON proxy_nodes (subscription_id);

CREATE INDEX ix_proxy_run_exits_run_id ON proxy_run_exits (run_id);

CREATE INDEX ix_ra_created ON redeem_activations (created_at);

CREATE INDEX ix_subscription_snapshots_subscription_id ON subscription_snapshots (subscription_id);

CREATE INDEX ix_xp_events_sid_created ON xp_events (steamid, created_at);

CREATE UNIQUE INDEX ux_frh_currency_date ON fx_rate_history (currency_code, rate_date);

CREATE UNIQUE INDEX ux_gph_snapshot ON game_price_history (appid, region_code, snapshot_at, COALESCE(sub_id, -1), COALESCE(price, 0), COALESCE(is_gold, 0));

CREATE UNIQUE INDEX ux_monitor_exclusion_active ON monitor_exclusions (target_type, target_id) WHERE active = 1;

CREATE UNIQUE INDEX ux_monitor_source ON monitor_sources (target_type, target_id, source);

CREATE UNIQUE INDEX ux_monitor_target ON monitor_targets (target_type, target_id);

CREATE UNIQUE INDEX ux_notification_candidate ON notification_candidates(source_kind,event_id,recipient);

CREATE UNIQUE INDEX ux_price_event_identity ON price_events (cycle_id, appid, COALESCE(region_code, ''), event_type);

CREATE TRIGGER games_name_bi_ad AFTER DELETE ON games BEGIN DELETE FROM games_name_fts WHERE appid = old.appid; END;

CREATE TRIGGER games_name_bi_ai AFTER INSERT ON games BEGIN INSERT INTO games_name_fts(appid, name_bi) VALUES (new.appid, name_bigram(new.name, new.name_en)); END;

CREATE TRIGGER games_name_bi_au AFTER UPDATE OF name, name_en ON games WHEN old.name IS NOT new.name OR old.name_en IS NOT new.name_en BEGIN DELETE FROM games_name_fts WHERE appid = old.appid; INSERT INTO games_name_fts(appid, name_bi) VALUES (new.appid, name_bigram(new.name, new.name_en)); END;
