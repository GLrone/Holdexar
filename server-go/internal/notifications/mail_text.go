package notifications

import (
	"html"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"unicode"
)

var mailTokens = regexp.MustCompile(`(?s)<!--.*?-->|<![^>]*>|<[^>]+>|[^<]+`)
var mailHref = regexp.MustCompile(`(?i)\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))`)

func safeMailURL(value string) string {
	if value == "" || strings.ContainsAny(value, "\\<>\"") || strings.ContainsFunc(value, func(r rune) bool { return unicode.IsSpace(r) || r < 32 || r == 127 }) {
		return ""
	}
	u, e := url.Parse(value)
	if e != nil || u.User != nil || (strings.ToLower(u.Scheme) != "http" && strings.ToLower(u.Scheme) != "https") {
		return ""
	}
	if raw := u.Port(); raw != "" {
		port, e := strconv.Atoi(raw)
		if e != nil || port < 1 || port > 65535 {
			return ""
		}
	}
	host := strings.ToLower(u.Hostname())
	for _, domain := range []string{"steampowered.com", "steamcommunity.com", "epicgames.com", "humblebundle.com"} {
		if host == domain || strings.HasSuffix(host, "."+domain) {
			return value
		}
	}
	return ""
}
func MailText(body string) string {
	var parts strings.Builder
	ignored := []string{}
	links := []string{}
	seen := map[string]bool{}
	blocks := map[string]bool{"p": true, "div": true, "tr": true, "li": true, "h1": true, "h2": true, "h3": true}
	for _, token := range mailTokens.FindAllString(body, -1) {
		if !strings.HasPrefix(token, "<") {
			if len(ignored) == 0 {
				parts.WriteString(html.UnescapeString(token))
			}
			continue
		}
		if strings.HasPrefix(token, "<!") {
			continue
		}
		tag := strings.TrimSpace(strings.Trim(token, "<>"))
		closing := strings.HasPrefix(tag, "/")
		tag = strings.TrimPrefix(tag, "/")
		fields := strings.Fields(tag)
		if len(fields) == 0 {
			if len(ignored) == 0 {
				parts.WriteString(token)
			}
			continue
		}
		name := strings.ToLower(strings.TrimRight(fields[0], "/"))
		if len(ignored) > 0 {
			if closing && name == ignored[len(ignored)-1] {
				ignored = ignored[:len(ignored)-1]
			} else if !closing && (name == "head" || name == "style" || name == "script") {
				ignored = append(ignored, name)
			}
			continue
		}
		if !closing && (name == "head" || name == "style" || name == "script") {
			ignored = append(ignored, name)
			continue
		}
		if blocks[name] || (!closing && name == "br") {
			parts.WriteByte('\n')
		}
		if closing && (name == "td" || name == "th") {
			parts.WriteByte(' ')
		}
		if name == "a" {
			if !closing {
				link := ""
				if m := mailHref.FindStringSubmatch(token); m != nil {
					for _, v := range m[1:] {
						if v != "" {
							link = safeMailURL(html.UnescapeString(v))
							break
						}
					}
				}
				links = append(links, link)
			} else if len(links) > 0 {
				link := links[len(links)-1]
				links = links[:len(links)-1]
				if link != "" && !seen[link] {
					parts.WriteString(" (" + link + ") ")
					seen[link] = true
				}
			}
		}
	}
	lines := []string{}
	for _, line := range strings.Split(parts.String(), "\n") {
		if line = strings.Join(strings.Fields(line), " "); line != "" {
			lines = append(lines, line)
		}
	}
	return strings.Join(lines, "\n")
}
