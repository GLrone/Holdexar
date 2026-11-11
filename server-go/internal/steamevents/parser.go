package steamevents

import (
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"time"

	"golang.org/x/net/html"
	"golang.org/x/net/html/atom"
)

type parsedEvent struct {
	Category, Name, Slug string
	Start, End           time.Time
}

var (
	seasonTitleRE = regexp.MustCompile(`(?i)(Spring|Summer|Autumn|Winter)\s+Sale|[春夏秋冬]季?\s*特卖|夏日\s*特卖`)
	nextFestRE    = regexp.MustCompile(`(?i)Next\s*Fest|新品节`)
	festYearRE    = regexp.MustCompile(`(?i)(\d{4})\s*年?\s*(?:Fests|各?游戏节)`)
	festSlugRE    = regexp.MustCompile(`(?i)upcoming_events/(themed_sales/[a-z0-9_]+)(?:[/?#]|$)`)
	linkLabelRE   = regexp.MustCompile(`(?i)^(?:更多信息|More info|View documentation|Registration details|注册详情)$`)
)

func parseUpcoming(raw string) []parsedEvent {
	doc, err := html.Parse(strings.NewReader(raw))
	if err != nil {
		return nil
	}

	var headings, themed []parsedEvent
	year := 0
	for node := range doc.Descendants() {
		switch node.DataAtom {
		case atom.H2:
			text := eventNodeText(node)
			year = festYear(text, year)
			var category string
			switch {
			case seasonTitleRE.MatchString(text):
				category = "seasonal_sale"
			case nextFestRE.MatchString(text):
				category = "next_fest"
			default:
				continue
			}
			dates := eventDateFragments(text, 0)
			if len(dates) < 2 {
				continue
			}
			name, _, _ := strings.Cut(text, "|")
			start, end := dates[0].date(), dates[len(dates)-1].date()
			if !start.IsZero() && !end.IsZero() && !end.Before(start) {
				headings = append(headings, parsedEvent{Category: category, Name: strings.TrimSpace(name), Start: start, End: end})
			}
		case atom.Table:
			if year != 0 {
				themed = append(themed, parseFestTable(node, year)...)
			}
		default:
			if node.Type == html.TextNode && node.Parent.DataAtom != atom.Script && node.Parent.DataAtom != atom.Style {
				year = festYear(node.Data, year)
			}
		}
	}

	seen := make(map[parsedEvent]struct{}, len(headings)+len(themed))
	rows := append(headings, themed...)
	result := rows[:0]
	for _, row := range rows {
		key := row
		key.Name, key.Slug = strings.ToLower(row.Name), ""
		if _, exists := seen[key]; !exists {
			seen[key] = struct{}{}
			result = append(result, row)
		}
	}
	return result
}

func parseFestTable(table *html.Node, year int) []parsedEvent {
	var rows []parsedEvent
	for row := range table.Descendants() {
		if row.DataAtom != atom.Tr {
			continue
		}
		var cells []*html.Node
		for cell := range row.ChildNodes() {
			if cell.DataAtom == atom.Td {
				cells = append(cells, cell)
			}
		}
		if len(cells) < 2 {
			continue
		}
		dates := eventDateFragments(eventNodeText(cells[0]), year)
		if len(dates) < 2 || dates[0].year != year || dates[1].year != year {
			continue
		}
		name := eventNodeText(cells[1])
		start, end := dates[0].date(), dates[1].date()
		if name == "" || start.IsZero() || end.IsZero() || end.Before(start) {
			continue
		}
		event := parsedEvent{Category: "themed_fest", Name: name, Start: start, End: end}
		for link := range row.Descendants() {
			if slug := eventLinkSlug(link); slug != "" {
				event.Slug = slug
				break
			}
		}
		rows = append(rows, event)
	}
	return rows
}

func parseZHNames(raw string) map[string]string {
	names := make(map[string]string)
	doc, err := html.Parse(strings.NewReader(raw))
	if err != nil {
		return names
	}
	for link := range doc.Descendants() {
		slug := eventLinkSlug(link)
		if slug == "" || names[slug] != "" {
			continue
		}
		name := eventNodeText(link)
		if name != "" && !linkLabelRE.MatchString(name) {
			names[slug] = name
		}
	}
	return names
}

func eventNodeText(node *html.Node) string {
	var text strings.Builder
	for child := range node.Descendants() {
		if child.Type == html.TextNode {
			text.WriteString(child.Data)
			text.WriteByte(' ')
		}
	}
	return strings.Join(strings.Fields(text.String()), " ")
}

func festYear(text string, year int) int {
	for _, mark := range festYearRE.FindAllStringSubmatch(text, -1) {
		year = max(year, dateNumber(mark[1]))
	}
	return year
}

func eventLinkSlug(node *html.Node) string {
	if node.DataAtom == atom.A {
		for _, attr := range node.Attr {
			if attr.Key == "href" {
				if match := festSlugRE.FindStringSubmatch(attr.Val); len(match) > 0 {
					return strings.ToLower(match[1])
				}
			}
		}
	}
	return ""
}

func validateEvents(rows []parsedEvent, today time.Time) error {
	var seasonal []parsedEvent
	for _, row := range rows {
		if row.Category == "seasonal_sale" {
			seasonal = append(seasonal, row)
		}
	}
	sort.SliceStable(seasonal, func(i, j int) bool { return seasonal[i].Start.Before(seasonal[j].Start) })
	var problems []error
	if len(seasonal) < 1 || len(seasonal) > 6 {
		problems = append(problems, fmt.Errorf("seasonal_sale count=%d out of range 1-6", len(seasonal)))
	}
	year, month, day := today.Date()
	cutoff := time.Date(year, month, day, 0, 0, 0, 0, time.UTC).AddDate(-1, 0, 0)
	for _, row := range seasonal {
		days := int(row.End.Sub(row.Start) / (24 * time.Hour))
		if days < 2 || days > 20 {
			problems = append(problems, fmt.Errorf("%s duration %dd abnormal (2-20)", row.Name, days))
		}
		if row.Start.Before(cutoff) {
			problems = append(problems, fmt.Errorf("%s starts more than a year ago", row.Name))
		}
	}
	for i := 1; i < len(seasonal); i++ {
		previous, current := seasonal[i-1], seasonal[i]
		if !current.Start.After(previous.End) {
			problems = append(problems, fmt.Errorf("%s overlaps %s", previous.Name, current.Name))
		}
	}
	for _, row := range rows {
		if row.End.Before(row.Start) {
			problems = append(problems, fmt.Errorf("%s end < start", row.Name))
		}
	}
	return errors.Join(problems...)
}
