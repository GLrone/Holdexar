package events

import (
	"context"
	"sync"
)

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

// Get 优先非阻塞排空 critical 再查 progress，均空时阻塞并发等待，确保关键事件不饥饿。
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

func NewBus() *Bus {
	return &Bus{subs: make(map[*Subscriber]struct{})}
}

func (b *Bus) Subscribe() *Subscriber {
	sub := newSubscriber()
	b.mu.Lock()
	b.subs[sub] = struct{}{}
	b.mu.Unlock()
	return sub
}

func (b *Bus) Unsubscribe(sub *Subscriber) {
	b.mu.Lock()
	delete(b.subs, sub)
	b.mu.Unlock()
}

func (b *Bus) Len() int {
	b.mu.RLock()
	defer b.mu.RUnlock()
	return len(b.subs)
}

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
			default:
			}
			continue
		}
		putCritical(sub, ev)
	}
}

// putCritical 满时先出队最旧再重试投递；并发写入下仍有极小重入丢新窗口。
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
