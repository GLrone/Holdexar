package family

import (
	"context"
	"database/sql"
	"errors"
	"slices"
)

func (s *Service) AchievementCandidates(ctx context.Context, sid string) (map[int64]string, bool, error) {
	owner := sid
	if _, err := readIdentity(ctx, s.db, sid); errors.Is(err, errIdentityChanged) {
		err = s.db.QueryRowContext(ctx, `SELECT g.steamid FROM family_groups g JOIN steam_accounts a ON a.steam_id=g.steamid
			WHERE g.family_groupid<>'' AND EXISTS(
				SELECT 1 FROM json_each(CASE WHEN json_valid(g.members_json) THEN g.members_json ELSE '[]' END) m
				WHERE CAST(json_extract(CASE WHEN m.type='object' THEN m.value END,'$.steamid') AS TEXT)=?)
			ORDER BY a.bound_at,a.steam_id LIMIT 1`, sid).Scan(&owner)
		if errors.Is(err, sql.ErrNoRows) {
			return nil, false, nil
		}
		if err != nil {
			return nil, false, err
		}
	} else if err != nil {
		return nil, false, err
	}
	library, err := s.Library(ctx, owner, false)
	if err != nil || library == nil {
		return nil, false, err
	}
	if !slices.ContainsFunc(library.Members, func(member libraryMember) bool { return member.SID == sid }) {
		return nil, false, nil
	}
	out := map[int64]string{}
	for _, game := range library.Games {
		if game.Shared && !game.Excluded && !slices.Contains(game.Owners, sid) {
			name, _ := game.Name.(string)
			out[game.AppID] = name
		}
	}
	return out, true, nil
}
