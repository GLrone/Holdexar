package epicfree

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"time"

	"holdexar/server-go/internal/derived"
	"holdexar/server-go/internal/store"
)

var epicDate = regexp.MustCompile(`(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?`)

func normalizeEpicDate(raw string) *string {
	match := epicDate.FindStringSubmatch(raw)
	if match == nil {
		return nil
	}
	year, _ := strconv.Atoi(match[1])
	month, _ := strconv.Atoi(match[2])
	day, _ := strconv.Atoi(match[3])
	normalized := time.Date(year, time.Month(month), day, 0, 0, 0, 0, time.UTC)
	if year == 0 || normalized.Year() != year || int(normalized.Month()) != month || normalized.Day() != day {
		return nil
	}
	stamp := fmt.Sprintf("%d-%d-%d", year, month, day)
	return &stamp
}

type gameTag struct {
	AppID int64   `json:"appid"`
	Name  string  `json:"name"`
	Title string  `json:"-"`
	Start *string `json:"start"`
}

func (s *Service) mark(ctx context.Context, tags []gameTag, now time.Time) ([]gameTag, error) {
	input := make([]gameTag, 0, len(tags))
	index := make(map[int64]int, len(tags))
	for _, tag := range tags {
		if tag.AppID <= 0 {
			continue
		}
		if i, ok := index[tag.AppID]; ok {
			if input[i].Start == nil {
				input[i].Start = tag.Start
			}
			continue
		}
		tag.Name = strings.TrimSpace(tag.Name)
		if tag.Name == "" {
			tag.Name = strings.TrimSpace(tag.Title)
		}
		if tag.Name == "" {
			tag.Name = fmt.Sprintf("appid %d", tag.AppID)
		}
		index[tag.AppID] = len(input)
		input = append(input, tag)
	}
	marked := make([]gameTag, 0, len(input))
	if len(input) == 0 {
		return marked, nil
	}
	stamp := now.Format("2006-01-02 15:04:05.000000")
	err := s.gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		for batch := range slices.Chunk(input, 400) {
			ids := make([]int64, 0, len(batch))
			for _, tag := range batch {
				ids = append(ids, tag.AppID)
			}
			before, err := derived.CatalogInputs(ctx, conn, ids)
			if err != nil {
				return err
			}
			raw, err := json.Marshal(batch)
			if err != nil {
				return err
			}
			rows, err := conn.QueryContext(ctx, `INSERT INTO games(appid,name,is_epic,epic_date,family_sharing,trading_cards,is_hb,
				hl_flag,pp_flag,diff_fen,is_adult,is_visual_novel,removed_strikes,positive_reviews,review_count,view_count,created_at)
				SELECT json_extract(value,'$.appid'),json_extract(value,'$.name'),1,json_extract(value,'$.start'),
				0,0,0,0,0,0,0,0,0,0,0,0,?
				FROM json_each(?) WHERE true ON CONFLICT(appid) DO UPDATE SET is_epic=1,
				epic_date=COALESCE(NULLIF(games.epic_date,''),excluded.epic_date)
				WHERE games.is_epic<>1 OR (COALESCE(games.epic_date,'')='' AND excluded.epic_date IS NOT NULL)
				RETURNING appid`, stamp, string(raw))
			if err != nil {
				return err
			}
			changed := make(map[int64]bool, len(batch))
			for rows.Next() {
				var id int64
				if err := rows.Scan(&id); err != nil {
					rows.Close()
					return err
				}
				changed[id] = true
			}
			err = rows.Err()
			rows.Close()
			if err != nil {
				return err
			}
			ids = ids[:0]
			for _, tag := range batch {
				if changed[tag.AppID] {
					ids = append(ids, tag.AppID)
					marked = append(marked, tag)
				} else {
					delete(before, tag.AppID)
				}
			}
			if _, err := derived.UpdatePriceDerived(ctx, conn, ids, derived.Options{Now: now, History: true, CatalogBefore: before}); err != nil {
				return err
			}
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return marked, nil
}
