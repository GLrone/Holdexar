package proxypool

// 节点生命周期状态机：状态常量 + 显式转移表 + 唯一出口（Python state.py 对齐）。
// 调用方不得直接改节点 state，一律经 ApplyTransition / EvaluateNodeState。

import (
	"fmt"
	"sort"
	"strings"
)

const (
	NodeNew     = "NEW"
	NodeActive  = "ACTIVE"
	NodeStale   = "STALE"
	NodeDead    = "DEAD"
	NodeRetired = "RETIRED"
)

// DefaultRetireAfterFailedProbes 退休阈值缺省（业务侧一律显式传参）。
const DefaultRetireAfterFailedProbes = 3

// retireFrom 允许「失败累计 → 退休」的起点：必须先离开运行集 NEW/ACTIVE。
var retireFrom = map[string]bool{NodeStale: true, NodeDead: true}

var nodeTransitions = map[string]map[string]bool{
	NodeNew:     {NodeActive: true, NodeDead: true, NodeStale: true},
	NodeActive:  {NodeStale: true, NodeDead: true},
	NodeStale:   {NodeActive: true, NodeDead: true, NodeRetired: true},
	NodeDead:    {NodeActive: true, NodeStale: true, NodeRetired: true},
	NodeRetired: {NodeActive: true, NodeStale: true},
}

// IllegalTransitionError 非法状态转移（携带起点、终点与全部合法目标）。
type IllegalTransitionError struct {
	Current string
	Target  string
	Allowed []string
}

func (e *IllegalTransitionError) Error() string {
	shown := make([]string, len(e.Allowed))
	copy(shown, e.Allowed)
	sort.Strings(shown)
	if len(shown) == 0 {
		shown = []string{"（无：起点未知）"}
	}
	return fmt.Sprintf("非法状态转移 %s → %s；%s 的合法目标为 %s",
		e.Current, e.Target, e.Current, strings.Join(shown, "、"))
}

// LegalTargets 该起点的全部合法目标集（未知起点 → 空）。
func LegalTargets(current string) []string {
	var out []string
	for t := range nodeTransitions[current] {
		out = append(out, t)
	}
	return out
}

// ApplyTransition 节点状态变更唯一出口；非法组合返回错误。
func ApplyTransition(current, next string) (string, error) {
	allowed := nodeTransitions[current]
	if !allowed[next] {
		var targets []string
		for t := range allowed {
			targets = append(targets, t)
		}
		return current, &IllegalTransitionError{Current: current, Target: next, Allowed: targets}
	}
	return next, nil
}

// pySettle 原地不动视为合法空转，其余必须走合法转移。
func pySettle(current, target string) (string, error) {
	if target == current {
		return current, nil
	}
	return ApplyTransition(current, target)
}

// EvaluateNodeStateInput 两路证据输入。
type EvaluateNodeStateInput struct {
	SourceSeen              bool
	ProbeOK                 *bool // nil = 无探测证据，只按来源判
	ConsecutiveFailures     int
	RetireAfterFailedProbes int
}

// EvaluateNodeState 按「来源 + 体检」两路证据算目标态；结果必然合法转移。
func EvaluateNodeState(current string, in EvaluateNodeStateInput) (string, error) {
	retireAfter := in.RetireAfterFailedProbes
	if retireAfter == 0 {
		retireAfter = DefaultRetireAfterFailedProbes
	}
	if retireAfter < 1 {
		return "", fmt.Errorf("retire_after_failed_probes 必须 >= 1，实际 %d", retireAfter)
	}

	if in.ProbeOK == nil {
		var target string
		switch {
		case in.SourceSeen:
			target = current
			if current == NodeNew || current == NodeActive {
				target = NodeActive
			}
		case current == NodeActive || current == NodeStale:
			target = NodeStale
		case current == NodeRetired:
			target = NodeRetired
		default: // NEW / DEAD：无来源又无体检证据，不进池
			target = NodeDead
		}
		return pySettle(current, target)
	}

	if *in.ProbeOK {
		next := NodeStale
		if in.SourceSeen {
			next = NodeActive
		}
		return pySettle(current, next)
	}

	if current == NodeRetired {
		return current, nil
	}
	target := NodeDead
	if in.ConsecutiveFailures >= retireAfter && retireFrom[current] {
		target = NodeRetired
	}
	return pySettle(current, target)
}
