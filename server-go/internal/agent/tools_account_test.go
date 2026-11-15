package agent

import (
	"context"
	"database/sql"
	"encoding/json"
	"net/http"
	"path/filepath"
	"strings"
	"testing"
)

// accountTestSchema 本批工具直读面所需的最小表集（briefs / 关注来源 / 运行账本）。
const accountTestSchema = `
CREATE TABLE games (
  appid INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_en TEXT,
  positive_rate INTEGER,
  review_count INTEGER NOT NULL DEFAULT 0,
  chinese_support TEXT
);
CREATE TABLE game_current_prices (
  appid INTEGER NOT NULL,
  region_code TEXT NOT NULL,
  currency TEXT,
  price INTEGER,
  discount_percent INTEGER NOT NULL DEFAULT 0,
  price_status TEXT NOT NULL DEFAULT 'ok',
  cny_fen INTEGER,
  PRIMARY KEY (appid, region_code)
);
CREATE TABLE monitor_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT, target_type TEXT, target_id INTEGER, source TEXT,
  priority INTEGER DEFAULT 0, active INTEGER DEFAULT 1
);
CREATE TABLE agent_runs (
  id TEXT PRIMARY KEY, session_id TEXT, runner TEXT, "trigger" TEXT, meta TEXT,
  status TEXT, error_code TEXT, created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE TABLE agent_events (
  id INTEGER PRIMARY KEY, run_id TEXT, seq INTEGER, event_type TEXT, payload TEXT, created_at TEXT
);
`

// accountStubModes 桩端点分支开关（工具不透传 query，用可变模式切换正例/空态）。
type accountStubModes struct {
	accountNoCookie, walletFail           bool
	followEmpty, alertsEmpty, eventsEmpty bool
	achSummaryEmpty, levelEmpty           bool
	feedEmpty, challengesEmpty            bool
	gamesEmpty, famLibEmpty               bool
	famStatusEmpty, billTxsEmpty          bool
	quotaNoCookie, achSyncBusy            bool
	billSyncNoCookie, famSyncBad          bool
	ratesUnavailable, sessionsEmpty       bool
}

