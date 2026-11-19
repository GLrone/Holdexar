package metadata

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"

	"holdexar/server-go/internal/config"
	"holdexar/server-go/internal/httpx"
	"holdexar/server-go/internal/store"
)

var ErrSourceDirRejected = errors.New("导入目录不存在或不可用")

type RegisterOption func(*registerConfig)

type registerConfig struct {
	dataDir string
}

func WithDataDir(dir string) RegisterOption {
	return func(c *registerConfig) { c.dataDir = dir }
}

func resolvePathBestEffort(p string) (string, error) {
	abs, err := filepath.Abs(p)
	if err != nil {
		return "", err
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		return resolved, nil
	}
	cur := abs
	var tail []string
	for {
		parent := filepath.Dir(cur)
		if parent == cur {
			return abs, nil
		}
		tail = append([]string{filepath.Base(cur)}, tail...)
		if resolved, err := filepath.EvalSymlinks(parent); err == nil {
			parts := append([]string{resolved}, tail...)
			return filepath.Join(parts...), nil
		}
		cur = parent
	}
}

// ConstrainSourceDir constrains a request-supplied source_dir inside dataRoot (defaulting to config.ResolveDataDir).
// Empty sourceDir falls back to METADATA_DATA_DIR without entering the request path check.
func ConstrainSourceDir(dataRoot, sourceDir string) (string, error) {
	if strings.TrimSpace(sourceDir) == "" {
		return os.Getenv("METADATA_DATA_DIR"), nil
	}
	root := strings.TrimSpace(dataRoot)
	if root == "" {
		var err error
		root, err = config.ResolveDataDir()
		if err != nil || strings.TrimSpace(root) == "" {
			return "", ErrSourceDirRejected
		}
	}
	resolvedRoot, err := resolvePathBestEffort(root)
	if err != nil {
		return "", ErrSourceDirRejected
	}
	resolvedTarget, err := resolvePathBestEffort(sourceDir)
	if err != nil {
		return "", ErrSourceDirRejected
	}
	rel, err := filepath.Rel(resolvedRoot, resolvedTarget)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || filepath.IsAbs(rel) {
		return "", ErrSourceDirRejected
	}
	return resolvedTarget, nil
}

const (
	epicFile     = "epic_free_games_clean_final.json"
	xgpTiered    = "xgp_tiered_steam.json"
	xgpLinks     = "xgp_steam_links.json"
	historicalDB = "steam_historical_lows.db"
)

// resolveSourcePath 显式 source_dir 优先，缺省回退 METADATA_DATA_DIR。
func resolveSourcePath(sourceDir, filename string) string {
	base := sourceDir
	if base == "" {
		base = os.Getenv("METADATA_DATA_DIR")
	}
	return filepath.Join(base, filename)
}

func Register(mux *http.ServeMux, db *sql.DB, gate *store.WriteGate, opts ...RegisterOption) {
	var cfg registerConfig
	for _, opt := range opts {
		if opt != nil {
			opt(&cfg)
		}
	}
	mux.HandleFunc("POST /api/v1/metadata/import", func(w http.ResponseWriter, r *http.Request) {
		source, sourceDir, verrs := decodeImportRequest(w, r)
		if verrs != nil {
			httpx.WriteValidationError(w, verrs...)
			return
		}
		constrained, err := ConstrainSourceDir(cfg.dataDir, sourceDir)
		if err != nil {
			httpx.WriteJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": err.Error()})
			return
		}
		switch source {
		case "all":
			results := []map[string]any{}
			for _, importer := range []func(context.Context, *sql.DB, *store.WriteGate, string) map[string]any{
				ImportEpic, ImportXGP, ImportHB,
			} {
				results = append(results, importer(r.Context(), db, gate, constrained))
			}
			httpx.WriteJSON(w, http.StatusOK, map[string]any{"results": results})
			return
		case "epic":
			httpx.WriteJSON(w, http.StatusOK, ImportEpic(r.Context(), db, gate, constrained))
		case "xgp":
			httpx.WriteJSON(w, http.StatusOK, ImportXGP(r.Context(), db, gate, constrained))
		case "hb":
			httpx.WriteJSON(w, http.StatusOK, ImportHB(r.Context(), db, gate, constrained))
		default:
			httpx.WriteJSON(w, http.StatusOK, map[string]any{
				"error": fmt.Sprintf("未知来源: %s（可选 epic/xgp/hb/all）", source)})
		}
	})
}

type valErr = httpx.ValErr

// decodeImportRequest 校验 JSON 对象体、必填 string source 及可选 string source_dir，其余返回 422。
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

func missingFile(source, path string) map[string]any {
	return map[string]any{"source": source, "ok": false,
		"error": fmt.Sprintf("找不到数据文件: %s", path)}
}

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

// markExisting 在写闸单事务内先统计命中行数，再 UPDATE..FROM 覆盖库内已有行。
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

var (
	xgpTrademarkRe = regexp.MustCompile(`\s*\-\s*pc edition|\s*\(pc\)|\s*\(windows\)|\s*windows edition|\s*xbox one|\s*xbox series x\|s`)
	xgpEditionRe   = regexp.MustCompile(`(?i)\s*standard edition|\s*game of the year edition|\s*anniversary edition|\s*complete edition` +
		`|\s*deluxe edition|\s*definitive edition|\s*remastered|\s*director's cut|\s*ultimate edition`)
	xgpNonWordRe = regexp.MustCompile(`[^\w\s]`)
	xgpSpacesRe  = regexp.MustCompile(`\s+`)
)

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

func collectXGPTiers(data map[string]any) map[int64]string {
	return collectXGPEntries(data, nil, nil)
}

func collectXGPWithLinks(data map[string]any, links map[string]map[string]string) (map[int64]string, int) {
	matched := 0
	return collectXGPEntries(data, links, &matched), matched
}

// collectXGPEntries 扁平化分档列表，同 appid 时 PC Collection 覆盖其余分组。
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
