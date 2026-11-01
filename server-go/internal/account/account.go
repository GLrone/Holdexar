// Package account：账户面只读域（MCP account 组读路径的数据面）。
// 与 Python app.domains.account / wishlist / games 的工具消费链逐字段对齐：
// 无凭据解密、无出网、无写库；同步/写路径与会话续期不在本包。
package account

import (
	"context"
	"database/sql"
	"fmt"
	"math/big"
	"strings"
)

// steamBase 好友码换算基准：SteamID64 − 76561197960265728 = 好友码。
const steamBase = 76561197960265728

// FriendCode SteamID64 → 好友码文本；非数字输入返回空串（Python 同语义）。
// big.Int 承接超位数字串，保持 Python 任意精度减法的输出形态。
func FriendCode(steamID string) string {
	n, ok := new(big.Int).SetString(strings.TrimSpace(steamID), 10)
	if !ok {
		return ""
	}
	return new(big.Int).Sub(n, big.NewInt(steamBase)).String()
}

// AccountBrief 账号行的工具消费面投影：list_accounts 工具只取这些字段；
// Python 全载荷里的会话新鲜度（需解密）、账单同步时刻、激活计数不进读取面。
type AccountBrief struct {
	SteamID       string
	PersonaName   string
	IsActive      bool
	IsPrimary     bool
	FriendCode    string
	WishlistCount int64
	GameCount     int64
}

// ListAccounts 绑定账号清单（绑定顺序，bound_at 空值排最前；第一个即主账号），
// 附愿望单/拥有计数（wishlist_items 活跃行按 owned 分组；未注册账号计 0）。
func ListAccounts(ctx context.Context, db *sql.DB) ([]AccountBrief, error) {
	rows, err := db.QueryContext(ctx,
		"SELECT steam_id, persona_name, is_active FROM steam_accounts ORDER BY bound_at, steam_id")
	if err != nil {
		return nil, fmt.Errorf("账号清单查询失败: %w", err)
	}
	defer rows.Close()
	out := []AccountBrief{}
	sids := []string{}
	for rows.Next() {
		var sid, persona string
		var active bool
		if err := rows.Scan(&sid, &persona, &active); err != nil {
			return nil, fmt.Errorf("账号行读取失败: %w", err)
		}
		out = append(out, AccountBrief{SteamID: sid, PersonaName: persona, IsActive: active})
		sids = append(sids, sid)
	}
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("账号清单遍历失败: %w", err)
	}
	if len(out) == 0 {
		return out, nil
	}
	out[0].IsPrimary = true
	counts, err := wishlistCounts(ctx, db, sids)
	if err != nil {
		return nil, err
	}
	for i := range out {
		c := counts[out[i].SteamID]
		out[i].WishlistCount = c.wishlist
		out[i].GameCount = c.owned
		out[i].FriendCode = FriendCode(out[i].SteamID)
	}
	return out, nil
}

type sidCounts struct{ wishlist, owned int64 }

// wishlistCounts 按账号统计活跃条目：owned=False 计愿望单数，owned=True 计游戏数。
func wishlistCounts(ctx context.Context, db *sql.DB, sids []string) (map[string]sidCounts, error) {
	out := make(map[string]sidCounts, len(sids))
	for _, part := range chunkStrings(sids, 500) {
		ph := make([]string, len(part))
		args := make([]any, 0, len(part))
		for i, sid := range part {
			ph[i] = "?"
			args = append(args, sid)
		}
		rows, err := db.QueryContext(ctx,
			"SELECT steamid, owned, COUNT(appid) FROM wishlist_items"+
				" WHERE steamid IN ("+strings.Join(ph, ", ")+") AND active = 1"+
				" GROUP BY steamid, owned", args...)
		if err != nil {
			return nil, fmt.Errorf("账号计数查询失败: %w", err)
		}
		err = collectCounts(rows, out)
		rows.Close()
		if err != nil {
			return nil, err
		}
	}
	return out, nil
}

func collectCounts(rows *sql.Rows, out map[string]sidCounts) error {
	for rows.Next() {
		var sid string
		var owned bool
		var cnt int64
		if err := rows.Scan(&sid, &owned, &cnt); err != nil {
			return fmt.Errorf("账号计数读取失败: %w", err)
		}
		c := out[sid]
		if owned {
			c.owned = cnt
		} else {
			c.wishlist = cnt
		}
		out[sid] = c
	}
	if err := rows.Err(); err != nil {
		return fmt.Errorf("账号计数遍历失败: %w", err)
	}
	return nil
}

// chunkStrings IN 列表分块（SQLite 变量上限内；并集语义不受分块影响）。
func chunkStrings(s []string, size int) [][]string {
	var out [][]string
	for start := 0; start < len(s); start += size {
		end := start + size
		if end > len(s) {
			end = len(s)
		}
		out = append(out, s[start:end])
	}
	return out
}

// chunkInt64s 同 chunkStrings，整数版。
func chunkInt64s(s []int64, size int) [][]int64 {
	var out [][]int64
	for start := 0; start < len(s); start += size {
		end := start + size
		if end > len(s) {
			end = len(s)
		}
		out = append(out, s[start:end])
	}
	return out
}

// placeholders 生成 n 个 "?" 占位符。
func placeholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("?, ", n), ", ")
}
