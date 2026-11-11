package achievements

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"path"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"golang.org/x/net/html"
)

func (s *Steam) Definitions(ctx context.Context, appID int64, creds Credentials) ([]Definition, error) {
	params := url.Values{"appid": {strconv.FormatInt(appID, 10)}, "language": {steamLanguage}}
	var defs []Definition
	err := s.webAPI(ctx, http.MethodGet, "/IPlayerService/GetGameAchievements/v1/", params, creds, true, func(data []byte) error {
		var body struct {
			Response struct {
				Rows []struct {
					APIName     string          `json:"internal_name"`
					Name        string          `json:"localized_name"`
					Description string          `json:"localized_desc"`
					Icon        string          `json:"icon"`
					Gray        string          `json:"icon_gray"`
					Percent     json.RawMessage `json:"player_percent_unlocked"`
				} `json:"achievements"`
			} `json:"response"`
		}
		if json.Unmarshal(data, &body) != nil {
			return errSteamResponse
		}
		var found []Definition
		for _, row := range body.Response.Rows {
			base, gray := steamImageBase(row.Icon), steamImageBase(row.Gray)
			if base == "" {
				continue
			}
			if gray == "" {
				gray = base
			}
			found = append(found, Definition{
				ImageName: base, APIName: row.APIName, Name: strings.TrimSpace(row.Name), Description: strings.TrimSpace(row.Description),
				IconURL: steamIcon(appID, base), GrayIconURL: steamIcon(appID, gray), Percent: steamPercent(strings.Trim(string(row.Percent), `"`)),
			})
		}
		if len(found) == 0 {
			return errSteamUnavailable
		}
		defs = orderedSteamDefinitions(found, true)
		return nil
	})
	if err == nil || ctx.Err() != nil {
		return defs, err
	}
	page, err := s.communityPage(ctx, fmt.Sprintf("/stats/%d/achievements/", appID))
	if err != nil {
		return nil, err
	}
	if data, ok := communityPayload(page, appID, ""); ok {
		if defs = data.definitions(appID); len(defs) > 0 {
			return defs, nil
		}
		return nil, errSteamUnavailable
	}
	for _, row := range legacyAchievementRows(page, appID) {
		base := steamImageBase(row.icon)
		if base != "" {
			defs = append(defs, Definition{ImageName: base, Name: row.name, Description: row.description, IconURL: steamIcon(appID, base), Percent: steamPercent(row.percent)})
		}
	}
	if len(defs) == 0 {
		return nil, errSteamUnavailable
	}
	return orderedSteamDefinitions(defs, false), nil
}

func (s *Steam) States(ctx context.Context, sid string, appID int64, creds Credentials, defs []Definition) (map[string]State, error) {
	if creds.Key != "" {
		raw, err := s.PlayerStates(ctx, sid, appID, creds)
		if err != nil {
			return nil, err
		}
		if matched := MatchAPIStates(raw, defs); len(matched) > 0 {
			return matched, nil
		}
		return nil, errSteamUnavailable
	}
	if id, err := strconv.ParseUint(sid, 10, 64); err != nil || len(sid) != 17 || id < 76561197960265728 {
		return nil, errSteamResponse
	}
	page, err := s.communityPage(ctx, fmt.Sprintf("/profiles/%s/stats/%d/", sid, appID))
	if err != nil {
		return nil, err
	}
	var rows []communityState
	if data, ok := communityPayload(page, appID, sid); ok {
		if data.player == nil {
			return nil, errSteamUnavailable
		}
		for _, def := range data.definitions(appID) {
			state := data.player[def.APIName]
			if !state.Achieved {
				state.GrayIconURL = def.GrayIconURL
			}
			rows = append(rows, communityState{key: def.ImageName, base: steamImageBase(def.IconURL), api: def.APIName, name: def.Name, state: state})
		}
	} else {
		seen := map[string]int{}
		for _, row := range legacyAchievementRows(page, appID) {
			base := steamImageBase(row.icon)
			if base == "" && row.name == "" {
				continue
			}
			state := State{Achieved: row.unlock != "", UnlockTime: steamUnlockTime(row.unlock)}
			if base != "" && strings.HasSuffix(row.icon, "_BW.jpg") {
				state.GrayIconURL = steamIcon(appID, base)
			}
			rows = append(rows, communityState{key: steamImageKey(base, seen), base: base, name: row.name, state: state})
		}
	}
	if matched := matchCommunityStates(defs, rows); len(matched) > 0 {
		return matched, nil
	}
	return nil, errSteamUnavailable
}

