package sched

import (
	"strings"
	"testing"
	"time"
)

// ---------------------------------------------------------------- 测试件：事件收集

type evLog struct{ evs []Event }

func (l *evLog) on(e Event) { l.evs = append(l.evs, e) }

func (l *evLog) of(kind, id string) []Event {
	var out []Event
	for _, e := range l.evs {
		if e.Kind == kind && (id == "" || e.JobID == id) {
			out = append(out, e)
		}
	}
	return out
}

func (l *evLog) job(id string) []Event {
	var out []Event
	for _, e := range l.evs {
		if e.JobID == id {
			out = append(out, e)
		}
	}
	return out
}

var emptyBody = func(rc *RunCtx) {}

func bj(t time.Time) string { return t.In(DefaultLoc).Format("2006-01-02 15:04") }

func parseBJ(s string) time.Time {
	t, err := time.ParseInLocation("2006-01-02 15:04", s, DefaultLoc)
	if err != nil {
		panic(err)
	}
	return t
}

// ---------------------------------------------------------------- 测试件：价格网格（与价格主轮自续约同构，锚点随太平洋夏令时切换）

var locPac = mustLoc("America/Los_Angeles")

func anchorHour(now time.Time) int {
	if now.In(locPac).IsDST() {
		return 1
	}
	return 2
}

func gridNext(now time.Time, step time.Duration) time.Time {
	b := now.In(DefaultLoc)
	base := time.Date(b.Year(), b.Month(), b.Day(), anchorHour(now), 0, 0, 0, DefaultLoc)
	for !base.After(now) {
		base = base.Add(step)
	}
	return base
}

// priceBody：价格主轮运行体——按运行时刻重算网格并自续约。
func priceBody(rc *RunCtx) { rc.SetNext(gridNext(rc.Now(), 6*time.Hour)) }

// ---------------------------------------------------------------- 触发器语义

func TestIntervalTriggerNext(t *testing.T) {
	start := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	tr := IntervalTrigger{Start: start, Period: time.Hour}
	cases := []struct {
		after time.Time
		want  time.Time
	}{
		{start.Add(-time.Minute), start},                                 // 早于锚点 → 首拍即锚点
		{start, start.Add(time.Hour)},                                    // 恰在锚点 → 严格晚一拍
		{start.Add(2*time.Hour + time.Second), start.Add(3 * time.Hour)}, // 锚定步进不漂移
		{start.Add(47 * time.Hour), start.Add(48 * time.Hour)},
	}
	for _, c := range cases {
		if got := tr.Next(c.after); !got.Equal(c.want) {
			t.Fatalf("Next(%s) = %s, want %s", c.after, got, c.want)
		}
	}
}

func TestDailyTriggerNext(t *testing.T) {
	tr := DailyTrigger{Hour: 3, Minute: 0}
	cases := []struct {
		after string
		want  string
	}{
		{"2026-11-01 00:00", "2026-11-01 03:00"},
		{"2026-11-01 03:00", "2026-11-02 03:00"}, // 恰在拍点 → 次日
		{"2026-11-01 23:59", "2026-11-02 03:00"},
	}
	for _, c := range cases {
		got := tr.Next(parseBJ(c.after))
		if !got.Equal(parseBJ(c.want)) {
			t.Fatalf("Next(%s) = %s, want %s", c.after, bj(got), c.want)
		}
	}
}

