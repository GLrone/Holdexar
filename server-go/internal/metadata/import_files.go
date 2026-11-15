// Package metadata 外部元数据导入：Epic / XGP / Humble Choice 三源数据文件
// 一次性打标进 games 库内已有行（等价 Python domains/metadata/service.py 的
// import_epic / import_xgp / import_hb / import_all）。只更新库内已有 appid，
// 不新建目录行。
package metadata

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"net/url"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"holdexar/server-go/internal/store"
)

// 外部数据文件名（Python service.py 常量同值）。
const (
	epicFile     = "epic_free_games_clean_final.json"
	xgpTiered    = "xgp_tiered_steam.json"
	xgpLinks     = "xgp_steam_links.json"
	historicalDB = "steam_historical_lows.db"
)

// resolveSourcePath 导入文件定位：显式 source_dir 优先，缺省走环境变量
// METADATA_DATA_DIR（Python DEFAULT_SOURCE_DIR 同语义，未设置即相对路径）。
func resolveSourcePath(sourceDir, filename string) string {
	base := sourceDir
	if base == "" {
		base = os.Getenv("METADATA_DATA_DIR")
	}
	return filepath.Join(base, filename)
}

// Register 装配导入端点：POST /api/v1/metadata/import。
func Register(mux *http.ServeMux, db *sql.DB, gate *store.WriteGate) {
	mux.HandleFunc("POST /api/v1/metadata/import", func(w http.ResponseWriter, r *http.Request) {
		source, sourceDir, verrs := decodeImportRequest(w, r)
		if verrs != nil {
			writeJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": verrs})
			return
		}
		switch source {
		case "all":
			results := []map[string]any{}
			for _, importer := range []func(context.Context, *sql.DB, *store.WriteGate, string) map[string]any{
				ImportEpic, ImportXGP, ImportHB,
			} {
				results = append(results, importer(r.Context(), db, gate, sourceDir))
			}
			writeJSON(w, http.StatusOK, map[string]any{"results": results})
			return
		case "epic":
			writeJSON(w, http.StatusOK, ImportEpic(r.Context(), db, gate, sourceDir))
		case "xgp":
			writeJSON(w, http.StatusOK, ImportXGP(r.Context(), db, gate, sourceDir))
		case "hb":
			writeJSON(w, http.StatusOK, ImportHB(r.Context(), db, gate, sourceDir))
		default:
			writeJSON(w, http.StatusOK, map[string]any{
				"error": fmt.Sprintf("未知来源: %s（可选 epic/xgp/hb/all）", source)})
		}
	})
}

// valErr 校验错误项（形状对齐 FastAPI/Pydantic venv 实测锚点）。
type valErr struct {
	Type  string `json:"type"`
	Loc   []any  `json:"loc"`
	Msg   string `json:"msg"`
	Input any    `json:"input"`
}

// decodeImportRequest 请求体校验：missing source / 非串 source / 非对象体
// 三类 422；通过校验后返回 strip+lower 的 source 与原样 source_dir。
func decodeImportRequest(w http.ResponseWriter, r *http.Request) (string, string, []valErr) {
	fail := func(errs []valErr) (string, string, []valErr) { return "", "", errs }
	raw, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
	if err != nil {
		return fail([]valErr{{Type: "json_invalid", Loc: []any{"body"}, Msg: "JSON decode error", Input: map[string]any{}}})
	}
	trimmed := strings.TrimSpace(string(raw))
	if trimmed == "" {
		return fail([]valErr{{Type: "missing", Loc: []any{"body"}, Msg: "Field required", Input: nil}})
	}
	var top any
	if err := json.Unmarshal([]byte(trimmed), &top); err != nil {
		return fail([]valErr{{Type: "json_invalid", Loc: []any{"body"}, Msg: "JSON decode error", Input: map[string]any{}}})
	}
	fields, ok := top.(map[string]any)
	if !ok {
		return fail([]valErr{{Type: "model_attributes_type", Loc: []any{"body"},
			Msg: "Input should be a valid dictionary or object to extract fields from", Input: top}})
	}
	rawSource, present := fields["source"]
	if !present || rawSource == nil {
		return fail([]valErr{{Type: "missing", Loc: []any{"body", "source"}, Msg: "Field required", Input: fields}})
	}
	source, ok := rawSource.(string)
	if !ok {
		return fail([]valErr{{Type: "string_type", Loc: []any{"body", "source"},
			Msg: "Input should be a valid string", Input: rawSource}})
	}
	sourceDir := ""
	if rawDir, present := fields["source_dir"]; present && rawDir != nil {
		dir, ok := rawDir.(string)
		if !ok {
			return fail([]valErr{{Type: "string_type", Loc: []any{"body", "source_dir"},
				Msg: "Input should be a valid string", Input: rawDir}})
		}
		sourceDir = dir
	}
	return strings.ToLower(strings.TrimSpace(source)), sourceDir, nil
}