func (s *Steam) communityPage(ctx context.Context, endpoint string) (*html.Node, error) {
	data, err := s.retryRequest(ctx, http.MethodGet, steamCommunity+endpoint, url.Values{"l": {steamLanguage}}, true)
	if err == steamStatus(404) {
		return nil, errSteamUnavailable
	}
	if err != nil {
		return nil, err
	}
	root, err := html.Parse(bytes.NewReader(data))
	if err != nil {
		return nil, errSteamResponse
	}
	return root, nil
}

var steamImagePattern = regexp.MustCompile(`^[a-zA-Z0-9_.-]+$`)

func steamImageName(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return ""
	}
	file := path.Base(u.Path)
	name := strings.TrimSuffix(file, path.Ext(file))
	if !steamImagePattern.MatchString(name) {
		return ""
	}
	return name
}

func steamImageBase(raw string) string { return strings.TrimSuffix(steamImageName(raw), "_BW") }

func steamIcon(appID int64, name string) string {
	return fmt.Sprintf("https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/%d/%s.jpg", appID, name)
}

func steamPercent(raw string) *float64 {
	pct, err := strconv.ParseFloat(strings.TrimSpace(strings.TrimSuffix(strings.TrimSpace(raw), "%")), 64)
	if err != nil || math.IsNaN(pct) || pct < 0 || pct > 100 {
		return nil
	}
	return &pct
}

func orderedSteamDefinitions(defs []Definition, sortByPercent bool) []Definition {
	if sortByPercent {
		sort.SliceStable(defs, func(i, j int) bool {
			left, right := defs[i].Percent, defs[j].Percent
			return left != nil && (right == nil || *left > *right)
		})
	}
	seen := map[string]int{}
	for i := range defs {
		defs[i].ImageName = steamImageKey(defs[i].ImageName, seen)
		defs[i].Order = i
	}
	return defs
}

func steamImageKey(base string, seen map[string]int) string {
	if base == "" {
		return ""
	}
	count := seen[base]
	seen[base]++
	if count == 0 {
		return base
	}
	return base + "#" + strconv.Itoa(count)
}

type communityState struct {
	key, base, api, name string
	state                State
}

func matchCommunityStates(defs []Definition, rows []communityState) map[string]State {
	byAPI, byKey := map[string]string{}, map[string]Definition{}
	byBase, byName := map[string][]string{}, map[string][]string{}
	for _, def := range defs {
		if def.APIName != "" {
			byAPI[def.APIName] = def.ImageName
		}
		byKey[def.ImageName] = def
		base := steamImageBase(def.IconURL)
		if base == "" {
			base, _, _ = strings.Cut(def.ImageName, "#")
		}
		byBase[base] = append(byBase[base], def.ImageName)
		if def.Name != "" {
			byName[def.Name] = append(byName[def.Name], def.ImageName)
		}
	}
	matched := make(map[string]State)
	available := func(key string) bool {
		_, used := matched[key]
		return key != "" && !used
	}
	first := func(keys []string) string {
		for _, key := range keys {
			if available(key) {
				return key
			}
		}
		return ""
	}
	for _, row := range rows {
		if key := byAPI[row.api]; available(key) {
			matched[key] = row.state
		}
	}
	for _, row := range rows {
		if byAPI[row.api] != "" {
			continue
		}
		key := ""
		if exact := byKey[row.key]; available(exact.ImageName) {
			key = exact.ImageName
			if len(byBase[row.base]) > 1 && row.name != "" && exact.Name != row.name {
				if named := first(byName[row.name]); named != "" {
					key = named
				}
			}
		}
		if key == "" && len(byBase[row.base]) == 1 {
			key = first(byBase[row.base])
		}
		if key == "" {
			key = first(byName[row.name])
		}
		if key == "" {
			key = first(byBase[row.base])
		}
		if key != "" {
			matched[key] = row.state
		}
	}
	return matched
}