func TestGridAnchors(t *testing.T) {
	step := 6 * time.Hour
	// 夏令时（太平洋 10:00 PDT = 北京 01:00）
	if got := gridNext(parseBJ("2026-10-31 17:00"), step); !got.Equal(parseBJ("2026-10-31 19:00")) {
		t.Fatalf("PDT 网格下一拍 = %s, want 10-31 19:00", bj(got))
	}
	// 秋季回拨后（太平洋 10:00 PST = 北京 02:00）：19:00 触发重锚 → 当日 20:00 追赶拍
	if got := gridNext(parseBJ("2026-11-01 19:00"), step); !got.Equal(parseBJ("2026-11-01 20:00")) {
		t.Fatalf("PST 重锚下一拍 = %s, want 11-01 20:00", bj(got))
	}
	if got := gridNext(parseBJ("2026-11-01 20:00"), step); !got.Equal(parseBJ("2026-11-02 02:00")) {
		t.Fatalf("追赶拍后下一拍 = %s, want 11-02 02:00", bj(got))
	}
	if h := anchorHour(parseBJ("2026-11-01 13:00")); h != 1 {
		t.Fatalf("回拨前锚点 = %d, want 1", h)
	}
	if h := anchorHour(parseBJ("2026-11-01 19:00")); h != 2 {
		t.Fatalf("回拨后锚点 = %d, want 2", h)
	}
}

// ---------------------------------------------------------------- DST 网格追赶拍

func TestGridDSTAutumn(t *testing.T) {
	start := parseBJ("2026-10-31 17:00")
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	if err := s.RegisterInterval("price_refresh", gridNext(start, 6*time.Hour), 6*time.Hour, priceBody, WithMisfireUnlimited()); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(start.Add(48 * time.Hour))

	fires := log.of(EventFire, "price_refresh")
	want := []string{
		"2026-10-31 19:00", "2026-11-01 01:00", "2026-11-01 07:00", "2026-11-01 13:00",
		"2026-11-01 19:00", "2026-11-01 20:00", "2026-11-02 02:00", "2026-11-02 08:00", "2026-11-02 14:00",
	}
	if len(fires) != len(want) {
		t.Fatalf("触发 %d 拍, want %d", len(fires), len(want))
	}
	for i, e := range fires {
		if !e.Scheduled.Equal(parseBJ(want[i])) {
			t.Fatalf("第 %d 拍原定 %s, want %s", i+1, bj(e.Scheduled), want[i])
		}
		if !e.At.Equal(e.Scheduled) {
			t.Fatalf("第 %d 拍存在偏差（模拟驱动应精确到拍）", i+1)
		}
	}
	if gap := fires[5].Scheduled.Sub(fires[4].Scheduled); gap != time.Hour {
		t.Fatalf("秋季跨步 = %s, want 1h（追赶拍）", gap)
	}
	if gap := fires[6].Scheduled.Sub(fires[5].Scheduled); gap != 6*time.Hour {
		t.Fatalf("追赶拍后步进 = %s, want 6h", gap)
	}
	if n := len(log.of(EventSetNext, "price_refresh")); n != len(fires) {
		t.Fatalf("自续约 setnext 事件 %d 次, want %d", n, len(fires))
	}
}

func TestGridDSTSpring(t *testing.T) {
	start := parseBJ("2026-03-07 17:00")
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	if err := s.RegisterInterval("price_refresh", gridNext(start, 6*time.Hour), 6*time.Hour, priceBody, WithMisfireUnlimited()); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(start.Add(48 * time.Hour))

	fires := log.of(EventFire, "price_refresh")
	want := []string{
		"2026-03-07 20:00", "2026-03-08 02:00", "2026-03-08 08:00", "2026-03-08 14:00",
		"2026-03-08 20:00", "2026-03-09 01:00", "2026-03-09 07:00", "2026-03-09 13:00",
	}
	if len(fires) != len(want) {
		t.Fatalf("触发 %d 拍, want %d", len(fires), len(want))
	}
	for i, e := range fires {
		if !e.Scheduled.Equal(parseBJ(want[i])) {
			t.Fatalf("第 %d 拍原定 %s, want %s", i+1, bj(e.Scheduled), want[i])
		}
	}
	// 春季方向：跨步拉长（02:00 锚点网格最后一拍 20:00 → 次日 01:00 新锚点网格，5h），无追赶拍
	for i := 1; i < len(fires); i++ {
		gap := fires[i].Scheduled.Sub(fires[i-1].Scheduled)
		wantGap := 6 * time.Hour
		if i == 5 {
			wantGap = 5 * time.Hour
		}
		if gap != wantGap {
			t.Fatalf("第 %d 拍步进 = %s, want %s", i+1, gap, wantGap)
		}
	}
}

