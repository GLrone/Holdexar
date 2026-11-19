package notifications

import (
	"net/url"
	"strconv"
	"strings"
	"unicode"

	nethtml "golang.org/x/net/html"
)

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
	doc, err := nethtml.Parse(strings.NewReader(body))
	if err != nil {
		return ""
	}
	var parts strings.Builder
	seen := map[string]bool{}
	blocks := map[string]bool{"p": true, "div": true, "tr": true, "li": true, "h1": true, "h2": true, "h3": true}

	var walk func(*nethtml.Node)
	walk = func(node *nethtml.Node) {
		switch node.Type {
		case nethtml.TextNode:
			parts.WriteString(node.Data)
			return
		case nethtml.ElementNode:
			name := strings.ToLower(node.Data)
			if name == "head" || name == "style" || name == "script" {
				return
			}
			if blocks[name] || name == "br" {
				parts.WriteByte('\n')
			}
			for child := node.FirstChild; child != nil; child = child.NextSibling {
				walk(child)
			}
			if blocks[name] {
				parts.WriteByte('\n')
			}
			if name == "td" || name == "th" {
				parts.WriteByte(' ')
			}
			if name == "a" {
				for _, attr := range node.Attr {
					if strings.EqualFold(attr.Key, "href") {
						if link := safeMailURL(attr.Val); link != "" && !seen[link] {
							parts.WriteString(" (" + link + ") ")
							seen[link] = true
						}
						break
					}
				}
			}
			return
		}
		for child := node.FirstChild; child != nil; child = child.NextSibling {
			walk(child)
		}
	}
	walk(doc)

	lines := []string{}
	for _, line := range strings.Split(parts.String(), "\n") {
		if line = strings.Join(strings.Fields(line), " "); line != "" {
			lines = append(lines, line)
		}
	}
	return strings.Join(lines, "\n")
}
