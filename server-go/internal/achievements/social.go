package achievements

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"net/http"
	"strings"
	"time"
	"unicode"
	"unicode/utf8"

	"holdexar/server-go/internal/store"
)

type SocialService struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	Account func(context.Context, string) (string, bool, error)
	Now     func() time.Time
}

func (s SocialService) Register(mux *http.ServeMux) {
	const prefix = " /api/v1/achievements"
	mux.HandleFunc("GET"+prefix+"/notes", func(w http.ResponseWriter, req *http.Request) {
		notes, err := s.notes(req.Context(), readParam(req, "steamid"))
		readReply(w, map[string]any{"total": len(notes), "notes": notes}, err)
	})
	mux.HandleFunc("POST"+prefix+"/notes", func(w http.ResponseWriter, req *http.Request) {
		input, ok := readSocialNoteInput(w, req)
		if !ok {
			return
		}
		result, err := s.saveNote(req.Context(), input)
		var validation socialNoteValidation
		if errors.As(err, &validation) {
			readJSON(w, http.StatusBadRequest, map[string]string{"detail": validation.Error()})
			return
		}
		readReply(w, result, err)
	})
	mux.HandleFunc("DELETE"+prefix+"/notes/{appid}/{image_name}", func(w http.ResponseWriter, req *http.Request) {
		raw := req.PathValue("appid")
		appid, err := parseReadAppID(raw)
		if err != nil {
			socialInvalid(w, "path", "appid", "int_parsing", "Input should be a valid integer, unable to parse string as an integer", raw)
			return
		}
		removed, err := s.removeNote(req.Context(), readParam(req, "steamid"), appid, req.PathValue("image_name"))
		readReply(w, map[string]bool{"removed": removed}, err)
	})
	mux.HandleFunc("GET"+prefix+"/compare", func(w http.ResponseWriter, req *http.Request) {
		accounts, err := s.compare(req.Context(), readParam(req, "steamids"))
		readReply(w, map[string]any{"accounts": accounts}, err)
	})
	mux.HandleFunc("GET"+prefix+"/feed", func(w http.ResponseWriter, req *http.Request) {
		var limit int64 = 30
		if req.URL.Query().Has("limit") {
			raw := readParam(req, "limit")
			var err error
			if limit, err = parseReadAppID(raw); err != nil {
				socialInvalid(w, "query", "limit", "int_parsing", "Input should be a valid integer, unable to parse string as an integer", raw)
				return
			}
		}
		items, err := s.feed(req.Context(), readParam(req, "steamid"), limit)
		readReply(w, map[string]any{"items": items}, err)
	})
}

func (s SocialService) socialSteamID(ctx context.Context, target string) (string, error) {
	if sid := strings.TrimSpace(target); sid != "" {
		return sid, nil
	}
	sid, _, err := (Reader{DB: s.DB, Account: s.Account}).readAccount(ctx, "")
	return sid, err
}

type socialNoteInput struct {
	SteamID, ImageName, Content string
	AppID                       int64
	Mood                        *string
}

