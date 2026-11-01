package proxypool

// 体检运行行编排测试：建轮即提交、收尾汇总、scheduled 读点、进度事件发布。

import (
	"context"
	"database/sql"
	"testing"
	"time"

	"holdexar/server-go/internal/events"
)

func TestStartHealthRunCommitsImmediately(t *testing.T) {
	db, gate, _ := poolTestDB(t)
	ctx := context.Background()
	now := time.Now()
	runID, err := StartHealthRun(ctx, gate, DefaultScheduleChannel, nil, now)
	if err != nil {
		t.Fatal(err)
	}
	if runID == 0 {
		t.Fatal("应返回运行行 id")
	}
	// 建轮即提交：闸外读可见（探测的网络段在闸外进行时，台账已在）
	var channel string
	var startedAt sql.NullString
	if err := db.QueryRow(`SELECT channel, CAST(started_at AS TEXT) FROM health_runs WHERE id = ?`, runID).
		Scan(&channel, &startedAt); err != nil {
		t.Fatal(err)
	}
	if channel != "scheduled" {
		t.Fatalf("通道应为 scheduled，实际 %s", channel)
	}
	if startedAt.String != pyTimeText(now) {
		t.Fatalf("started_at 应 %s，实际 %s", pyTimeText(now), startedAt.String)
	}
}

func TestFinishHealthRunSummary(t *testing.T) {
	db, gate, _ := poolTestDB(t)
	ctx := context.Background()
	started := time.Now()
	runID, err := StartHealthRun(ctx, gate, "manual", ptrInt64(7), started)
	if err != nil {
		t.Fatal(err)
	}
	finished := started.Add(3 * time.Second)
	if err := FinishHealthRun(ctx, gate, runID, HealthRunSummary{
		Total: 10, SteamOK: 8, IPKnown: 9, Failed: 2, EndReason: "l1_error",
	}, finished, 3000); err != nil {
		t.Fatal(err)
	}
	var total, steamOK, ipKnown, failed, durationMS int64
	var endReason sql.NullString
	if err := db.QueryRow(`SELECT total, steam_ok, ip_known, failed, end_reason, duration_ms
		FROM health_runs WHERE id = ?`, runID).
		Scan(&total, &steamOK, &ipKnown, &failed, &endReason, &durationMS); err != nil {
		t.Fatal(err)
	}
	if total != 10 || steamOK != 8 || ipKnown != 9 || failed != 2 || durationMS != 3000 {
		t.Fatalf("汇总不对：total=%d steam_ok=%d ip_known=%d failed=%d duration=%d",
			total, steamOK, ipKnown, failed, durationMS)
	}
	if endReason.String != "l1_error" {
		t.Fatalf("end_reason 应 l1_error，实际 %q", endReason.String)
	}
}

