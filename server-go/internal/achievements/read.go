package achievements

import (
	"cmp"
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

type Reader struct {
	DB       *sql.DB
	Account  func(context.Context, string) (sid string, hasCredential bool, err error)
	Snapshot func(context.Context) (owner, syncedAt string, running bool, err error)
	Warm     func(string)
}

func (r Reader) Register(mux *http.ServeMux) {
	const base = "GET /api/v1/achievements"
	mux.HandleFunc(base+"/summary", func(w http.ResponseWriter, req *http.Request) {
		body, err := r.summary(req.Context(), readParam(req, "steamid"))
		readReply(w, body, err)
	})
	mux.HandleFunc(base+"/accounts", func(w http.ResponseWriter, req *http.Request) {
		body, err := r.accounts(req.Context())
		readReply(w, map[string]any{"accounts": body}, err)
	})
	mux.HandleFunc(base+"/games", func(w http.ResponseWriter, req *http.Request) {
		body, err := r.listGames(req.Context(), readParam(req, "steamid"), readParam(req, "filter"), readParam(req, "sort"), readParam(req, "q"))
		readReply(w, map[string]any{"games": body, "count": len(body)}, err)
	})
	mux.HandleFunc(base+"/games/{appid}", func(w http.ResponseWriter, req *http.Request) {
		raw := req.PathValue("appid")
		appid, err := parseReadAppID(raw)
		if err != nil {
			readJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []map[string]any{{
				"type": "int_parsing", "loc": []string{"path", "appid"},
				"msg": "Input should be a valid integer, unable to parse string as an integer", "input": raw,
			}}})
			return
		}
		body, err := r.gameDetail(req.Context(), readParam(req, "steamid"), appid)
		if errors.Is(err, sql.ErrNoRows) {
			readJSON(w, http.StatusNotFound, map[string]string{"detail": "该游戏不在成就快照中，请先同步"})
			return
		}
		readReply(w, body, err)
	})
}

var readAppIDPattern = regexp.MustCompile(`^[+-]?[0-9]+(?:_[0-9]+)*(?:\.0+)?$`)

func parseReadAppID(raw string) (int64, error) {
	value := strings.TrimSpace(raw)
	if strings.ContainsAny(value, "_.") && readAppIDPattern.MatchString(value) {
		integer, _, _ := strings.Cut(value, ".")
		value = strings.ReplaceAll(integer, "_", "")
	}
	return strconv.ParseInt(value, 10, 64)
}

func readParam(req *http.Request, key string) string {
	values := req.URL.Query()[key]
	if len(values) == 0 {
		return ""
	}
	return values[len(values)-1]
}

func readReply(w http.ResponseWriter, body any, err error) {
	if err != nil {
		readJSON(w, http.StatusInternalServerError, map[string]string{"detail": "读取成就数据失败"})
		return
	}
	readJSON(w, http.StatusOK, body)
}

func readJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func (r Reader) readAccount(ctx context.Context, target string) (string, bool, error) {
	target = strings.TrimSpace(target)
	if r.Account != nil {
		return r.Account(ctx, target)
	}
	if target != "" {
		return target, false, nil
	}
	err := r.DB.QueryRowContext(ctx, `SELECT steam_id FROM steam_accounts ORDER BY bound_at, steam_id LIMIT 1`).Scan(&target)
	if errors.Is(err, sql.ErrNoRows) {
		target, err = r.legacyReadPrimary(ctx)
	}
	return target, false, err
}

func (r Reader) legacyReadPrimary(ctx context.Context) (string, error) {
	var primary string
	err := r.DB.QueryRowContext(ctx, `SELECT COALESCE(json_extract(value_json,'$'),'') FROM app_settings WHERE key='account.steam_id'`).Scan(&primary)
	if errors.Is(err, sql.ErrNoRows) {
		err = nil
	}
	return primary, err
}

type readGameName struct {
	AppID int64  `json:"appid"`
	Name  string `json:"name"`
}

type readGameArt struct {
	readGameName
	HeaderImage string `json:"headerImage"`
	PlaytimeMin int64  `json:"playtimeMin"`
	Total       int64  `json:"total"`
	Source      string `json:"source"`
}

type readGameProgress struct {
	Unlocked int64   `json:"unlocked"`
	Percent  float64 `json:"percent"`
}

type readGame struct {
	readGameArt
	readGameProgress
	LastPlayed int64 `json:"lastPlayed"`
	Platinum   bool  `json:"platinum"`
	Owned      bool  `json:"owned"`
}

type readGameItem struct {
	readGame
	Remaining int64 `json:"remaining"`
}

type readAchievementDetail struct {
	ImageName     string   `json:"imageName"`
	Name          string   `json:"name"`
	Description   string   `json:"description"`
	Icon          *string  `json:"icon"`
	IconGray      *string  `json:"iconGray"`
	GlobalPercent *float64 `json:"globalPercent"`
	Rarity        string   `json:"rarity"`
	Achieved      bool     `json:"achieved"`
	UnlockTime    int64    `json:"unlockTime"`
}

type readGameDetail struct {
	readGame
	PerfectDate  int64                   `json:"perfectDate"`
	Achievements []readAchievementDetail `json:"achievements"`
}

const readGameQuery = `SELECT a.appid, COALESCE(a.name,''), COALESCE(g.header_image,''),
	a.playtime_min, a.last_played, a.total_achievements, a.unlocked, COALESCE(NULLIF(a.source,''),'owned')
	FROM achievement_games a LEFT JOIN games g ON g.appid=a.appid WHERE a.steamid=?`

