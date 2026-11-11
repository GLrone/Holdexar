package monitoring

import (
	"context"
	"database/sql"
)

func (s *Service) SyncGameSourcesTx(ctx context.Context, conn *sql.Conn, ids []int64) (map[int64]string, error) {
	ids = dedup(ids)
	desired := map[int64][]string{}
	rows, e := conn.QueryContext(ctx, `SELECT appid,MAX(wishlisted),MAX(owned) FROM wishlist_items WHERE active=1 AND appid IN(SELECT value FROM json_each(?)) GROUP BY appid`, encode(ids))
	if e != nil {
		return nil, e
	}
	for rows.Next() {
		var id int64
		var wishlisted, owned bool
		if e = rows.Scan(&id, &wishlisted, &owned); e != nil {
			rows.Close()
			return nil, e
		}
		if wishlisted {
			desired[id] = append(desired[id], "family_wishlist")
		}
		if owned {
			desired[id] = append(desired[id], "owned")
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	return s.ApplyTx(ctx, conn, "game", ids, Change{Sources: desired, Managed: []string{"family_wishlist", "owned"}})
}