type legacyAchievement struct{ icon, name, description, percent, unlock string }

func legacyAchievementRows(root *html.Node, appID int64) []legacyAchievement {
	var rows []legacyAchievement
	assetPath := fmt.Sprintf("/community_assets/images/apps/%d/", appID)
	oldAssetPath := fmt.Sprintf("/steamcommunity/public/images/apps/%d/", appID)
	for node := range root.Descendants() {
		if !steamClass(node, "achieveRow") {
			continue
		}
		var row legacyAchievement
		for child := range node.Descendants() {
			switch {
			case child.Data == "img" && row.icon == "":
				src, _ := url.Parse(steamAttr(child, "src"))
				if src != nil && (strings.Contains(src.Path, assetPath) || strings.Contains(src.Path, oldAssetPath)) {
					row.icon = path.Base(src.Path)
				}
			case child.Data == "h3":
				row.name = steamNodeText(child)
			case child.Data == "h5":
				row.description = steamNodeText(child)
			case steamClass(child, "achievePercent"):
				row.percent = steamNodeText(child)
			case steamClass(child, "achieveUnlockTime"):
				row.unlock = steamNodeText(child)
			}
		}
		rows = append(rows, row)
	}
	return rows
}

func steamAttr(node *html.Node, key string) string {
	for _, attr := range node.Attr {
		if attr.Key == key {
			return attr.Val
		}
	}
	return ""
}

func steamClass(node *html.Node, class string) bool {
	for _, name := range strings.Fields(steamAttr(node, "class")) {
		if name == class {
			return true
		}
	}
	return false
}

func steamNodeText(node *html.Node) string {
	var text strings.Builder
	for child := range node.Descendants() {
		if child.Type == html.TextNode {
			text.WriteString(child.Data)
		} else if child.Type == html.ElementNode && child.Data == "br" {
			text.WriteByte(' ')
		}
	}
	return strings.TrimSpace(text.String())
}

var (
	steamChineseTime = regexp.MustCompile(`(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(上午|下午)?\s*(\d{1,2}):(\d{2})`)
	steamEnglishTime = regexp.MustCompile(`(?i)\b(?:\d{1,2} [a-z]{3},? \d{4}|[a-z]{3} \d{1,2},? \d{4})\s*@\s*\d{1,2}:\d{2}\s*(?:am|pm)\b`)
)

func steamUnlockTime(text string) int64 {
	text = strings.Join(strings.Fields(text), " ")
	if parts := steamChineseTime.FindStringSubmatch(text); parts != nil {
		hour, _ := strconv.Atoi(parts[5])
		if parts[4] != "" {
			hour %= 12
			if parts[4] == "下午" {
				hour += 12
			}
		}
		stamp := fmt.Sprintf("%s-%s-%s %d:%s", parts[1], parts[2], parts[3], hour, parts[6])
		if date, err := time.ParseInLocation("2006-1-2 15:04", stamp, time.Local); err == nil {
			return date.Unix()
		}
	}
	stamp := strings.ToLower(steamEnglishTime.FindString(text))
	stamp = strings.NewReplacer(",", "", "@", " ", "am", " am", "pm", " pm").Replace(stamp)
	stamp = strings.Join(strings.Fields(stamp), " ")
	for _, layout := range []string{"2 Jan 2006 3:04 pm", "Jan 2 2006 3:04 pm"} {
		if date, err := time.ParseInLocation(layout, stamp, time.Local); err == nil {
			return date.Unix()
		}
	}
	return 0
}
