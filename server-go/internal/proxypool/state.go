package proxypool

// 节点状态机唯一出口：禁止直接改 state，一律经 ApplyTransition / EvaluateNodeState。

import (
	"fmt"
	"slices"
	"strings"
)

const (
	NodeNew     = "NEW"
	NodeActive  = "ACTIVE"
	NodeStale   = "STALE"
	NodeDead    = "DEAD"
	NodeRetired = "RETIRED"
)

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

type IllegalTransitionError struct {
	Current string
	Target  string
	Allowed []string
}

func (e *IllegalTransitionError) Error() string {
	shown := make([]string, len(e.Allowed))
	copy(shown, e.Allowed)
	slices.Sort(shown)
	if len(shown) == 0 {
		shown = []string{"（无：起点未知）"}
	}
	return fmt.Sprintf("非法状态转移 %s → %s；%s 的合法目标为 %s",
		e.Current, e.Target, e.Current, strings.Join(shown, "、"))
}

func LegalTargets(current string) []string {
	var out []string
	for t := range nodeTransitions[current] {
		out = append(out, t)
	}
	return out
}

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

func pySettle(current, target string) (string, error) {
	if target == current {
		return current, nil
	}
	return ApplyTransition(current, target)
}

type EvaluateNodeStateInput struct {
	SourceSeen              bool
	ProbeOK                 *bool
	ConsecutiveFailures     int
	RetireAfterFailedProbes int
}

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
		default:
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