// newAccountTestEnv 最小库 + 桩端点 mux（端点响应形状取自真实装配实现）。
func newAccountTestEnv(t *testing.T) (*http.ServeMux, *sql.DB, *ToolRegistry, *accountStubModes) {
	t.Helper()
	db, err := sql.Open("sqlite", filepath.Join(t.TempDir(), "holdexar.db"))
	if err != nil {
		t.Fatalf("打开测试库失败: %v", err)
	}
	t.Cleanup(func() { db.Close() })
	if _, err := db.Exec(accountTestSchema); err != nil {
		t.Fatalf("建表失败: %v", err)
	}
	seed := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("种子写入失败: %v", err)
		}
	}
	seed(`INSERT INTO games (appid, name, positive_rate, review_count, chinese_support) VALUES
		(900001, '星露谷物语', 9800, 250000, 'supported'),
		(900002, '泰拉瑞亚', 9700, 180000, NULL)`)
	seed(`INSERT INTO game_current_prices (appid, region_code, currency, price, discount_percent, price_status, cny_fen) VALUES
		(900001, 'CN', 'CNY', 4800, 0, 'ok', 4800),
		(900002, 'CN', 'CNY', 3600, 25, 'ok', 2700)`)
	seed(`INSERT INTO monitor_sources (target_type, target_id, source, active) VALUES ('game', 900001, 'manual', 1)`)

	modes := &accountStubModes{}
	mux := http.NewServeMux()
	stub := func(pattern string, fn func(r *http.Request) (int, string)) {
		mux.HandleFunc(pattern, func(w http.ResponseWriter, r *http.Request) {
			code, body := fn(r)
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(code)
			_, _ = w.Write([]byte(body))
		})
	}
	// 账号状态 / 钱包同步（响应为 snake_case 原样状态图）。
	stub("GET /api/v1/account", func(r *http.Request) (int, string) {
		if modes.accountNoCookie {
			return 200, `{"has_cookie": false, "cookie_steam_id": "", "bound_steam_id": "76500000000000001", "mismatch": true, "profile": null, "wallet": null, "accounts": []}`
		}
		return 200, `{"has_cookie": true, "cookie_steam_id": "76500000000000001", "mismatch": false,
			"profile": {"steam_id": "76500000000000001", "persona_name": "矿工", "avatar_url": ""},
			"wallet": {"balance_display": "¥ 48.00", "ok_at": "2026-11-14T09:00:00", "check_ok": true},
			"session_expires_at": "2026-11-15T09:00:00", "session_expired": false, "session_has_refresh": true,
			"is_online": true, "in_game": "星露谷物语", "accounts": [{"steam_id": "76500000000000001"}, {"steam_id": "76500000000000002"}]}`
	})
	stub("POST /api/v1/account/sync", func(r *http.Request) (int, string) {
		if modes.walletFail {
			return 200, `{"has_cookie": true, "sync_error": "传输失败", "wallet": null, "accounts": []}`
		}
		return 200, `{"has_cookie": true, "sync_error": "", "wallet": {"balance_display": "¥ 48.00", "check_ok": true}, "accounts": []}`
	})
	// 关注 / 提醒。
	stub("GET /api/v1/follows", func(r *http.Request) (int, string) {
		if modes.followEmpty {
			return 200, `{"appids": []}`
		}
		return 200, `{"appids": [900002]}`
	})
	stub("GET /api/v1/alerts", func(r *http.Request) (int, string) {
		if modes.alertsEmpty {
			return 200, `[]`
		}
		return 200, `[
			{"id": 3, "appid": 900001, "region": "CN", "targetType": "price", "targetValue": 4500, "active": 1, "gameName": "星露谷物语"},
			{"id": 4, "appid": 900002, "region": "US", "targetType": "pct", "targetValue": 20, "active": 0, "gameName": ""}
		]`
	})
	stub("GET /api/v1/alerts/events", func(r *http.Request) (int, string) {
		if modes.eventsEmpty {
			return 200, `[]`
		}
		return 200, `[{"id": 9, "appid": 900001, "gameName": "星露谷物语", "region": "CN", "priceCny": 4800, "priceText": "¥48.00", "triggeredAt": "2026-11-14T10:00:00", "notified": 1}]`
	})
	// 成就面。
	stub("GET /api/v1/achievements/summary", func(r *http.Request) (int, string) {
		if modes.achSummaryEmpty {
			return 200, `{"hasCredential": false, "steamid": "", "platinum": 0, "totalAchievements": 0, "unlockedAchievements": 0, "completionRate": 0, "lastSyncedAt": null, "platinums": [], "recentUnlocks": []}`
		}
		return 200, `{"hasCredential": true, "steamid": "76500000000000001", "platinum": 2, "totalAchievements": 300, "unlockedAchievements": 150, "completionRate": 50.0, "lastSyncedAt": "2026-11-14T08:00:00",
			"platinums": [{"appid": 900001, "name": "星露谷物语"}, {"appid": 900002, "name": "泰拉瑞亚"}],
			"recentUnlocks": [{"appid": 900001, "name": "丰收", "gameName": "星露谷物语", "unlockTime": 1731546000}]}`
	})
	stub("GET /api/v1/achievements/level", func(r *http.Request) (int, string) {
		if modes.levelEmpty {
			return 200, `{"steamid": "", "level": 0, "tier": "青", "totalXp": 0, "xpIntoLevel": 0, "xpForNextLevel": 100, "activeBonuses": []}`
		}
		return 200, `{"steamid": "76500000000000001", "level": 12, "tier": "银", "totalXp": 8400, "xpIntoLevel": 400, "xpForNextLevel": 900, "activeBonuses": [{"kind": "streak", "multiplier": 1.2, "streakDays": 5}]}`
	})
	stub("GET /api/v1/achievements/feed", func(r *http.Request) (int, string) {
		if modes.feedEmpty {
			return 200, `{"items": []}`
		}
		return 200, `{"items": [{"appid": 900001, "name": "丰收", "gameName": "星露谷物语", "unlockTime": 1731546000}]}`
	})
	stub("GET /api/v1/achievements/challenges", func(r *http.Request) (int, string) {
		if modes.challengesEmpty {
			return 200, `{"steamid": "", "challenges": [], "summary": {"total": 0, "completed": 0, "claimed": 0, "claimable": 0}}`
		}
		return 200, `{"steamid": "76500000000000001", "summary": {"total": 10, "completed": 4, "claimed": 3, "claimable": 1},
			"challenges": [
				{"challengeId": "platinum_3", "status": "active", "target": 10, "progress": 8, "rewardXp": 300},
				{"challengeId": "review_1", "status": "active", "target": 5, "progress": 1, "rewardXp": 100},
				{"challengeId": "done_1", "status": "completed", "target": 2, "progress": 2, "rewardXp": 50}
			]}`
	})
	stub("GET /api/v1/achievements/games", func(r *http.Request) (int, string) {
		if modes.gamesEmpty {
			return 200, `{"games": [], "count": 0}`
		}
		return 200, `{"games": [{"appid": 900001, "name": "星露谷物语", "total": 40, "unlocked": 38, "percent": 95.0, "remaining": 2, "platinum": false}], "count": 1}`
	})
	stub("GET /api/v1/achievements/games/{appid}", func(r *http.Request) (int, string) {
		if r.PathValue("appid") == "900001" {
			return 200, `{"appid": 900001, "name": "星露谷物语", "total": 40, "unlocked": 38, "percent": 95.0, "platinum": false, "playtimeMin": 1200,
				"achievements": [
					{"name": "丰收", "achieved": true, "rarity": "common", "globalPercent": 88.5, "unlockTime": 1731546000},
					{"name": "全镇之心", "achieved": false, "rarity": "rare", "globalPercent": 4.2, "unlockTime": 0}
				]}`
		}
		return 404, `{"detail": "该游戏不在成就快照中，请先同步"}`
	})
	// 家庭 / 账单 / 额度。
	stub("GET /api/v1/family/library", func(r *http.Request) (int, string) {
		if modes.famLibEmpty {
			return 200, `{"familyGroupid": "g1", "games": [], "members": []}`
		}
		return 200, `{"familyGroupid": "g1", "games": [{"appid": 900002, "owners": ["a"]}], "members": []}`
	})
	stub("GET /api/v1/family/status", func(r *http.Request) (int, string) {
		if modes.famStatusEmpty {
			return 200, `{"bound": false, "primarySteamid": "", "joined": false, "walletRegion": null, "groups": [], "members": []}`
		}
		return 200, `{"bound": true, "primarySteamid": "76500000000000001", "joined": true, "walletRegion": "cn",
			"groups": [{"steamid": "76500000000000001", "joined": true, "members": [
				{"steamid": "76500000000000001", "role": "primary", "personaName": "矿工", "avatarUrl": "", "region": "cn"},
				{"steamid": "76500000000000002", "role": "member", "personaName": "小工", "avatarUrl": "http://a/2", "region": "us"}
			]}]}`
	})
	stub("GET /api/v1/bills/imports/{import_id}/overview", func(r *http.Request) (int, string) {
		if r.PathValue("import_id") != "7" {
			return 404, `{"detail": "'账单 8 不存在'"}`
		}
		return 200, `{"id": 7, "nickname": "十一月账单", "sourceFile": null, "importedAt": "2026-11-01T09:00:00",
			"summary": {"netFen": 4800, "spendFen": 5000, "refundFen": 200, "orders": 2, "txCount": 5, "accountValueFen": 4800, "topupNetFen": 3000},
			"years": [{"year": "2026", "netFen": 4800, "count": 5, "months": {}}, {"year": "2025", "netFen": 100, "count": 1, "months": {}}],
			"counts": {"gameTxs": 5}}`
	})
	stub("GET /api/v1/bills/imports/{import_id}/game-txs", func(r *http.Request) (int, string) {
		if r.PathValue("import_id") != "7" {
			return 404, `{"detail": "' 8 不存在'"}`
		}
		if modes.billTxsEmpty {
			return 200, `{"total": 0, "rows": [], "limit": 50, "offset": 0}`
		}
		return 200, `{"total": 1, "rows": [{"id": 1, "date": "2026-11-02", "txType": "purchase", "items": ["星露谷物语"], "cnyFen": 4800, "isRefund": false}], "limit": 50, "offset": 0}`
	})
	stub("GET /api/v1/redeem/quota", func(r *http.Request) (int, string) {
		if modes.quotaNoCookie {
			return 200, `{"hasCookie": false, "hasSessionId": false, "steamId": "", "used": 0, "limit": 0}`
		}
		return 200, `{"hasCookie": true, "hasSessionId": true, "steamId": "76500000000000001", "used": 2, "limit": 5}`
	})
	// 同步 POST 面。
	stub("POST /api/v1/accounts/{steamid}/sync", func(r *http.Request) (int, string) {
		return 200, `{"steamid": "` + r.PathValue("steamid") + `", "wishlistCount": 3, "ownedCount": 9, "added": 1, "addedOwned": 0, "active": 10, "newAppids": [900002], "newOwnedAppids": []}`
	})
	stub("POST /api/v1/achievements/sync", func(r *http.Request) (int, string) {
		if modes.achSyncBusy {
			return 409, `{"detail": "成就同步已在进行中"}`
		}
		return 200, `{"running": true, "ok": true, "stage": "library", "done": 0, "total": 0, "current": "", "error": "", "startedAt": "2026-11-14T09:00:00", "syncedAt": "", "steamid": "76500000000000001"}`
	})
	stub("POST /api/v1/bills/sync", func(r *http.Request) (int, string) {
		if modes.billSyncNoCookie {
			return 409, `{"detail": "尚未绑定 Steam Cookie，无法同步账单"}`
		}
		return 200, `{"ok": true, "status": "probed_skip", "steamId": "76500000000000001", "historyRows": 0, "licenseRows": 0}`
	})
	stub("POST /api/v1/family/sync", func(r *http.Request) (int, string) {
		if modes.famSyncBad {
			return 400, `{"detail": "尚未绑定 Steam Cookie，无法发现家庭组（请先在「我」页绑定）"}`
		}
		return 200, `{"results": [{"steamid": "76500000000000001", "joined": true}], "synced": 1, "joined": 1, "failed": 0}`
	})
	stub("POST /api/v1/rates/refresh", func(r *http.Request) (int, string) {
		if modes.ratesUnavailable {
			return 502, `{"detail": "汇率来源不可用"}`
		}
		return 200, `{"rows": [{"code": "USD", "rate": 7.25}], "source": "augmentedsteam"}`
	})
	// 领航员会话读面。
	stub("GET /api/v1/pilot/sessions", func(r *http.Request) (int, string) {
		if modes.sessionsEmpty {
			return 200, `{"items": []}`
		}
		return 200, `{"items": [{"sid": "abc12345xyz", "title": "问价格", "turn_total": 2, "updated_at": "2026-11-14T09:00:00"}]}`
	})
	stub("GET /api/v1/pilot/sessions/{session_id}", func(r *http.Request) (int, string) {
		if r.PathValue("session_id") != "abc12345xyz" {
			return 404, `{"detail": "session not found"}`
		}
		return 200, `{"session_id": "abc12345xyz",
			"turns": [
				{"q": "星露谷现在多少钱", "resp": {"answer": "国区 48 元，史低。", "tools": ["查到现价 4800 分"]}, "ts": "2026-11-14T09:00:00"},
				{"q": "那泰拉瑞亚呢", "resp": {"answer": "", "tools": ["查到现价 2700 分"]}, "ts": "2026-11-14T09:01:00"}
			],
			"turn_total": 2, "summary_through": 0, "title": "问价格"}`
	})

	// 独立空注册表 + 本批注册（buildRegistry 已含本批，重复调用即重名）。
	registry := newRegistry()
	if err := RegisterAccountTools(registry); err != nil {
		t.Fatalf("批次二注册失败: %v", err)
	}
	return mux, db, registry, modes
}

