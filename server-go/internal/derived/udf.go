package derived

import (
	"database/sql/driver"
	"time"

	"modernc.org/sqlite"
)

// Clock 评分「发行年龄」基准挂钟（UDF 闭包读取）；宿主注入冻结点对齐
// Python 侧可冻结时钟，缺省 UTC+8 实时。
var Clock = func() time.Time { return time.Now().UTC().Add(8 * time.Hour) }

func udfInt(v driver.Value) *int64 {
	switch t := v.(type) {
	case int64:
		return &t
	case float64:
		i := int64(t)
		return &i
	default:
		return nil
	}
}

func udfStr(v driver.Value) *string {
	switch t := v.(type) {
	case string:
		return &t
	case []byte:
		s := string(t)
		return &s
	default:
		return nil
	}
}

// smartScoreUDF game_smart_score(difference, positive_rate, review_count,
// hl_flag, discount, release_date)：派生 UPDATE 语句内联调用的评分函数。
func smartScoreUDF(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
	var release *string
	if len(args) > 5 {
		release = udfStr(args[5])
	}
	get := func(i int) *int64 {
		if i < len(args) {
			return udfInt(args[i])
		}
		return nil
	}
	return SmartScore(get(0), get(1), get(2), get(3), get(4), release, Clock()), nil
}

func init() {
	sqlite.MustRegisterDeterministicScalarFunction("game_smart_score", 6, smartScoreUDF)
}
