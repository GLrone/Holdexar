package storebrowse

import (
	"regexp"
	"sort"
	"strings"
	"unicode"
)

var (
	spanRE           = regexp.MustCompile(`(?i)<span[^>]*>.*?</span>`)
	breakRE          = regexp.MustCompile(`(?i)<br\s*/?>`)
	tagRE            = regexp.MustCompile(`<[^>]+>`)
	languageTailRE   = regexp.MustCompile(`\s*\([A-Za-z]+(\s*/\s*[A-Za-z]+)+\)\s*$`)
	currencyRE       = regexp.MustCompile(`(?i)руб\.?|р\.|kr|zł|Kč|Ft|дин|lei|лв|kn|ман\.?|Rp|KD|SR|QR|RM|S/\.?|\$U|CHF|USD|EUR|GBP|RUB|KZT|UAH|TRY|INR|BRL|IDR|JPY|TWD|HKD|SGD|CNY|CLP|COP|PEN|AED|SAR|QAR|KWD|BHD|OMR|JOD|ILS|ZAR|MYR|THB|PHP|VND|NZD|AUD|CAD|DKK|NOK|PLN|SEK|ARS|MXN|UYU|KRW|HUF`)
	prefixCurrencyRE = regexp.MustCompile(`^[A-Za-z]{1,4}\$|^[A-Za-z]{1,2}[\s\d/]`)
	priceDigitsRE    = regexp.MustCompile(`^\s*\d[\d.,\s]*-{0,2}\s*$`)
	editionKeys      = []string{"deluxe edition", "ultimate edition", "gold edition", "special edition", "premium edition", "game of the year edition", "goty edition", "complete edition", "definitive edition", "enhanced edition", "anniversary edition", "collector's edition", "legendary edition", "year one edition", "royal edition", "prime edition"}
)

func init() {
	sort.SliceStable(editionKeys, func(i, j int) bool { return len(editionKeys[i]) > len(editionKeys[j]) })
}

func IsGoldEdition(name string) bool {
	lower := strings.ToLower(name)
	return !strings.Contains(lower, "commercial license") && (strings.Contains(lower, "gold edition") || strings.Contains(name, "黄金版"))
}

func priceSegment(s string) bool {
	marked := currencyRE.MatchString(s)
	cleaned := currencyRE.ReplaceAllString(s, "")
	if prefixCurrencyRE.MatchString(cleaned) {
		marked = true
		prefix := prefixCurrencyRE.FindString(cleaned)
		cleaned = cleaned[len(prefix):]
		if len(prefix) > 0 {
			last := prefix[len(prefix)-1]
			if last >= '0' && last <= '9' {
				cleaned = string(last) + cleaned
			}
		}
	}
	cleaned = strings.Map(func(r rune) rune {
		if unicode.Is(unicode.Sc, r) {
			marked = true
			return -1
		}
		return r
	}, cleaned)
	return marked && priceDigitsRE.MatchString(cleaned)
}

func ExtractVersionSuffix(option, nameEn string) string {
	cleaned := spanRE.ReplaceAllString(option, "")
	cleaned = breakRE.ReplaceAllString(cleaned, " ")
	cleaned = tagRE.ReplaceAllString(cleaned, "")
	cleaned = strings.TrimSpace(strings.NewReplacer("\u00a0", " ", "&nbsp;", " ").Replace(cleaned))
	segments := strings.Split(cleaned, " - ")
	for i := range segments {
		segments[i] = strings.TrimSpace(segments[i])
	}
	for len(segments) > 1 && priceSegment(segments[len(segments)-1]) {
		segments = segments[:len(segments)-1]
	}
	var kept []string
	for _, s := range segments {
		if s != "" {
			kept = append(kept, s)
		}
	}
	name := strings.TrimSpace(strings.Join(kept, " - "))
	if name == "" || nameEn == "" {
		return ""
	}
	lower := strings.ToLower(strings.TrimSpace(languageTailRE.ReplaceAllString(name, "")))
	base := strings.ToLower(strings.TrimSpace(languageTailRE.ReplaceAllString(nameEn, "")))
	if lower == base {
		return ""
	}
	for _, sep := range []string{" ", ":", "："} {
		if strings.HasPrefix(lower, base+sep) {
			runes := []rune(name)
			offset := len([]rune(nameEn)) + len([]rune(sep))
			if offset <= len(runes) {
				return strings.TrimSpace(strings.Trim(string(runes[offset:]), " -–—:："))
			}
		}
	}
	for _, key := range editionKeys {
		if strings.Contains(lower, key) {
			words := strings.Fields(key)
			for i, w := range words {
				if w != "of" && w != "the" {
					words[i] = strings.ToUpper(w[:1]) + w[1:]
				}
			}
			return strings.Join(words, " ")
		}
	}
	if IsGoldEdition(name) {
		return "Gold Edition"
	}
	return ""
}
