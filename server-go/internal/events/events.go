package events

import (
	"strings"
	"time"
)

const CriticalMax = 256

const ProgressMax = 64

var progressTypes = map[string]bool{"crawl.progress": true}

// KV 用切片保持事件字段序列化键序（map 兜底按键排序）。
type KV struct {
	Key string
	Val any
}

type Event struct {
	Type string
	Ts   float64
	Data []KV
}

func Now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// AsSSE 渲染为 SSE event/data 帧，ts 置首且载荷含 "ts" 时覆盖事件时刻。
func (e *Event) AsSSE() string {
	tsVal := any(e.Ts)
	data := make([]KV, 0, len(e.Data)+1)
	for _, kv := range e.Data {
		if kv.Key == "ts" {
			tsVal = kv.Val
			continue
		}
		data = append(data, kv)
	}
	pairs := make([]KV, 0, len(data)+1)
	pairs = append(pairs, KV{Key: "ts", Val: tsVal})
	pairs = append(pairs, data...)
	var b strings.Builder
	pyObject(&b, pairs)
	return "event: " + e.Type + "\ndata: " + b.String() + "\n\n"
}

func PyJSONString(s string) string {
	var b strings.Builder
	pyString(&b, s)
	return b.String()
}
