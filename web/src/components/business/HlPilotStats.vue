<script setup lang="ts">
/**
 * 底栏会话统计：轮/步/速度一行读数，点击弹面板看总耗时与平均首字。
 * 数字全部来自服务端账本投影（会话统计折叠 + done 增量），前端不自测；
 * 无任何计时数字时保持纯读数不开弹窗（没有可展示的行就不给死按钮）。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { useI18n } from '@/locales'

interface StatsShape {
  turns: number
  steps: number
  elapsed_ms: number
  ttft_ms: number
  ttft_n: number
  decode_ms: number
  decode_out: number
}

const props = defineProps<{ stats: StatsShape }>()

const { t, locale } = useI18n()

const open = ref(false)
const root = ref<HTMLElement | null>(null)

/** ≥10 取整、否则一位小数；负值夹 0（DSH 速度格式纪律） */
const speedText = computed(() => {
  if (props.stats.decode_ms <= 0) return ''
  const tps = Math.max(0, props.stats.decode_out / (props.stats.decode_ms / 1000))
  return tps >= 10 ? String(Math.round(tps)) : String(Math.round(tps * 10) / 10)
})

const hasTiming = computed(() => props.stats.elapsed_ms > 0 || props.stats.ttft_n > 0 || props.stats.decode_ms > 0)

const label = computed(() => {
  const parts = [t('pilot.stats.counts', { turns: props.stats.turns, steps: props.stats.steps })]
  if (speedText.value) parts.push(t('pilot.stats.speed', { tps: speedText.value }))
  return parts.join(' · ')
})

/** <1s 显毫秒，<60s 一位小数秒，之后整分整秒 */
function fmtDur(ms: number): string {
  if (ms < 1000) return t('pilot.stats.durMs', { ms: Math.max(1, Math.round(ms)) })
  const s = ms / 1000
  if (s < 60) return t('pilot.stats.durSec', { s: Math.round(s * 10) / 10 })
  const whole = Math.round(s)
  return t('pilot.stats.durMin', { m: Math.floor(whole / 60), s: whole % 60 })
}

const numFmt = computed(() => new Intl.NumberFormat(locale.value === 'zh-CN' ? 'zh-CN' : 'en'))
const fmtInt = (v: number) => numFmt.value.format(v)

const ttftText = computed(() =>
  props.stats.ttft_n > 0 ? fmtDur(props.stats.ttft_ms / props.stats.ttft_n) : null,
)

function onDocClick(e: MouseEvent) {
  if (open.value && root.value && !root.value.contains(e.target as Node)) open.value = false
}
function onEsc(e: KeyboardEvent) {
  if (open.value && e.key === 'Escape') open.value = false
}

onMounted(() => {
  document.addEventListener('click', onDocClick)
  document.addEventListener('keydown', onEsc)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClick)
  document.removeEventListener('keydown', onEsc)
})
</script>

<template>
  <div ref="root" class="pstats">
    <span v-if="!hasTiming" class="pstats__pill">
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 4a8 8 0 1 1-7.4 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
        <path d="M12 12l3.5-3.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
      </svg>
      {{ label }}
    </span>
    <button
      v-else
      type="button"
      class="pstats__pill"
      :aria-expanded="open"
      :aria-label="label"
      @click="open = !open"
    >
      <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M12 4a8 8 0 1 1-7.4 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
        <path d="M12 12l3.5-3.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
      </svg>
      {{ label }}
    </button>
    <Transition name="hl-pop">
      <div v-if="open && hasTiming" class="pstats__panel" role="dialog" :aria-label="t('pilot.stats.title')">
        <div class="pstats__head">
          <span>{{ t('pilot.stats.title') }}</span>
          <span class="pstats__num">{{ t('pilot.stats.counts', { turns: stats.turns, steps: stats.steps }) }}</span>
        </div>
        <div class="pstats__rule" aria-hidden="true"></div>
        <dl class="pstats__rows">
          <div v-if="stats.elapsed_ms > 0" class="pstats__row">
            <dt>{{ t('pilot.stats.elapsed') }}</dt>
            <dd>{{ fmtDur(stats.elapsed_ms) }}</dd>
          </div>
          <div v-if="ttftText" class="pstats__row">
            <dt>{{ t('pilot.stats.ttft') }}</dt>
            <dd>{{ ttftText }}</dd>
          </div>
          <div v-if="speedText" class="pstats__row">
            <dt>{{ t('pilot.stats.speedRow') }}</dt>
            <dd>{{ t('pilot.stats.speed', { tps: speedText }) }}</dd>
          </div>
          <div v-if="stats.decode_out > 0" class="pstats__row">
            <dt>{{ t('pilot.stats.generated') }}</dt>
            <dd>{{ fmtInt(stats.decode_out) }}</dd>
          </div>
        </dl>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.pstats {
  position: relative;
  display: flex;
  justify-content: center;
}

/* 读数胶囊：三级字层 + 悬停胶囊底（与底栏 chip 同语言） */
.pstats__pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  padding: 1px 8px;
  border: none;
  border-radius: 999px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 18px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

button.pstats__pill {
  cursor: pointer;
}

button.pstats__pill:hover,
button.pstats__pill[aria-expanded='true'] {
  background: var(--accent-a10);
  color: var(--text-primary);
}

.pstats__pill svg {
  width: 12px;
  height: 12px;
  flex: none;
}

/* 弹窗：锚在药丸上方（底栏在页面底部，只向上弹） */
.pstats__panel {
  position: absolute;
  bottom: calc(100% + 6px);
  right: 0;
  z-index: 6;
  width: 240px;
  padding: 10px 12px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
}

.pstats__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.pstats__num {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

.pstats__rule {
  margin: 6px 0 4px;
  border-top: 1px solid var(--border-soft);
}

.pstats__row {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  padding: 3px 0;
  font-size: 11px;
}

.pstats__row dt {
  color: var(--text-secondary);
}

.pstats__row dd {
  margin: 0;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
</style>
