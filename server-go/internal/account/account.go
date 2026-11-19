package account

import (
	"context"
	"database/sql"
	"fmt"
	"math/big"
	"slices"
	"strings"
)

const steamBase = 76561197960265728

// FriendCode 用 big.Int 将 SteamID64 转为好友码，防大数精度溢出；非数字返回空串。
func FriendCode(steamID string) string {
	n, ok := new(big.Int).SetString(strings.TrimSpace(steamID), 10)
	if !ok {
		return ""
	}
	return new(big.Int).Sub(n, big.NewInt(steamBase)).String()
}

type AccountBrief struct {
	SteamID       string
	PersonaName   string
	IsActive      bool
	IsPrimary     bool
	FriendCode    string
	WishlistCount int64
	GameCount     int64
}

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

func wishlistCounts(ctx context.Context, db *sql.DB, sids []string) (map[string]sidCounts, error) {
	out := make(map[string]sidCounts, len(sids))
	for part := range slices.Chunk(sids, 500) {
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

func placeholders(n int) string {
	return strings.TrimSuffix(strings.Repeat("?, ", n), ", ")
}
