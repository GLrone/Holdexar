package achievements

import (
	"cmp"
	"context"
	"log/slog"
	"slices"
	"strings"
)

type readDisplayAccount struct {
	SteamID     string `json:"steamid"`
	PersonaName string `json:"personaName"`
	AvatarURL   string `json:"avatarUrl"`
	IsPrimary   bool   `json:"isPrimary"`
	IsActive    bool   `json:"isActive"`
	Relation    string `json:"relation"`
}

func (r Reader) accounts(ctx context.Context) ([]readDisplayAccount, error) {
	items := []readDisplayAccount{}
	index := map[string]int{}
	add := func(a readDisplayAccount) {
		if _, seen := index[a.SteamID]; a.SteamID != "" && !seen {
			index[a.SteamID] = len(items)
			items = append(items, a)
		}
	}
	primary, err := r.readBoundDisplayAccounts(ctx, add)
	if err != nil {
		return nil, err
	}
	if primary == "" {
		primary, err = r.legacyReadPrimary(ctx)
		if err != nil {
			return nil, err
		}
	}
	if err := r.readFamilyDisplayAccounts(ctx, primary, add); err != nil {
		slog.Warn("读取成就家庭账号失败", "error", err)
	}
	if len(items) > 0 {
		if err := r.fillTrackedProfiles(ctx, items, index); err != nil {
			slog.Warn("读取成就账号档案失败", "error", err)
		}
	}
	slices.SortStableFunc(items, func(a, b readDisplayAccount) int {
		return cmp.Or(
			cmp.Compare(readAccountRank(a), readAccountRank(b)),
			cmp.Compare(strings.ToLower(cmp.Or(a.PersonaName, a.SteamID)), strings.ToLower(cmp.Or(b.PersonaName, b.SteamID))),
		)
	})
	return items, ctx.Err()
}

func readAccountRank(a readDisplayAccount) int {
	if a.IsPrimary {
		return 0
	}
	if a.Relation == "bound" {
		return 1
	}
	return 2
}

func (r Reader) readBoundDisplayAccounts(ctx context.Context, add func(readDisplayAccount)) (string, error) {
	rows, err := r.DB.QueryContext(ctx, `SELECT steam_id,COALESCE(persona_name,''),COALESCE(avatar_url,''),COALESCE(is_active,0)
		FROM steam_accounts ORDER BY bound_at,steam_id`)
	if err != nil {
		return "", err
	}
	defer rows.Close()
	var primary string
	first := true
	for rows.Next() {
		a := readDisplayAccount{Relation: "bound", IsPrimary: first}
		if err := rows.Scan(&a.SteamID, &a.PersonaName, &a.AvatarURL, &a.IsActive); err != nil {
			return "", err
		}
		if first {
			primary = a.SteamID
			first = false
		}
		add(a)
	}
	return primary, rows.Err()
}

func (r Reader) readFamilyDisplayAccounts(ctx context.Context, primary string, add func(readDisplayAccount)) error {
	query := `SELECT COALESCE(CAST(NULLIF(json_extract(m.value,'$.steamid'),0) AS TEXT),''),
		COALESCE(NULLIF(json_extract(m.value,'$.personaName'),''),json_extract(m.value,'$.persona_name'),''),
		COALESCE(NULLIF(json_extract(m.value,'$.avatarUrl'),''),json_extract(m.value,'$.avatar_url'),'')
		FROM family_groups g, json_each(CASE WHEN json_valid(g.members_json) THEN
			CASE WHEN json_type(g.members_json)='array' THEN g.members_json END END) m
		WHERE m.type='object'`
	var args []any
	if primary != "" {
		query += ` AND g.steamid=?`
		args = append(args, primary)
	}
	rows, err := r.DB.QueryContext(ctx, query+` ORDER BY g.rowid,m.key`, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		a := readDisplayAccount{Relation: "family"}
		if err := rows.Scan(&a.SteamID, &a.PersonaName, &a.AvatarURL); err != nil {
			return err
		}
		add(a)
	}
	return rows.Err()
}

func (r Reader) fillTrackedProfiles(ctx context.Context, items []readDisplayAccount, index map[string]int) error {
	rows, err := r.DB.QueryContext(ctx, `SELECT steamid,COALESCE(persona_name,''),COALESCE(avatar_url,'') FROM tracked_accounts`)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var sid, name, avatar string
		if err := rows.Scan(&sid, &name, &avatar); err != nil {
			return err
		}
		if i, ok := index[sid]; ok {
			items[i].PersonaName = cmp.Or(items[i].PersonaName, name)
			items[i].AvatarURL = cmp.Or(items[i].AvatarURL, avatar)
		}
	}
	return rows.Err()
}
