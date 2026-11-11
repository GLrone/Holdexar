package epicfree

import (
	"context"
	"database/sql"
	"encoding/json"
	"strings"
	"time"

	"holdexar/server-go/internal/notifications"
	"holdexar/server-go/internal/store"
)

func titleIdentity(o Offer) string {
	if title := strings.ToLower(strings.TrimSpace(o.Title)); title != "" {
		return "title:" + title
	}
	return ""
}

func currentDifference(previous, incoming []Offer) []Offer {
	seen := map[string]bool{}
	for _, offer := range previous {
		if !offer.Upcoming {
			seen[titleIdentity(offer)] = true
		}
	}
	var fresh []Offer
	for _, offer := range incoming {
		id := titleIdentity(offer)
		if !offer.Upcoming && id != "" && !seen[id] {
			seen[id] = true
			fresh = append(fresh, offer)
		}
	}
	return fresh
}

func (s *Service) recordNotices(ctx context.Context, offers []Offer, now time.Time) (int, error) {
	offers = currentDifference(nil, offers)
	if len(offers) == 0 {
		return 0, nil
	}
	created := 0
	err := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		rows, err := conn.QueryContext(ctx, `SELECT fact_key FROM fact_notices WHERE source='epic_free' AND kind='free_rotation'`)
		if err != nil {
			return err
		}
		existing := map[string]bool{}
		for rows.Next() {
			var key string
			if err := rows.Scan(&key); err != nil {
				rows.Close()
				return err
			}
			if id, ok := strings.CutPrefix(key, "epic:"); ok {
				existing[id] = true
			} else if ids, ok := strings.CutPrefix(key, "epic_free:"); ok {
				for _, id := range strings.Split(ids, "|") {
					existing[id] = true
				}
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		type notice struct {
			Key  string         `json:"key"`
			Data map[string]any `json:"data"`
		}
		batch := make([]notice, 0, len(offers))
		dataByKey := make(map[string]map[string]any, len(offers))
		for _, offer := range offers {
			identity := titleIdentity(offer)
			if existing[identity] {
				continue
			}
			name := offer.TitleCn
			if name == "" {
				name = offer.Title
			}
			data := map[string]any{
				"appid": 0, "count": 1, "title": name, "titles": []string{name}, "labels": []string{"Epic 免费游戏"},
				"url": offer.URL, "items": []map[string]any{{
					"appid": nil, "name": name, "url": offer.URL, "image": offer.Image, "start": offer.Start, "end": offer.End,
				}},
			}
			key := "epic:" + identity
			batch = append(batch, notice{Key: key, Data: data})
			dataByKey[key] = data
		}
		if len(batch) == 0 {
			return nil
		}
		raw, err := json.Marshal(batch)
		if err != nil {
			return err
		}
		rows, err = conn.QueryContext(ctx, `INSERT INTO fact_notices(source,kind,fact_key,data,occurred_at)
			SELECT 'epic_free','free_rotation',json_extract(value,'$.key'),json_extract(value,'$.data'),?
			FROM json_each(?) WHERE true ON CONFLICT(fact_key) DO NOTHING RETURNING id,fact_key`,
			now.Format("2006-01-02 15:04:05.000000"), string(raw))
		if err != nil {
			return err
		}
		facts := make([]notifications.Fact, 0, len(batch))
		for rows.Next() {
			var id int64
			var key string
			if err := rows.Scan(&id, &key); err != nil {
				rows.Close()
				return err
			}
			payload := dataByKey[key]
			payload["eventType"], payload["source"], payload["kind"] = "EPIC_FREE", "epic_free", "free_rotation"
			payload["observedAt"] = now.Format(time.RFC3339)
			facts = append(facts, notifications.Fact{ID: id, EventType: "EPIC_FREE", Payload: payload, OccurredAt: now})
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if _, err := notifications.EnqueueFactsTx(ctx, conn, "fact_notice", facts, now); err != nil {
			return err
		}
		created = len(facts)
		return nil
	})
	return created, err
}