func TestCurrentMaintenanceRunReadPoint(t *testing.T) {
	db, gate, dir := poolTestDB(t)
	ctx := context.Background()
	if err := EnsureHealthSchema(ctx, mustConn(t, db)); err != nil {
		t.Fatal(err)
	}

	// 无运行行 → nil
	view, err := CurrentMaintenanceRun(ctx, db, dir)
	if err != nil {
		t.Fatal(err)
	}
	if view != nil {
		t.Fatalf("无进行中运行应返回 nil，实际 %+v", view)
	}

	started := time.Now()
	runID, err := StartHealthRun(ctx, gate, DefaultScheduleChannel, nil, started)
	if err != nil {
		t.Fatal(err)
	}
	seedNode(t, db, "n1", NodeActive, 0, int64(1))
	// 观测行：L1×1 成功、L2×2（一成功一失败）
	mustInsertObservation(t, db, "fp-n1", ProbeLevelL1, true, runID)
	mustInsertObservation(t, db, "fp-n1", ProbeLevelL2, true, runID)
	mustInsertObservation(t, db, "fp-n1", ProbeLevelL2, false, runID)

	view, err = CurrentMaintenanceRun(ctx, db, dir)
	if err != nil {
		t.Fatal(err)
	}
	if view == nil {
		t.Fatal("进行中运行应可见")
	}
	if view.RunID != runID || view.Channel != "scheduled" {
		t.Fatalf("运行行投影不对：%+v", view)
	}
	if view.ProbedL1 != 1 || view.ProbedL2 != 2 {
		t.Fatalf("逐层计数应 l1=1 l2=2，实际 %+v", view)
	}
	// totals 来自池文件读数（无池文件 = 0）
	if view.TotalL1 != 0 || view.TotalL2 != 0 {
		t.Fatalf("无池文件 totals 应为 0，实际 %+v", view)
	}
	// startedAt 是 isoformat 形态
	if want := pyIsoFormat(pyTimeText(started)); view.StartedAt != want {
		t.Fatalf("startedAt 应 %s，实际 %s", want, view.StartedAt)
	}

	// manual 通道运行行不被本读点负责
	manualID, err := StartHealthRun(ctx, gate, "manual", nil, started)
	if err != nil {
		t.Fatal(err)
	}
	view2, _ := CurrentMaintenanceRun(ctx, db, dir)
	if view2 == nil || view2.RunID != runID {
		t.Fatalf("manual 运行不得顶替 scheduled 读点：%+v", view2)
	}

	// 已收尾 → nil
	if err := FinishHealthRun(ctx, gate, runID, HealthRunSummary{Total: 2}, time.Now(), 100); err != nil {
		t.Fatal(err)
	}
	view3, _ := CurrentMaintenanceRun(ctx, db, dir)
	if view3 != nil {
		t.Fatalf("收尾后应返回 nil，实际 %+v", view3)
	}
	_ = manualID
}

func mustInsertObservation(t *testing.T, db *sql.DB, nodeID, level string, ok bool, runID int64) {
	t.Helper()
	if _, err := db.Exec(`INSERT INTO health_observations
		(node_id, level, ok, observed_at, run_id, channel) VALUES (?, ?, ?, ?, ?, 'scheduled')`,
		nodeID, level, boolInt(ok), pyTimeText(time.Now()), runID); err != nil {
		t.Fatal(err)
	}
}

func ptrInt64(v int64) *int64 { return &v }

func TestPublishProbeProgressGated(t *testing.T) {
	bus := events.NewBus()
	sub := bus.Subscribe()
	defer bus.Unsubscribe(sub)

	// 预约位缺省关：不发布
	total := 10
	PublishProbeProgress(bus, ProbeLevelL1, 3, &total)
	if _, err := sub.Get(testCtx(150 * time.Millisecond)); err == nil {
		t.Fatal("预约位关闭不得发布")
	}

	orig := MaintenanceHealthActive
	defer func() { MaintenanceHealthActive = orig }()
	MaintenanceHealthActive = func() bool { return true }

	PublishProbeProgress(bus, ProbeLevelL1, 3, &total)
	ev, err := sub.Get(testCtx(200 * time.Millisecond))
	if err != nil {
		t.Fatalf("应收到进度事件：%v", err)
	}
	if ev.Type != "maintenance.progress" {
		t.Fatalf("事件类型应 maintenance.progress，实际 %s", ev.Type)
	}
	got := map[string]any{}
	for _, kv := range ev.Data {
		got[kv.Key] = kv.Val
	}
	if got["kind"] != MaintenanceHealthKind || got["level"] != ProbeLevelL1 {
		t.Fatalf("载荷不对：%v", got)
	}
	if got["probed"] != 3 || got["total"] != 10 {
		t.Fatalf("进度数值不对：%v", got)
	}

	// total 缺席：不下发 total 键
	PublishProbeProgress(bus, ProbeLevelL2, 5, nil)
	ev2, err := sub.Get(testCtx(200 * time.Millisecond))
	if err != nil {
		t.Fatalf("应收到第二帧：%v", err)
	}
	for _, kv := range ev2.Data {
		if kv.Key == "total" {
			t.Fatal("total 缺席时不得下发 total 键")
		}
	}

	// bus 为 nil：静默
	PublishProbeProgress(nil, ProbeLevelL1, 1, &total)
}

// testCtx 测试用短超时上下文（到期即释放，避免 vet lostcancel）。
func testCtx(d time.Duration) context.Context {
	ctx, cancel := context.WithTimeout(context.Background(), d)
	go func() {
		<-ctx.Done()
		cancel()
	}()
	return ctx
}