// missingFile 找不到数据文件的标准错误形状。
func missingFile(source, path string) map[string]any {
	return map[string]any{"source": source, "ok": false,
		"error": fmt.Sprintf("找不到数据文件: %s", path)}
}

// ImportEpic applist → (appid, free_period) 映射，仅打标库内已有行：
// is_epic=1 + epic_date（无赠送期的 appid 落 NULL）。
func ImportEpic(ctx context.Context, db *sql.DB, gate *store.WriteGate, sourceDir string) map[string]any {
	path := resolveSourcePath(sourceDir, epicFile)
	raw, err := os.ReadFile(path)
	if err != nil {
		return missingFile("epic", path)
	}
	var data struct {
		Applist struct {
			Apps []struct {
				AppID      any     `json:"appid"`
				FreePeriod *string `json:"free_period"`
			} `json:"apps"`
		} `json:"applist"`
	}
	if err := json.Unmarshal(raw, &data); err != nil {
		return map[string]any{"source": "epic", "ok": false,
			"error": fmt.Sprintf("数据文件解析失败: %s", err)}
	}
	periods := map[int64]*string{}
	for _, app := range data.Applist.Apps {
		id, ok := asInt64(app.AppID)
		if !ok {
			continue
		}
		periods[id] = app.FreePeriod
	}
	updated, err := markExisting(ctx, db, gate, periods, func(pairs string) string {
		return `UPDATE games SET is_epic=1, epic_date=j.d
			FROM (SELECT CAST(json_extract(value,'$.a') AS INTEGER) a, json_extract(value,'$.d') d FROM json_each(?)) AS j
			WHERE games.appid=j.a`
	}, pairsJSON(periods))
	if err != nil {
		return map[string]any{"source": "epic", "ok": false, "error": err.Error()}
	}
	return map[string]any{"source": "epic", "ok": true, "file": path,
		"inSource": len(periods), "updated": updated}
}

// ImportXGP tiered 文件（+ 可选 links 文件名称匹配）→ xgp_tier 批量打标。
func ImportXGP(ctx context.Context, db *sql.DB, gate *store.WriteGate, sourceDir string) map[string]any {
	tieredPath := resolveSourcePath(sourceDir, xgpTiered)
	raw, err := os.ReadFile(tieredPath)
	if err != nil {
		return missingFile("xgp", tieredPath)
	}
	var data map[string]any
	if err := json.Unmarshal(raw, &data); err != nil {
		return map[string]any{"source": "xgp", "ok": false,
			"error": fmt.Sprintf("数据文件解析失败: %s", err)}
	}
	var tiers map[int64]string
	nameMatched := 0
	linksPath := resolveSourcePath(sourceDir, xgpLinks)
	if linksRaw, err := os.ReadFile(linksPath); err == nil {
		var links []struct {
			Name  any `json:"game_name"`
			AppID any `json:"steam_appid"`
		}
		if json.Unmarshal(linksRaw, &links) == nil {
			tiers, nameMatched = collectXGPWithLinks(data, buildLinksMap(links))
		} else {
			tiers = collectXGPTiers(data)
		}
	} else {
		tiers = collectXGPTiers(data)
	}
	pointer := map[int64]*string{}
	for id, tier := range tiers {
		value := tier
		pointer[id] = &value
	}
	updated, err := markExisting(ctx, db, gate, pointer, func(pairs string) string {
		return `UPDATE games SET xgp_tier=j.d
			FROM (SELECT CAST(json_extract(value,'$.a') AS INTEGER) a, json_extract(value,'$.d') d FROM json_each(?)) AS j
			WHERE games.appid=j.a`
	}, pairsJSON(pointer))
	if err != nil {
		return map[string]any{"source": "xgp", "ok": false, "error": err.Error()}
	}
	return map[string]any{"source": "xgp", "ok": true, "file": tieredPath,
		"inSource": len(tiers), "updated": updated, "nameMatched": nameMatched}
}

