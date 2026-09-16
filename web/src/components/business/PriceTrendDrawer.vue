<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  gamesApi,
  type GameListItem,
  type GameVersionPrices,
  type HistoryPayload,
} from '@/api/client'
import { flagUrl, formatCnyFen } from '@/api/regions'
import { isStandardVersion, selectableVariants, versionLabels, versionSelectOptions } from '@/lib/versions'
import { useI18n, type MessageKey } from '@/locales'
import { useRegionsStore } from '@/stores/regions'
import { useFilterStore } from '@/stores/gamesFilter'
import HlSelect, { type HlSelectOption } from '@/components/ui/HlSelect.vue'
import HlChip from '@/components/ui/HlChip.vue'
import HlDrawer from '@/components/ui/HlDrawer.vue'
import HlPaneSwitch from '@/components/ui/HlPaneSwitch.vue'
import HlSkeleton from '@/components/ui/HlSkeleton.vue'
import PriceTrendChart from './PriceTrendChart.vue'

/**
 * 价格走势侧边抽屉（游戏卡「📈 走势」入口）。
 * UI 对齐 component-framework.html 模块 F（price-chart-card）：
 * pc-header（游戏名 + 当前价块）/ 图例 / ECharts 主图 / 四格统计 / 事件标记。
 * 地区下拉只显示「我」页追踪区（未追踪区不进默认视图）；
 * 地区 × 版本 × 时间范围三维即选即看，非模态可继续浏览库。
 */
