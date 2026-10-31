package derived

// 派生链语义测试：四因子边界、史低判定矩阵、系列聚类与增量窄化。
// 史低判定经真实 SQLite 链路跑（price_snapshot 与生产同一条 SQL）。

import (
	"context"
	"database/sql"
	"fmt"
	"math"
	"path/filepath"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

var cmpNow = time.Date(2026, 10, 31, 12, 30, 45, 123456000, time.FixedZone("UTC+8", 8*3600))

var testTables = []string{
	`CREATE TABLE games (
	appid BIGINT NOT NULL, name VARCHAR(512) NOT NULL, name_en VARCHAR(512),
	type VARCHAR(20), series_id VARCHAR(100), hl_flag INTEGER NOT NULL,
	pp_flag INTEGER NOT NULL, pp_changed_at DATETIME, min_cny_fen BIGINT,
	diff_fen INTEGER NOT NULL, smart_score FLOAT, is_adult BOOLEAN NOT NULL,
	is_visual_novel BOOLEAN NOT NULL, release_date VARCHAR(50),
	positive_rate INTEGER, positive_reviews INTEGER NOT NULL,
	review_count INTEGER NOT NULL, created_at DATETIME, updated_at DATETIME,
	PRIMARY KEY (appid))`,
	`CREATE TABLE game_current_prices (
	appid BIGINT NOT NULL, region_code VARCHAR(10) NOT NULL, currency VARCHAR(10),
	price BIGINT, original_price BIGINT, discount_percent INTEGER NOT NULL,
	sub_id INTEGER, price_status VARCHAR(20) NOT NULL, fail_count INTEGER NOT NULL,
	cny_fen BIGINT, attempt_outcome VARCHAR(10), updated_at DATETIME,
	PRIMARY KEY (appid, region_code))`,
	`CREATE TABLE game_price_history (
	id INTEGER NOT NULL, appid BIGINT NOT NULL, region_code VARCHAR(10) NOT NULL,
	currency VARCHAR(10), price BIGINT, original_price BIGINT,
	discount_percent INTEGER NOT NULL, sub_id INTEGER, is_gold BOOLEAN NOT NULL,
	version_suffix VARCHAR(100), is_bundle BOOLEAN NOT NULL,
	price_status VARCHAR(20) NOT NULL, cny_fen BIGINT, product_unknown BOOLEAN DEFAULT '0' NOT NULL,
	snapshot_at DATETIME, PRIMARY KEY (id))`,
	`CREATE TABLE app_settings (
	key VARCHAR(64) NOT NULL, value_json JSON, updated_at DATETIME,
	PRIMARY KEY (key))`,
}

func newDerivedDB(t *testing.T) *sql.DB {
	t.Helper()
	db, err := store.Open(filepath.Join(t.TempDir(), "derived.db"))
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	for _, ddl := range testTables {
		if _, err := db.Exec(ddl); err != nil {
			t.Fatalf("建表失败: %v", err)
		}
	}
	return db
}

func mustExec(t *testing.T, db *sql.DB, q string, args ...any) {
	t.Helper()
	if _, err := db.Exec(q, args...); err != nil {
		t.Fatalf("执行 %s 失败: %v", q, err)
	}
}

func insertGame(t *testing.T, db *sql.DB, appid int64, release *string, pr, rc int64) {
	t.Helper()
	mustExec(t, db, `INSERT INTO games (appid, name, type, hl_flag, pp_flag, diff_fen,
		is_adult, is_visual_novel, release_date, positive_rate, positive_reviews,
		review_count) VALUES (?, ?, 'GAME', 0, 0, 0, 0, 0, ?, ?, 0, ?)`,
		appid, fmt.Sprintf("游戏%d", appid), release, pr, rc)
}

// insertCN 现价 + 历史（cny 直算）；snaps = (相对天数, 价格, 原价)。
func insertCN(t *testing.T, db *sql.DB, id *int64, appid int64, price int64, discount int64, snaps ...[3]int64) {
	t.Helper()
	mustExec(t, db, `INSERT INTO game_current_prices (appid, region_code, currency, price,
		original_price, discount_percent, sub_id, price_status, fail_count, cny_fen,
		attempt_outcome, updated_at) VALUES (?, 'CN', 'CNY', ?, ?, ?, 0, 'ok', 0, ?, 'success', ?)`,
		appid, price, snaps[len(snaps)-1][2], discount, price, cmpNow)
	for _, s := range snaps {
		*id++
		mustExec(t, db, `INSERT INTO game_price_history (id, appid, region_code, currency,
			price, original_price, discount_percent, sub_id, is_gold, is_bundle,
			price_status, cny_fen, snapshot_at) VALUES (?, ?, 'CN', 'CNY', ?, ?, 0, 0, 0, 0,
			'ok', ?, ?)`,
			*id, appid, s[1], s[2], s[1], cmpNow.Add(-time.Duration(s[0])*24*time.Hour))
	}
}

func insertRegion(t *testing.T, db *sql.DB, appid int64, region string, price int64, fen int64) {
	t.Helper()
	mustExec(t, db, `INSERT INTO game_current_prices (appid, region_code, currency, price,
		original_price, discount_percent, sub_id, price_status, fail_count, cny_fen,
		attempt_outcome, updated_at) VALUES (?, ?, 'USD', ?, ?, 0, 0, 'ok', 0, ?, 'success', ?)`,
		appid, region, price, price, fen, cmpNow)
}

// ── 四因子边界 ──────────────────────────────────────────────────

func i64(v int64) *int64    { return &v }
func strp(s string) *string { return &s }

func TestSaveScoreBoundaries(t *testing.T) {
	cases := []struct {
		in   *int64
		want float64
	}{
		{nil, 0}, {i64(0), 0}, {i64(-500), 0},
		{i64(200000), 1.0}, // ¥200 封顶
		{i64(2000000), 1.0},
	}
	for _, c := range cases {
		if got := SaveScore(c.in); math.Abs(got-c.want) > 1e-12 {
			t.Fatalf("SaveScore(%v) = %v, want %v", c.in, got, c.want)
		}
	}
	// ¥20（2000 分）: ln(2)/ln(11) ≈ 0.289
	want20 := math.Log(2) / math.Log(11)
	if got := SaveScore(i64(2000)); math.Abs(got-want20) > 1e-12 {
		t.Fatalf("SaveScore(¥20) = %v, want %v", got, want20)
	}
}

func TestQualityScoreBoundaries(t *testing.T) {
	// 有评测但好评率缺失 = 中性
	if got := QualityScore(nil, i64(100), nil, cmpNow); got != 0.5 {
		t.Fatalf("好评率缺失应为 0.5，实际 %v", got)
	}
	// 好评率 50% 恒中性（收缩不改变中性点）
	if got := QualityScore(i64(5000), i64(10), nil, cmpNow); math.Abs(got-0.5) > 1e-12 {
		t.Fatalf("50%% 好评应为 0.5，实际 %v", got)
	}
	// 满好评 + 巨量评测 → 趋近 1
	if got := QualityScore(i64(10000), i64(1000000), nil, cmpNow); got < 0.99 {
		t.Fatalf("十万级满好评应趋近 1，实际 %v", got)
	}
	// 零评价 + 无发行日 = 刚发行 = 中性 0.5
	if got := QualityScore(nil, i64(0), nil, cmpNow); math.Abs(got-0.5) > 1e-12 {
		t.Fatalf("零评价无日期应为 0.5，实际 %v", got)
	}
	// 零评价 + 未来日期 = 未发售 = 0.5
	if got := QualityScore(nil, i64(0), strp("2030-01-01"), cmpNow); math.Abs(got-0.5) > 1e-12 {
		t.Fatalf("未来日期应为 0.5，实际 %v", got)
	}
	// 零评价 + 不可解析 = 0.5
	if got := QualityScore(nil, i64(0), strp("not-a-date"), cmpNow); math.Abs(got-0.5) > 1e-12 {
		t.Fatalf("不可解析日期应为 0.5，实际 %v", got)
	}
	// 零评价 + 两年（730 天 / 365.25 年化）= 0.2 + 0.3·exp(-1.0007)
	wantAge := 730.0 / 365.25 / 2.0
	want := 0.20 + 0.30*math.Exp(-wantAge)
	if got := QualityScore(nil, i64(0), strp("2024-10-31"), cmpNow); math.Abs(got-want) > 1e-12 {
		t.Fatalf("两年零评价应 ≈ %v，实际 %v", want, got)
	}
	// 零评价 + 远古 → 地板 0.2
	if got := QualityScore(nil, i64(0), strp("2000-01-01"), cmpNow); got > 0.21 {
		t.Fatalf("远古零评价应近地板，实际 %v", got)
	}
}

func TestTimingAndFamiliarityBoundaries(t *testing.T) {
	if got := TimingScore(i64(1), nil); got != 0 {
		t.Fatalf("无折扣应 0，实际 %v", got)
	}
	if got := TimingScore(i64(1), i64(0)); got != 0 {
		t.Fatalf("折扣 0 应 0，实际 %v", got)
	}
	for flag, want := range map[int64]float64{1: 1.00, 2: 0.75, 0: 0.35, 3: 0.35} {
		if got := TimingScore(i64(flag), i64(50)); got != want {
			t.Fatalf("TimingScore(hl=%d) = %v, want %v", flag, got, want)
		}
	}
	if got := FamiliarityScore(i64(0)); got != 0 {
		t.Fatalf("零评测熟悉度应 0，实际 %v", got)
	}
	if got := FamiliarityScore(i64(100000)); math.Abs(got-1.0) > 1e-12 {
		t.Fatalf("10 万评测应满分，实际 %v", got)
	}
	if got := FamiliarityScore(i64(999999999)); got != 1.0 {
		t.Fatalf("超上限应封顶 1.0，实际 %v", got)
	}
}

func TestSmartScoreWeightedSum(t *testing.T) {
	// 全零输入 = 0.28·0.5（零评价中性） = 0.14
	got := SmartScore(i64(0), nil, i64(0), i64(0), i64(0), nil, cmpNow)
	if math.Abs(got-0.14) > 1e-12 {
		t.Fatalf("全零输入应 0.14，实际 %v", got)
	}
	// 因子和恒 ≤ 权重和
	full := SmartScore(i64(200000), i64(10000), i64(100000), i64(1), i64(90), strp("2026-10-01"), cmpNow)
	if full <= 0.99 || full > 1.0 {
		t.Fatalf("全满分应近 1，实际 %v", full)
	}
}

// ── 史低判定矩阵（真实 SQL 链路） ─────────────────────────────

func runDerived(t *testing.T, db *sql.DB, ids []int64, opts Options) {
	t.Helper()
	conn, err := db.Conn(context.Background())
	if err != nil {
		t.Fatalf("取连接失败: %v", err)
	}
	defer conn.Close()
	if _, err := UpdatePriceDerived(context.Background(), conn, ids, opts); err != nil {
		t.Fatalf("派生执行失败: %v", err)
	}
}

func gameCols(t *testing.T, db *sql.DB, appid int64) (hl, pp int64, changed any, minFen, diff int64, score float64) {
	t.Helper()
	err := db.QueryRow(`SELECT hl_flag, pp_flag, CAST(pp_changed_at AS TEXT),
		COALESCE(min_cny_fen, -1), diff_fen, COALESCE(smart_score, -1)
		FROM games WHERE appid = ?`, appid).
		Scan(&hl, &pp, &changed, &minFen, &diff, &score)
	if err != nil {
		t.Fatalf("读派生列失败: %v", err)
	}
	return
}

func TestPriceHistoryLowMatrix(t *testing.T) {
	db := newDerivedDB(t)
	prev := Clock
	Clock = func() time.Time { return cmpNow }
	t.Cleanup(func() { Clock = prev })

	var hid int64
	days := func(d int64) string {
		return cmpNow.Add(-time.Duration(d) * 24 * time.Hour).Format("2006-01-02")
	}

	// 2001 新史低（降价 + 有折扣，era 起点在 30 天窗内）：5000→6000 原价永降
	insertGame(t, db, 2001, strp(days(400)), 8000, 500)
	insertCN(t, db, &hid, 2001, 5000, 50, [3]int64{40, 6000, 6000}, [3]int64{10, 5000, 5000})
	insertRegion(t, db, 2001, "US", 999, 999)

	// 2002 平史低（长期同价，era 在 30 天窗外，有折扣 → 2）
	insertGame(t, db, 2002, strp(days(700)), 8000, 500)
	insertCN(t, db, &hid, 2002, 3000, 50,
		[3]int64{60, 3000, 3000}, [3]int64{50, 3000, 3000}, [3]int64{10, 3000, 3000})

	// 2003 降价但无折扣 → prior 更高仍记新史低 1（原价持平不触发 pp）
	insertGame(t, db, 2003, strp(days(400)), 8000, 500)
	insertCN(t, db, &hid, 2003, 4000, 0, [3]int64{40, 5000, 4000}, [3]int64{10, 4000, 4000})

	// 2004 涨价回非史低（prior 更低 → 无折扣 0）
	insertGame(t, db, 2004, strp(days(400)), 8000, 500)
	insertCN(t, db, &hid, 2004, 3000, 0, [3]int64{40, 2900, 2900}, [3]int64{10, 3000, 3000})

	// 2005 无国区有效价（missing）→ hl 0、diff 0；非 CN 锚最低价仍记
	insertGame(t, db, 2005, strp(days(400)), 8000, 500)
	mustExec(t, db, `INSERT INTO game_current_prices (appid, region_code, currency, price,
		original_price, discount_percent, sub_id, price_status, fail_count, cny_fen,
		attempt_outcome, updated_at) VALUES (2005, 'CN', 'CNY', NULL, NULL, 0, 0,
		'missing', 1, NULL, 'failed', ?)`, cmpNow)
	insertRegion(t, db, 2005, "JP", 1500, 800)

	// 2006 无任何非 CN 价 → min NULL、diff 0
	insertGame(t, db, 2006, strp(days(400)), 8000, 500)
	insertCN(t, db, &hid, 2006, 19900, 0, [3]int64{10, 19900, 19900})

	runDerived(t, db, []int64{2001, 2002, 2003, 2004, 2005, 2006}, Options{Now: cmpNow, History: true})

	if hl, pp, changed, minFen, diff, _ := gameCols(t, db, 2001); hl != 1 || pp != 1 ||
		changed == nil || minFen != 999 || diff != 5000-999 {
		t.Fatalf("2001 应为 新史低+永降+有最低价：hl=%d pp=%d changed=%v min=%d diff=%d",
			hl, pp, changed, minFen, diff)
	}
	if hl, _, _, _, _, _ := gameCols(t, db, 2002); hl != 2 {
		t.Fatalf("2002 应为平史低 2，实际 %d", hl)
	}
	if hl, pp, changed, _, _, _ := gameCols(t, db, 2003); hl != 1 || pp != 0 || changed != nil {
		t.Fatalf("2003 应为史低 1 + 无调价：hl=%d pp=%d changed=%v", hl, pp, changed)
	}
	if hl, pp, _, _, _, _ := gameCols(t, db, 2004); hl != 0 || pp != 2 {
		t.Fatalf("2004 应为非史低 0 + 永涨 2：hl=%d pp=%d", hl, pp)
	}
	if hl, _, _, minFen, diff, _ := gameCols(t, db, 2005); hl != 0 || minFen != 800 || diff != 0 {
		t.Fatalf("2005 无国区价：hl=%d min=%d diff=%d", hl, minFen, diff)
	}
	if _, _, changed, minFen, diff, _ := gameCols(t, db, 2006); changed != nil || minFen != -1 || diff != 0 {
		t.Fatalf("2006 无外区价：changed=%v min=%d diff=%d", changed, minFen, diff)
	}
	// smart_score 随输入单调：新史低+差价大的 2001 高于无折扣平价 2003
	_, _, _, _, _, s1 := gameCols(t, db, 2001)
	_, _, _, _, _, s3 := gameCols(t, db, 2003)
	if s1 <= s3 || s1 <= 0 {
		t.Fatalf("评分应 s1(史低+差价) > s3(无折扣) 且为正: %v vs %v", s1, s3)
	}
}

func TestPriceSnapshotNonHistoryKeepsFlags(t *testing.T) {
	db := newDerivedDB(t)
	prev := Clock
	Clock = func() time.Time { return cmpNow }
	t.Cleanup(func() { Clock = prev })

	insertGame(t, db, 3001, strp("2024-01-01"), 0, 0)
	mustExec(t, db, `UPDATE games SET hl_flag = 2 WHERE appid = 3001`)
	mustExec(t, db, `INSERT INTO game_current_prices (appid, region_code, currency, price,
		original_price, discount_percent, sub_id, price_status, fail_count, cny_fen,
		attempt_outcome, updated_at) VALUES (3001, 'CN', 'CNY', 8800, 9900, 0, 0, 'ok',
		0, 8800, 'success', ?)`, cmpNow)

	runDerived(t, db, []int64{3001}, Options{Now: cmpNow, History: false})
	if hl, _, _, _, diff, _ := gameCols(t, db, 3001); hl != 2 || diff != 0 {
		t.Fatalf("非历史口径应保留 hl 并只刷新分差：hl=%d diff=%d", hl, diff)
	}
}

// ── 系列聚类 ────────────────────────────────────────────────────

func TestIsSameSeriesRules(t *testing.T) {
	cases := []struct {
		n1, e1, n2, e2 string
		want           bool
	}{
		{"传送门", "Portal", "传送门 2", "Portal 2", true},                                 // 规则 4
		{"巫师 3：狂猎", "The Witcher 3: Wild Hunt", "巫师", "The Witcher", true},           // 规则 1
		{"使命召唤：二战", "Call of Duty: WWII", "使命召唤：先锋", "Call of Duty: Vanguard", true}, // 规则 1
		{"FIFA 22", "FIFA 22", "FIFA 23", "FIFA 23", true},                           // 规则 5
		{"辐射4", "", "辐射：新维加斯", "", true},                                             // 规则 6
		{"塞尔达传说", "The Legend of Zelda", "塞尔达传说", "The Legend of Zelda: TotK", true}, // 规则 3
		{"NBA 2K25", "NBA 2K25", "NBA 2K26", "NBA 2K26", true},                       // 规则 5
		{"赛博朋克2077", "Cyberpunk 2077", "巫师 3", "The Witcher 3", false},
		{"空洞骑士", "Hollow Knight", "丝之歌", "Hollow Knight: Silksong", true}, // 规则 4
		{"星露谷物语", "Stardew Valley", "牧场物语", "Story of Seasons", false},
		{"Call of Juarez", "", "Call of Duty", "", false}, // 泛用次词
		{"Portal", "", "Portal 2", "Portal 2", true},
	}
	for _, c := range cases {
		var e1, e2 *string
		if c.e1 != "" {
			e1 = strp(c.e1)
		}
		if c.e2 != "" {
			e2 = strp(c.e2)
		}
		if got := isSameSeries(c.n1, e1, c.n2, e2); got != c.want {
			t.Fatalf("isSameSeries(%q, %q, %q, %q) = %v, want %v", c.n1, c.e1, c.n2, c.e2, got, c.want)
		}
	}
}

func TestBuildClustersAndSeriesName(t *testing.T) {
	rows := []SeriesRow{
		{AppID: 400, Name: strp("传送门"), NameEn: strp("Portal")},
		{AppID: 401, Name: strp("传送门 2"), NameEn: strp("Portal 2")},
		{AppID: 402, Name: strp("星露谷物语"), NameEn: strp("Stardew Valley")},
		{AppID: 403, Name: strp("巫师"), NameEn: strp("The Witcher")},
		{AppID: 404, Name: strp("巫师 3：狂猎"), NameEn: strp("The Witcher 3: Wild Hunt")},
	}
	clusters := buildClusters(rows, nil)
	if len(clusters) != 2 {
		t.Fatalf("应聚出 2 簇，实际 %d: %+v", len(clusters), clusters)
	}
	byName := map[string][]int64{}
	for _, c := range clusters {
		byName[c.name] = c.ids
	}
	if ids := byName["Portal"]; len(ids) != 2 || ids[0] != 400 || ids[1] != 401 {
		t.Fatalf("Portal 簇成员不符: %v", byName)
	}
	if ids := byName["Witcher"]; len(ids) != 2 {
		t.Fatalf("Witcher 簇成员不符: %v", byName)
	}
}

func TestRefreshSeriesIncremental(t *testing.T) {
	db := newDerivedDB(t)
	ctx := context.Background()
	conn, err := db.Conn(ctx)
	if err != nil {
		t.Fatalf("取连接失败: %v", err)
	}
	defer conn.Close()

	insert := func(appid int64, name, en string) {
		mustExec(t, db, `INSERT INTO games (appid, name, name_en, type, hl_flag, pp_flag,
			diff_fen, is_adult, is_visual_novel, positive_reviews, review_count)
			VALUES (?, ?, ?, 'GAME', 0, 0, 0, 0, 0, 0, 0)`, appid, name, en)
	}
	insert(5001, "传送门", "Portal")
	insert(5002, "传送门 2", "Portal 2")

	// 首轮（changed 非空、版本未存 → 全量重算）：两簇成员落名，app_settings 存版本
	if n, err := RefreshSeries(ctx, conn, map[int64]string{5001: "", 5002: ""}, nil); err != nil || n != 2 {
		t.Fatalf("首轮应更新 2 行: n=%d err=%v", n, err)
	}
	for appid, want := range map[int64]any{5001: "Portal", 5002: "Portal"} {
		var got any
		if err := db.QueryRow("SELECT series_id FROM games WHERE appid=?", appid).Scan(&got); err != nil || got != want {
			t.Fatalf("appid %d series_id = %v (%v)，want %v", appid, got, err, want)
		}
	}
	var version string
	if err := db.QueryRow(
		"SELECT value_json FROM app_settings WHERE key='games.derived_inputs'").Scan(&version); err != nil || version != `{"series": {}}` {
		t.Fatalf("版本应存 {\"series\": {}}: %q (%v)", version, err)
	}

	// 增量路径：未受影响的落单新行保持 NULL（全量重算会写成 ''）
	insert(5003, "星露谷物语", "Stardew Valley")
	if _, err := RefreshSeries(ctx, conn, map[int64]string{5001: "Portal"}, nil); err != nil {
		t.Fatalf("增量刷新失败: %v", err)
	}
	var sid any
	if err := db.QueryRow("SELECT series_id FROM games WHERE appid=5003").Scan(&sid); err != nil || sid != nil {
		t.Fatalf("未受影响行应保持未计算 NULL: %v (%v)", sid, err)
	}

	// 增量路径吃掉新邻居：与 Portal 同名的行进 changed 后并入簇
	insert(5004, "传送门 3", "Portal 3")
	if _, err := RefreshSeries(ctx, conn, map[int64]string{5004: ""}, nil); err != nil {
		t.Fatalf("增量刷新失败: %v", err)
	}
	if err := db.QueryRow("SELECT series_id FROM games WHERE appid=5004").Scan(&sid); err != nil || sid != "Portal" {
		t.Fatalf("新邻居应并入 Portal 簇: %v (%v)", sid, err)
	}

	// 非可聚类类型清系列
	mustExec(t, db, `UPDATE games SET type='DLC', series_id='Portal' WHERE appid=5002`)
	if _, err := RefreshSeries(ctx, conn, map[int64]string{}, nil); err != nil {
		t.Fatalf("清系刷新失败: %v", err)
	}
	if err := db.QueryRow("SELECT series_id FROM games WHERE appid=5002").Scan(&sid); err != nil || sid != "" {
		t.Fatalf("DLC 行应清空系列: %v (%v)", sid, err)
	}
}
