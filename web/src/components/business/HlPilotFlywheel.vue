<script setup lang="ts">
/**
 * 问答质量沉淀（决策飞轮画像）：跨会话统计明确评价与实际运行结果，
 * 并提供导出训练数据入口。数据全部来自后端只读投影（决策日志 × 会话账本），
 * 前端不重算；属维护面信息（设置页领航员卡内），不进对话主流程。
 */
import { computed, onMounted, ref } from 'vue'

import { pilotApi, type PilotFlywheelStats } from '@/api/client'
import { useI18n, type MessageKey } from '@/locales'
import { HlButton, HlEmpty, HlSkeleton, HlStat, message } from '@/components/ui'

const { t } = useI18n()

const loading = ref(true)
const loadFailed = ref(false)
const exporting = ref(false)
const stats = ref<PilotFlywheelStats | null>(null)

const accepted = computed(() => stats.value?.by_feedback?.accepted ?? 0)
const rejected = computed(() => stats.value?.by_feedback?.rejected ?? 0)
const rated = computed(() => accepted.value + rejected.value)
const failedOrEmpty = computed(() =>
  (stats.value?.by_feedback?.failed ?? 0) + (stats.value?.by_feedback?.empty ?? 0),
)
const unlabeled = computed(() => stats.value?.unlabeled ?? 0)
const total = computed(() => stats.value?.total ?? 0)
const hasData = computed(() => total.value > 0)

/** 满意率只基于用户明确点选的满意/不满意评价。 */
const acceptedRate = computed(() => {
  if (!rated.value) return null
  return `${Math.round((accepted.value / rated.value) * 100)}%`
})

/** 意图名走词条；词表外（含历史 unknown）回落原键，绝不把内部键直显成句 */
function intentLabel(intent: string): string {
  const key = `pilot.flywheel.intent.${intent}` as MessageKey
  const text = t(key)
  return text === key ? intent : text
}

/** 有记录的意图先显示。 */
const intentRows = computed(() => {
  const rows = stats.value?.by_intent ?? []
  return [...rows].sort((a, b) => b.total - a.total)
})

async function load() {
  loading.value = true
  try {
    stats.value = await pilotApi.flywheel()
    loadFailed.value = false
  } catch {
    // 读取失败与「暂无数据」是两回事：显式失败态，不冒充空数据
    stats.value = null
    loadFailed.value = true
  } finally {
    loading.value = false
  }
}

async function onExport() {
  exporting.value = true
  try {
    const r = await pilotApi.exportFlywheel()
    if (r.ok) message.success(t('pilot.flywheel.exported', {
      count: r.count,
      pairs: r.preference_count,
    }))
    else message.error(t('pilot.flywheel.exportFailed'))
  } catch {
    message.error(t('pilot.flywheel.exportFailed'))
  } finally {
    exporting.value = false
  }
}

onMounted(load)

defineExpose({ reload: load })
</script>

<template>
  <div class="fw">
    <div class="fw__head">
      <div class="fw__heading">
        <span class="fw__title">{{ t('pilot.flywheel.title') }}</span>
        <span class="fw__desc">{{ t('pilot.flywheel.desc') }}</span>
      </div>
      <HlButton
        v-if="hasData"
        size="sm"
        :loading="exporting"
        @click="onExport"
      >
        {{ t('pilot.flywheel.export') }}
      </HlButton>
    </div>

    <HlSkeleton v-if="loading" :rows="2" />

    <HlEmpty v-else-if="loadFailed" size="sm" :text="t('pilot.flywheel.loadFailed')" />

    <HlEmpty v-else-if="!hasData" size="sm" :text="t('pilot.flywheel.empty')" />

    <template v-else>
      <div class="fw__kpi">
        <HlStat size="sm" :label="t('pilot.flywheel.turns')" :value="String(total)" />
        <HlStat size="sm" tone="good" :label="t('pilot.flywheel.adopted')" :value="String(accepted)" />
        <HlStat size="sm" tone="warn" :label="t('pilot.flywheel.reasked')" :value="String(rejected)" />
        <HlStat
          size="sm"
          :label="t('pilot.flywheel.adoptRate')"
          :value="acceptedRate ?? '—'"
        />
        <HlStat size="sm" :label="t('pilot.flywheel.unlabeled')" :value="String(unlabeled)" />
        <HlStat size="sm" :label="t('pilot.flywheel.failedEmpty')" :value="String(failedOrEmpty)" />
        <HlStat size="sm" :label="t('pilot.flywheel.pairs')" :value="String(stats?.preference_pairs ?? 0)" />
      </div>

      <div class="fw__intents">
        <div class="fw__intents-title">{{ t('pilot.flywheel.byIntent') }}</div>
        <div v-for="row in intentRows" :key="row.intent" class="fw__intent">
          <span class="fw__intent-name">{{ intentLabel(row.intent) }}</span>
          <span class="fw__intent-bar" aria-hidden="true">
            <span
              class="fw__intent-fill"
              :style="{ width: `${row.accepted + row.rejected ? Math.round((row.accepted / (row.accepted + row.rejected)) * 100) : 0}%` }"
            ></span>
          </span>
          <span class="fw__intent-num">
            {{ t('pilot.flywheel.adopted') }} {{ row.accepted }} · {{ t('pilot.flywheel.reasked') }} {{ row.rejected }}
            <span class="fw__intent-total">/ {{ row.total }}</span>
          </span>
        </div>
      </div>

      <div v-if="(stats?.preference_pairs ?? 0) > 0" class="fw__hint">
        {{ t('pilot.flywheel.pairsHint') }}
      </div>
    </template>
  </div>
</template>

<style scoped>
.fw {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--border-soft);
}

.fw__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.fw__heading {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.fw__title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

.fw__desc {
  font-size: 12px;
  line-height: 1.5;
  color: var(--text-secondary);
}

.fw__kpi {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}

.fw__kpi :deep(.hl-stat) {
  border: 1px solid var(--line-1);
  border-radius: var(--radius-sm);
}

@media (max-width: 1100px) {
  .fw__kpi {
    grid-template-columns: repeat(3, 1fr);
  }
}

.fw__intents {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.fw__intents-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.fw__intent {
  display: grid;
  grid-template-columns: 88px 1fr auto;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}

.fw__intent-name {
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.fw__intent-bar {
  height: 6px;
  border-radius: 999px;
  background: var(--accent-a10);
  overflow: hidden;
}

.fw__intent-fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--success);
}

.fw__intent-num {
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.fw__intent-total {
  color: var(--text-faint);
}

.fw__hint {
  font-size: 11px;
  color: var(--text-muted);
}
</style>
