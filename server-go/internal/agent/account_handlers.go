// account 组只读工具：绑定账号 / 愿望单 / 已拥有（库内快照，无凭据无出网）。
package agent

import (
	"holdexar/server-go/internal/account"
)

// rowsResult 对齐 Python _shared.rows_result：行截 8，total 取截前全量。
func rowsResult(title string, rows []any, total int) *Obj {
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", title)
	if len(rows) > 8 {
		rows = rows[:8]
	}
	out.set("rows", rows)
	out.set("total", total)
	return out
}

// gamesCard 对齐 Python _shared.games_card：富卡清单截 8，total 同上。
func gamesCard(title string, items []any, total int) *Obj {
	out := newObj()
	out.set("kind", "games")
	out.set("titleKey", title)
	if len(items) > 8 {
		items = items[:8]
	}
	out.set("items", items)
	out.set("total", total)
	return out
}

// briefItem 对齐 Python _shared.brief_item：缺简报行降级为纯 appid 不丢对象。
func briefItem(appid int64, brief account.Brief, hasBrief bool) *Obj {
	out := newObj()
	out.set("appid", appid)
	out.set("name", nil)
	out.set("cnyFen", nil)
	out.set("discount", 0)
	out.set("positiveRate", nil)
	out.set("reviewCount", nil)
	out.set("chineseSupport", nil)
	if hasBrief {
		if brief.Name != nil {
			out.set("name", *brief.Name)
		}
		if brief.CnyFen != nil {
			out.set("cnyFen", *brief.CnyFen)
		}
		if brief.Discount != 0 {
			out.set("discount", brief.Discount)
		}
		if brief.PositiveRate != nil {
			out.set("positiveRate", *brief.PositiveRate)
		}
		if brief.ReviewCount != 0 {
			out.set("reviewCount", brief.ReviewCount)
		}
		if brief.ChineseSupport != nil {
			out.set("chineseSupport", *brief.ChineseSupport)
		}
	}
	return out
}

// listAccounts 绑定 Steam 账号清单（名称/好友码/主账号/拥有统计）。
func listAccounts(c *CallContext) (*ToolResult, error) {
	accounts, err := account.ListAccounts(c.Ctx, c.db)
	if err != nil {
		return nil, err
	}
	rows := make([]any, 0, len(accounts))
	for _, a := range accounts {
		k := a.PersonaName
		if k == "" {
			k = a.SteamID
		}
		if k == "" {
			k = "—"
		}
		row := newObj()
		row.set("k", k)
		if a.IsPrimary {
			row.set("vKey", "acctPrimary")
		} else {
			row.set("vKey", "acctActive")
		}
		data := newObj()
		data.set("friend", a.FriendCode)
		data.set("wish", a.WishlistCount)
		data.set("owned", a.GameCount)
		row.set("data", data)
		if a.IsPrimary {
			row.set("tone", "ok")
		} else {
			row.set("tone", nil)
		}
		rows = append(rows, row)
	}
	out := newObj()
	out.set("kind", "rows")
	out.set("titleKey", "accounts")
	if len(rows) > 8 {
		rows = rows[:8]
	}
	out.set("rows", rows)
	out.set("total", len(accounts))
	return &ToolResult{Data: out}, nil
}

// wishlistIDs 愿望单/已拥有的 appid 序列（工具消费面按标记过滤）。
func wishlistIDs(items []account.ItemFlags, owned bool) []int64 {
	ids := make([]int64, 0, len(items))
	for _, it := range items {
		if owned && it.Owned {
			ids = append(ids, it.AppID)
		}
		if !owned && it.Wishlisted {
			ids = append(ids, it.AppID)
		}
	}
	return ids
}

// gamesListCard 愿望单/已拥有共用的清单卡组装。
func gamesListCard(c *CallContext, title string, owned bool) (*ToolResult, error) {
	items, err := account.ListItems(c.Ctx, c.db)
	if err != nil {
		return nil, err
	}
	ids := wishlistIDs(items, owned)
	briefs, err := account.BriefsFor(c.Ctx, c.db, ids)
	if err != nil {
		return nil, err
	}
	cards := make([]any, 0, len(ids))
	for _, id := range ids {
		brief, has := briefs[id]
		cards = append(cards, briefItem(id, brief, has))
	}
	out := newObj()
	out.set("kind", "games")
	out.set("titleKey", title)
	if len(cards) > 8 {
		cards = cards[:8]
	}
	out.set("items", cards)
	out.set("total", len(ids))
	return &ToolResult{Data: out}, nil
}

// listWishlist 账户愿望单游戏（监控池内 wishlisted 来源，库内快照）。
func listWishlist(c *CallContext) (*ToolResult, error) {
	return gamesListCard(c, "wishlist", false)
}

// listOwned 账户已拥有游戏（监控池内 owned 来源，带国区现价）。
func listOwned(c *CallContext) (*ToolResult, error) {
	return gamesListCard(c, "owned", true)
}
