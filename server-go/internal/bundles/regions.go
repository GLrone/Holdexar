// 追踪区集：crawl_regions 启用集读取（列表/详情/排序快照共用口径）。
package bundles

import (
	"context"
	"database/sql"
	"encoding/json"
	"sort"
	"strings"
)

// trackedRegionCodes 追踪区集：crawl_regions enabled=1 → 大写码集。
// 空/异常 → nil = 不过滤（对齐 Python _tracked_region_codes 的「未启用即全部」语义）。
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

// trackedJSONParam 区集 → JSON 数组参数（排序后序列化）；nil = 不过滤。
func trackedJSONParam(tracked map[string]bool) any {
	if len(tracked) == 0 {
		return nil
	}
	codes := make([]string, 0, len(tracked))
	for code := range tracked {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	raw, err := json.Marshal(codes)
	if err != nil {
		return nil
	}
	return string(raw)
}
