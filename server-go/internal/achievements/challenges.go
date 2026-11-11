package achievements

import (
	"context"
	"database/sql"
	"errors"
	"net/http"
	"strings"
	"time"

	"holdexar/server-go/internal/store"
)

const (
	challengeActive    = "active"
	challengeCompleted = "completed"
	challengeClaimed   = "claimed"
	xpChallenge        = "challenge_claim"
)

type challengeMeasure int

const (
	measureStates challengeMeasure = iota
	measureStreak
	measurePlatinum
	measureGamesWithUnlocks
	measurePlaytime
	measureBuckets
	measureNotes
	measureSocial
	measureSocialTen
	measureCount
)

type challengeSpec struct {
	id, tier, category string
	target             int
	measure            challengeMeasure
}

var challengeCatalog = [...]challengeSpec{
	{"first_trophy", "bronze", "milestone", 1, measureStates},
	{"hunter_twelve", "bronze", "milestone", 12, measureStates},
	{"hunter_fifty", "silver", "milestone", 50, measureStates},
	{"hunter_hundred", "gold", "milestone", 100, measureStates},
	{"streak_three", "bronze", "streak", 3, measureStreak},
	{"streak_seven", "silver", "streak", 7, measureStreak},
	{"streak_thirty", "gold", "streak", 30, measureStreak},
	{"platinum_first", "bronze", "platinum", 1, measurePlatinum},
	{"platinum_five", "silver", "platinum", 5, measurePlatinum},
	{"platinum_twelve", "gold", "platinum", 12, measurePlatinum},
	{"breadth_twenty", "bronze", "collection", 20, measureGamesWithUnlocks},
	{"full_clear_first", "silver", "collection", 1, measurePlatinum},
	{"full_clear_triple", "gold", "collection", 3, measurePlatinum},
	{"hours_hundred", "silver", "cumulative", 6000, measurePlaytime},
	{"hours_thousand", "gold", "cumulative", 60000, measurePlaytime},
	{"buckets_ten", "silver", "cumulative", 10, measureBuckets},
	{"note_first", "bronze", "comment", 1, measureNotes},
	{"note_gallery", "silver", "comment", 25, measureNotes},
	{"social_duo", "bronze", "social", 2, measureSocial},
	{"social_trio", "gold", "social", 3, measureSocialTen},
}

var challengeReward = map[string]int{"bronze": 80, "silver": 200, "gold": 500}

type challengeItem struct {
	ID          string  `json:"challengeId"`
	Tier        string  `json:"tier"`
	Category    string  `json:"category"`
	Target      int     `json:"target"`
	Progress    int     `json:"progress"`
	RewardXP    int     `json:"rewardXp"`
	Status      string  `json:"status"`
	Claimable   bool    `json:"claimable"`
	CompletedAt *string `json:"completedAt"`
	ClaimedAt   *string `json:"claimedAt"`
	UpdatedAt   *string `json:"updatedAt"`
}

type challengeSummary struct {
	Total     int `json:"total"`
	Completed int `json:"completed"`
	Claimed   int `json:"claimed"`
	Claimable int `json:"claimable"`
}

type challengeOverview struct {
	SteamID    string           `json:"steamid"`
	Challenges []challengeItem  `json:"challenges"`
	Summary    challengeSummary `json:"summary"`
}

type challengeClaimLevel struct {
	Previous  int  `json:"previous"`
	Current   int  `json:"current"`
	LeveledUp bool `json:"leveledUp"`
}

type challengeClaim struct {
	ID        string              `json:"challengeId"`
	Status    string              `json:"status"`
	XPGranted int                 `json:"xpGranted"`
	TotalXP   int                 `json:"totalXp"`
	Level     challengeClaimLevel `json:"level"`
}

type ChallengeService struct {
	DB      *sql.DB
	Gate    *store.WriteGate
	Account func(context.Context, string) (string, bool, error)
	Now     func() time.Time
}

var (
	errChallengeUnknown    = errors.New("未知挑战")
	errChallengeNoAccount  = errors.New("暂无可领取的挑战")
	errChallengeIncomplete = errors.New("挑战尚未完成，完成后才能领取")
	errChallengeClaimed    = errors.New("该挑战奖励已领取，不能重复领取")
)

func (s ChallengeService) Register(mux *http.ServeMux) {
	const prefix = "/api/v1/achievements/challenges"
	mux.HandleFunc("GET "+prefix, func(w http.ResponseWriter, r *http.Request) {
		overview, err := s.overview(r.Context(), readParam(r, "steamid"))
		readReply(w, overview, err)
	})
	mux.HandleFunc("POST "+prefix+"/{challengeID}/claim", func(w http.ResponseWriter, r *http.Request) {
		result, err := s.claim(r.Context(), readParam(r, "steamid"), r.PathValue("challengeID"))
		switch err {
		case nil:
			readJSON(w, http.StatusOK, result)
		case errChallengeUnknown:
			readJSON(w, http.StatusNotFound, map[string]string{"detail": err.Error()})
		case errChallengeNoAccount, errChallengeIncomplete, errChallengeClaimed:
			readJSON(w, http.StatusConflict, map[string]string{"detail": err.Error()})
		default:
			readReply(w, result, err)
		}
	})
}

func (s ChallengeService) account(ctx context.Context, sid string) (string, error) {
	if sid = strings.TrimSpace(sid); sid != "" {
		return sid, nil
	}
	sid, _, err := (Reader{DB: s.DB, Account: s.Account}).readAccount(ctx, "")
	return sid, err
}

func (s ChallengeService) now() time.Time {
	if s.Now != nil {
		return s.Now()
	}
	return time.Now()
}

func (s ChallengeService) Recompute(ctx context.Context, sid string) error {
	_, err := s.overview(ctx, sid)
	return err
}

func (s ChallengeService) overview(ctx context.Context, sid string) (challengeOverview, error) {
	sid, err := s.account(ctx, sid)
	if err != nil {
		return challengeOverview{}, err
	}
	if sid == "" {
		return assembleChallenges("", [measureCount]int{}, nil, sql.NullTime{}), nil
	}
	return s.recompute(ctx, sid)
}