func readSocialNoteInput(w http.ResponseWriter, req *http.Request) (socialNoteInput, bool) {
	var input socialNoteInput
	var fields map[string]json.RawMessage
	decoder := json.NewDecoder(http.MaxBytesReader(w, req.Body, 128<<10))
	if err := decoder.Decode(&fields); err != nil || fields == nil || decoder.Decode(new(any)) != io.EOF {
		socialInvalid(w, "body", "", "value_error", "Input should be a valid dictionary", nil)
		return input, false
	}
	for _, field := range []struct {
		name   string
		target *string
	}{{"steamid", &input.SteamID}, {"imageName", &input.ImageName}, {"content", &input.Content}} {
		raw, exists := fields[field.name]
		if !exists {
			if field.name == "steamid" {
				continue
			}
			socialInvalid(w, "body", field.name, "missing", "Field required", fields)
			return input, false
		}
		if err := json.Unmarshal(raw, field.target); err != nil || string(raw) == "null" {
			socialInvalid(w, "body", field.name, "string_type", "Input should be a valid string", raw)
			return input, false
		}
	}
	if raw, exists := fields["mood"]; exists {
		if err := json.Unmarshal(raw, &input.Mood); err != nil {
			socialInvalid(w, "body", "mood", "string_type", "Input should be a valid string", raw)
			return input, false
		}
	}
	raw, exists := fields["appid"]
	if !exists {
		socialInvalid(w, "body", "appid", "missing", "Field required", fields)
		return input, false
	}
	value := string(raw)
	switch value {
	case "true":
		value = "1"
	case "false":
		value = "0"
	default:
		if len(raw) > 0 && raw[0] == '"' {
			_ = json.Unmarshal(raw, &value)
		}
	}
	appid, err := parseReadAppID(value)
	if err != nil {
		socialInvalid(w, "body", "appid", "int_parsing", "Input should be a valid integer, unable to parse string as an integer", raw)
		return input, false
	}
	input.AppID = appid
	return input, true
}

func socialInvalid(w http.ResponseWriter, section, field, code, message string, input any) {
	loc := []string{section}
	if field != "" {
		loc = append(loc, field)
	}
	readJSON(w, http.StatusUnprocessableEntity, map[string]any{"detail": []map[string]any{{
		"type": code, "loc": loc, "msg": message, "input": input,
	}}})
}

type socialNote struct {
	AppID               int64   `json:"appid"`
	ImageName           string  `json:"imageName"`
	Content             string  `json:"content"`
	Mood                *string `json:"mood"`
	AchievementName     string  `json:"achievementName"`
	AchievementIcon     *string `json:"achievementIcon"`
	AchievementGrayIcon *string `json:"achievementGrayIcon"`
	GameName            string  `json:"gameName"`
	CreatedAt           *string `json:"createdAt"`
	UpdatedAt           *string `json:"updatedAt"`
}

type socialNoteResult struct {
	Note                socialNote `json:"note"`
	XPGranted           int        `json:"xpGranted"`
	FirstForAchievement bool       `json:"firstForAchievement"`
}

type socialNoteValidation string

func (e socialNoteValidation) Error() string { return string(e) }

const socialNoteQuery = `SELECT n.appid,n.image_name,n.content,n.mood,COALESCE(NULLIF(d.name,''),n.image_name),
	d.icon_url,d.icon_gray_url,COALESCE(g.name,''),n.created_at,n.updated_at FROM achievement_notes n
	LEFT JOIN achievement_defs d ON d.appid=n.appid AND d.image_name=n.image_name
	LEFT JOIN achievement_games g ON g.steamid=n.steamid AND g.appid=n.appid WHERE n.steamid=?`

func scanSocialNote(row interface{ Scan(...any) error }) (socialNote, error) {
	var note socialNote
	var created, updated sql.NullTime
	err := row.Scan(&note.AppID, &note.ImageName, &note.Content, &note.Mood, &note.AchievementName, &note.AchievementIcon,
		&note.AchievementGrayIcon, &note.GameName, &created, &updated)
	note.CreatedAt, note.UpdatedAt = socialNoteTime(created), socialNoteTime(updated)
	return note, err
}

func socialNoteTime(value sql.NullTime) *string {
	if !value.Valid {
		return nil
	}
	format := "2006-01-02T15:04:05"
	if value.Time.Nanosecond() >= 1000 {
		format += ".000000"
	}
	stamp := value.Time.Format(format)
	return &stamp
}

func (s SocialService) notes(ctx context.Context, target string) ([]socialNote, error) {
	notes := []socialNote{}
	sid, err := s.socialSteamID(ctx, target)
	if err != nil || sid == "" {
		return notes, err
	}
	rows, err := s.DB.QueryContext(ctx, socialNoteQuery+` ORDER BY n.updated_at DESC,n.created_at DESC`, sid)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		note, err := scanSocialNote(rows)
		if err != nil {
			return nil, err
		}
		notes = append(notes, note)
	}
	return notes, rows.Err()
}

