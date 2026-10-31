package events

import (
	"context"
	"sync"
)

// Subscriber 单订阅者的双通道收件箱。
type Subscriber struct {
	critical chan Event
	progress chan Event
}

func newSubscriber() *Subscriber {
	return &Subscriber{
		critical: make(chan Event, CriticalMax),
		progress: make(chan Event, ProgressMax),
	}
}

// Get 取下一条事件：critical 优先，阻塞至任一通道就绪、ctx 取消或超时。
//
// 竞速语义对齐 Python 双 getter（asyncio.wait FIRST_COMPLETED）：
//   - 阻塞前先非阻塞查 critical、再查 progress（对齐 get_nowait 顺序）；
//   - 阻塞期两侧同时就绪时 select 随机取一侧，未选中侧留在自家通道，下次 Get
//     仍 critical 优先——Python 的 _pending 补投缓存在单接收者 select 下退化为
//     「事件留在自家通道」，关键事件同样永不丢；
//   - Python 竞速窗口内被取消的 progress getter 会丢掉已取走的进度，Go 单接收者
//     select 不双取、无此丢失（进度本就可丢，只强不弱）。
func (s *Subscriber) Get(ctx context.Context) (Event, error) {
	select {
	case ev := <-s.critical:
		return ev, nil
	default:
	}
	select {
	case ev := <-s.progress:
		return ev, nil
	default:
	}
	select {
	case ev := <-s.critical:
		return ev, nil
	case ev := <-s.progress:
		return ev, nil
	case <-ctx.Done():
		return Event{}, ctx.Err()
	}
}

// Bus 发布/订阅总线。progress 满即丢（不阻塞爬虫主流程）；critical 满挤旧保新。
type Bus struct {
	mu   sync.RWMutex
	subs map[*Subscriber]struct{}
}

// NewBus 创建空总线。
func NewBus() *Bus {
	return &Bus{subs: make(map[*Subscriber]struct{})}
}

// Subscribe 登记新订阅者。
func (b *Bus) Subscribe() *Subscriber {
	sub := newSubscriber()
	b.mu.Lock()
	b.subs[sub] = struct{}{}
	b.mu.Unlock()
	return sub
}

// Unsubscribe 注销订阅者；未登记时静默（对齐 set.discard）。
func (b *Bus) Unsubscribe(sub *Subscriber) {
	b.mu.Lock()
	delete(b.subs, sub)
	b.mu.Unlock()
}

// Len 当前订阅数（观测与测试用）。
func (b *Bus) Len() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return len(b.subs)
}

// Publish 同步非阻塞广播：对订阅者快照逐一投递。
func (b *Bus) Publish(eventType string, kvs ...KV) {
	ev := Event{Type: eventType, Ts: Now(), Data: kvs}
	b.mu.RLock()
	subs := make([]*Subscriber, 0, len(b.subs))
	for sub := range b.subs {
		subs = append(subs, sub)
	}
	b.mu.RUnlock()
	for _, sub := range subs {
		if progressTypes[eventType] {
			select {
			case sub.progress <- ev:
			default: // 满：进度可丢，不阻塞发布方
			}
			continue
		}
		putCritical(sub, ev)
	}
}

// putCritical 投递 critical：满时挤掉最旧保留最新（状态语义，消费端只认最新）。
// 并发发布方在挤旧与重投之间再次塞满的窗口里丢新事件——Python 发布方串行无此
// 窗口，Go 侧一次重投已覆盖实际负载形态。
func putCritical(sub *Subscriber, ev Event) {
	select {
	case sub.critical <- ev:
		return
	default:
	}
	select {
	case <-sub.critical:
	default:
	}
	select {
	case sub.critical <- ev:
	default:
	}
}
