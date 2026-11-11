package account

import (
	"encoding/base64"
	"encoding/json"
	"math"
	"net/url"
	"strings"
	"time"
)

var accountZone = time.FixedZone("Beijing", 8*60*60)
var loginCookieKeys = []string{"sessionid", "steamCountry", "steamLoginSecure", "steamRefresh_steam", "steamRememberLogin"}

func ParseCookies(raw string) map[string]string {
	raw = strings.TrimSpace(raw)
	if len(raw) >= 7 && strings.EqualFold(raw[:7], "cookie:") {
		raw = strings.TrimSpace(raw[7:])
	}
	out := map[string]string{}
	for _, part := range strings.FieldsFunc(raw, func(r rune) bool { return r == ';' || r == '\r' || r == '\n' }) {
		k, v, ok := strings.Cut(part, "=")
		if ok {
			out[strings.TrimSpace(k)] = strings.TrimSpace(v)
		}
	}
	return out
}
func FilterCookies(raw string) string {
	jar := ParseCookies(raw)
	parts := []string{}
	for _, key := range loginCookieKeys {
		if value := jar[key]; value != "" {
			parts = append(parts, key+"="+value)
		}
	}
	return strings.Join(parts, "; ")
}
func cookieToken(raw string) (string, string) {
	body, e := url.PathUnescape(strings.TrimSpace(raw))
	if e != nil {
		body = strings.TrimSpace(raw)
	}
	if sid, token, ok := strings.Cut(body, "||"); ok {
		return strings.TrimSpace(sid), strings.TrimSpace(token)
	}
	if numericID(body) {
		return body, ""
	}
	return "", body
}
func numericID(v string) bool {
	if v == "" {
		return false
	}
	for _, r := range v {
		if r < '0' || r > '9' {
			return false
		}
	}
	return true
}
func CookieSteamID(raw string) string {
	sid, _ := cookieToken(ParseCookies(raw)["steamLoginSecure"])
	if numericID(sid) {
		return sid
	}
	return ""
}
func RefreshToken(raw string) string {
	_, token := cookieToken(ParseCookies(raw)["steamRefresh_steam"])
	return token
}

type Freshness struct {
	ExpiresAt                     *string
	Expired, Expiring, HasRefresh bool
	SecondsLeft                   *float64
}

func jwtClaims(token string) map[string]any {
	parts := strings.Split(token, ".")
	if len(parts) < 2 {
		return nil
	}
	payload, e := base64.RawURLEncoding.DecodeString(strings.TrimRight(parts[1], "="))
	if e != nil {
		return nil
	}
	var claims map[string]any
	if json.Unmarshal(payload, &claims) != nil {
		return nil
	}
	return claims
}
func credentialOwnerMatches(raw, expected string) bool {
	sid, token := cookieToken(ParseCookies(raw)["steamLoginSecure"])
	subject, _ := jwtClaims(token)["sub"].(string)
	return sid == expected && (subject == "" || subject == expected)
}

func CookieFreshness(raw string, now time.Time) Freshness {
	f := Freshness{HasRefresh: RefreshToken(raw) != ""}
	_, token := cookieToken(ParseCookies(raw)["steamLoginSecure"])
	claims := jwtClaims(token)
	exp, ok := claims["exp"].(float64)
	if !ok || math.IsNaN(exp) || math.IsInf(exp, 0) || exp < -62135596800 || exp > 253402300799 {
		return f
	}
	seconds, fraction := math.Modf(exp)
	expires := time.Unix(int64(seconds), int64(fraction*1e9))
	left := exp - float64(now.Unix()) - float64(now.Nanosecond())/1e9
	at := expires.In(accountZone).Format("2006-01-02T15:04:05")
	f.ExpiresAt = &at
	f.SecondsLeft = &left
	f.Expired = left <= 0
	f.Expiring = left <= 2*60*60
	return f
}
