package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"slices"
	"strings"
)

// trackedRegionCodes 返回启用区大写码集；空或查询异常返回 nil 表示不过滤。
func trackedRegionCodes(ctx context.Context, db *sql.DB) map[string]bool {
	rows, err := db.QueryContext(ctx, "SELECT code FROM crawl_regions WHERE enabled = 1")
	if err != nil {
		return nil
	}
	defer rows.Close()
	out := map[string]bool{}
	for rows.Next() {
		var code string
		if err := rows.Scan(&code); err != nil {
			return nil
		}
		out[strings.ToUpper(code)] = true
	}
	if err := rows.Err(); err != nil {
		return nil
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func trackedJSONParam(tracked map[string]bool) any {
	if len(tracked) == 0 {
		return nil
	}
	codes := make([]string, 0, len(tracked))
	for code := range tracked {
		codes = append(codes, code)
	}
	slices.Sort(codes)
	raw, err := json.Marshal(codes)
	if err != nil {
		return nil
	}
	return string(raw)
}
