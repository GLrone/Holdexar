package proxypool

import "testing"

func TestStateTransitionsLegal(t *testing.T) {
	legal := [][2]string{
		{NodeNew, NodeActive}, {NodeNew, NodeDead}, {NodeNew, NodeStale},
		{NodeActive, NodeStale}, {NodeActive, NodeDead},
		{NodeStale, NodeActive}, {NodeStale, NodeDead}, {NodeStale, NodeRetired},
		{NodeDead, NodeActive}, {NodeDead, NodeStale}, {NodeDead, NodeRetired},
		{NodeRetired, NodeActive}, {NodeRetired, NodeStale},
	}
	for _, pair := range legal {
		got, err := ApplyTransition(pair[0], pair[1])
		if err != nil || got != pair[1] {
			t.Fatalf("%s → %s 应合法，got %v err %v", pair[0], pair[1], got, err)
		}
	}
}

func TestStateTransitionsIllegal(t *testing.T) {
	illegal := [][2]string{
		{NodeNew, NodeRetired}, // 退休必须先离开运行集
		{NodeActive, NodeRetired},
		{NodeActive, NodeNew},
		{NodeRetired, NodeDead},
		{"UNKNOWN", NodeActive},
	}
	for _, pair := range illegal {
		_, err := ApplyTransition(pair[0], pair[1])
		var ite *IllegalTransitionError
		if !asIllegal(err, &ite) {
			t.Fatalf("%s → %s 应拒绝，got %v", pair[0], pair[1], err)
		}
		if ite.Current != pair[0] || ite.Target != pair[1] {
			t.Fatalf("错误未携带起点/终点：%+v", ite)
		}
	}
}

func asIllegal(err error, target **IllegalTransitionError) bool {
	if e, ok := err.(*IllegalTransitionError); ok {
		*target = e
		return true
	}
	return false
}

func boolPtr(b bool) *bool { return &b }

func TestEvaluateNodeStateProbeTrue(t *testing.T) {
	cases := []struct {
		current    string
		sourceSeen bool
		want       string
	}{
		{NodeNew, true, NodeActive},
		{NodeNew, false, NodeStale},
		{NodeActive, true, NodeActive},
		{NodeActive, false, NodeStale},
		{NodeDead, true, NodeActive},    // 体检成功是最强证据：DEAD 直接回运行集
		{NodeRetired, true, NodeActive}, // 新的健康成功证据让 RETIRED 回 ACTIVE/STALE
		{NodeRetired, false, NodeStale},
	}
	for _, c := range cases {
		got, err := EvaluateNodeState(c.current, EvaluateNodeStateInput{
			SourceSeen: c.sourceSeen, ProbeOK: boolPtr(true),
		})
		if err != nil || got != c.want {
			t.Fatalf("%s source=%v: want %s got %s err %v", c.current, c.sourceSeen, c.want, got, err)
		}
	}
}

func TestEvaluateNodeStateProbeFalse(t *testing.T) {
	// 未达阈值：一律 DEAD（从 RETIRED 之外的任意态）
	got, err := EvaluateNodeState(NodeActive, EvaluateNodeStateInput{ProbeOK: boolPtr(false), ConsecutiveFailures: 2})
	if err != nil || got != NodeDead {
		t.Fatalf("ACTIVE 失败 2 次应 DEAD，got %s err %v", got, err)
	}
	// 达阈值且在 STALE/DEAD：RETIRED
	got, err = EvaluateNodeState(NodeStale, EvaluateNodeStateInput{ProbeOK: boolPtr(false), ConsecutiveFailures: 3})
	if err != nil || got != NodeRetired {
		t.Fatalf("STALE 失败 3 次应 RETIRED，got %s err %v", got, err)
	}
	got, err = EvaluateNodeState(NodeDead, EvaluateNodeStateInput{ProbeOK: boolPtr(false), ConsecutiveFailures: 5})
	if err != nil || got != NodeRetired {
		t.Fatalf("DEAD 失败 5 次应 RETIRED，got %s err %v", got, err)
	}
	// RETIRED 停留（失败证据不改变退休结论）
	got, err = EvaluateNodeState(NodeRetired, EvaluateNodeStateInput{ProbeOK: boolPtr(false), ConsecutiveFailures: 99})
	if err != nil || got != NodeRetired {
		t.Fatalf("RETIRED 失败应停留，got %s err %v", got, err)
	}
	// NEW 失败未达阈值：DEAD（NEW 不在退休起点集）
	got, err = EvaluateNodeState(NodeNew, EvaluateNodeStateInput{ProbeOK: boolPtr(false), ConsecutiveFailures: 99})
	if err != nil || got != NodeDead {
		t.Fatalf("NEW 失败应 DEAD（不允许直跳 RETIRED），got %s err %v", got, err)
	}
}

func TestEvaluateNodeStateNoProbe(t *testing.T) {
	// 有来源：NEW/ACTIVE → ACTIVE；DEAD/RETIRED/STALE 原地
	cases := []struct {
		current string
		seen    bool
		want    string
	}{
		{NodeNew, true, NodeActive},
		{NodeActive, true, NodeActive},
		{NodeDead, true, NodeDead}, // 未经体检不凭来源证据跃回运行权重
		{NodeRetired, true, NodeRetired},
		{NodeActive, false, NodeStale}, // 通过过体检的才降级
		{NodeStale, false, NodeStale},
		{NodeNew, false, NodeDead}, // 无来源又无体检证据不进池
		{NodeDead, false, NodeDead},
		{NodeRetired, false, NodeRetired},
	}
	for _, c := range cases {
		got, err := EvaluateNodeState(c.current, EvaluateNodeStateInput{SourceSeen: c.seen, ProbeOK: nil})
		if err != nil || got != c.want {
			t.Fatalf("%s seen=%v: want %s got %s err %v", c.current, c.seen, c.want, got, err)
		}
	}
}

func TestEvaluateNodeStateCustomThreshold(t *testing.T) {
	if _, err := EvaluateNodeState(NodeDead, EvaluateNodeStateInput{ProbeOK: boolPtr(false), RetireAfterFailedProbes: 0}); err != nil {
		t.Fatalf("阈值 0 应回落缺省，err %v", err)
	}
	if _, err := EvaluateNodeState(NodeDead, EvaluateNodeStateInput{ProbeOK: boolPtr(false), RetireAfterFailedProbes: -1}); err == nil {
		t.Fatal("阈值 -1 应报错")
	}
	got, err := EvaluateNodeState(NodeDead, EvaluateNodeStateInput{
		ProbeOK: boolPtr(false), ConsecutiveFailures: 2, RetireAfterFailedProbes: 2,
	})
	if err != nil || got != NodeRetired {
		t.Fatalf("自定义阈值 2：失败 2 次应 RETIRED，got %s err %v", got, err)
	}
}
