package wishlist

import (
	"context"
	"database/sql"
	"reflect"
	"testing"

	_ "modernc.org/sqlite"
)

// 表结构照抄真实库 sqlite_master（Python create_all 产物），字段语义见
// monitoring/wishlist 模型；games 仅保留排序断言所需的最小列。
const schema = `
CREATE TABLE wishlist_items (
	steamid VARCHAR(20) NOT NULL,
	appid BIGINT NOT NULL,
	added_at DATETIME,
	active BOOLEAN NOT NULL,
	owned BOOLEAN DEFAULT 0,
	manual BOOLEAN DEFAULT 0,
	wishlisted BOOLEAN DEFAULT 0,
	manual_pool BOOLEAN DEFAULT 0,
	excluded BOOLEAN DEFAULT 0,
	board_pool BOOLEAN DEFAULT 0,
	PRIMARY KEY (steamid, appid)
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
CREATE TABLE games (
	appid BIGINT NOT NULL PRIMARY KEY,
	min_cny_fen INTEGER
);
`

func newTestDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := sql.Open("sqlite", ":memory:")
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	db.SetMaxOpenConns(1)
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(schema); err != nil {
		t.Fatalf("建表失败: %v", err)
	}
	return db
}

// 种子：关注 3 款（100/200/300）、愿望单 2 款（100/400，100 与关注重叠）、
// 其余 500/600；干扰行 = 非激活关注、非愿望单来源、停用愿望单行。
func seed(t *testing.T, db *sql.DB) {
	t.Helper()
	for _, c := range []struct {
		sql  string
		args []any
	}{
		{"INSERT INTO monitor_sources (id, target_type, target_id, source, priority, active) VALUES (1,'game',300,'favorite',10,1)", nil},
		{"INSERT INTO monitor_sources (id, target_type, target_id, source, priority, active) VALUES (2,'game',100,'favorite',10,1)", nil},
		{"INSERT INTO monitor_sources (id, target_type, target_id, source, priority, active) VALUES (3,'game',200,'favorite',10,1)", nil},
		{"INSERT INTO monitor_sources (id, target_type, target_id, source, priority, active) VALUES (4,'game',999,'favorite',10,0)", nil},
		{"INSERT INTO monitor_sources (id, target_type, target_id, source, priority, active) VALUES (5,'game',888,'owned',5,1)", nil},
		{"INSERT INTO monitor_sources (id, target_type, target_id, source, priority, active) VALUES (6,'app',777,'favorite',10,1)", nil},
		{"INSERT INTO wishlist_items (steamid, appid, active, wishlisted) VALUES ('s1',400,1,1)", nil},
		{"INSERT INTO wishlist_items (steamid, appid, active, wishlisted) VALUES ('s2',100,1,1)", nil},
		{"INSERT INTO wishlist_items (steamid, appid, active, wishlisted) VALUES ('s1',100,1,1)", nil},
		{"INSERT INTO wishlist_items (steamid, appid, active, wishlisted) VALUES ('s1',600,1,0)", nil},
		{"INSERT INTO wishlist_items (steamid, appid, active, wishlisted) VALUES ('s1',555,0,1)", nil},
	} {
		if _, err := db.Exec(c.sql, c.args...); err != nil {
			t.Fatalf("种子写入失败: %v", err)
		}
	}
	for _, r := range []struct{ appid, fen int64 }{
		{100, 99999}, {200, 2000}, {300, 1000}, {400, 500}, {500, 50}, {600, 100},
	} {
		if _, err := db.Exec("INSERT INTO games (appid, min_cny_fen) VALUES (?, ?)", r.appid, r.fen); err != nil {
			t.Fatalf("games 种子写入失败: %v", err)
		}
	}
}

func ids(t *testing.T, db *sql.DB, query string, args ...any) []int64 {
	t.Helper()
	rows, err := db.Query(query, args...)
	if err != nil {
		t.Fatalf("查询失败: %v", err)
	}
	defer rows.Close()
	var out []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			t.Fatalf("扫描失败: %v", err)
		}
		out = append(out, id)
	}
	if err := rows.Err(); err != nil {
		t.Fatalf("遍历失败: %v", err)
	}
	return out
}