// ImportHB 历史低价 SQLite 的 game_lows.humble_choice 列 → is_hb + hb_data 批量打标。
func ImportHB(ctx context.Context, db *sql.DB, gate *store.WriteGate, sourceDir string) map[string]any {
	path := resolveSourcePath(sourceDir, historicalDB)
	if _, err := os.Stat(path); err != nil {
		return missingFile("hb", path)
	}
	values, err := readHBLows(path)
	if err != nil {
		return map[string]any{"source": "hb", "ok": false, "error": err.Error()}
	}
	pointer := map[int64]*string{}
	for id, label := range values {
		value := label
		pointer[id] = &value
	}
	updated, err := markExisting(ctx, db, gate, pointer, func(pairs string) string {
		return `UPDATE games SET is_hb=1, hb_data=j.d
			FROM (SELECT CAST(json_extract(value,'$.a') AS INTEGER) a, json_extract(value,'$.d') d FROM json_each(?)) AS j
			WHERE games.appid=j.a`
	}, pairsJSON(pointer))
	if err != nil {
		return map[string]any{"source": "hb", "ok": false, "error": err.Error()}
	}
	return map[string]any{"source": "hb", "ok": true, "file": path,
		"inSource": len(values), "updated": updated}
}

// markExisting 写闸单事务批量打标：先数库内命中行（updated 口径），再经
// UPDATE..FROM 全量覆盖写（FROM 联接天然只触库内已有行）。
func markExisting(ctx context.Context, db *sql.DB, gate *store.WriteGate,
	values map[int64]*string, buildSQL func(string) string, pairs string) (int, error) {
	if len(values) == 0 {
		return 0, nil
	}
	updated := 0
	err := gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if err := conn.QueryRowContext(ctx,
			`SELECT COUNT(*) FROM games WHERE appid IN (SELECT CAST(json_extract(value,'$.a') AS INTEGER) FROM json_each(?))`,
			pairs).Scan(&updated); err != nil {
			return err
		}
		_, err := conn.ExecContext(ctx, buildSQL(pairs), pairs)
		return err
	})
	if err != nil {
		return 0, err
	}
	return updated, nil
}

// pairsJSON (appid, value) 对序列：null 值也照写（Python case() 同形）。
func pairsJSON(values map[int64]*string) string {
	pairs := make([]string, 0, len(values))
	for id, value := range values {
		encoded := "null"
		if value != nil {
			if raw, err := json.Marshal(*value); err == nil {
				encoded = string(raw)
			}
		}
		pairs = append(pairs, fmt.Sprintf(`{"a":%d,"d":%s}`, id, encoded))
	}
	return "[" + strings.Join(pairs, ",") + "]"
}

// asInt64 Python int() 语义的宽松收数（数字串/整值浮点收下）。
func asInt64(v any) (int64, bool) {
	switch x := v.(type) {
	case int64:
		return x, true
	case int:
		return int64(x), true
	case float64:
		return int64(x), true
	case string:
		n, err := strconv.ParseInt(strings.TrimSpace(x), 10, 64)
		return n, err == nil
	case json.Number:
		n, err := x.Int64()
		return n, err == nil
	case bool:
		if x {
			return 1, true
		}
		return 0, true
	}
	return 0, false
}

