package clashcfg

// YAML 1.1 标量 resolver（对齐 PyYAML safe_load 的隐式类型判定）。

import (
	"math"
	"regexp"
	"strconv"
	"strings"
)

var boolLiterals = map[string]bool{
	"yes": true, "no": true, "true": true, "false": true,
	"on": true, "off": true,
}

var (
	intRe   = regexp.MustCompile(`^[-+]?[0-9][0-9_]*$`)
	octRe   = regexp.MustCompile(`^[-+]?0[0-7_]+$`)
	hexRe   = regexp.MustCompile(`^[-+]?0x[0-9a-fA-F_]+$`)
	binRe   = regexp.MustCompile(`^[-+]?0b[0-1_]+$`)
	sexaRe  = regexp.MustCompile(`^[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+$`)
	floatRe = regexp.MustCompile(`^[-+]?(\.[0-9]+|[0-9][0-9_]*(\.[0-9_]*)?)([eE][-+]?[0-9]+)?$`)
	fsexaRe = regexp.MustCompile(`^[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*$`)
	infRe   = regexp.MustCompile(`^[-+]?(\.inf|\.Inf|\.INF)$`)
	nanRe   = regexp.MustCompile(`^\.[nN]a[nN]$`)
)

// resolveScalarText 纯文本 → YAML 1.1 隐式类型（PyYAML 同判据）。
func resolveScalarText(s string) any {
	switch s {
	case "", "~", "null", "Null", "NULL":
		return nil
	}
	lower := strings.ToLower(s)
	if boolLiterals[lower] {
		return lower == "yes" || lower == "true" || lower == "on"
	}
	if infRe.MatchString(s) || nanRe.MatchString(s) {
		return parseYAMLFloatLower(s)
	}
	if v, ok := parseYAMLInt(s); ok {
		return v
	}
	if v, ok := parseYAMLFloat(s); ok {
		return v
	}
	return s
}

func parseYAMLInt(s string) (int64, bool) {
	clean := strings.ReplaceAll(s, "_", "")
	if m := hexRe.FindString(clean); m != "" {
		neg := strings.HasPrefix(clean, "-")
		body := strings.TrimPrefix(strings.TrimPrefix(clean, "-"), "+")
		v, err := strconv.ParseInt(body[2:], 16, 64)
		if err != nil {
			return 0, false
		}
		if neg {
			v = -v
		}
		return v, true
	}
	if binRe.MatchString(clean) {
		neg := strings.HasPrefix(clean, "-")
		body := strings.TrimPrefix(strings.TrimPrefix(clean, "-"), "+")
		v, err := strconv.ParseInt(body[2:], 2, 64)
		if err != nil {
			return 0, false
		}
		if neg {
			v = -v
		}
		return v, true
	}
	if octRe.MatchString(clean) {
		neg := strings.HasPrefix(clean, "-")
		body := strings.TrimPrefix(strings.TrimPrefix(clean, "-"), "+")
		v, err := strconv.ParseInt(body[1:], 8, 64)
		if err != nil {
			return 0, false
		}
		if neg {
			v = -v
		}
		return v, true
	}
	if sexaRe.MatchString(clean) {
		return parseSexagesimal(clean)
	}
	if intRe.MatchString(clean) {
		v, err := strconv.ParseInt(clean, 10, 64)
		if err != nil {
			return 0, false
		}
		return v, true
	}
	return 0, false
}

// parseSexagesimal YAML 1.1 六十进制整数：1:30 → 90。
func parseSexagesimal(s string) (int64, bool) {
	neg := strings.HasPrefix(s, "-")
	body := strings.TrimPrefix(strings.TrimPrefix(s, "-"), "+")
	parts := strings.Split(body, ":")
	var total int64
	for _, part := range parts {
		part = strings.ReplaceAll(part, "_", "")
		v, err := strconv.ParseInt(part, 10, 64)
		if err != nil {
			return 0, false
		}
		total = total*60 + v
	}
	if neg {
		total = -total
	}
	return total, true
}

func parseYAMLFloat(s string) (float64, bool) {
	clean := strings.ReplaceAll(s, "_", "")
	if fsexaRe.MatchString(clean) {
		neg := strings.HasPrefix(clean, "-")
		intPart, frac, _ := strings.Cut(strings.TrimPrefix(strings.TrimPrefix(clean, "-"), "+"), ".")
		total := 0.0
		for _, part := range strings.Split(intPart, ":") {
			v, err := strconv.ParseFloat(part, 64)
			if err != nil {
				return 0, false
			}
			total = total*60 + v
		}
		if frac != "" {
			v, err := strconv.ParseFloat("0."+frac, 64)
			if err != nil {
				return 0, false
			}
			total += v / 60
		}
		if neg {
			total = -total
		}
		return total, true
	}
	if floatRe.MatchString(clean) {
		v, err := strconv.ParseFloat(clean, 64)
		if err != nil {
			return 0, false
		}
		return v, true
	}
	return 0, false
}

func parseYAMLFloatLower(s string) float64 {
	neg := strings.HasPrefix(s, "-")
	switch {
	case strings.Contains(strings.ToLower(s), "inf"):
		if neg {
			return math.Inf(-1)
		}
		return math.Inf(1)
	default:
		return math.NaN()
	}
}