// runAccountTool 批次二测试入口：本地注册表取工具 → 执行 → 解码 payload。
func runAccountTool(t *testing.T, mux *http.ServeMux, db *sql.DB, registry *ToolRegistry, name, argsJSON string) map[string]any {
	t.Helper()
	spec := registry.get(name)
	if spec == nil {
		t.Fatalf("工具未注册: %s", name)
	}
	var args map[string]any
	if argsJSON != "" {
		if err := json.Unmarshal([]byte(argsJSON), &args); err != nil {
			t.Fatalf("入参解码失败: %v", err)
		}
	}
	res := Execute(&CallContext{Ctx: context.Background(), Args: args, fetch: newMuxFetcher(mux), post: newMuxPoster(mux), db: db}, spec, false)
	var payload map[string]any
	if err := json.Unmarshal([]byte(encodeJSON(payloadWithStatus(res))), &payload); err != nil {
		t.Fatalf("结果解码失败: %v", err)
	}
	return payload
}

// rowsOf payload["rows"] → map 切片。
func rowsOf(t *testing.T, payload map[string]any) []map[string]any {
	t.Helper()
	raw, ok := payload["rows"].([]any)
	if !ok {
		t.Fatalf("rows 缺失: %v", payload)
	}
	out := make([]map[string]any, 0, len(raw))
	for _, v := range raw {
		m, ok := v.(map[string]any)
		if !ok {
			t.Fatalf("行形状不符: %v", v)
		}
		out = append(out, m)
	}
	return out
}