// readHBLows 只读打开外部低价库，抽 Humble Choice 行 → {appid: 标记文案}。
func readHBLows(path string) (map[int64]string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return nil, err
	}
	p := filepath.ToSlash(abs)
	if !strings.HasPrefix(p, "/") {
		p = "/" + p
	}
	uri := url.URL{Scheme: "file", Path: p, RawQuery: "mode=ro"}
	db, err := sql.Open("sqlite", uri.String())
	if err != nil {
		return nil, err
	}
	defer db.Close()
	rows, err := db.Query(`SELECT name FROM pragma_table_info('game_lows')`)
	if err != nil {
		return nil, err
	}
	columns := map[string]bool{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			rows.Close()
			return nil, err
		}
		columns[name] = true
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	rows.Close()
	if !columns["humble_choice"] {
		return nil, fmt.Errorf("game_lows 无 humble_choice 列")
	}
	lows, err := db.Query(`SELECT appid, humble_choice FROM game_lows WHERE humble_choice LIKE '%Humble Choice%'`)
	if err != nil {
		return nil, err
	}
	defer lows.Close()
	values := map[int64]string{}
	for lows.Next() {
		var rawAppID any
		var label sql.NullString
		if err := lows.Scan(&rawAppID, &label); err != nil {
			return nil, err
		}
		id, ok := asInt64(rawAppID)
		if !ok {
			continue
		}
		values[id] = fmtHBData(label.String)
	}
	return values, lows.Err()
}

var hbRe = regexp.MustCompile(`(?i)Humble Choice\s*\(([A-Za-z]{3})\s*(\d{4})\)`)

var hbMonthMap = map[string]string{
	"Jan": "1", "Feb": "2", "Mar": "3", "Apr": "4",
	"May": "5", "Jun": "6", "Jul": "7", "Aug": "8",
	"Sep": "9", "Oct": "10", "Nov": "11", "Dec": "12",
}

// fmtHBData 月份标注格式化（Python _fmt_hb_data 同构）。
func fmtHBData(raw string) string {
	if m := hbRe.FindStringSubmatch(raw); m != nil {
		name := strings.ToLower(m[1])
		if len(name) > 0 {
			name = strings.ToUpper(name[:1]) + name[1:]
		}
		month := hbMonthMap[name[:3]]
		if month == "" {
			month = "1"
		}
		yearShort := m[2]
		if len(yearShort) >= 2 {
			yearShort = yearShort[len(yearShort)-2:]
		}
		return "HB慈善包" + yearShort + "年" + month + "月包"
	}
	return "HB慈善包"
}

// ─── XGP 名称匹配（Python _clean_name / _build_links_map 同构）──────────────

var (
	xgpTrademarkRe = regexp.MustCompile(`\s*\-\s*pc edition|\s*\(pc\)|\s*\(windows\)|\s*windows edition|\s*xbox one|\s*xbox series x\|s`)
	xgpEditionRe   = regexp.MustCompile(`(?i)\s*standard edition|\s*game of the year edition|\s*anniversary edition|\s*complete edition` +
		`|\s*deluxe edition|\s*definitive edition|\s*remastered|\s*director's cut|\s*ultimate edition`)
	xgpNonWordRe = regexp.MustCompile(`[^\w\s]`)
	xgpSpacesRe  = regexp.MustCompile(`\s+`)
)

