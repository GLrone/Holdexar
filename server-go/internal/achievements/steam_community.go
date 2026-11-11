package achievements

import (
	"encoding/json"
	"math"
	"strconv"

	"golang.org/x/net/html"
)

type communityAchievement struct {
	Key         string
	APIName     string `json:"api_name"`
	Name        string `json:"name"`
	Description string `json:"description"`
	Icon        string `json:"icon_achieved"`
	Gray        string `json:"icon_unachieved"`
	Archived    bool   `json:"archived"`
}

type communityData struct {
	achievements []communityAchievement
	percent      map[string]float64
	player       map[string]State
}

func communityPayload(root *html.Node, appID int64, sid string) (communityData, bool) {
	for node := range root.Descendants() {
		if node.Data == "script" && steamAttr(node, "id") == "valve-ssr-data" {
			return decodeCommunityPayload(steamNodeText(node), appID, sid), true
		}
	}
	return communityData{}, false
}

func decodeCommunityPayload(text string, appID int64, sid string) communityData {
	var document struct {
		Render struct {
			Queries string `json:"queryData"`
		} `json:"renderContext"`
	}
	var cache struct {
		Queries []struct {
			Key   []json.RawMessage `json:"queryKey"`
			State struct {
				Status string          `json:"status"`
				Data   json.RawMessage `json:"data"`
			} `json:"state"`
		} `json:"queries"`
	}
	if json.Unmarshal([]byte(text), &document) != nil || json.Unmarshal([]byte(document.Render.Queries), &cache) != nil {
		return communityData{}
	}
	var out communityData
	var byID map[string]State
	for _, query := range cache.Queries {
		if query.State.Status != "success" || len(query.Key) < 3 || string(query.Key[0]) != `"achievements"` || string(query.Key[1]) != strconv.FormatInt(appID, 10) {
			continue
		}
		switch string(query.Key[2]) {
		case `"schema_v2"`:
			out.achievements = communitySchema(query.State.Data, appID)
		case `"globalpercentages"`:
			var global struct {
				Percent map[string]float64 `json:"percentages"`
			}
			if json.Unmarshal(query.State.Data, &global) == nil {
				out.percent = global.Percent
			}
		case `"user_achievements"`:
			if sid != "" && len(query.Key) == 4 && string(query.Key[3]) == strconv.Quote(sid) {
				byID = communityPlayer(query.State.Data, appID, sid)
			}
		}
	}
	if byID != nil {
		out.player = make(map[string]State, len(byID))
		for _, achievement := range out.achievements {
			if state, ok := byID[achievement.Key]; ok {
				out.player[achievement.APIName] = state
			}
		}
	}
	return out
}

func communitySchema(data []byte, appID int64) []communityAchievement {
	var schema struct {
		AppID  int64 `json:"appid"`
		Groups []struct {
			Public       *bool `json:"ispublic"`
			Archived     bool  `json:"archived"`
			Developer    bool  `json:"developeronly"`
			Achievements []struct {
				ID json.Number `json:"internal_key"`
				communityAchievement
			} `json:"achievements"`
		} `json:"groups"`
	}
	if json.Unmarshal(data, &schema) != nil || schema.AppID != appID {
		return nil
	}
	var achievements []communityAchievement
	for _, group := range schema.Groups {
		if group.Archived || group.Developer || group.Public != nil && !*group.Public {
			continue
		}
		for _, row := range group.Achievements {
			if !row.Archived && row.ID != "" && row.APIName != "" && steamImageBase(row.Icon) != "" {
				row.Key = row.ID.String()
				achievements = append(achievements, row.communityAchievement)
			}
		}
	}
	return achievements
}

func communityPlayer(data []byte, appID int64, sid string) map[string]State {
	var body struct {
		AppID        int64  `json:"appid"`
		Owner        string `json:"steamid"`
		Achievements map[string]struct {
			ID       json.Number `json:"internal_key"`
			Unlocked *bool       `json:"unlocked"`
			Time     int64       `json:"unlock_time"`
		} `json:"achievements"`
	}
	if json.Unmarshal(data, &body) != nil || body.AppID != appID || body.Owner != sid || body.Achievements == nil {
		return nil
	}
	states := make(map[string]State, len(body.Achievements))
	for id, row := range body.Achievements {
		if row.ID.String() != id || row.Unlocked == nil || row.Time < 0 {
			return nil
		}
		states[id] = State{Achieved: *row.Unlocked, UnlockTime: row.Time}
	}
	return states
}

func (data communityData) definitions(appID int64) []Definition {
	defs := make([]Definition, 0, len(data.achievements))
	for _, row := range data.achievements {
		base := steamImageBase(row.Icon)
		def := Definition{ImageName: base, APIName: row.APIName, Name: row.Name, Description: row.Description, IconURL: steamIcon(appID, base)}
		if gray := steamImageName(row.Gray); gray != "" {
			def.GrayIconURL = steamIcon(appID, gray)
		}
		if percent, ok := data.percent[row.Key]; ok && percent >= 0 && percent <= 100 {
			rounded := math.Round(percent*10) / 10
			def.Percent = &rounded
		}
		defs = append(defs, def)
	}
	return orderedSteamDefinitions(defs, true)
}