// seedAgentRuns 运行账本种子（jobrun 同表形状）。
func seedAgentRuns(t *testing.T, db *sql.DB) {
	t.Helper()
	ins := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("运行账本种子失败: %v", err)
		}
	}
	ins(`INSERT INTO agent_runs (id, session_id, runner, "trigger", meta, status, created_at, started_at, finished_at) VALUES
		('runaaaa1111', 'runtime', 'job', 'scheduled', '{"job":"wallet_sync","entry":"scheduled"}', 'done', '2026-11-14 09:00:00.000000', '2026-11-14 09:00:00.000000', '2026-11-14 09:01:00.000000'),
		('runbbbb2222', 'runtime', 'job', 'manual', '{"job":"achievements_sync","entry":"manual_refresh_button"}', 'failed', '2026-11-14 08:00:00.000000', '2026-11-14 08:00:00.000000', NULL)`)
	ins(`INSERT INTO agent_events (run_id, seq, event_type, created_at) VALUES
		('runaaaa1111', 1, 'run.state', '2026-11-14 09:00:00.000000'),
		('runaaaa1111', 2, 'run.finished', '2026-11-14 09:01:00.000000')`)
}

// TestAccountToolsReadShapes 读面正例：形状与词条逐字段核对（对照 §五基线）。
func TestAccountToolsReadShapes(t *testing.T) {
	mux, db, registry, _ := newAccountTestEnv(t)
	seedAgentRuns(t, db)

	payload := runAccountTool(t, mux, db, registry, "account_status", ``)
	if payload["kind"] != "rows" || payload["titleKey"] != "accountStatus" {
		t.Fatalf("账号状态形状不符: %v", payload)
	}
	if payload["steamId"] != "76500000000000001" || payload["personaName"] != "矿工" ||
		payload["walletBalance"] != "¥ 48.00" || payload["sessionExpired"] != false ||
		payload["accountsCount"].(float64) != 2 {
		t.Fatalf("账号状态顶层字段不符: %v", payload)
	}
	statusRows := rowsOf(t, payload)
	if len(statusRows) != 3 || statusRows[0]["vKey"] != "acctStatus" || statusRows[1]["vKey"] != "walletBalance" {
		t.Fatalf("账号状态行序不符: %v", statusRows)
	}

	payload = runAccountTool(t, mux, db, registry, "list_follows", ``)
	if payload["kind"] != "games" || payload["titleKey"] != "follows" || payload["total"].(float64) != 2 {
		t.Fatalf("关注清单形状不符: %v", payload)
	}
	items := payload["items"].([]any)
	first := items[0].(map[string]any)
	if first["appid"].(float64) != 900001 {
		t.Fatalf("关注并集排序不符: %v", items)
	}
	if first["note"].(map[string]any)["key"] != "manual" {
		t.Fatalf("manual 来源注记不符: %v", first)
	}
	second := items[1].(map[string]any)
	if second["note"].(map[string]any)["key"] != "favorite" {
		t.Fatalf("favorite 来源注记不符: %v", second)
	}
	if first["cnyFen"].(float64) != 4800 || second["discount"].(float64) != 25 {
		t.Fatalf("关注简报字段不符: %v", items)
	}

	payload = runAccountTool(t, mux, db, registry, "list_alerts", ``)
	alertRows := rowsOf(t, payload)
	if payload["total"].(float64) != 2 || alertRows[0]["vKey"] != "alertPrice" || alertRows[0]["tone"] != "ok" {
		t.Fatalf("提醒清单形状不符: %v", payload)
	}
	if alertRows[1]["vKey"] != "alertPct" || alertRows[1]["tone"] != "warn" || alertRows[1]["k"] != "AppID 900002" {
		t.Fatalf("提醒缺名兜底/词条不符: %v", alertRows[1])
	}

	payload = runAccountTool(t, mux, db, registry, "alert_history", `{"limit": 5}`)
	historyRows := rowsOf(t, payload)
	if payload["total"].(float64) != 1 || historyRows[0]["vKey"] != "alertHit" || historyRows[0]["tone"] != "ok" {
		t.Fatalf("提醒历史形状不符: %v", payload)
	}
	data := historyRows[0]["data"].(map[string]any)
	if data["notified"] != true || data["priceText"] != "¥48.00" {
		t.Fatalf("提醒历史数据不符: %v", data)
	}

	payload = runAccountTool(t, mux, db, registry, "audit_follows_workflow", ``)
	stepper := payload["stepper"].(map[string]any)
	if payload["kind"] != "stepper" || stepper["currentStepIndex"].(float64) != 2 {
		t.Fatalf("巡检 stepper 形状不符: %v", payload)
	}
	steps := stepper["steps"].([]any)
	if len(steps) != 3 || steps[0].(map[string]any)["status"] != "ok" {
		t.Fatalf("巡检步骤序不符: %v", steps)
	}

	payload = runAccountTool(t, mux, db, registry, "achievements_summary", ``)
	if payload["kind"] != "achievements" || payload["platinum"].(float64) != 2 ||
		payload["unlocked"].(float64) != 150 || payload["total"].(float64) != 300 {
		t.Fatalf("成就概览 KPI 不符: %v", payload)
	}
	platinums := payload["platinums"].([]any)
	if len(platinums) != 2 || platinums[0].(map[string]any)["appid"].(float64) != 900001 {
		t.Fatalf("白金陈列不符: %v", platinums)
	}
	recent := payload["recent"].([]any)
	if len(recent) != 1 || recent[0].(map[string]any)["gameName"] != "星露谷物语" {
		t.Fatalf("最近解锁不符: %v", recent)
	}

	payload = runAccountTool(t, mux, db, registry, "level_profile", ``)
	levelRows := rowsOf(t, payload)
	if levelRows[0]["vKey"] != "levelNow" || payload["level"].(float64) != 12 || payload["totalXp"].(float64) != 8400 {
		t.Fatalf("等级读数不符: %v", payload)
	}
	if payload["activeBonusMultiplier"].(float64) != 1.2 || payload["streakDays"].(float64) != 5 {
		t.Fatalf("连猎加成不符: %v", payload)
	}
	if levelRows[2]["vKey"] != "levelBonus" || levelRows[2]["data"].(map[string]any)["percent"].(float64) != 20 {
		t.Fatalf("加成行不符: %v", levelRows[2])
	}

	payload = runAccountTool(t, mux, db, registry, "challenges_overview", ``)
	challengeRows := rowsOf(t, payload)
	if payload["completed"].(float64) != 4 || payload["claimable"].(float64) != 1 {
		t.Fatalf("挑战汇总不符: %v", payload)
	}
	near := payload["nearComplete"].([]any)
	if len(near) != 2 || near[0].(map[string]any)["challengeId"] != "platinum_3" {
		t.Fatalf("挑战近完成排序不符: %v", near)
	}
	if challengeRows[0]["vKey"] != "challengeSummary" {
		t.Fatalf("挑战行序不符: %v", challengeRows)
	}

	payload = runAccountTool(t, mux, db, registry, "achievements_near_completion", ``)
	nearRows := rowsOf(t, payload)
	if payload["titleKey"] != "achNearDone" || payload["total"].(float64) != 1 ||
		nearRows[0]["data"].(map[string]any)["remaining"].(float64) != 2 {
		t.Fatalf("快白金清单不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "achievements_game_detail", `{"appid": 900001}`)
	if payload["appid"].(float64) != 900001 || payload["percent"].(float64) != 95.0 || payload["unlocked"].(float64) != 38 {
		t.Fatalf("成就明细顶层不符: %v", payload)
	}
	detailRows := rowsOf(t, payload)
	if detailRows[0]["vKey"] != "achProgress" || detailRows[1]["vKey"] != "achItem" || detailRows[1]["tone"] != "ok" {
		t.Fatalf("成就明细行序不符: %v", detailRows)
	}

	payload = runAccountTool(t, mux, db, registry, "list_family_library", ``)
	if payload["titleKey"] != "famLibrary" || payload["total"].(float64) != 1 {
		t.Fatalf("家庭库形状不符: %v", payload)
	}
	famItems := payload["items"].([]any)
	if famItems[0].(map[string]any)["appid"].(float64) != 900002 {
		t.Fatalf("家庭库条目不符: %v", famItems)
	}

	payload = runAccountTool(t, mux, db, registry, "family_status", ``)
	if payload["kind"] != "family" || payload["bound"] != true || payload["joined"] != true || payload["walletRegion"] != "cn" {
		t.Fatalf("家庭组状态不符: %v", payload)
	}
	members := payload["members"].([]any)
	if len(members) != 2 || members[0].(map[string]any)["role"] != "primary" || payload["total"].(float64) != 2 {
		t.Fatalf("家庭组成员不符: %v", members)
	}

	payload = runAccountTool(t, mux, db, registry, "bills_spend_query", `{"import_id": 7}`)
	if payload["titleKey"] != "billsQuery" || payload["importId"].(float64) != 7 {
		t.Fatalf("账单总览形状不符: %v", payload)
	}
	summary := payload["summary"].(map[string]any)
	if summary["netFen"].(float64) != 4800 || summary["topupNetFen"].(float64) != 3000 || summary["orders"].(float64) != 2 {
		t.Fatalf("账单汇总不符: %v", summary)
	}
	billRows := rowsOf(t, payload)
	if billRows[0]["k"] != "十一月账单" || billRows[1]["vKey"] != "billTopup" {
		t.Fatalf("账单总览行不符: %v", billRows)
	}

	payload = runAccountTool(t, mux, db, registry, "bills_spend_query", `{"import_id": 7, "mode": "game_txs"}`)
	txRows := rowsOf(t, payload)
	if payload["total"].(float64) != 1 || txRows[0]["k"] != "星露谷物语" || txRows[0]["vKey"] != "billTx" {
		t.Fatalf("账单明细形状不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "redeem_quota", ``)
	quotaRows := rowsOf(t, payload)
	if quotaRows[0]["vKey"] != "redeemQuota" || quotaRows[0]["tone"] != "ok" {
		t.Fatalf("额度行不符: %v", quotaRows)
	}
	if _, has := payload["total"]; has {
		t.Fatal("redeem_quota 不应携带 total（Python 同径）")
	}

	payload = runAccountTool(t, mux, db, registry, "list_sessions", ``)
	sessionRows := rowsOf(t, payload)
	if payload["titleKey"] != "sessions" || payload["total"].(float64) != 1 ||
		sessionRows[0]["k"] != "问价格" || sessionRows[0]["vKey"] != "sessionTurns" {
		t.Fatalf("会话清单形状不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "read_session", `{"sid": "abc12345xyz"}`)
	if payload["titleKey"] != "sessionRead" || payload["sid"] != "abc12345xyz" ||
		payload["startTurn"].(float64) != 0 || payload["turnCount"].(float64) != 2 ||
		payload["sessionTitle"] != "问价格" {
		t.Fatalf("会话回读顶层不符: %v", payload)
	}
	readRows := rowsOf(t, payload)
	if readRows[0]["k"] != "星露谷现在多少钱" || readRows[0]["v"] != "国区 48 元，史低。" {
		t.Fatalf("会话回读行不符: %v", readRows)
	}
	if readRows[1]["v"] != "查到现价 2700 分" {
		t.Fatalf("会话回读工具回退不符: %v", readRows[1])
	}
	digest := payload["digest"].(string)
	if !strings.HasPrefix(digest, "用户：星露谷现在多少钱\n助手：国区 48 元，史低。\n---\n") {
		t.Fatalf("digest 形状不符: %q", digest)
	}

	payload = runAccountTool(t, mux, db, registry, "agent_runs_list", ``)
	runRows := rowsOf(t, payload)
	if payload["titleKey"] != "agentRuns" || payload["total"].(float64) != 2 {
		t.Fatalf("运行账本形状不符: %v", payload)
	}
	if runRows[0]["run_id"] != "runaaaa1111" || runRows[0]["v"] != "done" || runRows[0]["v2"] != "job" {
		t.Fatalf("运行账本行不符: %v", runRows[0])
	}
	if runRows[1]["at"] != "2026-11-14 08:00:00.000000" {
		t.Fatalf("无终态运行应回落 started_at: %v", runRows[1])
	}

	payload = runAccountTool(t, mux, db, registry, "agent_run_events", `{"run_id": "runaaaa1111"}`)
	eventRows := rowsOf(t, payload)
	if payload["runId"] != "runaaaa1111" || payload["runStatus"] != "done" || payload["total"].(float64) != 2 {
		t.Fatalf("事件回放形状不符: %v", payload)
	}
	if eventRows[0]["k"] != "run.state" || eventRows[0]["v"] != "1" {
		t.Fatalf("事件行不符: %v", eventRows[0])
	}
}

// TestAccountToolsEmptyAndCredentialEdges 空态 / 无凭据边界（对照 §五案名）。
func TestAccountToolsEmptyAndCredentialEdges(t *testing.T) {
	mux, db, registry, modes := newAccountTestEnv(t)
	seed := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("种子写入失败: %v", err)
		}
	}

	modes.accountNoCookie = true
	payload := runAccountTool(t, mux, db, registry, "account_status", ``)
	if payload["kind"] != "empty" || payload["hasCookie"] != false || payload["bound"] != true || payload["mismatch"] != true {
		t.Fatalf("无凭据账号状态形状不符: %v", payload)
	}
	modes.accountNoCookie = false

	seed(`DELETE FROM monitor_sources`)
	seed(`INSERT INTO monitor_sources (target_type, target_id, source, active) VALUES ('game', 900001, 'manual', 0)`)
	payload = runAccountTool(t, mux, db, registry, "list_follows", ``)
	if payload["total"].(float64) != 1 || payload["items"].([]any)[0].(map[string]any)["note"].(map[string]any)["key"] != "favorite" {
		t.Fatalf("仅收藏关注合并不符: %v", payload)
	}
	seed(`DELETE FROM monitor_sources`)
	modes.followEmpty = true
	payload = runAccountTool(t, mux, db, registry, "list_follows", ``)
	if payload["total"].(float64) != 0 || len(payload["items"].([]any)) != 0 {
		t.Fatalf("空关注清单不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "audit_follows_workflow", ``)
	stepper := payload["stepper"].(map[string]any)
	if stepper["currentStepIndex"].(float64) != 0 || stepper["steps"].([]any)[0].(map[string]any)["status"] != "empty" {
		t.Fatalf("空关注库 stepper 不符: %v", payload)
	}

	modes.alertsEmpty = true
	payload = runAccountTool(t, mux, db, registry, "list_alerts", ``)
	if payload["total"].(float64) != 0 || len(rowsOf(t, payload)) != 0 {
		t.Fatalf("空提醒清单不符: %v", payload)
	}

	modes.eventsEmpty = true
	payload = runAccountTool(t, mux, db, registry, "alert_history", ``)
	if payload["kind"] != "empty" || len(payload["events"].([]any)) != 0 {
		t.Fatalf("空提醒历史不符: %v", payload)
	}

	modes.achSummaryEmpty = true
	payload = runAccountTool(t, mux, db, registry, "achievements_summary", ``)
	if payload["hasCredential"] != false || len(payload["platinums"].([]any)) != 0 || len(payload["recent"].([]any)) != 0 {
		t.Fatalf("无凭据成就概览不符: %v", payload)
	}

	modes.levelEmpty = true
	modes.feedEmpty = true
	payload = runAccountTool(t, mux, db, registry, "level_profile", ``)
	if payload["level"].(float64) != 0 || payload["activeBonusMultiplier"].(float64) != 1.0 {
		t.Fatalf("空等级读数不符: %v", payload)
	}

	modes.challengesEmpty = true
	payload = runAccountTool(t, mux, db, registry, "challenges_overview", ``)
	if payload["completed"].(float64) != 0 || len(payload["nearComplete"].([]any)) != 0 {
		t.Fatalf("空挑战读数不符: %v", payload)
	}

	modes.gamesEmpty = true
	payload = runAccountTool(t, mux, db, registry, "achievements_near_completion", ``)
	if payload["kind"] != "empty" || len(payload["games"].([]any)) != 0 {
		t.Fatalf("空快白金清单不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "achievements_game_detail", `{"appid": 42}`)
	if payload["kind"] != "empty" || payload["appid"].(float64) != 42 {
		t.Fatalf("成就缺游戏空态不符: %v", payload)
	}

	modes.famLibEmpty = true
	payload = runAccountTool(t, mux, db, registry, "list_family_library", ``)
	if payload["total"].(float64) != 0 || len(payload["items"].([]any)) != 0 {
		t.Fatalf("空家庭库不符: %v", payload)
	}

	modes.famStatusEmpty = true
	payload = runAccountTool(t, mux, db, registry, "family_status", ``)
	if payload["bound"] != false || len(payload["members"].([]any)) != 0 {
		t.Fatalf("未绑定家庭组不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "bills_spend_query", `{"import_id": 8}`)
	if payload["kind"] != "empty" || payload["importId"].(float64) != 8 || payload["mode"] != "overview" {
		t.Fatalf("缺 import 空态不符: %v", payload)
	}

	modes.billTxsEmpty = true
	payload = runAccountTool(t, mux, db, registry, "bills_spend_query", `{"import_id": 7, "mode": "game_txs", "year": "1999"}`)
	if payload["kind"] != "empty" || payload["mode"] != "game_txs" {
		t.Fatalf("无交易空态不符: %v", payload)
	}

	modes.quotaNoCookie = true
	payload = runAccountTool(t, mux, db, registry, "redeem_quota", ``)
	quotaRows := rowsOf(t, payload)
	if quotaRows[0]["vKey"] != "redeemNoCookie" || quotaRows[0]["tone"] != "warn" {
		t.Fatalf("无 Cookie 额度行不符: %v", quotaRows)
	}

	modes.walletFail = true
	payload = runAccountTool(t, mux, db, registry, "sync_wallet", ``)
	walletRows := rowsOf(t, payload)
	if walletRows[0]["vKey"] != "walletFailed" || walletRows[0]["tone"] != "bad" || walletRows[0]["v"] != "传输失败" {
		t.Fatalf("钱包同步失败行不符: %v", walletRows)
	}

	modes.billSyncNoCookie = true
	payload = runAccountTool(t, mux, db, registry, "sync_bills", ``)
	billRows := rowsOf(t, payload)
	if billRows[0]["vKey"] != "billNoCookie" || billRows[0]["tone"] != "warn" {
		t.Fatalf("账单无 Cookie 行不符: %v", billRows)
	}

	modes.achSyncBusy = true
	payload = runAccountTool(t, mux, db, registry, "sync_achievements", ``)
	achRows := rowsOf(t, payload)
	if achRows[0]["vKey"] != "syncRunning" || achRows[0]["tone"] != "warn" {
		t.Fatalf("成就同步进行中行不符: %v", achRows)
	}

	modes.famSyncBad = true
	payload = runAccountTool(t, mux, db, registry, "sync_family", ``)
	if payload["status"] != "error" {
		t.Fatalf("家庭组同步无凭据应归一 failed: %v", payload)
	}

	modes.ratesUnavailable = true
	payload = runAccountTool(t, mux, db, registry, "refresh_rates", ``)
	if payload["status"] != "error" {
		t.Fatalf("汇率源不可用应归一 failed: %v", payload)
	}

	modes.sessionsEmpty = true
	payload = runAccountTool(t, mux, db, registry, "list_sessions", ``)
	if payload["total"].(float64) != 0 {
		t.Fatalf("空会话清单不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "read_session", `{"sid": "missing"}`)
	if payload["kind"] != "empty" || payload["note"] != "no_data" {
		t.Fatalf("缺会话空态不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "agent_run_events", `{"run_id": "ghost"}`)
	if payload["kind"] != "empty" || payload["note"] != "no_run" {
		t.Fatalf("缺运行空态不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "agent_run_events", ``)
	if payload["kind"] != "empty" || payload["note"] != "no_run_id" {
		t.Fatalf("缺 run_id 空态不符: %v", payload)
	}
}

// TestAccountToolsSyncDoneShapes 同步面 done 词条与库同步链（wishlist 执行入口）。
func TestAccountToolsSyncDoneShapes(t *testing.T) {
	mux, db, registry, _ := newAccountTestEnv(t)

	payload := runAccountTool(t, mux, db, registry, "sync_wallet", ``)
	if rowsOf(t, payload)[0]["vKey"] != "walletSynced" {
		t.Fatalf("钱包同步成功行不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "sync_library", `{"steam_id": "76500000000000001"}`)
	libRows := rowsOf(t, payload)
	if libRows[0]["vKey"] != "syncDone" || payload["titleKey"] != "library" {
		t.Fatalf("库同步形状不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "sync_library", ``)
	if payload["status"] != "error" {
		t.Fatalf("缺 steam_id 库同步应归一 failed（Python 同径）: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "sync_achievements", `{"steam_id": "76500000000000001"}`)
	if rowsOf(t, payload)[0]["vKey"] != "syncDone" {
		t.Fatalf("成就同步成功行不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "sync_bills", ``)
	if rowsOf(t, payload)[0]["vKey"] != "syncDone" {
		t.Fatalf("账单同步成功行不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "sync_family", ``)
	if rowsOf(t, payload)[0]["vKey"] != "syncDone" {
		t.Fatalf("家庭组同步成功行不符: %v", payload)
	}

	payload = runAccountTool(t, mux, db, registry, "refresh_rates", ``)
	if rowsOf(t, payload)[0]["vKey"] != "syncDone" {
		t.Fatalf("汇率刷新成功行不符: %v", payload)
	}
}

// TestAccountToolsListTools 元工具：CORE_TOOLS 差集 + 自身排除 + 描述折叠截断。
func TestAccountToolsListTools(t *testing.T) {
	mux, db, registry, _ := newAccountTestEnv(t)

	payload := runAccountTool(t, mux, db, registry, "list_tools", ``)
	if payload["kind"] != "tool_catalog" {
		t.Fatalf("工具目录形状不符: %v", payload)
	}
	tools := payload["tools"].([]any)
	names := map[string]bool{}
	for _, v := range tools {
		row := v.(map[string]any)
		names[row["name"].(string)] = true
		if len([]rune(row["desc"].(string))) > 56 {
			t.Fatalf("描述未按 56 字截断: %v", row)
		}
		if row["group"] != "read" {
			t.Fatalf("分组应为 read: %v", row)
		}
	}
	if !names["account_status"] || !names["sync_wallet"] || !names["family_status"] {
		t.Fatalf("批次二工具应在解锁清单内: %v", names)
	}
	for _, core := range []string{"list_follows", "list_alerts", "read_session", "list_tools"} {
		if names[core] {
			t.Fatalf("常驻/自身不应出现在解锁清单: %s", core)
		}
	}
	if int(payload["count"].(float64)) != len(tools) {
		t.Fatalf("count 与清单不符: %v", payload["count"])
	}
}

// TestAccountToolsTimeoutBudgets 预算分档钉住（批次二预算表）。
func TestAccountToolsTimeoutBudgets(t *testing.T) {
	r := newRegistry()
	if err := RegisterAccountTools(r); err != nil {
		t.Fatal(err)
	}
	for _, tc := range []struct {
		name string
		want float64
	}{
		{"sync_wallet", 240}, {"sync_library", 240}, {"sync_achievements", 240},
		{"sync_bills", 240}, {"sync_family", 240}, {"refresh_rates", 240},
		{"achievements_summary", 60}, {"level_profile", 60}, {"challenges_overview", 60},
		{"list_family_library", 60}, {"achievements_near_completion", 60},
		{"bills_spend_query", 60}, {"read_session", 60},
		{"audit_follows_workflow", 90},
		{"account_status", 20}, {"list_follows", 20}, {"list_alerts", 20},
		{"alert_history", 20}, {"achievements_game_detail", 20}, {"family_status", 20},
		{"redeem_quota", 20}, {"list_sessions", 20}, {"agent_runs_list", 20},
		{"agent_run_events", 20}, {"list_tools", 20},
	} {
		spec := r.get(tc.name)
		if spec == nil {
			t.Fatalf("工具未注册: %s", tc.name)
		}
		if spec.TimeoutS != tc.want {
			t.Fatalf("%s 预算=%v，应为 %v", tc.name, spec.TimeoutS, tc.want)
		}
		if spec.Group != GroupRead {
			t.Fatalf("%s 分组应为 read", tc.name)
		}
	}
}

// TestAccountRegistryNoDupes 批次二重名防线：重复注册报错（空注册表上
// 注册两次——buildRegistry 已含本批，不再重复调用）。
func TestAccountRegistryNoDupes(t *testing.T) {
	r := newRegistry()
	if err := RegisterAccountTools(r); err != nil {
		t.Fatal(err)
	}
	if err := RegisterAccountTools(r); err == nil {
		t.Fatal("重复注册应报错")
	}
}
