package hostkit

import "strings"

// VersionFromTag strips a leading 'v' or 'V' only when immediately followed by an ASCII digit.
func VersionFromTag(tag string) string {
	text := strings.TrimSpace(tag)
	if len(text) > 1 && (text[0] == 'v' || text[0] == 'V') && text[1] >= '0' && text[1] <= '9' {
		return text[1:]
	}
	return text
}

// VersionGT reports whether first is strictly newer than second; release versions beat prereleases on equal numeric segments.
func VersionGT(first, second string) bool {
	parse := func(v string) ([]int, string) {
		v = VersionFromTag(v)
		mainPart, pre := v, ""
		if idx := strings.IndexAny(v, "-+"); idx >= 0 {
			mainPart, pre = v[:idx], v[idx+1:]
		}
		pieces := strings.Split(mainPart, ".")
		nums := make([]int, 0, len(pieces))
		for _, piece := range pieces {
			n := 0
			for _, r := range piece {
				if r < '0' || r > '9' {
					break
				}
				n = n*10 + int(r-'0')
			}
			nums = append(nums, n)
		}
		return nums, pre
	}

	left, leftPre := parse(first)
	right, rightPre := parse(second)
	for i := 0; i < len(left) || i < len(right); i++ {
		l, r := 0, 0
		if i < len(left) {
			l = left[i]
		}
		if i < len(right) {
			r = right[i]
		}
		if l != r {
			return l > r
		}
	}
	if leftPre == "" && rightPre != "" {
		return true
	}
	if leftPre != "" && rightPre == "" {
		return false
	}
	return leftPre > rightPre
}