// ---------------------------------------------------------------- 执行超过周期：跳拍序列

func TestBusySkipsTicks(t *testing.T) {
	start := parseBJ("2026-10-31 17:00")
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	body := func(rc *RunCtx) { rc.BusyFor(14 * time.Hour) }
	if err := s.RegisterInterval("heavy", gridNext(start, 6*time.Hour), 6*time.Hour, body, WithMisfireGrace(time.Second)); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(start.Add(24 * time.Hour))

	type step struct {
		kind string
		at   string
	}
	var got []step
	for _, e := range log.job("heavy") {
		got = append(got, step{e.Kind, bj(e.Scheduled)})
	}
	want := []step{
		{EventFire, "2026-10-31 19:00"},
		{EventSkip, "2026-11-01 01:00"},
		{EventSkip, "2026-11-01 07:00"},
		{EventFire, "2026-11-01 13:00"},
	}
	if len(got) != len(want) {
		t.Fatalf("事件序列 %d 条, want %d: %v", len(got), len(want), got)
	}
	for i, w := range want {
		if got[i] != w {
			t.Fatalf("第 %d 事件 = %v, want %v（跳过拍不重排队、不延后网格）", i+1, got[i], w)
		}
	}
}

// ---------------------------------------------------------------- 错过拍：补跑与放弃

func TestMissedTicks(t *testing.T) {
	start := parseBJ("2026-10-31 17:00")
	first := gridNext(start, 6*time.Hour)
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	if err := s.RegisterInterval("catchup", first, 6*time.Hour, emptyBody, WithMisfireUnlimited()); err != nil {
		t.Fatal(err)
	}
	if err := s.RegisterInterval("limited", first, 6*time.Hour, emptyBody, WithMisfireGrace(time.Hour)); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(parseBJ("2026-11-01 07:30"))
	s.SimJump(parseBJ("2026-11-01 17:30")) // 冻结 10h，错过 13:00 拍
	s.RunSim(parseBJ("2026-11-02 03:00"))

	// 无限宽限：恢复瞬间补跑最新一拍，只补一拍
	cu := log.of(EventFire, "catchup")
	if len(cu) != 6 {
		t.Fatalf("catchup 触发 %d 拍, want 6", len(cu))
	}
	if !cu[3].At.Equal(parseBJ("2026-11-01 17:30")) || !cu[3].Scheduled.Equal(parseBJ("2026-11-01 13:00")) {
		t.Fatalf("catchup 第 4 拍 = At %s / 原定 %s, want 17:30 / 13:00", bj(cu[3].At), bj(cu[3].Scheduled))
	}
	if !cu[4].Scheduled.Equal(parseBJ("2026-11-01 19:00")) {
		t.Fatalf("catchup 恢复后下一拍 = %s, want 19:00", bj(cu[4].Scheduled))
	}
	// 有限宽限：错过拍超宽限放弃并出 missed 事件，网格按原样恢复
	lim := log.of(EventFire, "limited")
	miss := log.of(EventMissed, "limited")
	if len(miss) != 1 || !miss[0].Scheduled.Equal(parseBJ("2026-11-01 13:00")) {
		t.Fatalf("limited missed 事件 = %v, want 1 条且原定 13:00", miss)
	}
	if len(lim) != 5 {
		t.Fatalf("limited 触发 %d 拍, want 5（错过拍不补）", len(lim))
	}
	for _, e := range lim {
		if e.At.Equal(parseBJ("2026-11-01 17:30")) {
			t.Fatalf("limited 不应在恢复瞬间补跑")
		}
	}
}

