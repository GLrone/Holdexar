// Package events：进程内事件总线（爬取进度 / 任务状态 → SSE 订阅端）。
//
// 双通道分级背压：crawl.progress 高频可丢走 progress 通道（满即丢，不阻塞
// 发布方）；job.started / job.status 等关键状态走 critical 通道（满时挤掉
// 最旧保留最新）——丢一条进度只是界面少刷一次，丢一条状态前端会停在旧状态。
// 发布同步非阻塞。
package events

import (
	"strings"
	"time"
)

// CriticalMax critical 通道容量：状态类事件积压上限。
const CriticalMax = 256

// ProgressMax progress 通道容量：进度类事件积压上限。
const ProgressMax = 64

// progressTypes 走 progress 通道（可丢）的事件类型；其余全部走 critical。
var progressTypes = map[string]bool{"crawl.progress": true}

// KV 有序键值对：事件载荷与嵌套对象用切片承载以保持键序（对齐 Python
// kwargs 的键序；Go map 无序，仅作兜底按键排序渲染）。
type KV struct {
	Key string
	Val any
}

// Event 一次业务事件。Ts 为 Unix 秒浮点（对齐 Python time.time()）。
type Event struct {
	Type string
	Ts   float64
	Data []KV
}

// Now 当前 Unix 秒浮点。
func Now() float64 { return float64(time.Now().UnixNano()) / 1e9 }

// AsSSE 渲染为事件帧：event 行 + data 行（JSON：ts 在前、载荷键序保持；
// Python json.dumps 缺省分隔符与 ensure_ascii=False 语义，非 ASCII 原样 UTF-8）。
// 载荷携带 "ts" 键时覆盖事件时刻（对齐 {"ts": ts, **payload} 的字典字面量语义）。
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

// PyJSONString 字符串的 Python json.dumps（ensure_ascii=False）文本：
// 日志流 data 行与帧内字符串值共用同一转义词表。
func PyJSONString(s string) string {
	var b strings.Builder
	pyString(&b, s)
	return b.String()
}
