<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { Refresh } from '@element-plus/icons-vue'

import {
  RATE_RANGES,
  ratesApi,
  type RateHistoryItem,
  type RateItem,
  type RateRange,
} from '@/api/client'
import { useI18n } from '@/locales'
import { message, HlButton, HlEmpty, HlIcon, HlInput } from '@/components/ui'
import { CURRENCIES, currencyFlagUrl, currencyIndex, currencyName } from '@/api/currencies'
import { currencySelectOptions as buildCurrencyOptions } from '@/api/selectOptions'
import {
  axisPointerStyle,
  revealChart as revealChartShared,
  useChartPalette,
  useTipPalette,
  withAlpha,
} from '@/api/chartTheme'
import { useRatesStore } from '@/stores/rates'
import CurrencyFlag from '@/components/CurrencyFlag.vue'
import HlSelect, { type HlSelectOption } from '@/components/ui/HlSelect.vue'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent])

const { t } = useI18n()

const rates = ref<RateItem[]>([])
const history = ref<RateHistoryItem[]>([])
const selectedCurrency = ref('USD')
const range = ref<RateRange>('all')

const route = useRoute()

/** 深链预选：/rates?currency=CODE（仪表盘迷你汇率行点击直达历史走势板块）。
 *  必须在 selectedCurrency 的 watch 注册前赋值——注册后赋值会触发一次
 *  loadHistory，与 onMounted 的 load(true) 重复取数。未知/缺省参数回退默认 USD。 */
const deepLinkCurrency = (() => {
  const q = route.query.currency
  const code = typeof q === 'string' ? q.toUpperCase() : ''
  return code && currencyIndex(code) < CURRENCIES.length ? code : null
})()
if (deepLinkCurrency) selectedCurrency.value = deepLinkCurrency
const loading = ref(true)
const refreshing = ref(false)
const lastSource = ref('')

const ratesStore = useRatesStore()

/** tooltip 主题化配色（api/chartTheme 统一出口；悬停提示窗红线配套） */
const tipPalette = useTipPalette()

/** 画布配色（网格 / 轴 / 轴文字 / 系列色，同出口）。 */
const palette = useChartPalette()

/** 追踪多选下拉的选项源（服务端白名单全集，与 CC_LIST 同序）。
 *  走 `el-select` 而非 HlSelect（HlSelect 暂无 multiple），故不复用上面的
 *  `currencySelectOptions`——那份的 value/label/flag 结构给 el-option 用不上。 */
const currencyOptions = CURRENCIES

/** HlSelect 格式币种选项（带国旗）。
 *  走 `api/selectOptions.ts` 的统一出口，label 随语言（currencyName）。 */
const currencySelectOptions = computed<HlSelectOption[]>(() => buildCurrencyOptions())

/** 自选追踪（localStorage 持久化；只影响展示，服务端始终抓取全量） */
const trackedModel = computed({
  get: () => ratesStore.tracked,
  set: (codes: string[]) => ratesStore.setTracked(codes),
})

const byWhitelistOrder = (a: RateItem, b: RateItem) =>
  currencyIndex(a.currency) - currencyIndex(b.currency)

/** 全部币种（白名单序） */
const allRates = computed(() => [...rates.value].sort(byWhitelistOrder))

/** 追踪中的币种（自选集 ∩ 库内数据） */
const trackedCurrencies = computed(() =>
  allRates.value.filter((r) => ratesStore.isTracked(r.currency)),
)