func TestCoalesce(t *testing.T) {
	start := parseBJ("2026-10-31 17:00")
	first := gridNext(start, 6*time.Hour)
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	if err := s.RegisterInterval("merged", first, 6*time.Hour, emptyBody, WithMisfireUnlimited()); err != nil {
		t.Fatal(err)
	}
	if err := s.RegisterInterval("each", first, 6*time.Hour, emptyBody, WithMisfireUnlimited(), WithCoalesce(false)); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(parseBJ("2026-11-01 07:30"))
	s.SimJump(parseBJ("2026-11-01 20:30")) // 冻结 13h，错过 13:00 与 19:00 两拍
	s.RunSim(parseBJ("2026-11-02 03:00"))

	// 合并：两拍并一拍，取最新原定时刻
	m := log.of(EventFire, "merged")
	var mAt []time.Time
	for _, e := range m {
		if e.At.Equal(parseBJ("2026-11-01 20:30")) {
			mAt = append(mAt, e.Scheduled)
		}
	}
	if len(mAt) != 1 || !mAt[0].Equal(parseBJ("2026-11-01 19:00")) {
		t.Fatalf("merged 恢复瞬间触发 %d 次（原定 %v）, want 1 次且原定 19:00", len(mAt), mAt)
	}
	// 逐拍：两拍各补一次，同为恢复瞬间
	ea := log.of(EventFire, "each")
	var eaAt []time.Time
	for _, e := range ea {
		if e.At.Equal(parseBJ("2026-11-01 20:30")) {
			eaAt = append(eaAt, e.Scheduled)
		}
	}
	if len(eaAt) != 2 || !eaAt[0].Equal(parseBJ("2026-11-01 13:00")) || !eaAt[1].Equal(parseBJ("2026-11-01 19:00")) {
		t.Fatalf("each 恢复瞬间触发 %d 次（原定 %v）, want 2 次且为 13:00/19:00", len(eaAt), eaAt)
	}
}

// ---------------------------------------------------------------- SetNext 立即生效

func TestSetNextImmediate(t *testing.T) {
	start := parseBJ("2026-10-31 17:00")
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	body := func(rc *RunCtx) { rc.SetNext(rc.Now().Add(10 * time.Minute)) }
	if err := s.RegisterInterval("re", start.Add(time.Hour), time.Hour, body, WithMisfireGrace(time.Second)); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(start.Add(3 * time.Hour))

	fires := log.of(EventFire, "re")
	if len(fires) == 0 || !fires[0].At.Equal(start.Add(time.Hour)) {
		t.Fatalf("首拍应落在触发器网格 +1h")
	}
	for i := 1; i < len(fires); i++ {
		if gap := fires[i].At.Sub(fires[i-1].At); gap != 10*time.Minute {
			t.Fatalf("第 %d 拍步进 = %s, want 10m（重锚立即生效且早于默认续排）", i+1, gap)
		}
		if n := len(log.of(EventSetNext, "re")); n != len(fires) {
			t.Fatalf("setnext 事件 %d 次, want %d", n, len(fires))
		}
	}
}

func TestSetNextExternal(t *testing.T) {
	start := parseBJ("2026-10-31 17:00")
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	if err := s.RegisterInterval("job", start.Add(time.Hour), time.Hour, emptyBody, WithMisfireGrace(time.Second)); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(start.Add(65 * time.Minute)) // 首拍 +1h 已发生，默认续排 +2h
	if err := s.SetNext("job", start.Add(80*time.Minute)); err != nil {
		t.Fatal(err)
	}
	s.RunSim(start.Add(3 * time.Hour))
	fires := log.of(EventFire, "job")
	// 重锚单次生效：下一拍被改写到 +80min（早于默认 +2h）；此后触发器网格恢复
	want := []string{"2026-10-31 18:00", "2026-10-31 18:20", "2026-10-31 19:00", "2026-10-31 20:00"}
	if len(fires) != len(want) {
		t.Fatalf("外部重锚后触发 %d 拍, want %d", len(fires), len(want))
	}
	for i, e := range fires {
		if !e.At.Equal(parseBJ(want[i])) {
			t.Fatalf("第 %d 拍 = %s, want %s", i+1, bj(e.At), want[i])
		}
	}
	if err := s.SetNext("missing", time.Now()); err == nil {
		t.Fatalf("未注册任务重锚应报错")
	}
}

// ---------------------------------------------------------------- Stop 语义

