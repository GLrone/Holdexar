package games

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// ImportAppids 仅分类不落库，也不建监控来源：games.updated_at 非空或已有价格行判 own，余判 ok/fail。
func ImportAppids(ctx context.Context, db *sql.DB, appids []any) (map[string]any, error) {
	results := []map[string]any{}
	ok, own, fail := 0, 0, 0
	clean := []int64{}
	for _, raw := range appids {
		appid, err := importInt(raw)
		if err != nil || appid <= 0 {
			results = append(results, map[string]any{"appid": raw, "status": "fail", "detail": "AppID 无效"})
			fail++
			continue
		}
		clean = append(clean, appid)
	}
	if len(clean) > 0 {
		known := map[int64][2]bool{}
		rows, err := db.QueryContext(ctx,
			`SELECT g.appid, g.updated_at, EXISTS(SELECT 1 FROM game_current_prices p WHERE p.appid=g.appid)
			 FROM games g WHERE g.appid IN (SELECT CAST(value AS INTEGER) FROM json_each(?))`,
			catalogJSON(clean))
		if err != nil {
			return nil, err
		}
		for rows.Next() {
			var appid int64
			var updated sql.NullString
			var priced bool
			if err := rows.Scan(&appid, &updated, &priced); err != nil {
				rows.Close()
				return nil, err
			}
			known[appid] = [2]bool{updated.Valid, priced}
		}
		if err := rows.Err(); err != nil {
			rows.Close()
			return nil, err
		}
		rows.Close()
		for _, appid := range clean {
			state := known[appid]
			if state[0] || state[1] {
				results = append(results, map[string]any{"appid": appid, "status": "own", "detail": "已在库"})
				own++
			} else {
				results = append(results, map[string]any{"appid": appid, "status": "ok", "detail": "待首爬入库"})
				ok++
			}
		}
	}
	return map[string]any{"results": results, "ok": ok, "own": own, "fail": fail}, nil
}

// importInt 接受整数、bool 与浮点截断，拒绝带小数/指数的非整数字符串。
func importInt(v any) (int64, error) {
	switch x := v.(type) {
	case nil:
		return 0, fmt.Errorf("int() argument is None")
	case bool:
		if x {
			return 1, nil
		}
		return 0, nil
	case json.Number:
		if strings.ContainsAny(x.String(), ".eE") {
			return 0, fmt.Errorf("invalid literal for int(): %s", x.String())
		}
		return strconv.ParseInt(strings.TrimSpace(x.String()), 10, 64)
	case string:
		if strings.ContainsAny(x, ".eE") {
			return 0, fmt.Errorf("invalid literal for int(): %s", x)
		}
		return strconv.ParseInt(strings.TrimSpace(x), 10, 64)
	case float64:
		return int64(x), nil
	case int:
		return int64(x), nil
	case int64:
		return x, nil
	}
	return 0, fmt.Errorf("int() argument must be a number or string, not %T", v)
}
