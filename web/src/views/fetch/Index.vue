<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  settingsApi,
  type ContentSchedule,
  type FetchSettingsPayload,
} from '@/api/client'
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
 * 另有价格更新间隔、两个内容源的自定义刷新节奏（时刻+间隔天数）与恢复默认。
 * 开关只控制定时更新（下一轮起生效）；已有数据保留，手动刷新不受影响——
 * 这条语义由页首说明与每行描述承载。刷新节奏改动即时重排对应定时拍。
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

/** 带自定义节奏的内容源：行 key → payload 字段 */
const SOURCE_SCHEDULES = {
  epic_free: 'epic_schedule',
  hb_choice: 'hb_schedule',
} as const satisfies Record<string, keyof FetchSettingsPayload>
type ScheduleRowKey = keyof typeof SOURCE_SCHEDULES

/* 间隔天数候选项（天）。库里出现清单外的值（如手改）时补一项显示 */
const DAY_CHOICES = [1, 2, 3, 5, 7, 14, 30]

function scheduleOf(rowKey: ScheduleRowKey): ContentSchedule {
  const field = SOURCE_SCHEDULES[rowKey]
  return (
    prefs.value?.[field] ?? { time: '', days: 1 }
  )
}

/* 刷新时刻用「时 + 分」两个主题下拉：原生时间控件的弹层是浏览器自绘的
 * （白色吃不到主题、分段输入迟钝），换主题化选项后既配色又跟手。
 * 分钟粒度 5 分钟；库里存过清单外的值（如手改的 07 分）时补一项显示。 */
const HOUR_CHOICES = Array.from({ length: 24 }, (_, i) => String(i).padStart(2, '0'))
const MINUTE_CHOICES = Array.from({ length: 12 }, (_, i) => String(i * 5).padStart(2, '0'))

function partsOf(rowKey: ScheduleRowKey): { hour: string; minute: string } {
  const [h = '', m = ''] = scheduleOf(rowKey).time.split(':')
  return { hour: h.padStart(2, '0'), minute: m.padStart(2, '0') }
}

const hourOptions = computed<HlSelectOption[]>(() =>
  HOUR_CHOICES.map((h) => ({ value: h, label: `${h} ${t('fetch.schedule.hour')}` })),
)

function minuteOptions(rowKey: ScheduleRowKey): HlSelectOption[] {
  const opts = MINUTE_CHOICES.map((m) => ({
    value: m,
    label: `${m} ${t('fetch.schedule.minute')}`,
  }))
  const cur = partsOf(rowKey).minute
  if (!MINUTE_CHOICES.includes(cur)) {
    opts.unshift({ value: cur, label: `${cur} ${t('fetch.schedule.minute')}` })
  }
  return opts
}

/** 时刻某一位变化 → 与另一位拼回 HH:MM 保存 */
async function changeSchedulePart(
  rowKey: ScheduleRowKey,
  part: 'hour' | 'minute',
  value: string | number,
) {
  const cur = partsOf(rowKey)
  const time =
    part === 'hour' ? `${value}:${cur.minute}` : `${cur.hour}:${value}`
  await changeSchedule(rowKey, time)
}

function dayOptions(rowKey: ScheduleRowKey): HlSelectOption[] {
  const opts = DAY_CHOICES.map((d) => ({
    value: d,
    label: d === 1 ? t('fetch.schedule.daily') : t('fetch.schedule.everyDays', { n: d }),
  }))
  const cur = scheduleOf(rowKey).days
  if (!DAY_CHOICES.includes(cur)) {
    opts.unshift({ value: cur, label: t('fetch.schedule.everyDays', { n: cur }) })
  }
  return opts
}

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
    // 节奏字段同判：请求带了、响应没有 = 对端没保存上
    const scheduleField = (['hb_schedule', 'epic_schedule'] as const).find(
      (f) => patch[f] != null && saved[f] == null,
    )
    if (scheduleField) {
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

/** 目录层随价格更新开关：只落偏好，改动从下一轮价格更新生效 */
async function toggleCatalog(on: boolean) {
  if (saving.value) return
  savingKey.value = 'catalog_refresh'
  const ok = await save({ catalog_refresh: on })
  savingKey.value = null
  if (ok) message.success(t(on ? 'fetch.catalog.toastOn' : 'fetch.catalog.toastOff'))
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

/** 内容源刷新节奏：时刻或间隔任一变化即保存（保存失败由 save 回滚提示） */
async function changeSchedule(rowKey: ScheduleRowKey, patchTime?: string, patchDays?: number) {
  if (saving.value) return
  const field = SOURCE_SCHEDULES[rowKey]
  const cur = scheduleOf(rowKey)
  const next: ContentSchedule = {
    time: patchTime ?? cur.time,
    days: patchDays ?? cur.days,
  }
  if (!next.time) return
  savingKey.value = field
  const ok = await save({ [field]: next } as Partial<FetchSettingsPayload>)
  savingKey.value = null
  if (ok) message.success(t('fetch.schedule.toast', { time: next.time }))
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
    catalog_refresh: true,
    price_interval_hours: 6,
    hb_schedule: { time: '02:30', days: 1 },
    epic_schedule: { time: '23:05', days: 1 },
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

      <div class="fetch-row">
        <div class="fetch-row__line">
          <span class="fetch-row__label">{{ t('fetch.catalog.label') }}</span>
          <HlSwitch
            :model-value="prefs.catalog_refresh"
            accent
            :disabled="saving"
            :label="t('fetch.catalog.label')"
            @update:model-value="toggleCatalog"
          />
        </div>
        <p class="fetch-row__desc">{{ t('fetch.catalog.desc') }}</p>
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
        <!-- 内容源自定义刷新节奏：时刻（时+分下拉）+ 间隔天数，改动即时保存并重排定时拍 -->
        <div v-if="row.key in SOURCE_SCHEDULES" class="fetch-row__schedule">
          <span class="fetch-row__schedule-label">{{ t('fetch.schedule.time') }}</span>
          <HlSelect
            class="fetch-row__time-part"
            :model-value="partsOf(row.key as ScheduleRowKey).hour"
            :options="hourOptions"
            :disabled="saving"
            @update:model-value="(v: string | number) => changeSchedulePart(row.key as ScheduleRowKey, 'hour', v)"
          />
          <HlSelect
            class="fetch-row__time-part"
            :model-value="partsOf(row.key as ScheduleRowKey).minute"
            :options="minuteOptions(row.key as ScheduleRowKey)"
            :disabled="saving"
            @update:model-value="(v: string | number) => changeSchedulePart(row.key as ScheduleRowKey, 'minute', v)"
          />
          <span class="fetch-row__schedule-label">{{ t('fetch.schedule.interval') }}</span>
          <HlSelect
            class="fetch-row__days"
            :model-value="scheduleOf(row.key as ScheduleRowKey).days"
            :options="dayOptions(row.key as ScheduleRowKey)"
            :disabled="saving"
            @update:model-value="(d: string | number) => changeSchedule(row.key as ScheduleRowKey, undefined, Number(d))"
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

/* 内容源刷新节奏行：时刻输入 + 间隔下拉，嵌在对应开关行下方 */
.fetch-row__schedule {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}

.fetch-row__schedule-label {
  font-size: 12.5px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.fetch-row__time-part {
  width: 104px;
  flex-shrink: 0;
}

.fetch-row__days {
  width: 128px;
  flex-shrink: 0;
}

.fetch-row__desc {
  margin: 4px 0 0;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--text-muted);
}
</style>