func (s SocialService) saveNote(ctx context.Context, input socialNoteInput) (socialNoteResult, error) {
	var result socialNoteResult
	sid, err := s.socialSteamID(ctx, input.SteamID)
	if err != nil {
		return result, err
	}
	if sid == "" {
		return result, socialNoteValidation("未绑定 Steam 账号，无法保存评语")
	}
	content := strings.Join(strings.FieldsFunc(input.Content, func(r rune) bool {
		return unicode.IsSpace(r) || (r >= '\x1c' && r <= '\x1f')
	}), " ")
	if content == "" {
		return result, socialNoteValidation("评语不能为空")
	}
	if utf8.RuneCountInString(content) > 140 {
		return result, socialNoteValidation("评语最长 140 字")
	}
	if input.Mood != nil {
		switch *input.Mood {
		case "love", "wow", "hard", "fun":
		default:
			return result, socialNoteValidation("评语心情只能是 love/wow/hard/fun")
		}
	}
	image := strings.TrimSpace(input.ImageName)
	if image == "" {
		return result, socialNoteValidation("成就标识缺失")
	}
	now := (LevelService{Now: s.Now}).now()
	stamp := now.UTC().Format(levelSQLTime)
	var facts []levelFact
	err = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		if _, err := conn.ExecContext(ctx, `INSERT INTO achievement_notes(steamid,appid,image_name,content,mood,created_at,updated_at)
			VALUES (?,?,?,?,?,?,?) ON CONFLICT(steamid,appid,image_name)
			DO UPDATE SET content=excluded.content,mood=excluded.mood,updated_at=excluded.updated_at`,
			sid, input.AppID, image, content, input.Mood, stamp, stamp); err != nil {
			return err
		}
		ref := fmt.Sprintf("%d:%s", input.AppID, image)
		var total int
		var recorded bool
		if err := conn.QueryRowContext(ctx, `SELECT COALESCE(SUM(xp),0),COALESCE(MAX(source='comment' AND source_ref=?),0)
			FROM xp_events WHERE steamid=?`, ref, sid).Scan(&total, &recorded); err != nil {
			return err
		}
		if !recorded {
			streak, err := profileStreakDays(ctx, conn, sid)
			if err != nil {
				return err
			}
			mult := levelStreakMultiplier(streak)
			xp := int(math.Floor(10*mult + .5))
			events := []levelXPEvent{{levelXPKey: levelXPKey{source: "comment", ref: ref}, xp: xp, multiplier: levelDecimalRound(mult, 4)}}
			if err := recordLevelXP(ctx, conn, sid, events, now); err != nil {
				return err
			}
			result.XPGranted, result.FirstForAchievement = xp, true
			facts = levelSyncFacts(sid, total, events, nil)
		}
		result.Note, err = scanSocialNote(conn.QueryRowContext(ctx, socialNoteQuery+` AND n.appid=? AND n.image_name=?`, sid, input.AppID, image))
		return err
	})
	if err != nil || len(facts) == 0 {
		return result, err
	}
	err = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		return recordLevelFacts(ctx, conn, facts, now)
	})
	return result, err
}

func (s SocialService) removeNote(ctx context.Context, target string, appid int64, image string) (bool, error) {
	sid, err := s.socialSteamID(ctx, target)
	if err != nil || sid == "" {
		return false, err
	}
	var count int64
	err = s.Gate.WithWrite(ctx, store.PriorityBackground, func(conn *sql.Conn) error {
		result, err := conn.ExecContext(ctx, `DELETE FROM achievement_notes WHERE steamid=? AND appid=? AND image_name=?`, sid, appid, image)
		if err == nil {
			count, err = result.RowsAffected()
		}
		return err
	})
	return count != 0, err
}
