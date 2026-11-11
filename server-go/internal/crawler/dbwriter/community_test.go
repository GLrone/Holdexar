package dbwriter

import (
	"context"
	"testing"
	"time"
)

func communityFixture(t *testing.T) *Writer {
	t.Helper()
	w, _ := newTestWriter(t)
	if _, e := w.DB.Exec(`INSERT INTO games (appid, name, family_sharing, trading_cards, is_epic, is_hb, hl_flag, pp_flag, diff_fen, is_adult, is_visual_novel, removed_strikes, positive_reviews, review_count, view_count)
		VALUES (872410, 'T', 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)`); e != nil {
		t.Fatal(e)
	}
	// 官方观测：2026-01-01 10:00 价格 19900（standard，product_unknown=0）。
	if _, e := w.DB.Exec(`INSERT INTO game_price_history
		(appid, region_code, currency, price, discount_percent, sub_id, is_gold, version_suffix, is_bundle, price_status, cny_fen, snapshot_at, source, product_unknown)
		VALUES (872410, 'CN', 'CNY', 19900, 30, NULL, 0, NULL, 0, 'ok', 1990, '2026-01-01 10:00:00', 'steam', 0)`); e != nil {
		t.Fatal(e)
	}
	return w
}

func beiTime(s string) time.Time {
	t, e := time.ParseInLocation("2006-01-02 15:04:05", s, beijingFixedZone)
	if e != nil {
		panic(e)
	}
	return t
}

func TestMergeCommunityValidationRejectsWholeBatch(t *testing.T) {
	w := communityFixture(t)
	ctx := context.Background()
	cases := []struct {
		name string
		obs  []CommunityObservation
		want string
	}{
		{"未知身份携带购买包", []CommunityObservation{{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(100), Currency: "CNY", ProductUnknown: true, SubID: int64p(5)}}, "商品身份未知"},
		{"未知身份标记金色", []CommunityObservation{{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(100), Currency: "CNY", ProductUnknown: true, IsGold: true}}, "商品身份未知"},
		{"有价缺币种", []CommunityObservation{{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(100)}}, "缺少币种"},
		{"非正价格", []CommunityObservation{{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(0), Currency: "CNY"}}, "正数"},
		{"未来时间", []CommunityObservation{{SnapshotAt: beiTime("2030-01-01 08:00:00"), PriceMinor: int64p(100), Currency: "CNY"}}, "晚于当前时间"},
		{"缺时间戳", []CommunityObservation{{PriceMinor: int64p(100), Currency: "CNY"}}, "时间戳"},
	}
	for _, tc := range cases {
		if _, e := w.MergeCommunityHistory(ctx, 872410, "cn", tc.obs); e == nil || !contains(e.Error(), tc.want) {
			t.Fatalf("%s: err=%v want~%s", tc.name, e, tc.want)
		}
	}
	// 校验失败整批拒绝：合法成员也不得落库。
	mixed := []CommunityObservation{
		{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(100), Currency: "CNY"},
		{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(0), Currency: "CNY"},
	}
	if _, e := w.MergeCommunityHistory(ctx, 872410, "cn", mixed); e == nil {
		t.Fatal("mixed batch must fail")
	}
	var rows int
	if e := w.DB.QueryRow(`SELECT count(*) FROM game_price_history WHERE source!='steam'`).Scan(&rows); e != nil {
		t.Fatal(e)
	}
	if rows != 0 {
		t.Fatalf("rejected batch leaked %d rows", rows)
	}
	// 未知身份的游戏不落库。
	ok := []CommunityObservation{{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(100), Currency: "CNY", CnyFen: int64p(100)}}
	if _, e := w.MergeCommunityHistory(ctx, 999999, "cn", ok); e == nil {
		t.Fatal("unknown appid must fail")
	}
	// 巨批拒绝。
	big := make([]CommunityObservation, mergeBatchLimit+1)
	for i := range big {
		big[i] = CommunityObservation{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(100), Currency: "CNY"}
	}
	if _, e := w.MergeCommunityHistory(ctx, 872410, "cn", big); e == nil {
		t.Fatal("oversized batch must fail")
	}
}

func TestMergeCommunityNeverOverwritesOfficialObservation(t *testing.T) {
	w := communityFixture(t)
	ctx := context.Background()
	// 与官方行六段键完全相同（snapshot_at/sub/price/is_gold 一致）→ 冲突放弃。
	obs := []CommunityObservation{
		{SnapshotAt: beiTime("2026-01-01 10:00:00"), PriceMinor: int64p(19900), Currency: "CNY", CnyFen: int64p(1)},
		{SnapshotAt: beiTime("2026-01-01 08:00:00"), PriceMinor: int64p(19900), Currency: "CNY", CnyFen: int64p(1990)},
		{SnapshotAt: beiTime("2026-01-02 08:00:00"), PriceMinor: int64p(18800), Currency: "CNY", CnyFen: int64p(1880)},
	}
	inserted, e := w.MergeCommunityHistory(ctx, 872410, "CN", obs)
	if e != nil {
		t.Fatal(e)
	}
	// 键冲突一行放弃 + 同日同价佐证一行跳过 + 新价一行入库。
	if inserted != 1 {
		t.Fatalf("inserted=%d", inserted)
	}
	var price, discount, cnyFen, source string
	if e = w.DB.QueryRow(`SELECT price, discount_percent, cny_fen, source FROM game_price_history WHERE snapshot_at='2026-01-01 10:00:00'`).Scan(&price, &discount, &cnyFen, &source); e != nil {
		t.Fatal(e)
	}
	if price != "19900" || discount != "30" || cnyFen != "1990" || source != "steam" {
		t.Fatalf("official row mutated: %s %s %s %s", price, discount, cnyFen, source)
	}
	var jan1Early int
	if e = w.DB.QueryRow(`SELECT count(*) FROM game_price_history WHERE snapshot_at LIKE '2026-01-01 08%'`).Scan(&jan1Early); e != nil {
		t.Fatal(e)
	}
	if jan1Early != 0 {
		t.Fatal("same-day same-price community point must be skipped")
	}
	var community int
	if e = w.DB.QueryRow(`SELECT count(*) FROM game_price_history WHERE source='community'`).Scan(&community); e != nil {
		t.Fatal(e)
	}
	if community != 1 {
		t.Fatalf("community rows=%d", community)
	}
	// 幂等：重复合流零新增。
	inserted, e = w.MergeCommunityHistory(ctx, 872410, "cn", obs)
	if e != nil || inserted != 0 {
		t.Fatalf("replay inserted=%d err=%v", inserted, e)
	}
	// 促销详情保持官方职责：社区行不带折扣/原价/捆绑归属。
	var nulls int
	if e = w.DB.QueryRow(`SELECT count(*) FROM game_price_history WHERE source='community' AND (original_price IS NOT NULL OR discount_percent!=0 OR bundle_id IS NOT NULL)`).Scan(&nulls); e != nil {
		t.Fatal(e)
	}
	if nulls != 0 {
		t.Fatalf("community rows carry promotion details: %d", nulls)
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (s == sub || len(sub) == 0 || indexSub(s, sub) >= 0)
}

func indexSub(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func int64p(v int64) *int64 { return &v }
