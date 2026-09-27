<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { settingsApi, type FetchSettingsPayload } from '@/api/client'
import {
  HlButton,
  HlIcon,
  HlSelect,
  HlSkeleton,
  HlSwitch,
  message,
  type HlSelectOption,
} from '@/components/ui'
import { useI18n } from '@/locales'

/**
 * 自动抓取 —— 系统组的维护面板：一个开关管一个「自动联网更新的内容源」，
 * 另有价格更新间隔与恢复默认。开关只控制定时更新（下一轮起生效）；
 * 已有数据保留，手动刷新不受影响——这条语义由页首说明与每行描述承载。
 */
const { t } = useI18n()

const prefs = ref<FetchSettingsPayload | null>(null)
const loading = ref(true)
const loadError = ref(false)
const savingKey = ref<string | null>(null)

/* 行配置只存词条 key（模块级常量不许存译文），渲染期 t() */
const ROWS = [
  { key: 'epic_free', labelKey: 'fetch.epic.label', descKey: 'fetch.epic.desc' },
  { key: 'hb_choice', labelKey: 'fetch.hb.label', descKey: 'fetch.hb.desc' },
  { key: 'boards', labelKey: 'fetch.boards.label', descKey: 'fetch.boards.desc' },
  {
    key: 'bundle_counts',
    labelKey: 'fetch.bundleCounts.label',
    descKey: 'fetch.bundleCounts.desc',
  },
  { key: 'fx_auto', labelKey: 'fetch.fx.label', descKey: 'fetch.fx.desc' },
  {
    key: 'fx_history',
    labelKey: 'fetch.fxHistory.label',
    descKey: 'fetch.fxHistory.desc',
  },
] as const

/* 价格更新间隔候选项（小时）。库里出现清单外的值（如手改）时补一项显示 */
const INTERVAL_CHOICES = [1, 2, 3, 4, 6, 8, 12, 24, 48]
const intervalOptions = computed<HlSelectOption[]>(() => {
  const opts = INTERVAL_CHOICES.map((h) => ({
    value: h,
    label: t('fetch.interval.hours', { n: h }),
  }))
  const cur = prefs.value?.price_interval_hours
  if (cur != null && !INTERVAL_CHOICES.includes(cur)) {
    opts.unshift({ value: cur, label: t('fetch.interval.hours', { n: cur }) })
  }
  return opts
})

const saving = computed(() => savingKey.value !== null)

/** 响应缺 price_interval_hours = 对端是不认识该字段的旧版后端（跑的就是
 *  默认 6h 网格），按 6 回显；保存路径的缺字段另行报错，不在此吞掉 */
function withInterval(p: Partial<FetchSettingsPayload>): FetchSettingsPayload {
  return { ...(p as FetchSettingsPayload), price_interval_hours: p.price_interval_hours ?? 6 }
}

async function save(patch: Partial<FetchSettingsPayload>): Promise<boolean> {
  if (!prefs.value) return false
  const prev = { ...prefs.value }
  Object.assign(prefs.value, patch)
  try {
    const saved = (await settingsApi.updateFetch(patch)) as Partial<FetchSettingsPayload>
    if (patch.price_interval_hours != null && saved.price_interval_hours == null) {
      // 旧版后端会静默丢弃不认识的字段：请求带了间隔、响应没有 = 没保存上
      prefs.value = prev
      message.error(t('fetch.toast.failed'))
      return false
    }
    prefs.value = withInterval(saved)
    return true
  } catch {
    prefs.value = prev
    message.error(t('fetch.toast.failed'))
    return false
  }
}

async function toggle(key: keyof FetchSettingsPayload, on: boolean) {
  if (saving.value) return
  savingKey.value = key
  const ok = await save({ [key]: on } as Partial<FetchSettingsPayload>)
  savingKey.value = null
  if (ok) message.success(t(on ? 'fetch.toast.on' : 'fetch.toast.off'))
}

async function changeInterval(raw: string | number) {
  if (saving.value) return
  const hours = Number(raw)
  if (!Number.isFinite(hours)) return
  savingKey.value = 'price_interval_hours'
  const ok = await save({ price_interval_hours: hours })
  savingKey.value = null
  if (ok) message.success(t('fetch.interval.toast', { n: hours }))
}

async function resetDefaults() {
  if (saving.value) return
  savingKey.value = 'reset'
  const ok = await save({
    epic_free: true,
    hb_choice: true,
    boards: true,
    bundle_counts: true,
    fx_auto: true,
    fx_history: true,
    price_interval_hours: 6,
  })
  savingKey.value = null
  if (ok) message.success(t('fetch.reset.toast'))
}

async function load() {
  loading.value = true
  loadError.value = false
  try {
    prefs.value = withInterval(
      (await settingsApi.getFetch()) as Partial<FetchSettingsPayload>,
    )
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="fetch-page">
    <div class="fetch-page__head">
      <p class="fetch-page__desc">{{ t('fetch.desc') }}</p>
      <HlButton
        variant="text"
        size="sm"
        class="fetch-page__reset"
        :disabled="saving || loading"
        @click="resetDefaults"
      >
        <HlIcon name="refresh" />
        {{ t('fetch.reset') }}
      </HlButton>
    </div>

    <HlSkeleton
      v-if="loading"
      variant="text"
      :rows="6"
      class="fetch-page__skeleton"
    />

    <div v-else-if="loadError || !prefs" class="card fetch-page__error">
      <HlButton art="outline" tone="blue" size="sm" @click="load">
        <HlIcon name="refresh" />
        {{ t('fetch.toast.failed') }}
      </HlButton>
    </div>

    <div v-else class="card fetch-page__card">
      <div class="fetch-row">
        <div class="fetch-row__line">
          <span class="fetch-row__label">{{ t('fetch.interval.label') }}</span>
          <HlSelect
            class="fetch-row__select"
            :model-value="prefs.price_interval_hours"
            :options="intervalOptions"
            :disabled="saving"
            @update:model-value="changeInterval"
          />
        </div>
        <p class="fetch-row__desc">{{ t('fetch.interval.desc') }}</p>
      </div>

      <div v-for="row in ROWS" :key="row.key" class="fetch-row">
        <div class="fetch-row__line">
          <span class="fetch-row__label">{{ t(row.labelKey) }}</span>
          <HlSwitch
            :model-value="prefs[row.key]"
            accent
            :disabled="saving"
            :label="t(row.labelKey)"
            @update:model-value="(on: boolean) => toggle(row.key, on)"
          />
        </div>
        <p class="fetch-row__desc">{{ t(row.descKey) }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.fetch-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-width: 720px;
}

.fetch-page__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

/* 按钮文字不换行（窄容器里「恢复默认」曾被拆成两行） */
.fetch-page__reset {
  flex-shrink: 0;
  white-space: nowrap;
}

.fetch-page__head .fetch-page__desc {
  flex: 1 1 auto;
}

.fetch-page__desc {
  margin: 0;
  font-size: 13px;
  line-height: 1.65;
  color: var(--text-secondary);
}

.fetch-page__skeleton {
  padding: 20px;
}

.fetch-page__error {
  display: grid;
  place-items: center;
  padding: 32px;
}

.fetch-page__card {
  padding: 6px 20px;
}

.fetch-row {
  padding: 14px 0;
}

.fetch-row + .fetch-row {
  border-top: 1px solid var(--border-soft);
}

.fetch-row__line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.fetch-row__label {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}

.fetch-row__select {
  width: 150px;
  flex-shrink: 0;
}

.fetch-row__desc {
  margin: 4px 0 0;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-muted);
}
</style>