const props = defineProps<{
  game: GameListItem
  modelValue: boolean
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()

const regionsStore = useRegionsStore()
const filterStore = useFilterStore()
const { t } = useI18n()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

/** 时间范围 chips。**存 key 不存文案**：模块级常量只在模块加载时求值一次，
 *  直接写 t() 会把语言冻死在首次加载那一刻（同 HlSelect 默认值的坑）。
 *  显示文本在模板里 t(r.labelKey) 现取，切语言即跟随。 */
const RANGE_OPTIONS: { labelKey: MessageKey; days: number }[] = [
  { labelKey: 'trendDrawer.range.90d', days: 90 },
  { labelKey: 'trendDrawer.range.1y', days: 365 },
  { labelKey: 'trendDrawer.range.3y', days: 1095 },
  { labelKey: 'trendDrawer.range.all', days: 0 },
]

/** 追踪区 ∩ 该游戏有价区；游戏在追踪区全无价时回落全部有价区（下拉不可为空）。
 *  dimmed：该区无历史数据（标准版全量探测为空）→ 下拉里置灰提示，
 *  依旧可选（选了切过去就是空幕布，属用户主动行为）。 */
const trackedRegionOptions = computed<HlSelectOption[]>(() => {
  const enabled = regionsStore.enabledCodes
  const all = Object.keys(props.game.priceMatrix).map((c) => c.toLowerCase())
  const filtered = enabled.length > 0 ? all.filter((c) => enabled.includes(c)) : all
  const codes = filtered.length > 0 ? filtered : all
  return codes.map((c) => ({
    value: c,
    label: regionsStore.regionName(c),
    flag: flagUrl(c),
    dimmed: regionHasHistory.value[c] === false,
  }))
})

/** 各下拉区「标准版有无历史」探测结果（true=有 / false=无 / undefined=未知不置灰）。
 *  抽屉打开时并行轻探测（days=0 与真实查询同口径，空幕布判定才准确），
 *  覆盖当前选中版本以外的区——当前区的真数据由 load() 本身承载。 */
const regionHasHistory = ref<Record<string, boolean>>({})
let probeSeq = 0

async function probeRegionHistory() {
  const seq = ++probeSeq
  const codes = trackedRegionOptions.value.map((o) => String(o.value))
  const results = await Promise.all(
    codes.map(async (c) => {
      try {
        const res = await gamesApi.history(props.game.appid, c, 0)
        return [c, (res.points?.length ?? 0) > 0] as const
      } catch {
        return [c, true] as const // 请求失败不置灰（保守，避免误伤）
      }
    }),
  )
  if (seq !== probeSeq) return // 抽屉已重开（换游戏），丢弃旧探测
  regionHasHistory.value = Object.fromEntries(results)
}

const region = ref('cn')
/** 0 = 标准版（请求不带 subId）；其余 = sub 包号 */
const versionKey = ref(0)
/** 可见时间窗（天）：0=全部。历史全量一次拉齐，缩放在图表端进行（SteamDB 式导航条） */
const days = ref(0)
const history = ref<HistoryPayload | null>(null)
const loading = ref(false)
let requestSeq = 0

/** 三段互斥态的标识（加载 / 图表 / 空态）。HlPaneSwitch 只认 key 变化，
 *  用布尔无法区分「加载中」与「无数据」——两者都渲染空幕布就退化成瞬切。 */
const paneState = computed(() =>
  loading.value ? 'loading' : history.value?.points.length ? 'chart' : 'empty',
)

function initialRegion(): string {
  const preferred = (filterStore.region || '').toLowerCase()
  if (preferred && trackedRegionOptions.value.some((o) => o.value === preferred)) return preferred
  if (trackedRegionOptions.value.some((o) => o.value === 'cn')) return 'cn'
  return (trackedRegionOptions.value[0]?.value as string) ?? 'cn'
}

async function load() {
  const seq = ++requestSeq
  loading.value = true
  try {
    // days=0 全量拉齐：时间窗缩放全部在图表端（时间 chips / 导航条），不回源
    const res = await gamesApi.history(
      props.game.appid,
      region.value,
      0,
      versionKey.value || undefined,
    )
    if (seq !== requestSeq) return // 已有更新请求，丢弃旧响应
    history.value = res
    // 版本列表随地区/响应刷新，当前选择可能已不在列表 → 回落标准版
    if (versionKey.value !== 0 && !versionOptions.value.some((v) => v.value === versionKey.value)) {
      versionKey.value = 0
    }
  } catch {
    if (seq === requestSeq) history.value = null
  } finally {
    if (seq === requestSeq) loading.value = false
  }
}

// ─── 全版本区块（B1 拍板：全部版本收拢到抽屉，版本 × 地区连锁）───
const versionsAll = ref<GameVersionPrices[] | null>(null)
let versionsSeq = 0

async function loadVersions() {
  const seq = ++versionsSeq
  try {
    const res = await gamesApi.versions(props.game.appid)
    if (seq !== versionsSeq) return
    versionsAll.value = res.versions
  } catch {
    if (seq === versionsSeq) versionsAll.value = null
  }
}

const versionChips = computed(() => {
  const list = versionsAll.value ?? []
  const regionKey = region.value.toUpperCase()
  const labelMap = versionLabels(list, t)
  // 标准版占一条且指向 versionKey=0（跨 sub 代际的并集序列）——它横跨的多个
  // sub 代际不各占一条；价格取其中任一有效区价（即当前在售那个 sub 的价）。
  const standardCell =
    list.filter(isStandardVersion).map((v) => v.regions[regionKey]).find(Boolean) ?? null
  return [
    { subId: 0, label: t('trendDrawer.version.standard'), isGold: false, price: standardCell },
    ...selectableVariants(list).map((v) => ({
      subId: v.subId,
      label: labelMap.get(v.subId) ?? `#${v.subId}`,
      isGold: v.isGold,
      price: v.regions[regionKey] ?? null,
    })),
  ]
})

function selectVersion(subId: number) {
  if (versionKey.value === subId) return
  versionKey.value = subId // watch → load()，图表/当前价/史低连锁刷新
}

watch(visible, (open) => {
  if (!open) return
  region.value = initialRegion()
  versionKey.value = 0
  days.value = 0
  load()
  loadVersions()
  probeRegionHistory()
})

// 切地区 → 版本数据源变化，重置标准版再拉
watch(region, () => {
  versionKey.value = 0
  load()
})
// 时间窗是纯前端开窗，不再触发回源
watch(versionKey, () => load())

const hasMultipleVersions = computed(
  () => selectableVariants(history.value?.versions ?? []).length > 0,
)

const versionOptions = computed(() => versionSelectOptions(history.value?.versions ?? [], t))

// ─── pc-header 当前价块（该地区现价，取自卡片 priceMatrix）───
const currentCell = computed(() => {
  const cell = props.game.priceMatrix[region.value.toUpperCase()]
  return Array.isArray(cell) ? cell : null
})
const currentNative = computed(() => currentCell.value?.[0] ?? null)
const currentCnyFen = computed(() => currentCell.value?.[1] ?? null)

/** 事件标签的 key：首点「首发」、末点「史低」、中间「降至」；
 *  单节点序列（数据刚起步）只有一个点，标「史低」更贴切。 */
function eventLabelKey(i: number, len: number): MessageKey {
  if (i === 0 && len > 1) return 'trendDrawer.event.first'
  if (i === len - 1) return 'trendDrawer.event.lowest'
  return 'trendDrawer.event.drop'
}

/** 事件标记：价格刷新最低值的拐点（模版时间线：首发/降至/史低，最多 6 条）。
 *  这里**存 key 不存已译文案**——标签在 computed 里定、在模板里 t() 现取，
 *  避免把语言冻进 computed 的结果缓存。 */
const lowEvents = computed(() => {
  const pts = history.value?.points ?? []
  let runningLow = Infinity
  const events: { ts: string; cnyFen: number }[] = []
  for (const p of pts) {
    if (!p.cnyFen || p.cnyFen <= 0) continue
    if (p.cnyFen < runningLow) {
      events.push({ ts: (p.timestamp ?? '').slice(0, 10), cnyFen: p.cnyFen })
      runningLow = p.cnyFen
    }
  }
  const out = events.slice(-6)
  return out.map((e, i) => ({ ...e, labelKey: eventLabelKey(i, out.length) }))
})

const points = computed(() => history.value?.points ?? [])
const latestPoint = computed(() => points.value[points.value.length - 1] ?? null)
const latestDiscount = computed(() => latestPoint.value?.discount ?? 0)

/** 抽屉最高处须低于整个导航层叠（吸顶 navbar + 可能出现的 filter-toolbar）。
    navbar 高度随布局变化（桌面 68px / 窄屏 50px），硬编码不可靠——
    每次打开时实测层叠内最底元素的视口 bottom + 6px 间隙。 */
const stackTop = ref(140)

watch(visible, (open) => {
  if (!open) return
  let bottom = 0
  for (const sel of ['.app-header', '.navbar', '.filter-toolbar']) {
    const el = document.querySelector(sel)
    if (el) bottom = Math.max(bottom, el.getBoundingClientRect().bottom)
  }
  stackTop.value = Math.round(bottom + 6)
})
</script>

<template>
  <HlDrawer
    v-model="visible"
    :modal="false"
    :with-header="false"
    width="520px"
    :top="stackTop"
    class="trend-drawer"
  >
    <div class="trend-drawer__body">
      <button class="trend-drawer__close" :title="t('common.close')" @click="visible = false">✕</button>

      <!-- 控制行：地区（仅追踪区）× 版本 × 时间范围 -->
      <div class="trend-drawer__controls">
        <div class="trend-drawer__selects">
          <HlSelect v-model="region" :options="trackedRegionOptions" class="ctrl-region" />
          <HlSelect
            v-if="hasMultipleVersions"
            v-model="versionKey"
            :options="versionOptions"
            class="ctrl-version"
          />
        </div>
        <div class="trend-drawer__ranges">
          <HlChip
            v-for="r in RANGE_OPTIONS"
            :key="r.days"
            tone="accent"
            :on="days === r.days"
            @click="days = r.days"
          >{{ t(r.labelKey) }}</HlChip>
        </div>
      </div>

      <!-- 框架标准走势卡（模块 F：price-chart-card）。三段互斥态（加载 / 图表 / 空态）
           此前是瞬切，改走 HlPaneSwitch——它是**唯一**被过渡包裹的那个元素，
           里面仍是 v-if 链（<Transition> 的单子节点要求由 HlPaneSwitch 的包裹盒满足）。 -->
      <HlPaneSwitch :pane-key="paneState">
      <HlSkeleton v-if="loading" variant="text" :count="1" :rows="6" />
      <div v-else-if="history && history.points.length" class="price-chart-card">
        <div class="pc-header">
          <div class="pc-title">
            <HlImg class="pc-game-icon" :src="game.headerImage" :alt="game.name" />
            <div style="min-width: 0">
              <div style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap">{{ game.name }}</div>
              <div class="pc-sub">
                {{
                  t('trendDrawer.header.subtitle', {
                    appid: game.appid,
                    region: regionsStore.regionName(region),
                  })
                }}
              </div>
            </div>
          </div>
          <div v-if="currentCnyFen !== null" class="pc-price-now">
            <div class="pc-current" :class="{ 'is-discount': latestDiscount > 0 }">
              {{ currentNative || formatCnyFen(currentCnyFen) }}
            </div>
            <div v-if="latestDiscount > 0" class="pc-original">
              {{ formatCnyFen(Math.round(currentCnyFen / (1 - latestDiscount / 100))) }}
            </div>
            <div v-if="latestDiscount > 0" class="pc-discount">
              {{ t('trendDrawer.price.discount', { pct: latestDiscount }) }}
            </div>
            <div v-else class="pc-discount" style="color: var(--text-dim)">
              {{ t('trendDrawer.price.noDiscount') }}
            </div>
          </div>
        </div>

        <div class="pc-legend">
          <span class="pc-legend-item"><i :style="{ background: 'var(--accent)' }"></i>{{ t('trendDrawer.legend.localPrice') }}</span>
          <span class="pc-legend-item"><i :style="{ background: 'var(--warning)', borderTop: '2px dashed var(--warning)', height: 0 }"></i>{{ t('trendDrawer.legend.lowest') }}</span>
        </div>

        <div class="pc-chart-wrap">
          <PriceTrendChart
            :payload="history"
            height="240px"
            :window-days="days"
            @window-change="days = $event"
          />
        </div>

        <div class="pc-stats-row">
          <div class="pc-stat">
            <div class="pc-stat__num" style="color: var(--success)">
              {{ history.lowest ? formatCnyFen(history.lowest.cnyFen) : '—' }}
            </div>
            <div class="pc-stat__lbl">{{ t('trendDrawer.legend.lowest') }}</div>
          </div>
          <div class="pc-stat">
            <div class="pc-stat__num" style="color: var(--accent)">
              {{ currentCnyFen !== null ? formatCnyFen(currentCnyFen) : '—' }}
            </div>
            <div class="pc-stat__lbl">{{ t('trendDrawer.stats.current') }}</div>
          </div>
          <div class="pc-stat">
            <div class="pc-stat__num" style="color: var(--warning)">
              {{ history.highest ? formatCnyFen(history.highest.cnyFen) : '—' }}
            </div>
            <div class="pc-stat__lbl">{{ t('trendDrawer.stats.highest') }}</div>
          </div>
          <div class="pc-stat">
            <div class="pc-stat__num">{{ history.lowestHits ?? '—' }}</div>
            <div class="pc-stat__lbl">{{ t('trendDrawer.stats.lowestHits') }}</div>
          </div>
          <div class="pc-stat">
            <div class="pc-stat__num">{{ history.count ?? history.points.length }}</div>
            <div class="pc-stat__lbl">{{ t('trendDrawer.stats.slices') }}</div>
          </div>
        </div>

        <div v-if="lowEvents.length" class="pc-event-markers">
          <span
            v-for="(e, i) in lowEvents"
            :key="i"
            class="pc-event-tag"
          >
            <span class="pc-event-dot"></span>
            {{ e.ts }} {{ t(e.labelKey) }} ¥{{ (e.cnyFen / 100).toFixed(0) }}
          </span>
        </div>
      </div>

      <div v-else class="trend-drawer__empty">
        <div style="font-size: 32px; margin-bottom: 10px">📉</div>
        <div>{{ t('trendDrawer.empty.noData') }}</div>
        <div style="font-size: 11px; margin-top: 4px">{{ t('trendDrawer.empty.hint') }}</div>
      </div>
      </HlPaneSwitch>

      <!-- 全部版本（B1 拍板：收拢到抽屉；点 chip = 版本 × 地区连锁切换） -->
      <div
        v-if="versionChips.length"
        class="trend-drawer__versions"
        data-section="trendDrawer.versions.title"
      >
        <div class="td-versions-title">{{ t('trendDrawer.versions.title') }} <span class="td-versions-hint">{{ t('trendDrawer.versions.hint') }}</span></div>
        <div class="td-versions-list">
          <button
            v-for="c in versionChips"
            :key="c.subId"
            class="td-version-chip"
            :class="{ active: versionKey === c.subId }"
            :title="c.label"
            @click="selectVersion(c.subId)"
          >
            <span class="td-chip-label">
              {{ c.label }}
              <span v-if="c.isGold" class="td-chip-gold">Gold</span>
            </span>
            <span v-if="c.price" class="td-chip-price" :class="{ 'is-discount': c.price.discount > 0 }">
              {{ c.price.formatted }}
            </span>
            <span v-else class="td-chip-price td-chip-price--none">—</span>
          </button>
        </div>
      </div>

      <router-link
        class="trend-drawer__detail-link"
        :to="`/game/${game.appid}`"
        @click="visible = false"
      >{{ t('trendDrawer.detail.viewFull') }} →</router-link>
    </div>
  </HlDrawer>
</template>

<style scoped>
.trend-drawer__body {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px;
  min-height: 100%;
}

.trend-drawer__controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.trend-drawer__selects {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.ctrl-region {
  width: 160px;
}

.ctrl-version {
  flex: 1;
  min-width: 140px;
}

.ctrl-region :deep(.hl-select-wrap),
.ctrl-version :deep(.hl-select-wrap) {
  width: 100%;
}

.trend-drawer__ranges {
  display: flex;
  gap: 6px;
}

/* 史低事件点。原先是行内 style 写死 #2ed573 + rgba(46,213,115,.5)——那个绿在
   token 表里根本不存在，而走势图上同一个「史低」语义的点用的是 --success，
   一绿两源。改取令牌阶梯，主题切换随之跟随。 */
.pc-event-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--success);
  box-shadow: 0 0 5px var(--success-a50);
}

