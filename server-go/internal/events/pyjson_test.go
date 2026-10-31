package events

import (
	"math"
	"strings"
	"testing"
)

func TestAsSSEFrameShape(t *testing.T) {
	cases := []struct {
		name string
		ev   Event
		want string
	}{
		{
			name: "状态事件",
			ev: Event{
				Type: "job.started", Ts: 1730367300.123456,
				Data: []KV{{"job_id", 5}, {"scope", "appids"}, {"count", 3},
					{"regions", []any{"CN", "US"}}},
			},
			want: "event: job.started\ndata: {\"ts\": 1730367300.123456, \"job_id\": 5, " +
				"\"scope\": \"appids\", \"count\": 3, \"regions\": [\"CN\", \"US\"]}\n\n",
		},
		{
			name: "进度事件含中文与浮点",
			ev: Event{
				Type: "crawl.progress", Ts: 1730367300.0,
				Data: []KV{{"done", 12}, {"ok", 11}, {"fail", 1}, {"total", 100},
					{"qsize", 0}, {"speed", 0.55}},
			},
			want: "event: crawl.progress\ndata: {\"ts\": 1730367300.0, \"done\": 12, " +
				"\"ok\": 11, \"fail\": 1, \"total\": 100, \"qsize\": 0, \"speed\": 0.55}\n\n",
		},
		{
			name: "嵌套对象与布尔",
			ev: Event{
				Type: "maintenance.started", Ts: 1730367300.5,
				Data: []KV{{"kind", "health"}, {"totals", []KV{{"l1", 3}, {"ok", true}}}},
			},
			want: "event: maintenance.started\ndata: {\"ts\": 1730367300.5, \"kind\": \"health\", " +
				"\"totals\": {\"l1\": 3, \"ok\": true}}\n\n",
		},
		{
			name: "空载荷",
			ev:   Event{Type: "crawl.idle", Ts: 42.0},
			want: "event: crawl.idle\ndata: {\"ts\": 42.0}\n\n",
		},
		{
			name: "载荷 ts 键覆盖事件时刻（字典字面量后键覆盖语义）",
			ev: Event{
				Type: "job.status", Ts: 1.0,
				Data: []KV{{"status", "stopped"}, {"ts", 99}},
			},
			want: "event: job.status\ndata: {\"ts\": 99, \"status\": \"stopped\"}\n\n",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := c.ev.AsSSE(); got != c.want {
				t.Fatalf("帧不一致\n got: %q\nwant: %q", got, c.want)
			}
		})
	}
}

func TestPyJSONStringEscapes(t *testing.T) {
	cases := []struct{ in, want string }{
		{"hello", `"hello"`},
		{"中文保持原样", `"中文保持原样"`},
		{"引号\"与\\斜杠", `"引号\"与\\斜杠"`},
		{"换行\n制\t表", `"换行\n制\t表"`},
		{"\x01\x1f", `"\u0001\u001f"`},
		{"😀", `"😀"`},
	}
	for _, c := range cases {
		if got := PyJSONString(c.in); got != c.want {
			t.Fatalf("PyJSONString(%q) = %s, want %s", c.in, got, c.want)
		}
	}
}

func TestPyFloatRepr(t *testing.T) {
	cases := []struct {
		in   float64
		want string
	}{
		{0.5, "0.5"},
		{0.1, "0.1"},
		{2.0, "2.0"},
		{math.Copysign(0, -1), "-0.0"},
		{0.001, "0.001"},
		{0.0001, "0.0001"},
		{0.00001, "1e-05"},
		{1.5e-5, "1.5e-05"},
		{1e15, "1000000000000000.0"},
		{1e16, "1e+16"},
		{1e100, "1e+100"},
		{123456.789, "123456.789"},
		{1730367300.123456, "1730367300.123456"},
		{-42.25, "-42.25"},
	}
	for _, c := range cases {
		var sb strings.Builder
		pyFloat(&sb, c.in)
		if sb.String() != c.want {
			t.Fatalf("pyFloat(%v) = %s, want %s", c.in, sb.String(), c.want)
		}
	}
}

func TestPyJSONPayloadTypes(t *testing.T) {
	// map 兜底按键排序、list 混型、null
	var b strings.Builder
	pyValue(&b, map[string]any{"b": 1, "a": nil})
	if b.String() != `{"a": null, "b": 1}` {
		t.Fatalf("map 渲染 = %s", b.String())
	}
	b.Reset()
	pyValue(&b, []any{"x", 1, false, nil})
	if b.String() != `["x", 1, false, null]` {
		t.Fatalf("list 渲染 = %s", b.String())
	}
}