func scanReadGame(row interface{ Scan(...any) error }) (readGame, error) {
	var g readGame
	err := row.Scan(&g.AppID, &g.Name, &g.HeaderImage, &g.PlaytimeMin, &g.LastPlayed, &g.Total, &g.Unlocked, &g.Source)
	if err != nil {
		return g, err
	}
	if g.HeaderImage == "" {
		g.HeaderImage = fmt.Sprintf("https://cdn.cloudflare.steamstatic.com/steam/apps/%d/header.jpg", g.AppID)
	}
	g.Owned = g.Source == "owned"
	g.Platinum = g.Total > 0 && g.Unlocked >= g.Total
	return g, nil
}

func readGames(ctx context.Context, q interface {
	QueryContext(context.Context, string, ...any) (*sql.Rows, error)
}, sid string) ([]readGame, error) {
	rows, err := q.QueryContext(ctx, readGameQuery+` ORDER BY a.appid`, sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var games []readGame
	for rows.Next() {
		game, err := scanReadGame(rows)
		if err != nil {
			return nil, err
		}
		games = append(games, game)
	}
	return games, rows.Err()
}

func readPercent(unlocked, total int64) float64 {
	if total == 0 {
		return 0
	}
	value, _ := strconv.ParseFloat(strconv.FormatFloat(float64(unlocked)*100/float64(total), 'f', 1, 64), 64)
	return value
}

func (r Reader) listGames(ctx context.Context, target, filter, order, search string) ([]readGameItem, error) {
	items := []readGameItem{}
	sid, _, err := r.readAccount(ctx, target)
	if err != nil || sid == "" {
		return items, err
	}
	games, err := readGames(ctx, r.DB, sid)
	if err != nil {
		return nil, err
	}
	needle := strings.ToLower(strings.TrimSpace(search))
	for _, g := range games {
		var include bool
		switch filter {
		case "all":
			include = true
		case "platinum":
			include = g.Platinum
		case "progress":
			include = g.Unlocked > 0 && g.Unlocked < g.Total
		case "external":
			include = !g.Owned
		default:
			include = g.Total > 0
		}
		if include && strings.Contains(strings.ToLower(g.Name), needle) {
			g.Percent = readPercent(g.Unlocked, g.Total)
			items = append(items, readGameItem{readGame: g, Remaining: max(0, g.Total-g.Unlocked)})
		}
	}
	slices.SortStableFunc(items, func(a, b readGameItem) int {
		byName := cmp.Compare(strings.ToLower(a.Name), strings.ToLower(b.Name))
		switch order {
		case "progress":
			return cmp.Or(cmp.Compare(b.Percent, a.Percent), cmp.Compare(b.Total, a.Total), byName)
		case "recent":
			return cmp.Or(cmp.Compare(b.LastPlayed, a.LastPlayed), byName)
		case "name":
			return byName
		default:
			return cmp.Or(cmp.Compare(b.PlaytimeMin, a.PlaytimeMin), byName)
		}
	})
	return items, nil
}

func (r Reader) gameDetail(ctx context.Context, target string, appid int64) (readGameDetail, error) {
	var detail readGameDetail
	sid, _, err := r.readAccount(ctx, target)
	if err != nil {
		return detail, err
	}
	if sid == "" {
		return detail, sql.ErrNoRows
	}
	tx, err := r.DB.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return detail, err
	}
	defer tx.Rollback()
	detail.readGame, err = scanReadGame(tx.QueryRowContext(ctx, readGameQuery+` AND a.appid=?`, sid, appid))
	if err != nil {
		return detail, err
	}
	rows, err := tx.QueryContext(ctx, `SELECT d.image_name, COALESCE(NULLIF(d.name,''),d.image_name), COALESCE(d.description,''),
		d.icon_url, d.icon_gray_url, d.global_percent, COALESCE(s.achieved,0) != 0, COALESCE(s.unlock_time,0)
		FROM achievement_defs d LEFT JOIN achievement_states s ON s.steamid=? AND s.appid=d.appid AND s.image_name=d.image_name
		WHERE d.appid=? ORDER BY d.display_order,d.image_name`, sid, appid)
	if err != nil {
		return detail, err
	}
	defer rows.Close()
	detail.Achievements = []readAchievementDetail{}
	for rows.Next() {
		var a readAchievementDetail
		if err := rows.Scan(&a.ImageName, &a.Name, &a.Description, &a.Icon, &a.IconGray, &a.GlobalPercent, &a.Achieved, &a.UnlockTime); err != nil {
			return detail, err
		}
		a.Rarity = rarityTier(a.GlobalPercent)
		detail.Achievements = append(detail.Achievements, a)
	}
	if err := rows.Err(); err != nil {
		return detail, err
	}
	slices.SortStableFunc(detail.Achievements, func(a, b readAchievementDetail) int {
		if a.Achieved != b.Achieved {
			if a.Achieved {
				return -1
			}
			return 1
		}
		if a.Achieved && a.UnlockTime != b.UnlockTime {
			return cmp.Compare(b.UnlockTime, a.UnlockTime)
		}
		return cmp.Compare(readDetailRarity(a.GlobalPercent), readDetailRarity(b.GlobalPercent))
	})
	if detail.Platinum && len(detail.Achievements) > 0 && detail.Achievements[0].Achieved {
		detail.PerfectDate = detail.Achievements[0].UnlockTime
	}
	if detail.Total == 0 {
		detail.Total = int64(len(detail.Achievements))
	}
	detail.Percent = readPercent(detail.Unlocked, detail.Total)
	return detail, nil
}

func readDetailRarity(percent *float64) float64 {
	if percent == nil {
		return 999
	}
	return *percent
}
