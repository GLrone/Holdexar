package crawler

import (
	"context"
	"errors"
	"testing"
	"time"
)

// ── 熔断退避序列 ─────────────────────────────────────────────────

func TestBreakerBackoffSequence(t *testing.T) {
	b := NewBreaker429()
	b.clock = newAdvancingClock()
	want := []time.Duration{
		5 * time.Second, 10 * time.Second, 20 * time.Second,
		40 * time.Second, 60 * time.Second, 60 * time.Second,
	}
	for i, w := range want {
		b.Trip()
		if got := b.Cooldown(); got != w {
			t.Fatalf("第 %d 次熔断冷却应为 %v，实际 %v", i+1, w, got)
		}
		if !b.Tripped() {
			t.Fatalf("第 %d 次熔断后应处于熔断态", i+1)
		}
	}
}

func TestBreakerResetRestartsSequence(t *testing.T) {
	b := NewBreaker429()
	b.clock = newAdvancingClock()
	b.Trip()
	b.Trip()
	if b.Cooldown() != 10*time.Second {
		t.Fatalf("连续两次熔断冷却应为 10s，实际 %v", b.Cooldown())
	}
	b.Reset()
	if b.Tripped() {
		t.Fatalf("复位后不应处于熔断态")
	}
	b.Trip()
	if b.Cooldown() != Base429Cooldown {
		t.Fatalf("复位后首次熔断应从基础冷却重新起步，实际 %v", b.Cooldown())
	}
}

func TestBreakerWaitTrippedCooldown(t *testing.T) {
	clk := newAdvancingClock()
	b := NewBreaker429()
	b.clock = clk
	// 未熔断：即取即发
	if err := b.WaitTripped(context.Background()); err != nil {
		t.Fatalf("未熔断不应等待: %v", err)
	}
	// 已冷却 2s 后进入等待：只等剩余 3s
	b.Trip()
	clk.mu.Lock()
	clk.now = clk.now.Add(2 * time.Second)
	clk.mu.Unlock()
	if err := b.WaitTripped(context.Background()); err != nil {
		t.Fatalf("冷却等待失败: %v", err)
	}
	if clk.sleepCount() != 1 || clk.sleeps[0] != 3*time.Second {
		t.Fatalf("应只等剩余冷却 3s，实际 %v", clk.sleeps)
	}
	// 到期放行试探：不自动复位
	if !b.Tripped() {
		t.Fatalf("冷却到期放行试探，不应自动复位")
	}
	// 已过冷却：不再等待
	if err := b.WaitTripped(context.Background()); err != nil {
		t.Fatalf("过冷却期等待失败: %v", err)
	}
	if clk.sleepCount() != 1 {
		t.Fatalf("过冷却期不应再等待")
	}
	// 试探再失败：连续次数继续累加，冷却翻倍
	b.Trip()
	if b.Cooldown() != 10*time.Second {
		t.Fatalf("试探失败后冷却应翻倍到 10s，实际 %v", b.Cooldown())
	}
}

func TestBreakerWaitCancel(t *testing.T) {
	clk := newManualClock()
	b := NewBreaker429()
	b.clock = clk
	b.Trip()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- b.WaitTripped(ctx) }()
	// 等待者应停在冷却等待里（冷却未到不返回）
	select {
	case err := <-done:
		t.Fatalf("冷却未到不应返回，实际 %v", err)
	case <-time.After(50 * time.Millisecond):
	}
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatalf("冷却等待应可被取消，实际 %v", err)
	}
}

// ── 按出口分账 ───────────────────────────────────────────────────

func TestExitBreakersIsolation(t *testing.T) {
	fallback := NewBreaker429()
	exits := NewExitBreakers([]string{"a", "a", "b"}, fallback)
	if exits.ExitCount() != 2 {
		t.Fatalf("重复出口键应只建一个熔断器，实际 %d", exits.ExitCount())
	}
	exits.ForExit("a").Trip()
	if exits.ForExit("a").Tripped() != true {
		t.Fatalf("出口 a 熔断后应处于熔断态")
	}
	if exits.ForExit("b").Tripped() {
		t.Fatalf("出口 b 不应受 a 熔断影响")
	}
	if exits.ForExit("unknown") != fallback {
		t.Fatalf("未知出口键应回落共享实例")
	}
	exits2 := NewExitBreakers(nil, fallback)
	if exits2.ForExit("") != fallback || exits2.ExitCount() != 0 {
		t.Fatalf("空账本应全部回落共享实例")
	}
}

// ── 幻觉 429 形状判定 ────────────────────────────────────────────

func TestAppDetailsPhantom429(t *testing.T) {
	cases := []struct {
		name  string
		body  string
		appid string
		want  bool
	}{
		{"带 appid 的幻觉 429", `{"730": {"success": true, "data": {"price": 1}}}`, "730", true},
		{"不带 appid 的幻觉 429", `{"success": true, "data": {"price": 1}}`, "", true},
		{"仅 success 不足判幻觉", `{"success": true}`, "", false},
		{"data 非对象不足判幻觉", `{"success": true, "data": "x"}`, "", false},
		{"StoreBrowse 形状不匹配", `{"response": {"success": true, "data": {}}}`, "", false},
		{"appid 不在载荷中", `{"success": true, "data": {}}`, "730", false},
		{"非 JSON 体", `<html>429</html>`, "730", false},
		{"JSON 数组体不匹配", `[1,2,3]`, "", false},
		{"success 为假", `{"730": {"success": false, "data": {}}}`, "730", false},
	}
	for _, tc := range cases {
		_, got := AppDetailsPhantom429([]byte(tc.body), tc.appid)
		if got != tc.want {
			t.Fatalf("%s：判定应为 %v，实际 %v", tc.name, tc.want, got)
		}
	}
}
