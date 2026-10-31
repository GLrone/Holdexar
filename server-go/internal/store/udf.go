package store

import (
	"database/sql/driver"
	"fmt"
	"strings"

	"modernc.org/sqlite"
)

// cjkRanges CJK 三段区间：部首扩展/音标/主平面、谚文、兼容表意。
var cjkRanges = [][2]rune{
	{0x2E80, 0x9FFF},
	{0xAC00, 0xD7AF},
	{0xF900, 0xFAFF},
}

func hasCJK(token string) bool {
	for _, r := range token {
		for _, rg := range cjkRanges {
			if r >= rg[0] && r <= rg[1] {
				return true
			}
		}
	}
	return false
}

// nameBigram 名称 → 空格分隔的 2-gram 影子检索文本：含 CJK 的词内滑窗成片
// （单字产出为空），其余词原样保留；去重保序。与 FTS5 unicode61 分词口径配套，
// 写入侧触发器与查询侧必须共用本函数。
func nameBigram(text string) string {
	var out []string
	seen := make(map[string]struct{})
	add := func(s string) {
		if _, ok := seen[s]; !ok {
			seen[s] = struct{}{}
			out = append(out, s)
		}
	}
	for _, token := range strings.Fields(text) {
		if hasCJK(token) {
			rs := []rune(token)
			for i := 0; i+1 < len(rs); i++ {
				add(string(rs[i : i+2]))
			}
		} else {
			add(token)
		}
	}
	return strings.Join(out, " ")
}

func udfText(v driver.Value) string {
	switch t := v.(type) {
	case nil:
		return ""
	case string:
		return t
	case []byte:
		return string(t)
	default:
		return fmt.Sprintf("%v", t)
	}
}

// nameBigramUDF 触发器两参拼接：name + " " + name_en，空参按空串计。
func nameBigramUDF(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
	var name, nameEn string
	if len(args) > 0 {
		name = udfText(args[0])
	}
	if len(args) > 1 {
		nameEn = udfText(args[1])
	}
	return nameBigram(name + " " + nameEn), nil
}

func init() {
	sqlite.MustRegisterDeterministicScalarFunction("name_bigram", 2, nameBigramUDF)
}