// cleanNameXGP XGP 条目名清洗（Python _clean_name 同构）。
func cleanNameXGP(n string) string {
	if n == "" {
		return ""
	}
	n = strings.ToLower(n)
	n = strings.NewReplacer("™", "", "®", "", "©", "", "’", "'", "–", "-", "—", "-").Replace(n)
	n = xgpTrademarkRe.ReplaceAllString(n, "")
	n = xgpEditionRe.ReplaceAllString(n, "")
	n = strings.ReplaceAll(n, "ea sports fc", "fc")
	n = strings.ReplaceAll(n, "ea sports ", "")
	n = xgpNonWordRe.ReplaceAllString(n, "")
	return strings.TrimSpace(xgpSpacesRe.ReplaceAllString(n, " "))
}

type xgpLinksEntry struct {
	name  string
	appID string
}

// buildLinksMap name→appid 三级查找表（exact / clean / 罗马数字互换）。
func buildLinksMap(entries []struct {
	Name  any `json:"game_name"`
	AppID any `json:"steam_appid"`
}) map[string]map[string]string {
	m := map[string]map[string]string{"exact": {}, "clean": {}, "alt": {}}
	for _, entry := range entries {
		name := pyStr(entry.Name)
		appID := pyStr(entry.AppID)
		if name == "" || appID == "" {
			continue
		}
		cleaned := cleanNameXGP(name)
		alt1 := strings.NewReplacer(" 2", " ii", " 3", " iii", " 4", " iv").Replace(cleaned)
		alt2 := strings.NewReplacer(" ii", " 2", " iii", " 3", " iv", " 4").Replace(cleaned)
		m["exact"][strings.ToLower(strings.TrimSpace(name))] = appID
		m["clean"][cleaned] = appID
		m["alt"][alt1] = appID
		m["alt"][alt2] = appID
	}
	return m
}

// matchAppIDByName 三级查找（exact / clean / alt）。
func matchAppIDByName(name string, links map[string]map[string]string) string {
	exact := strings.ToLower(strings.TrimSpace(name))
	cleaned := cleanNameXGP(name)
	if id, ok := links["exact"][exact]; ok {
		return id
	}
	if id, ok := links["clean"][cleaned]; ok {
		return id
	}
	if id, ok := links["alt"][cleaned]; ok {
		return id
	}
	return ""
}

// collectXGPTiers 扁平化 tiered JSON；PC Collection 优先。
func collectXGPTiers(data map[string]any) map[int64]string {
	return collectXGPEntries(data, nil, nil)
}

// collectXGPWithLinks 缺 appid 条目走名称匹配，返回匹配计数。
func collectXGPWithLinks(data map[string]any, links map[string]map[string]string) (map[int64]string, int) {
	matched := 0
	return collectXGPEntries(data, links, &matched), matched
}

// collectXGPEntries 共同扁平化逻辑：tier 名取内层键名，同 appid 冲突时
// PC Collection 后到者胜（Python _collect_xgp_* 同构）。
func collectXGPEntries(data map[string]any, links map[string]map[string]string, matched *int) map[int64]string {
	tiers := map[int64]string{}
	for collectionType, tierGroup := range data {
		group, ok := tierGroup.(map[string]any)
		if !ok {
			continue
		}
		for tierName, gamesRaw := range group {
			games, ok := gamesRaw.([]any)
			if !ok {
				continue
			}
			for _, gameRaw := range games {
				game, ok := gameRaw.(map[string]any)
				if !ok {
					continue
				}
				id, ok := asInt64(game["appid"])
				if !ok {
					if links == nil {
						continue
					}
					strID := matchAppIDByName(pyStr(game["name"]), links)
					parsed, parsedOK := asInt64(strID)
					if !parsedOK || strID == "" {
						continue
					}
					id = parsed
					if matched != nil {
						*matched++
					}
				}
				if _, seen := tiers[id]; !seen || collectionType == "PC Collection" {
					tiers[id] = tierName
				}
			}
		}
	}
	return tiers
}

func pyStr(v any) string {
	switch x := v.(type) {
	case string:
		return x
	case json.Number:
		return x.String()
	case float64:
		return strconv.FormatFloat(x, 'f', -1, 64)
	}
	return ""
}

func writeJSON(w http.ResponseWriter, status int, payload any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(payload)
}
