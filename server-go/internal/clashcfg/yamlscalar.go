package clashcfg

import (
	"math"
	"regexp"
	"strconv"
	"strings"
)

var (
	reYAMLBool    = regexp.MustCompile(`^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$`)
	reYAMLNull    = regexp.MustCompile(`^(?:~|null|Null|NULL|)$`)
	reYAMLInt     = regexp.MustCompile(`^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(:[0-5]?[0-9])+)$`)
	reYAMLFloat   = regexp.MustCompile(`^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$`)
	reSexagesimal = regexp.MustCompile(`^[-+]?[0-9][0-9_]*(:[0-5]?[0-9])+(\.[0-9_]*)?$`)
)

func resolveScalarText(s string) any {
	if reYAMLNull.MatchString(s) {
		return nil
	}
	if reYAMLBool.MatchString(s) {
		switch strings.ToLower(s) {
		case "yes", "true", "on":
			return true
		}
		return false
	}
	if reYAMLInt.MatchString(s) {
		if v, ok := parseYAMLInt(s); ok {
			return v
		}
		return s
	}
	if reYAMLFloat.MatchString(s) {
		if v, ok := parseYAMLFloat(s); ok {
			return v
		}
		return s
	}
	return s
}

func parseYAMLInt(s string) (int64, bool) {
	neg := false
	body := s
	if strings.HasPrefix(body, "-") {
		neg = true
		body = body[1:]
	} else if strings.HasPrefix(body, "+") {
		body = body[1:]
	}
	clean := strings.ReplaceAll(body, "_", "")
	var v int64
	switch {
	case strings.HasPrefix(clean, "0b"):
		n, err := strconv.ParseInt(clean[2:], 2, 64)
		if err != nil {
			return 0, false
		}
		v = n
	case strings.HasPrefix(clean, "0x"):
		n, err := strconv.ParseInt(clean[2:], 16, 64)
		if err != nil {
			return 0, false
		}
		v = n
	case len(clean) > 1 && clean[0] == '0':
		n, err := strconv.ParseInt(clean[1:], 8, 64)
		if err != nil {
			return 0, false
		}
		v = n
	case strings.Contains(clean, ":"):
		n, ok := parseSexagesimalInt(clean)
		if !ok {
			return 0, false
		}
		v = n
	default:
		n, err := strconv.ParseInt(clean, 10, 64)
		if err != nil {
			return 0, false
		}
		v = n
	}
	if neg {
		v = -v
	}
	return v, true
}

func parseSexagesimalInt(s string) (int64, bool) {
	parts := strings.Split(s, ":")
	total, err := strconv.ParseInt(parts[0], 10, 64)
	if err != nil {
		return 0, false
	}
	for _, p := range parts[1:] {
		n, err := strconv.ParseInt(p, 10, 64)
		if err != nil {
			return 0, false
		}
		total = total*60 + n
	}
	return total, true
}

func parseYAMLFloat(s string) (float64, bool) {
	lower := strings.ToLower(s)
	if strings.Contains(lower, "inf") {
		if strings.HasPrefix(s, "-") {
			return math.Inf(-1), true
		}
		return math.Inf(1), true
	}
	if strings.Contains(lower, "nan") {
		return math.NaN(), true
	}
	if strings.Contains(s, ":") {
		if !reSexagesimal.MatchString(s) {
			return 0, false
		}
		neg := strings.HasPrefix(s, "-")
		body := strings.TrimLeft(s, "+-")
		intPart, frac, hasDot := strings.Cut(body, ".")
		total, ok := parseSexagesimalInt(strings.ReplaceAll(intPart, "_", ""))
		if !ok {
			return 0, false
		}
		f := float64(total)
		if hasDot && frac != "" {
			fv, err := strconv.ParseFloat("0."+strings.ReplaceAll(frac, "_", ""), 64)
			if err != nil {
				return 0, false
			}
			f += fv
		}
		if neg {
			f = -f
		}
		return f, true
	}
	f, err := strconv.ParseFloat(strings.ReplaceAll(s, "_", ""), 64)
	if err != nil {
		return 0, false
	}
	return f, true
}
