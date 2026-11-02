// bills_summary MCP 工具：账单导入记录与消费合计（本地账本只读投影）。
// 对齐 Python _bills_summary → rows_result("bills", rows)。
package agent

import (
	"strconv"

	"holdexar/server-go/internal/bills"
)

// billsSummary 账单汇总行：k=昵称/源文件/#id，data.spendFen，v=订单数，at=导入时刻。
func billsSummary(c *CallContext) (*ToolResult, error) {
	records, err := bills.ListImports(c.Ctx, c.db)
	if err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(records))
	for _, im := range records {
		row := newObj()
		key := ""
		switch {
		case im.Nickname.Valid && im.Nickname.String != "":
			key = im.Nickname.String
		case im.SourceFile.Valid && im.SourceFile.String != "":
			key = im.SourceFile.String
		default:
			key = "#" + strconv.FormatInt(im.ID, 10)
		}
		row.set("k", key)
		row.set("vKey", "billSpend")
		data := newObj()
		data.set("spendFen", im.GameSpendFen)
		row.set("data", data)
		row.set("v", intToOrders(im.Orders))
		if im.ImportedAt.Valid {
			row.set("at", im.ImportedAt.String)
		} else {
			row.set("at", nil)
		}
		row.set("tone", nil)
		rows = append(rows, row)
	}
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", "bills")
	if len(rows) > 8 {
		rows = rows[:8]
	}
	out.set("rows", rows)
	out.set("total", len(records))
	return &ToolResult{Data: out}, nil
}

func intToOrders(v int64) any {
	return strconv.FormatInt(v, 10) + "单"
}
