package account

import (
	"context"
	"database/sql"
	"path/filepath"
	"reflect"
	"testing"

	"holdexar/server-go/internal/store"
)

// testSchema 与 Python ORM create_all 的真实建表 DDL 逐列一致
// （metadata CreateTable 导出，含唯一索引）。
const testSchema = `
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
CREATE UNIQUE INDEX ux_monitor_source ON monitor_sources (target_type, target_id, source);
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
CREATE UNIQUE INDEX ux_monitor_exclusion_active ON monitor_exclusions (target_type, target_id);
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
CREATE UNIQUE INDEX ux_monitor_target ON monitor_targets (target_type, target_id);
`

func newAccountDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "holdexar.db"))
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(testSchema); err != nil {
		t.Fatalf("建表失败: %v", err)
	}
	return db
}

func mustExec(t *testing.T, db *sql.DB, query string, args ...any) {
	t.Helper()
	if _, err := db.Exec(query, args...); err != nil {
		t.Fatalf("种子写入失败: %v (%v)", err, query)
	}
}

func seedAccount(t *testing.T, db *sql.DB, sid, persona string, active int, boundAt any) {
	t.Helper()
	mustExec(t, db,
		"INSERT INTO steam_accounts (steam_id, cookies, persona_name, avatar_url, is_active,"+
			" is_online, in_game, bound_at, wallet_error, wallet_backoff_level, wallet_fail_streak,"+
			" wallet_frozen) VALUES (?, '', ?, '', ?, 0, '', ?, '', 0, 0, 0)",
		sid, persona, active, boundAt)
}

const (
	sidA = "76561197960265730" // 好友码 2
	sidB = "76561197960265731" // 好友码 3
	sidC = "76561197960265732" // 好友码 4
)

func TestFriendCode(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"76561197960265730", "2"},
		{"76561197960265728", "0"},
		{" 76561197960265729 ", "1"},
		{"abc", ""},
		{"", ""},
	}
	for _, c := range cases {
		if got := FriendCode(c.in); got != c.want {
			t.Errorf("FriendCode(%q) = %q, 期望 %q", c.in, got, c.want)
		}
	}
}

func TestListAccounts(t *testing.T) {
	db := newAccountDB(t)
	ctx := context.Background()
	seedAccount(t, db, sidA, "大号", 1, "2026-01-02 10:00:00")
	seedAccount(t, db, sidB, "小号", 0, nil) // bound_at 空值排最前
	seedAccount(t, db, sidC, "", 0, "2026-01-01 10:00:00")

	// 计数分支：活跃愿望单/拥有、非活跃不计、未知账号不计、双账号并集
	seedItem := func(sid string, appid int64, active, owned, wishlisted int) {
		mustExec(t, db,
			"INSERT INTO wishlist_items (steamid, appid, active, owned, manual, wishlisted,"+
				" manual_pool, excluded, board_pool) VALUES (?, ?, ?, ?, 0, ?, 0, 0, 0)",
			sid, appid, active, owned, wishlisted)
	}
	seedItem(sidB, 10, 1, 0, 1)
	seedItem(sidB, 11, 1, 0, 0)
	seedItem(sidB, 12, 1, 1, 0)
	seedItem(sidB, 13, 1, 1, 0)
	seedItem(sidB, 14, 1, 1, 0)
	seedItem(sidB, 15, 0, 0, 1) // 非活跃不计
	seedItem(sidC, 16, 1, 0, 1)
	seedItem("76561197960265799", 17, 1, 0, 1) // 不在账号清单内

	got, err := ListAccounts(ctx, db)
	if err != nil {
		t.Fatalf("ListAccounts: %v", err)
	}
	want := []AccountBrief{
		{SteamID: sidB, PersonaName: "小号", IsActive: false, IsPrimary: true,
			FriendCode: "3", WishlistCount: 2, GameCount: 3},
		{SteamID: sidC, PersonaName: "", IsActive: false,
			FriendCode: "4", WishlistCount: 1, GameCount: 0},
		{SteamID: sidA, PersonaName: "大号", IsActive: true,
			FriendCode: "2", WishlistCount: 0, GameCount: 0},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("ListAccounts 形状不符:\n got  %+v\n want %+v", got, want)
	}
}