func TestFollowedAppIDs(t *testing.T) {
	db := newTestDB(t)
	seed(t, db)
	got, err := FollowedAppIDs(context.Background(), db)
	if err != nil {
		t.Fatalf("FollowedAppIDs 失败: %v", err)
	}
	want := []int64{100, 200, 300}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("关注集 = %v, 期望 %v", got, want)
	}
}

func TestWishlistAppIDs(t *testing.T) {
	db := newTestDB(t)
	seed(t, db)
	got, err := WishlistAppIDs(context.Background(), db)
	if err != nil {
		t.Fatalf("WishlistAppIDs 失败: %v", err)
	}
	want := []int64{100, 400}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("愿望单集 = %v, 期望 %v", got, want)
	}
}

// 位次序：关注段 > 愿望单段 > 原价格排序；wishlistPriority=false 只剩关注段。
func TestOrderPrefix(t *testing.T) {
	db := newTestDB(t)
	seed(t, db)
	ctx := context.Background()

	prefix, args, err := OrderPrefix(ctx, db)
	if err != nil {
		t.Fatalf("OrderPrefix 失败: %v", err)
	}
	if prefix != "g.appid IN (?, ?, ?) DESC, g.appid IN (?, ?) DESC" {
		t.Fatalf("前缀 SQL = %q", prefix)
	}
	if !reflect.DeepEqual(args, []any{int64(100), int64(200), int64(300), int64(100), int64(400)}) {
		t.Fatalf("前缀参数 = %v", args)
	}
	got := ids(t, db, "SELECT g.appid FROM games g ORDER BY "+prefix+
		", COALESCE(g.min_cny_fen, 999999) ASC, g.appid DESC", args...)
	want := []int64{100, 300, 200, 400, 500, 600}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("位次序 = %v, 期望 %v", got, want)
	}
}

func TestOrderPrefixNoWishlist(t *testing.T) {
	db := newTestDB(t)
	seed(t, db)
	ctx := context.Background()

	prefix, args, err := OrderPrefixFor(ctx, db, false)
	if err != nil {
		t.Fatalf("OrderPrefixFor 失败: %v", err)
	}
	if prefix != "g.appid IN (?, ?, ?) DESC" {
		t.Fatalf("前缀 SQL = %q", prefix)
	}
	got := ids(t, db, "SELECT g.appid FROM games g ORDER BY "+prefix+
		", COALESCE(g.min_cny_fen, 999999) ASC, g.appid DESC", args...)
	want := []int64{300, 200, 100, 500, 600, 400}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("位次序 = %v, 期望 %v", got, want)
	}
}

// 空集行为：无有效关注与愿望单行时前缀为空串、参数为空，SQL 保持原样。
func TestOrderPrefixEmpty(t *testing.T) {
	db := newTestDB(t)
	seed(t, db)
	// 清空有效来源：关注与愿望单只剩非激活/停用行
	for _, stmt := range []string{
		"UPDATE monitor_sources SET active = 0",
		"UPDATE wishlist_items SET active = 0",
	} {
		if _, err := db.Exec(stmt); err != nil {
			t.Fatalf("停用种子失败: %v", err)
		}
	}
	ctx := context.Background()
	prefix, args, err := OrderPrefix(ctx, db)
	if err != nil {
		t.Fatalf("OrderPrefix 失败: %v", err)
	}
	if prefix != "" || len(args) != 0 {
		t.Fatalf("空集前缀 = %q, 参数 = %v", prefix, args)
	}
	got := ids(t, db, "SELECT g.appid FROM games g ORDER BY "+prefix+
		"COALESCE(g.min_cny_fen, 999999) ASC, g.appid DESC", args...)
	want := []int64{500, 600, 400, 300, 200, 100}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("位次序 = %v, 期望 %v", got, want)
	}
}
