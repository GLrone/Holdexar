package timeutil

import (
	"sync"
	"time"
	_ "time/tzdata"
)

const (
	layoutMicro = "2006-01-02 15:04:05.000000"
	layoutSec   = "2006-01-02 15:04:05"
)

// 东八区 Location 按 zone 名缓存为单例；zone 名进入 time.String() 等输出不得改。
const east8Offset = 8 * 3600

var (
	Beijing     = time.FixedZone("Asia/Shanghai", east8Offset)
	ZoneBeijing = time.FixedZone("Beijing", east8Offset)
	ZoneUTC8    = time.FixedZone("UTC+8", east8Offset)
	ZoneCST     = time.FixedZone("CST", east8Offset)
)

var pacificLoc = sync.OnceValue(func() *time.Location {
	loc, err := time.LoadLocation("America/Los_Angeles")
	if err != nil || loc == nil {
		return time.FixedZone("PST", -8*3600)
	}
	return loc
})

func Now() time.Time {
	return time.Now().In(Beijing)
}

func FormatMicro(t time.Time) string {
	return t.Format(layoutMicro)
}

func FormatSec(t time.Time) string {
	return t.Format(layoutSec)
}

func Pacific() *time.Location {
	return pacificLoc()
}
