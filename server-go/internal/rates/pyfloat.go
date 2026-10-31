package rates

import (
	"math"
	"strconv"
	"strings"
)

// pyFloat 汇率数值序列化：对齐 Python json 的浮点形态
// （整值带 .0，如 1.0 → "1.0"；Go 缺省 marshal 会输出 "1"）。
type pyFloat float64

func (f pyFloat) MarshalJSON() ([]byte, error) {
	return []byte(formatPyFloat(float64(f))), nil
}

// formatPyFloat Python repr 浮点形态：|x| ∈ [1e-4, 1e16) 用定点，
// 越界用 e 记法（指数两位、带符号），整值补 .0。
func formatPyFloat(f float64) string {
	abs := math.Abs(f)
	if abs != 0 && (abs >= 1e16 || abs < 1e-4) {
		return strconv.FormatFloat(f, 'e', -1, 64)
	}
	s := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(s, ".") {
		s += ".0"
	}
	return s
}