func TestListAccountsEmpty(t *testing.T) {
	db := newAccountDB(t)
	got, err := ListAccounts(context.Background(), db)
	if err != nil {
		t.Fatalf("ListAccounts: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("空库应返回空清单，得 %+v", got)
	}
}

func seedMonitor(t *testing.T, db *sql.DB, appid int64, sources []string, exclusion bool) {
	t.Helper()
	mustExec(t, db,
		"INSERT INTO monitor_targets (target_type, target_id) VALUES ('game', ?)", appid)
	for _, src := range sources {
		mustExec(t, db,
			"INSERT INTO monitor_sources (target_type, target_id, source, priority, active)"+
				" VALUES ('game', ?, ?, 0, 1)", appid, src)
	}
	if exclusion {
		mustExec(t, db,
			"INSERT INTO monitor_exclusions (target_type, target_id, active) VALUES ('game', ?, 1)", appid)
	}
}

func TestListItems(t *testing.T) {
	db := newAccountDB(t)
	ctx := context.Background()
	seedItem := func(sid string, appid int64, active, owned, wishlisted int) {
		mustExec(t, db,
			"INSERT INTO wishlist_items (steamid, appid, active, owned, manual, wishlisted,"+
				" manual_pool, excluded, board_pool) VALUES (?, ?, ?, ?, 0, ?, 0, 0, 0)",
			sid, appid, active, owned, wishlisted)
	}

	seedItem(sidA, 100, 1, 0, 1)                         // 纯愿望单
	seedItem(sidA, 200, 1, 1, 0)                         // 账户 A：拥有
	seedItem(sidB, 200, 1, 0, 1)                         // 账户 B：愿望单 → 并集双标
	seedItem(sidA, 300, 1, 0, 0)                         // 无标记 + monitor favorite
	seedItem(sidA, 400, 1, 0, 1)                         // 被 excluded 剔除
	seedItem(sidA, 700, 0, 0, 1)                         // 非活跃行不计
	seedItem(sidA, 800, 1, 0, 0)                         // 无任何标记无来源
	seedItem(sidA, 900, 1, 0, 1)                         // monitor 目标 released 态不剔除
	seedItem(sidA, 1000, 1, 0, 1)                        // 排除行但无 monitor_targets 行 → 不剔除
	seedMonitor(t, db, 300, []string{"favorite"}, false) // followed 来源不进两清单
	seedMonitor(t, db, 400, []string{"favorite"}, true)  // active_ids 与 states_of 双剔除
	seedMonitor(t, db, 500, []string{"family_wishlist"}, false)
	seedMonitor(t, db, 600, []string{"owned"}, false)
	seedMonitor(t, db, 900, nil, false)

	got, err := ListItems(ctx, db)
	if err != nil {
		t.Fatalf("ListItems: %v", err)
	}
	var wishlisted, owned []int64
	for _, it := range got {
		if it.Wishlisted {
			wishlisted = append(wishlisted, it.AppID)
		}
		if it.Owned {
			owned = append(owned, it.AppID)
		}
	}
	if want := []int64{100, 200, 500, 900, 1000}; !reflect.DeepEqual(wishlisted, want) {
		t.Errorf("愿望单集不符: got %v want %v", wishlisted, want)
	}
	if want := []int64{200, 600}; !reflect.DeepEqual(owned, want) {
		t.Errorf("拥有集不符: got %v want %v", owned, want)
	}
}

func TestBriefsFor(t *testing.T) {
	db := newAccountDB(t)
	ctx := context.Background()
	seedGame := func(appid int64, name string, positive, chinese any) {
		mustExec(t, db,
			"INSERT INTO games (appid, name, family_sharing, trading_cards, is_epic, is_hb,"+
				" hl_flag, pp_flag, diff_fen, is_adult, is_visual_novel, removed_strikes,"+
				" positive_reviews, review_count, view_count, positive_rate, chinese_support)"+
				// 常量依次为 family_sharing…positive_reviews 共 11 列，review_count 走参数
				" VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, ?, 0, ?, ?)",
			appid, name, 5, positive, chinese)
	}
	seedPrice := func(appid int64, status string, cny, discount any) {
		mustExec(t, db,
			"INSERT INTO game_current_prices (appid, region_code, discount_percent, price_status,"+
				" fail_count, cny_fen) VALUES (?, 'CN', ?, ?, 0, ?)",
			appid, discount, status, cny)
	}

	seedGame(100, "游戏一", 9800, "全部")
	seedPrice(100, "ok", 19900, 50)
	seedGame(200, "游戏二", nil, nil)
	seedPrice(200, "ok", nil, 0)
	seedGame(300, "游戏三", 7500, "界面")
	seedPrice(300, "locked", 1, 10) // 非 ok 不进 join
	seedGame(350, "游戏四", 0, nil)
	seedPrice(350, "ok", 1000, 0)

	got, err := BriefsFor(ctx, db, []int64{100, 200, 300, 350, 400, 500, 0})
	if err != nil {
		t.Fatalf("BriefsFor: %v", err)
	}
	if _, ok := got[400]; ok {
		t.Errorf("缺主档行不应出键: %v", got)
	}
	if _, ok := got[500]; ok {
		t.Errorf("无 games 行不应出键: %v", got)
	}
	b := got[100]
	if b.Name == nil || *b.Name != "游戏一" || b.CnyFen == nil || *b.CnyFen != 19900 ||
		b.Discount != 50 || b.PositiveRate == nil || *b.PositiveRate != 98.0 ||
		b.ReviewCount != 5 || b.ChineseSupport == nil || *b.ChineseSupport != "全部" {
		t.Errorf("100 简报不符: %+v", b)
	}
	b = got[200]
	if b.CnyFen != nil || b.Discount != 0 || b.PositiveRate != nil || b.ChineseSupport != nil {
		t.Errorf("200 空值语义不符: %+v", b)
	}
	b = got[300]
	if b.CnyFen != nil || b.PositiveRate == nil || *b.PositiveRate != 75.0 {
		t.Errorf("300 非 ok 价应不入简报: %+v", b)
	}
	b = got[350]
	if b.Discount != 0 || b.CnyFen == nil || *b.CnyFen != 1000 {
		t.Errorf("350 零折扣语义不符: %+v", b)
	}
}