func TestStopNoWait(t *testing.T) {
	start := parseBJ("2026-10-31 17:00")
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	body := func(rc *RunCtx) { rc.BusyFor(3 * time.Hour) }
	if err := s.RegisterInterval("longrun", start.Add(10*time.Minute), time.Hour, body, WithMisfireGrace(time.Second)); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(start.Add(11 * time.Minute)) // 首拍进入 3h 占用
	s.Stop()

	stops := log.of(EventStop, "")
	if len(stops) != 1 || !strings.Contains(stops[0].Note, "in-flight=1") {
		t.Fatalf("stop 事件 = %v, want 1 条且登记在跑 1", stops)
	}
	before := len(log.evs)
	s.RunSim(start.Add(20 * time.Hour)) // 停机后已排触发作废，零新事件
	s.Stop()                            // 幂等
	if len(log.evs) != before {
		t.Fatalf("停机后新增事件 %d 条, want 0", len(log.evs)-before)
	}
	if err := s.Register("late", IntervalTrigger{Start: start, Period: time.Hour}, emptyBody); err == nil {
		t.Fatalf("停机后注册应报错")
	}
	if err := s.SetNext("longrun", start.Add(time.Hour)); err == nil {
		t.Fatalf("停机后重锚应报错")
	}
}

// ---------------------------------------------------------------- 运行体崩溃隔离

func TestPanicRecovered(t *testing.T) {
	start := parseBJ("2026-10-31 17:00")
	var log evLog
	s := NewScheduler(WithOnEvent(log.on))
	if err := s.RegisterInterval("boom", start.Add(30*time.Minute), 30*time.Minute,
		func(rc *RunCtx) { panic("模拟崩溃") }, WithMisfireGrace(time.Second)); err != nil {
		t.Fatal(err)
	}
	s.StartSim(start)
	s.RunSim(start.Add(time.Hour)) // 两次到点均崩溃，调度不停转
	if n := len(log.of(EventFire, "boom")); n != 2 {
		t.Fatalf("崩溃后仍应续排，触发 %d 拍, want 2", n)
	}
	if n := len(log.of(EventPanic, "boom")); n != 2 {
		t.Fatalf("panic 事件 %d 条, want 2", n)
	}
}

// ---------------------------------------------------------------- 注册与参数校验

func TestRegisterErrors(t *testing.T) {
	s := NewScheduler()
	if err := s.RegisterInterval("j", time.Now(), 0, emptyBody); err == nil {
		t.Fatalf("零周期应报错")
	}
	if err := s.RegisterDaily("j", 24, 0, emptyBody); err == nil {
		t.Fatalf("小时越界应报错")
	}
	if err := s.RegisterDaily("j", 3, 60, emptyBody); err == nil {
		t.Fatalf("分钟越界应报错")
	}
	if err := s.Register("j", IntervalTrigger{Start: time.Now(), Period: time.Hour}, nil); err == nil {
		t.Fatalf("空运行体应报错")
	}
	if err := s.Register("", IntervalTrigger{Start: time.Now(), Period: time.Hour}, emptyBody); err == nil {
		t.Fatalf("空 ID 应报错")
	}
	if err := s.RegisterDaily("ok", 3, 0, emptyBody); err != nil {
		t.Fatal(err)
	}
	if err := s.RegisterDaily("ok", 4, 0, emptyBody); err == nil {
		t.Fatalf("重复 ID 应报错")
	}

	// 运行中注册即时初锚
	var log evLog
	s2 := NewScheduler(WithOnEvent(log.on))
	t0 := parseBJ("2026-10-31 17:00")
	s2.StartSim(t0)
	if err := s2.RegisterInterval("late", t0.Add(30*time.Minute), time.Hour, emptyBody, WithMisfireGrace(time.Second)); err != nil {
		t.Fatal(err)
	}
	s2.RunSim(t0.Add(time.Hour))
	fires := log.of(EventFire, "late")
	if len(fires) != 1 || !fires[0].At.Equal(t0.Add(30*time.Minute)) {
		t.Fatalf("运行中注册应即时初锚，实测 %v", fires)
	}
}
