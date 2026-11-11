package derived

import (
	"sync"
	"testing"
	"time"
)

func TestConcurrentClockReplacement(t *testing.T) {
	a := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	b := a.Add(time.Hour)
	old := SetClock(func() time.Time { return a })
	defer SetClock(old)
	var wg sync.WaitGroup
	for i := 0; i < 12; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			for j := 0; j < 1000; j++ {
				if i%2 == 0 {
					SetClock(func() time.Time { return b })
				} else {
					SetClock(func() time.Time { return a })
				}
				got := Clock()
				if got != a && got != b {
					t.Errorf("unexpected clock=%v", got)
					return
				}
			}
		}(i)
	}
	wg.Wait()
}
