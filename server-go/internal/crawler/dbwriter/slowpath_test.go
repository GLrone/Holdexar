package dbwriter

// B1 逐款隔离回退 + B2 段前背压的单元测试。
// 注入面：表触发器（RAISE 定向失败）、Writer.Diag（假写闸诊断）。

import (
	"context"
	"database/sql"
	"fmt"
	"strings"
	"testing"
	"time"

	"holdexar/server-go/internal/store"
)

// TestSlowPathFinishingSavepointFailureKeepsChunk B1②：收尾保存点失败只回滚
// 保存点不拖垮主段——ok 款照常提交，第二次调用（注入解除）复活清标生效。
func TestSlowPathFinishingSavepointFailureKeepsChunk(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 7.25}
	if _, err := db.Exec(`INSERT INTO games (appid, name, family_sharing, trading_cards, is_epic, is_hb, hl_flag, pp_flag, diff_fen, is_adult, is_visual_novel, removed_strikes, positive_reviews, review_count, view_count, removed_at)
		VALUES (5001, '下架标记行', 0, 0, 0, 0, 0, 0, 0, 0, 0, 3, 0, 0, 0, '2026-01-01 00:00:00')`); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(`CREATE TRIGGER hx_fail_clear BEFORE UPDATE ON games
		WHEN NEW.removed_at IS NULL AND OLD.removed_at IS NOT NULL
		BEGIN SELECT RAISE(ABORT, 'injected clear failure'); END`); err != nil {
		t.Fatal(err)
	}
	logs := []string{}
	w.Log = func(format string, args ...any) { logs = append(logs, fmt.Sprintf(format, args...)) }

	results, err := w.writeChunkSlow(context.Background(), []BatchEntry{
		{Game: gameData(5001, "下架标记行"), Prices: []PriceRow{priceRow(5001, "CN", 19900)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !results[0] {
		t.Fatalf("收尾保存点失败不应拖垮主段: %v", results)
	}
	if !strings.Contains(strings.Join(logs, "\n"), "收尾保存点失败") {
		t.Fatalf("应记录收尾保存点失败日志: %v", logs)
	}
	var removedAt any
	if err := db.QueryRow("SELECT removed_at FROM games WHERE appid=5001").Scan(&removedAt); err != nil {
		t.Fatal(err)
	}
	if removedAt == nil {
		t.Fatal("收尾保存点失败应已回滚，清标不应生效")
	}
	if _, err := db.Exec("DROP TRIGGER hx_fail_clear"); err != nil {
		t.Fatal(err)
	}
	if results, err = w.writeChunkSlow(context.Background(), []BatchEntry{
		{Game: gameData(5001, "下架标记行"), Prices: []PriceRow{priceRow(5001, "CN", 19900)}},
	}); err != nil || !results[0] {
		t.Fatalf("注入解除后应照常写入: %v %v", results, err)
	}
	if err := db.QueryRow("SELECT removed_at FROM games WHERE appid=5001").Scan(&removedAt); err != nil {
		t.Fatal(err)
	}
	if removedAt != nil {
		t.Fatalf("复活清标应生效: %v", removedAt)
	}
}

// TestSlowPathSegmentFailureOnlyVoidOwnChunk B1③：段级异常只作废本段——
// 触发器 RAISE(ROLLBACK) 连根回滚外层事务，本段整段作废，后续段照常落库。
func TestSlowPathSegmentFailureOnlyVoidOwnChunk(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 7.25}
	if _, err := db.Exec(`CREATE TRIGGER hx_seg_fail BEFORE INSERT ON game_current_prices
		WHEN NEW.appid = 6002
		BEGIN SELECT RAISE(ROLLBACK, 'injected segment failure'); END`); err != nil {
		t.Fatal(err)
	}
	entries := []BatchEntry{
		{Game: gameData(6001, "好行一"), Prices: []PriceRow{priceRow(6001, "CN", 19900)}},
		{Game: gameData(6002, "毒行"), Prices: []PriceRow{priceRow(6002, "CN", 9900)}},
		{Game: gameData(6003, "好行二"), Prices: []PriceRow{priceRow(6003, "CN", 8800)}},
	}
	for id := 6100; id < 6250; id++ {
		entries = append(entries, BatchEntry{Game: gameData(int64(id), "后续段"), Prices: []PriceRow{priceRow(int64(id), "CN", 1000)}})
	}
	results, err := w.writeChunkSlow(context.Background(), entries)
	if err != nil {
		t.Fatalf("段级失败应只作废本段不上抛: %v", err)
	}
	for i := 0; i < 3; i++ {
		if results[i] {
			t.Fatalf("首段（含毒行）应整段作废，第 %d 款不应为 true", i)
		}
	}
	for i := 100; i < len(entries); i++ {
		if !results[i] {
			t.Fatalf("后续段应照常落库，第 %d 款不应为 false", i)
		}
	}
	var first, second int
	if err := db.QueryRow("SELECT COUNT(*) FROM game_current_prices WHERE appid IN (6001,6002,6003)").Scan(&first); err != nil {
		t.Fatal(err)
	}
	if err := db.QueryRow("SELECT COUNT(*) FROM game_current_prices WHERE appid >= 6100").Scan(&second); err != nil {
		t.Fatal(err)
	}
	if first != 0 || second != 53 {
		t.Fatalf("首段应零落库、第二段（53 款）应照常落库: first=%d second=%d", first, second)
	}
}

// TestSlowPathHandsWriterSlotBetweenChunks B1④：段间交还写者位——交互写
// 在段间可插入（慢路径两段之间，交互写完整跑完且不被饿死）。
func TestSlowPathHandsWriterSlotBetweenChunks(t *testing.T) {
	w, db := newTestWriter(t)
	w.Now = testNow
	w.FxRates = map[string]float64{"USD": 7.25}
	if _, err := db.Exec("CREATE TABLE probe_slots (id INTEGER PRIMARY KEY, tag TEXT)"); err != nil {
		t.Fatal(err)
	}
	entries := []BatchEntry{}
	for id := int64(7000); id < 7150; id++ {
		entries = append(entries, BatchEntry{Game: gameData(id, "分段"), Prices: []PriceRow{priceRow(id, "CN", 1000)}})
	}
	paced := 0
	interactiveDone := make(chan error, 1)
	var interactiveErr error
	w.Diag = func(ctx context.Context) map[string]any {
		paced++
		if paced == 2 && interactiveDone != nil {
			// 第二段起闸前：写者位已交还——交互写在此窗口完整执行
			go func() {
				interactiveDone <- w.Gate.WithWrite(context.Background(), store.PriorityInteractive, func(conn *sql.Conn) error {
					_, err := conn.ExecContext(context.Background(), "INSERT INTO probe_slots (tag) VALUES ('interactive')")
					return err
				})
			}()
			select {
			case interactiveErr = <-interactiveDone:
				interactiveDone = nil
			case <-time.After(2 * time.Second):
			}
		}
		return map[string]any{"waiting_background": 0}
	}
	results, err := w.writeChunkSlow(context.Background(), entries)
	if err != nil {
		t.Fatal(err)
	}
	for i, ok := range results {
		if !ok {
			t.Fatalf("第 %d 款应落库: %v", i, results)
		}
	}
	if interactiveErr != nil {
		t.Fatalf("段间交互写未完成: %v", interactiveErr)
	}
	var marked int
	if err := db.QueryRow("SELECT COUNT(*) FROM probe_slots WHERE tag='interactive'").Scan(&marked); err != nil {
		t.Fatal(err)
	}
	if marked != 1 {
		t.Fatalf("交互写应恰落一行: %d", marked)
	}
}

// TestBackpressurePaceSleepsOnDeepQueue B2：积压超阈值按深度线性退避。
func TestBackpressurePaceSleepsOnDeepQueue(t *testing.T) {
	w := &Writer{Log: func(string, ...any) {}}
	w.Diag = func(context.Context) map[string]any {
		return map[string]any{"waiting_background": 40}
	}
	backpressureActive.Store(false)
	start := time.Now()
	w.backpressurePace(context.Background())
	elapsed := time.Since(start)
	if elapsed < 350*time.Millisecond || elapsed > 900*time.Millisecond {
		t.Fatalf("深度 40 应退避约 0.4s，实际 %v", elapsed)
	}
}

// TestBackpressurePaceIdleUnderThreshold B2：深度≤阈值零退避。
func TestBackpressurePaceIdleUnderThreshold(t *testing.T) {
	w := &Writer{Log: func(string, ...any) {}}
	w.Diag = func(context.Context) map[string]any {
		return map[string]any{"waiting_background": 10}
	}
	backpressureActive.Store(false)
	start := time.Now()
	w.backpressurePace(context.Background())
	if time.Since(start) > 50*time.Millisecond {
		t.Fatalf("深度低于阈值不应退避: %v", time.Since(start))
	}
}

// TestBackpressurePaceLogsEntryAndExitOnce B2：进/退场日志各一条（atomic 去重）。
func TestBackpressurePaceLogsEntryAndExitOnce(t *testing.T) {
	logs := []string{}
	w := &Writer{Log: func(format string, args ...any) { logs = append(logs, fmt.Sprintf(format, args...)) }}
	depth := 40
	w.Diag = func(context.Context) map[string]any {
		return map[string]any{"waiting_background": depth}
	}
	backpressureActive.Store(false)
	for i := 0; i < 3; i++ {
		w.backpressurePace(context.Background())
	}
	depth = 10
	for i := 0; i < 3; i++ {
		w.backpressurePace(context.Background())
	}
	joined := strings.Join(logs, "\n")
	if got := strings.Count(joined, "背压生效"); got != 1 {
		t.Fatalf("进场日志应恰一条: %d\n%v", got, logs)
	}
	if got := strings.Count(joined, "积压回落"); got != 1 {
		t.Fatalf("退场日志应恰一条: %d\n%v", got, logs)
	}
}