/* `.range-chip`（胶囊 + 常驻强调色）曾在这里——它与 `tabs-shared.css` 的 `.bill-chip`、
   `game-detail` 的同名块是同一造型的三份副本，已统一到 `HlChip.vue` 的
   `<HlChip tone="accent">`。此处只留注释，样式见 `hl-framework.css` 的 `.hl-chip`。 */

.pc-game-icon {
  width: 44px;
  height: 21px;
  border-radius: 4px;
  object-fit: cover;
  background: var(--surface-chip-2);
  flex-shrink: 0;
}

.pc-current.is-discount {
  color: var(--rate-good);
}

.trend-drawer__close {
  position: absolute;
  top: 10px;
  right: 12px;
  z-index: 5;
  background: var(--surface-chip-2);
  border: 1px solid var(--border-soft);
  color: var(--text-muted);
  font-size: 13px;
  cursor: pointer;
  padding: 4px 9px;
  border-radius: 6px;
  flex-shrink: 0;
  transition: all 0.2s;
}

.trend-drawer__close:hover {
  color: var(--text-primary);
  border-color: var(--accent);
}

.trend-drawer__empty {
  min-height: 200px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: var(--text-muted);
  text-align: center;
  padding: 24px 16px;
}

.trend-drawer__detail-link {
  font-size: 12.5px;
  color: var(--accent);
  text-decoration: none;
  text-align: center;
}

.trend-drawer__detail-link:hover {
  text-decoration: underline;
}

/* ─── 全部版本区块（B1）─── */
.trend-drawer__versions {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.td-versions-title {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-secondary);
}

.td-versions-hint {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-dim);
}

.td-versions-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 260px;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.td-version-chip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--surface-chip);
  cursor: pointer;
  transition: all 0.15s;
  text-align: left;
}

.td-version-chip:hover {
  border-color: var(--accent);
  background: var(--accent-a10, rgba(102, 192, 244, 0.08));
}

.td-version-chip.active {
  border-color: var(--accent);
  background: var(--accent-a20, rgba(102, 192, 244, 0.16));
}

.td-chip-label {
  font-size: 12.5px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.td-chip-gold {
  display: inline-block;
  margin-left: 5px;
  padding: 1px 6px;
  border-radius: 999px;
  font-size: 10px;
  background: var(--warning-a15, rgba(255, 183, 77, 0.15));
  color: var(--warning);
}

.td-chip-price {
  font-size: 12.5px;
  font-weight: 600;
  color: var(--text-primary);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.td-chip-price.is-discount {
  color: var(--rate-good);
}

.td-chip-price--none {
  color: var(--text-dim);
  font-weight: 400;
}
</style>
