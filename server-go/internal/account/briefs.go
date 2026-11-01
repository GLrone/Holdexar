package account

import (
	"context"
	"database/sql"
	"fmt"
)

// Brief 游戏简报（Python games.service.briefs_for 行形状：名称/国区价/折扣/
// 好评/评测数/中文支持）。指针 = Python None 语义。
type Brief struct {
	Name           *string
	CnyFen         *int64
	Discount       int64
	PositiveRate   *float64
	ReviewCount    int64
	ChineseSupport *string
}

// BriefsFor appid 批量简报：games 主档 LEFT JOIN 国区 ok 价（region_code='CN'
// 且 price_status='ok'）；缺失行不出键（上层降级为纯 appid）。
func BriefsFor(ctx context.Context, db *sql.DB, appids []int64) (map[int64]Brief, error) {
	out := map[int64]Brief{}
	ids := make([]int64, 0, len(appids))
	for _, a := range appids {
		if a != 0 {
			ids = append(ids, a)
		}
	}
	if len(ids) == 0 {
		return out, nil
	}
	for _, part := range chunkInt64s(ids, 500) {
		rows, err := db.QueryContext(ctx,
			"SELECT g.appid, g.name, g.positive_rate, g.review_count, g.chinese_support,"+
				" cn.cny_fen, cn.discount_percent FROM games g"+
				" LEFT JOIN game_current_prices cn"+
				" ON cn.appid = g.appid AND cn.region_code = 'CN' AND cn.price_status = 'ok'"+
				" WHERE g.appid IN ("+placeholders(len(part))+")",
			int64Args(part)...)
		if err != nil {
			return nil, fmt.Errorf("游戏简报查询失败: %w", err)
		}
		err = func() error {
			defer rows.Close()
			for rows.Next() {
				var appid int64
				var name, chinese sql.NullString
				var positive, cnyFen, discount sql.NullInt64
				var reviewCount int64
				if err := rows.Scan(&appid, &name, &positive, &reviewCount, &chinese, &cnyFen, &discount); err != nil {
					return fmt.Errorf("游戏简报读取失败: %w", err)
				}
				b := Brief{Name: nullStr(name), ChineseSupport: nullStr(chinese)}
				b.ReviewCount = reviewCount
				if positive.Valid {
					// positive_rate 为万分比，/100 即 Python 真除法浮点。
					rate := float64(positive.Int64) / 100.0
					b.PositiveRate = &rate
				}
				if cnyFen.Valid {
					v := cnyFen.Int64
					b.CnyFen = &v
				}
				if discount.Valid && discount.Int64 != 0 {
					b.Discount = discount.Int64
				}
				out[appid] = b
			}
			return rows.Err()
		}()
		if err != nil {
			return nil, fmt.Errorf("游戏简报遍历失败: %w", err)
		}
	}
	return out, nil
}

func nullStr(n sql.NullString) *string {
	if !n.Valid {
		return nil
	}
	v := n.String
	return &v
}