async function load(withReveal = false) {
  loading.value = true
  try {
    rates.value = (await ratesApi.list()).rates
    await loadHistory()
    if (withReveal) await revealChart()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function loadHistory() {
  try {
    history.value = await ratesApi.history(selectedCurrency.value, range.value)
  } catch {
    history.value = []
  }
}

async function refresh() {
  refreshing.value = true
  try {
    const res = await ratesApi.refresh()
    lastSource.value = res.source
    message.success(t('rates.toast.refreshed', { count: res.count, source: res.source }))
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    refreshing.value = false
  }
}

/**
 * 两套图表动效，各管一个触发源：
 * - animateChart：时间窗切换 → echarts 数据过渡（画布已有旧帧兜底，安全）；
 * - wipeChart：初始加载 / 切币种 → 左→右展开。用 CSS clip-path 而非 echarts
 *   入场动画实现：CSS 动画按文档时间线推进，后台标签里照样走完，回到前台
 *   必然完整显示——不会像 rAF 驱动的入场动画那样在后台永远停在第一帧
 *   （正是之前的白屏根因）。底下 echarts 保持 animation:false 同步落画布，
 *   展开的只是可视层。
 */
const animateChart = ref(false)
const wipeChart = ref(false)
let animResetTimer: number | undefined

/** 重新触发一次左→右展开（先摘 class 再挂，restart 动画；出口实现） */
async function revealChart() {
  await revealChartShared(wipeChart)
}

watch(selectedCurrency, () => {
  animateChart.value = false // 币种整体换数据，走展开动画，不叠加数据过渡
  loadHistory().then(revealChart)
})
watch(range, () => {
  animateChart.value = true
  loadHistory()
})

watch(history, async () => {
  if (!animateChart.value) return
  await nextTick() // 等 VChart 把新 option setOption 进图表
  window.clearTimeout(animResetTimer)
  animResetTimer = window.setTimeout(() => {
    animateChart.value = false
  }, 500)
})

/** 窗口内值域（±5% padding；空数据回退 0~1 防发散） */
const yDomain = computed(() => {
  const values = history.value.map((h) => h.rateToCny)
  if (!values.length) return { min: 0, max: 1 }
  let min = Math.min(...values)
  let max = Math.max(...values)
  if (min === max) {
    min -= 0.05
    max += 0.05
  }
  const padding = (max - min) * 0.05
  return { min: min - padding, max: max + padding }
})

/** 点密度：稀疏窗口（≤240 点）显示节点圆点，密集窗口关 symbol（节点互相叠死） */
const showSymbols = computed(() => history.value.length <= 240)

const chartOption = computed(() => {
  const c = palette.value
  const tip = tipPalette.value
  return {
  // 动画只用于数据更新过渡（见 animateChart）；animationThreshold 抬到 1 万：
  // 默认 2000 会让 10 年/全量（3.6k/6k 点）窗口切换瞬间跳变无过渡
  animation: animateChart.value,
  animationDuration: 300,
  animationDurationUpdate: 400,
  animationEasingUpdate: 'cubicOut',
  animationThreshold: 10000,
  backgroundColor: 'transparent',
  grid: { left: 72, right: 24, top: 24, bottom: 36 },
  // 悬停时贴在轴上的指示线与轴标签框；不配就是 echarts 默认的灰线灰底白字
  axisPointer: axisPointerStyle(c, tip),
  tooltip: {
    trigger: 'axis',
    // 悬停窗口走 hl-popper 同款主题 token（echarts 默认白底黑字脱离双主题）
    backgroundColor: tip.bg,
    borderColor: tip.border,
    borderWidth: 1,
    borderRadius: 6,
    padding: [7, 11],
    textStyle: { color: tip.text, fontSize: 12 },
    extraCssText: `box-shadow: ${tip.shadow};`,
    // 类目轴按索引取数，tooltip 从 history 还原完整时间与值。
    // t() 在**悬停时**现取（不是构造 option 时）——formatter 每次调用都读
    // locale store，切语言后无需重算 option 也是对的。
    formatter: (params: unknown) => {
      const p = (Array.isArray(params) ? params[0] : params) as { dataIndex: number }
      const h = history.value[p.dataIndex]
      return h
        ? t('rates.chart.tooltip', {
            date: h.date,
            currency: currencyName(selectedCurrency.value),
            code: selectedCurrency.value,
            color: tip.accent,
            value: h.rateToCny.toFixed(6),
          })
        : ''
    },
  },
  xAxis: {
    type: 'category',
    boundaryGap: false,
    data: history.value.map((h) => h.date),
    axisLabel: { color: c.label, fontSize: 11 },
    axisLine: { lineStyle: { color: c.axis } },
  },
  yAxis: {
    type: 'value',
    min: yDomain.value.min,
    max: yDomain.value.max,
    // 轴名在 computed 求值期取 t()：读 locale store 即建立依赖，切语言会重算
    name: t('rates.chart.yAxis', {
      currency: currencyName(selectedCurrency.value),
      code: selectedCurrency.value,
    }),
    nameTextStyle: { color: c.label },
    axisLabel: {
      color: c.label,
      fontSize: 11,
      formatter: (v: number) => (yDomain.value.max - yDomain.value.min < 0.01 ? v.toFixed(4) : v.toFixed(2)),
    },
    splitLine: { lineStyle: { color: c.grid } },
  },
  series: [
    {
      type: 'line',
      // 类目轴下 series 按索引对齐；[完整时间戳, 值] 二元组的类目名
      // 匹配不到 xAxis.data，ECharts 会整组丢弃 → 画布只剩空坐标轴
      data: history.value.map((h) => h.rateToCny),
      symbol: showSymbols.value ? 'circle' : 'none',
      symbolSize: 4,
      lineStyle: { color: c.success, width: 1.5 },
      itemStyle: { color: c.success },
      areaStyle: { color: withAlpha(c.success, 0.1) },
    },
  ],
  }
})

const fmtTime = (t: string | null) => (t ? t.slice(0, 19).replace('T', ' ') : '—')

// ─── 货币换算器 ──────────────────────────────────────────
// 双向可编辑：convAmount 是用户输入的锚定值，convAnchor 记录锚定在哪一侧，
// 另一侧由汇率实时派生——单数据源 + 可写 computed，天然无循环更新。
const convFrom = ref('CNY')
const convTo = ref('USD')
const convAmount = ref('100')
const convAnchor = ref<'from' | 'to'>('from')

const rateIndex = computed(() => {
  const m = new Map<string, number>()
  for (const r of rates.value) m.set(r.currency, r.rateToCny)
  return m
})

/** 1 convFrom 兑若干 convTo（各币种 rateToCny 相除，CNY 基准桥接任意币对）
 *  局部变量刻意叫 rateFrom / rateTo 而不是 f / t：`t` 会遮蔽 useI18n 的 `t`，
 *  而这个 computed 现在没有 t() 调用、将来很可能有——那时 `t(...)` 会静默
 *  抛 "t is not a function"（被遮的那个是 number|null）。 */
const convRate = computed(() => {
  const rateFrom = rateIndex.value.get(convFrom.value)
  const rateTo = rateIndex.value.get(convTo.value)
  if (rateFrom == null || rateTo == null || rateTo === 0) return null
  return rateFrom / rateTo
})

/** 动态精度：微小值（IDR/VND 级）多留几位，常规值两位 */
const fmtConv = (v: number) => {
  const a = Math.abs(v)
  if (a === 0) return '0'
  if (a < 0.01) return v.toFixed(6)
  if (a < 1) return v.toFixed(4)
  return v.toFixed(2)
}

const fromModel = computed({
  get: () => {
    if (convAnchor.value === 'from') return convAmount.value
    const r = convRate.value
    const n = parseFloat(convAmount.value)
    if (r == null || Number.isNaN(n)) return ''
    return fmtConv(n / r)
  },
  set: (v: string) => {
    convAmount.value = v
    convAnchor.value = 'from'
  },
})

const toModel = computed({
  get: () => {
    if (convAnchor.value === 'to') return convAmount.value
    const r = convRate.value
    const n = parseFloat(convAmount.value)
    if (r == null || Number.isNaN(n)) return ''
    return fmtConv(n * r)
  },
  set: (v: string) => {
    convAmount.value = v
    convAnchor.value = 'to'
  },
})

/** 左右换向：币种对调、锚定侧翻到对侧——输入的数值跟着自己的币种卡走 */
function swapCurrencies() {
  ;[convFrom.value, convTo.value] = [convTo.value, convFrom.value]
  convAnchor.value = convAnchor.value === 'from' ? 'to' : 'from'
}

const historyCard = ref<HTMLElement | null>(null)

onMounted(async () => {
  await load(true)
  // 深链进入时定位到走势板块（等首帧渲染完再滚）
  if (!deepLinkCurrency) return
  await nextTick()
  historyCard.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
})
</script>

<template>
  <section class="rates-page">
    <div class="card section-card" data-section="rates.section.rates">
      <div class="settings-card__header">
        <div>
          <div class="section-title">{{ t('rates.section.rates') }}</div>
          <div class="section-desc">
            {{
              lastSource
                ? t('rates.header.descWithSource', { source: lastSource })
                : t('rates.header.desc')
            }}
          </div>
        </div>
        <el-button type="primary" :icon="Refresh" :loading="refreshing" @click="refresh">
          {{ t('rates.action.refresh') }}
        </el-button>
      </div>

      <div class="tracked-row">
        <span class="tracked-row__label">{{ t('rates.tracked.label') }}</span>
        <!-- 例外：HlSelect 暂不支持 multiple+filterable+collapse-tags，待增强后替换 -->
        <el-select
          v-model="trackedModel"
          class="tracked-row__select"
          multiple
          filterable
          collapse-tags
          collapse-tags-tooltip
          :max-collapse-tags="8"
          :placeholder="t('rates.tracked.placeholder')"
        >
          <el-option v-for="c in currencyOptions" :key="c.code" :value="c.code" :label="currencyName(c.code)">
            <span class="currency-option">
              <img :src="currencyFlagUrl(c.code)" class="currency-option__flag" alt="" />
              <span>{{ currencyName(c.code) }}</span>
              <span class="currency-option__code">{{ c.code }}</span>
            </span>
          </el-option>
        </el-select>
      </div>

      <div v-if="trackedCurrencies.length" class="rate-grid">
        <div v-for="r in trackedCurrencies" :key="r.currency" class="rate-tile"
          :class="{ 'is-active': r.currency === selectedCurrency }"
          @click="selectedCurrency = r.currency"
        >
          <div class="rate-tile__head">
            <CurrencyFlag :code="r.currency" />
          </div>
          <div class="rate-tile__rate">{{ r.rateToCny.toFixed(4) }}</div>
          <div class="rate-tile__time">{{ fmtTime(r.fetchedAt) }}</div>
        </div>
      </div>
      <HlEmpty v-else-if="!loading" size="sm" icon="" :text="t('rates.tracked.empty')" />
    </div>

    <div ref="historyCard" class="card section-card" data-section="rates.section.history">
      <div class="settings-card__header">
        <div class="section-title">
          {{
            t('rates.history.title', {
              currency: currencyName(selectedCurrency),
              code: selectedCurrency,
            })
          }}
        </div>
        <div class="history-controls">
          <div class="range-group">
            <button
              v-for="r in RATE_RANGES"
              :key="r.id"
              type="button"
              class="range-btn"
              :class="{ 'is-active': range === r.id }"
              @click="range = r.id"
            >
              {{ t(r.labelKey) }}
            </button>
          </div>
          <HlSelect v-model="selectedCurrency" :options="currencySelectOptions" class="history-select" />
        </div>
      </div>
      <VChart
        v-if="history.length"
        class="history-chart"
        :class="{ 'hl-chart-wipe': wipeChart }"
        :option="chartOption"
        autoresize
      />
      <HlEmpty
        v-else
        size="sm"
        icon=""
        :text="
          t('rates.history.empty', {
            currency: currencyName(selectedCurrency),
            code: selectedCurrency,
          })
        "
      />
    </div>

    <div class="card section-card" data-section="rates.section.convert">
      <div class="settings-card__header">
        <div class="section-title">{{ t('rates.section.convert') }}</div>
        <div v-if="convRate != null" class="conv-hint">
          {{
            t('rates.conv.hint', { from: convFrom, rate: fmtConv(convRate), to: convTo })
          }}
        </div>
      </div>
      <div class="conv-row">
        <div class="conv-side">
          <HlSelect v-model="convFrom" :options="currencySelectOptions" class="conv-select" />
          <HlInput
            v-model="fromModel"
            inputmode="decimal"
            :placeholder="t('rates.conv.amountPlaceholder')"
            class="conv-amount"
          />
        </div>
        <HlButton
          variant="ghost"
          size="sm"
          class="conv-swap"
          :title="t('rates.conv.swap')"
          @click="swapCurrencies"
        >
          <HlIcon name="arrow-left-right" />
        </HlButton>
        <div class="conv-side">
          <HlSelect v-model="convTo" :options="currencySelectOptions" class="conv-select" />
          <HlInput
            v-model="toModel"
            inputmode="decimal"
            :placeholder="t('rates.conv.amountPlaceholder')"
            class="conv-amount"
          />
        </div>
      </div>
    </div>

    <div class="card section-card" data-section="rates.section.allCurrencies">
      <div class="section-title">{{ t('rates.allCurrencies.title', { n: allRates.length }) }}</div>
      <el-table
        v-if="allRates.length"
        :data="allRates"
        size="small"
        max-height="320"
        style="width: 100%; margin-top: 12px"
      >
        <el-table-column :label="t('rates.table.name')" min-width="160">
          <template #default="{ row }">
            <CurrencyFlag :code="row.currency" />
          </template>
        </el-table-column>
        <el-table-column prop="currency" :label="t('rates.table.currency')" width="100" />
        <el-table-column :label="t('rates.table.rate')" min-width="140">
          <template #default="{ row }">{{ row.rateToCny.toFixed(6) }}</template>
        </el-table-column>
        <el-table-column :label="t('rates.table.updated')" width="170">
          <template #default="{ row }">{{ fmtTime(row.fetchedAt) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </section>
</template>

<style scoped>
.rates-page {
  max-width: 960px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-card {
  padding: 20px 24px;
}

.settings-card__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

/* ─── 追踪币种自选 ─── */
.tracked-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 12px;
}

.tracked-row__label {
  font-size: 13px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.tracked-row__select {
  flex: 1;
  min-width: 0;
}

.currency-option {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.currency-option__flag {
  width: 18px;
  height: 13px;
  border-radius: 2px;
  object-fit: cover;
  flex-shrink: 0;
}

.currency-option__code {
  font-size: 11px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

/* ─── 汇率瓦片 ─── */
.rate-grid {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(150px, 1fr));
  gap: 8px;
}

.rate-tile {
  padding: 12px 14px;
  border-radius: var(--radius);
  border: 1px solid var(--border-soft);
  background: var(--bg-soft);
  cursor: pointer;
  transition: all var(--transition);
}

.rate-tile:hover {
  border-color: var(--border-strong);
}

.rate-tile.is-active {
  /* 取令牌阶梯而不是写 rgba(164,208,7,…)：那是深色主题 --success 的取值，
     浅色主题下 --success 是 #6b9a00，选中态会一直挂着深色的绿。 */
  border-color: var(--success-a50);
  background: var(--success-a08);
}

.rate-tile__head {
  font-size: 12px;
  color: var(--text-muted);
}

.rate-tile__rate {
  font-size: 17px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 2px 0;
}

.rate-tile__time {
  font-size: 11px;
  color: var(--text-faint, #5a7080);
}

.history-select {
  width: 160px;
}

.history-controls {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}

.range-group {
  display: inline-flex;
  gap: 2px;
  padding: 2px;
  border-radius: var(--radius, 6px);
  background: var(--bg-soft);
  border: 1px solid var(--border-soft);
}

.range-btn {
  appearance: none;
  border: none;
  background: transparent;
  color: var(--text-muted);
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 4px;
  cursor: pointer;
  transition: all var(--transition, 0.15s);
  line-height: 1.4;
}

.range-btn:hover {
  color: var(--text-primary);
  background: var(--accent-soft);
}

.range-btn.is-active {
  color: var(--on-accent-fill);
  background: var(--accent-fill);
  font-weight: 600;
}

.history-chart {
  width: 100%;
  height: 280px;
  margin-top: 12px;
}

/* 图表左→右展开的 keyframes 与降级已上移到 hl-framework.css（.hl-chart-wipe）：
   本文件与 PriceTrendChart.vue 此前各持一份逐字相同的副本。 */

/* ─── 货币换算器 ─── */
.conv-hint {
  font-size: 12px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.conv-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}

.conv-side {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.conv-select {
  width: 100%;
}

.conv-amount {
  width: 100%;
}

.conv-swap {
  flex-shrink: 0;
}

/* 深链定位目标（仪表盘迷你汇率行直达）：sticky 导航栏占据视口顶部，滚动定位让出其高度 */
.section-card[data-section='rates.section.history'] {
  scroll-margin-top: calc(var(--navbar-h, 68px) + 8px);
}
</style>
