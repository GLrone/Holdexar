package derived

import (
	"database/sql/driver"
	"fmt"
	"math"
	"sync/atomic"
	"time"

	"modernc.org/sqlite"
)

var scoreClock = func() *atomic.Value {
	v := &atomic.Value{}
	v.Store(func() time.Time { return time.Now().UTC().Add(8 * time.Hour) })
	return v
}()

func Clock() time.Time { return scoreClock.Load().(func() time.Time)() }
func SetClock(clock func() time.Time) func() time.Time {
	return scoreClock.Swap(clock).(func() time.Time)
}

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
	sqlite.MustRegisterDeterministicScalarFunction("bundle_quality_score", 3, func(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
		return QualityScore(udfInt(args[0]), udfInt(args[1]), udfStr(args[2]), Clock()), nil
	})
	sqlite.MustRegisterDeterministicScalarFunction("bundle_familiarity_score", 1, func(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
		return FamiliarityScore(udfInt(args[0])), nil
	})
	sqlite.MustRegisterDeterministicScalarFunction("bundle_smart_score", 4, func(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
		return BlendScore(SaveScore(udfInt(args[0])), udfFloat(args[1]), udfFloat(args[2]), udfFloat(args[3])), nil
	})
	sqlite.MustRegisterDeterministicScalarFunction("price_cny_fen", 2, func(_ *sqlite.FunctionContext, args []driver.Value) (driver.Value, error) {
		value := math.RoundToEven(udfFloat(args[0]) * udfFloat(args[1]))
		if math.IsNaN(value) || math.IsInf(value, 0) || value < math.MinInt64 || value >= 9223372036854775808.0 {
			return nil, fmt.Errorf("人民币价格超出整数范围")
		}
		return int64(value), nil
	})
}

func udfFloat(value driver.Value) float64 {
	switch value := value.(type) {
	case int64:
		return float64(value)
	case float64:
		return value
	}
	return 0
}
