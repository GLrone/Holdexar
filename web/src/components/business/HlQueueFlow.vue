<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { crawlApi, settingsApi } from '@/api/client'
import { message, HlSwitch } from '@/components/ui'
import { vStagger } from '@/lib/stagger'
import {
  QUEUE_SEGMENT_SPECS,
  resolveQueueChain,
  resolveQueueSegments,
  type QueueSegmentView,
} from '@/lib/queueFlow'
import { useI18n } from '@/locales'
import { useCrawlStatusStore } from '@/stores/crawlStatus'

const { t } = useI18n()
const crawlStatus = useCrawlStatusStore()

const segments = ref<QueueSegmentView[]>(resolveQueueSegments([]))
const chain = ref<QueueSegmentView[]>([])
const loaded = ref(false)

/* 有任务在跑才流动；空闲时静线。reduce-motion 下循环动画整体关停（scoped 尾部）。 */
const flowing = computed(() => crawlStatus.running)

/** 流线几何：5 条固定贝塞尔（源点均分左缘、汇入点收拢右缘中带），
 * 段数固定不变，is-off 段的线灰色且不流动 */
const linkPaths = QUEUE_SEGMENT_SPECS.map((_, i) => {
  const sy = 32 + i * 64
  const ty = 96 + i * 32
  return `M0 ${sy} C 48 ${sy}, 48 ${ty}, 96 ${ty}`
})

async function reload() {
  try {
    const payload = await crawlApi.queueSpecs()
    const names = payload.specs.map((spec) => spec.name)
    segments.value = resolveQueueSegments(names)
    chain.value = resolveQueueChain(names)
    loaded.value = true
  } catch {
    // 组成读不到时保留旧态：卡片是读侧投影，不打断页面其余刷新链
  }
}

onMounted(reload)

async function toggle(seg: QueueSegmentView, on: boolean) {
  if (!seg.setting) return
  const prev = seg.on
  seg.on = on
  try {
    await settingsApi.updateFetch({ [seg.setting]: on })
    await reload()
    message.success(t('crawl.queue.savedNextRound'))
  } catch (e) {
    seg.on = prev
    message.error(e instanceof Error ? e.message : String(e))
  }
}
</script>

<template>
  <div class="card section-card" data-section="crawl.section.queue">
    <div class="section-card__header">
      <div>
        <div class="section-title">{{ t('crawl.section.queue') }}</div>
        <div class="section-desc">{{ t('crawl.queue.desc') }}</div>
      </div>
    </div>

    <div class="qf-body">
      <div class="qf-sources hl-stagger" v-stagger>
        <div
          v-for="seg in segments"
          :key="seg.key"
          class="qf-node"
          :class="{ 'is-off': !seg.on }"
        >
          <div class="qf-node__head">
            <span class="qf-node__name">{{ t(seg.nameKey) }}</span>
            <HlSwitch
              v-if="seg.setting"
              :model-value="seg.on"
              :label="t(seg.nameKey)"
              accent
              @update:model-value="(v: boolean) => toggle(seg, v)"
            />
            <span v-else class="qf-node__follow">{{ t('crawl.queue.followCatalog') }}</span>
          </div>
          <p class="qf-node__desc">{{ t(seg.descKey) }}</p>
          <p v-if="seg.key === 'pool' && !seg.on" class="qf-node__warn">
            {{ t('crawl.queue.poolOffWarn') }}
          </p>
        </div>
      </div>

      <div class="qf-links" aria-hidden="true">
        <svg viewBox="0 0 96 320" preserveAspectRatio="none">
          <path
            v-for="(d, i) in linkPaths"
            :key="i"
            class="qf-link"
            :class="{ 'is-off': !segments[i]?.on, 'is-flowing': flowing && segments[i]?.on }"
            :d="d"
            vector-effect="non-scaling-stroke"
          />
        </svg>
      </div>

      <div class="qf-total">
        <div class="qf-total__title">{{ t('crawl.queue.total') }}</div>
        <div v-if="loaded && chain.length > 0" class="qf-total__chain">
          <span v-for="(seg, i) in chain" :key="seg.key" class="qf-chip">
            <span class="qf-chip__order">{{ i + 1 }}</span>{{ t(seg.nameKey) }}
          </span>
        </div>
        <p v-if="loaded && chain.length === 0" class="qf-total__empty">
          {{ t('crawl.queue.emptyAll') }}
        </p>
        <p class="qf-total__state">
          {{ flowing ? t('crawl.queue.stateRunning') : t('crawl.queue.stateIdle') }}
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.qf-body {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 96px minmax(0, 1fr);
  gap: 14px;
  align-items: center;
}

.qf-sources {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.qf-node {
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  background: var(--surface-card);
  padding: 10px 12px;
}

.qf-node__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.qf-node__name {
  font-weight: 600;
  color: var(--text);
}

.qf-node__desc {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--text-muted);
}

.qf-node__follow {
  font-size: 12px;
  color: var(--text-dim);
  white-space: nowrap;
}

/* 停用 = 灰：面退到轨道灰、文字退到最淡一档（不用 opacity，开关态要可读） */
.qf-node.is-off {
  background: var(--surface-track);
  border-color: var(--border-soft);
}

.qf-node.is-off .qf-node__name,
.qf-node.is-off .qf-node__desc {
  color: var(--text-faint);
}

.qf-node__warn {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--warning);
}

.qf-links svg {
  display: block;
  width: 100%;
  height: 100%;
}

.qf-link {
  fill: none;
  stroke: var(--accent-a60);
  stroke-width: 2;
  stroke-dasharray: 5 7;
}

.qf-link.is-off {
  stroke: var(--skeleton-2);
}

.qf-link.is-flowing {
  animation: qfFlow 1.2s linear infinite;
}

@keyframes qfFlow {
  to {
    stroke-dashoffset: -12;
  }
}

.qf-total {
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  background: var(--surface-card);
  padding: 12px 14px;
}

.qf-total__title {
  font-weight: 600;
  color: var(--text);
}

.qf-total__chain {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
}

.qf-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text);
}

.qf-chip__order {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: var(--accent-a15);
  color: var(--accent);
  font-size: 11px;
  font-weight: 600;
  flex-shrink: 0;
}

.qf-total__empty {
  margin: 10px 0 0;
  font-size: 13px;
  color: var(--text-faint);
}

.qf-total__state {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--text-dim);
}

@media (max-width: 720px) {
  .qf-body {
    grid-template-columns: 1fr;
  }

  .qf-links {
    display: none;
  }
}

/* 循环流动在减少动态效果下显式关停（同 hl-skeleton 既有形态） */
@media (prefers-reduced-motion: reduce) {
  .qf-link.is-flowing {
    animation: none;
  }
}
</style>
