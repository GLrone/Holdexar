package crawl

import (
	"context"
	"fmt"
	"sync"

	"holdexar/server-go/internal/crawler"
	"holdexar/server-go/internal/storebrowse"
)

func (r *Runtime) prefetchMetadata(ctx, workCtx context.Context, ids []int64, transport crawlTransport) map[string]map[int64]storebrowse.Item {
	localized := map[string]map[int64]storebrowse.Item{"schinese": {}, "english": {}}
	for _, lang := range []string{"schinese", "english"} {
		pending := append([]int64{}, ids...)
		for _, cc := range []string{"cn", "us", "ua", "id", "pk"} {
			if len(pending) == 0 || ctx.Err() != nil {
				break
			}
			var mu sync.Mutex
			router := crawler.NewRouter()
			_ = router.Register("metadata", func(task *crawler.Context) error {
				fetch := task.Client.(crawlFetch)
				items, err := fetch(workCtx, task.Task.Body.([]int64), cc, lang, true)
				if err != nil {
					return err
				}
				mu.Lock()
				defer mu.Unlock()
				for id, item := range items {
					if storebrowse.IsVisible(item) {
						localized[lang][id] = item
					}
				}
				return nil
			})
			pool := transport.scheduler(router)
			work := []crawler.Task{}
			for i, batch := range storebrowse.PlanBatches(pending, cc, lang, true, 300) {
				work = append(work, crawler.Task{ID: fmt.Sprintf("meta:%s:%s:%d", lang, cc, i), Type: "metadata", Body: batch})
			}
			runBatches(ctx, pool, work)
			next := []int64{}
			for _, id := range pending {
				if _, ok := localized[lang][id]; !ok {
					next = append(next, id)
				}
			}
			pending = next
		}
	}
	return localized
}
