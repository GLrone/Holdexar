<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { DataZoomComponent, GridComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'

import { gamesApi, type HistoryPayload } from '@/api/client'
import {
  axisPointerStyle,
  revealChart,
  useChartPalette,
  useTipPalette,
  withAlpha,
} from '@/api/chartTheme'
import { useI18n } from '@/locales'
use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, DataZoomComponent])

/**
 * 价格历史走势图 —— component-framework.html 模块 F 权威模版：
 * Steam 价阶梯线 + Key 店走线（绿色）+ 史低黄色平虚线 + 面积幕布渐变
 * （贴线处不透明 → 向下淡出）+ 史低节点绿点光晕 + 底部全局缩略导航条。
 * Key 店现价来自 /games/{appid}/cdk（SteamPY/SteamCICI 实时查价，无历史
 * 序列 → 以「当前价横线」呈现，与库内史低平线同一形态语义）。
 * 卡片抽屉 / 详情页共用。数据须为全量序列（接口 days=0），时间窗由
 * windowDays 预设、导航条拖拽后经 window-change 回传实际跨度，不回源。
 * 颜色走主题语义变量（--chart-*），依赖 isDark 触发重算。
 * animation:false —— rAF 后台限流会让入场动画永停首帧（汇率图同款坑）。
 * 左→右描线入场用 CSS clip-path wipe（revealChart）实现：文档时间线推进，
 * 后台标签页照样走完，与 echarts animation 互不干扰。
 */
const props = withDefaults(
  defineProps<{
    payload: HistoryPayload | null
    height?: string
    /** 可见时间窗（天）：0 = 全部 */
    windowDays?: number
    /** 是否叠加 Key 店走线（详情页大图开，抽屉窄图关） */
    withKeyLine?: boolean
    /** Key 店查价用 appid（withKeyLine 时必传） */
    appid?: number
  }>(),
  { height: '300px', windowDays: 0, withKeyLine: false, appid: 0 },
)

const emit = defineEmits<{ (e: 'window-change', days: number): void }>()

/** 文案出口（词条在 src/locales/{zh-CN,en}/trendChart.ts） */
const { t } = useI18n()

/**
 * series.name —— **语言无关的标识，不是文案**，故不进词典、不接 t()。
 *
 * ① 用户看不到它：本组件没注册 echarts legend，tooltip 又是全自定义 formatter
 *    （只读 params.dataIndex，从不读 seriesName）。用户能看到的文案各有其键 ——
 *    图例走 trendChart.legend.*、悬停窗走 trendChart.tip.*，都与这里无关。
 * ② 它却是 echarts 的**组件身份键**：同一实例上反复 setOption 时按 id → name →
 *    下标匹配（vue-echarts 深度侦听 option，option 一变就重新 setOption）。
 *    接上 t() 会让整个 option 随语言重算 —— 切一次语言白白重刷整幅图，并让
 *    option 里的 dataZoom startValue/endValue 再按 props.windowDays 重放一次。
 */
const SERIES = { current: 'current', keyStore: 'keyStore', lowest: 'lowest' } as const

function parseTs(ts: string | null | undefined): number | null {
  if (!ts) return null
  const t = new Date(ts.replace(' ', 'T')).getTime()
  return Number.isFinite(t) ? t : null
}

/** "¥3.0" / "3.0" → 3（Key 店挂牌价字符串 → 元） */
function parseYuan(s: string | null | undefined): number | null {
  if (!s) return null
  const n = Number(s.replace(/[^\d.]/g, ''))
  return Number.isFinite(n) && n > 0 ? n : null
}

/** 画布配色：Steam 价（品牌色）/ Key 店（成功色）/ 史低线（警示色），走共享出口 */
const palette = useChartPalette()

/** tooltip 主题化配色（悬停提示窗红线：echarts 默认白底脱离双主题，禁止） */
const tipPalette = useTipPalette()

const points = computed(() => props.payload?.points ?? [])
const hasPoints = computed(() => points.value.length > 0)

/** 有效点：缺 CNY 折算的行剔除（一个假 0 会把史低幕布永久砸穿），
 *  时间戳解析不出的行同样剔除——时间轴上一个 ts=0 的点会把轴拉到 1970。 */
const pts = computed(() =>
  points.value.filter((p) => p.cnyYuan != null && parseTs(p.timestamp) != null),
)
const ys = computed(() => pts.value.map((p) => p.cnyYuan ?? 0))
/** 与 pts 同序的时间戳（ms）。时间轴要求数据以 [时间, 值] 成对给出 */
const tsList = computed(() => pts.value.map((p) => parseTs(p.timestamp) as number))
/** 史低 = 全序列最低（平线，对齐模版「历史最低」语义） */
const lowYuan = computed(() => {
  const v = ys.value
  return v.length ? Math.min(...v) : null
})
/** 史低节点：价格刷新全序列最低值的拐点（tooltip 標「史低节点」用） */
const lowEventIdx = computed(() => {
  const set = new Set<number>()
  let min = Infinity
  pts.value.forEach((p, i) => {
    if (p.cnyFen && p.cnyFen > 0 && p.cnyFen < min) {
      set.add(i)
      min = p.cnyFen
    }
  })
  return set
})

/** Key 店现价（CDK 实时查价；请求失败/未挂牌 = null 不画） */
const keyPriceYuan = ref<number | null>(null)

async function loadKeyPrice() {
  if (!props.withKeyLine || !props.appid || !props.payload) {
    keyPriceYuan.value = null
    return
  }
  try {
    const res = await gamesApi.cdk(props.appid)
    keyPriceYuan.value = parseYuan(res.steampy.price ?? res.steamcici.price ?? null)
  } catch {
    keyPriceYuan.value = null
  }
}

watch([() => props.payload, () => props.withKeyLine, () => props.appid], () => loadKeyPrice(), {
  immediate: true,
})

/** 时间窗起点（时间戳，ms）。以最后切片时刻为锚，爬取滞后也成立 */
function windowStartTs(days: number): number {
  const t = tsList.value
  if (!t.length) return 0
  if (!days) return t[0]
  return Math.max(t[0], t[t.length - 1] - days * 86400_000)
}

/** 轴标签粒度随可见跨度自适应：跨度 >300 天看年月，否则看月日 */
function fmtTick(ts: number, yearMonth: boolean): string {
  const d = new Date(ts)
  const p = (n: number) => String(n).padStart(2, '0')
  return yearMonth ? `${d.getFullYear()}-${p(d.getMonth() + 1)}` : `${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

const tickFormatter = computed(() => {
  const t = tsList.value
  const full = t.length > 1 ? (t[t.length - 1] - t[0]) / 86400_000 : 0
  const win = props.windowDays && props.windowDays > 0 ? Math.min(props.windowDays, full) : full
  return (v: number) => fmtTick(v, win > 300)
})

/** 面积幕布：贴线不透明 → 底部淡出（模版 rgba(164,208,7,0.12) 顶重底轻） */
function fadeArea(color: string, top: number, bottom: number) {
  return {
    type: 'linear',
    x: 0,
    y: 0,
    x2: 0,
    y2: 1,
    global: false,
    colorStops: [
      { offset: 0, color: withAlpha(color, top) },
      { offset: 1, color: withAlpha(color, bottom) },
    ],
  }
}

const chartOption = computed(() => {
  const c = palette.value
  const tip = tipPalette.value
  const ps = pts.value
  const n = ps.length
  const low = lowYuan.value
  const keyPrice = keyPriceYuan.value
  const series: Record<string, unknown>[] = [
    {
      name: SERIES.current,
      type: 'line',
      step: 'end',
      symbol: 'none',
      // 时间轴：数据必须是 [时间戳, 值] 对。样本是按变价事件记录的（促销期密集、
      // 平稳期稀疏），类别轴会把 201 天的空档画得和 8 天一样宽——「时间对不上」
      // 的另一半根因就在这。
      data: ps.map((p, i) => [tsList.value[i], p.cnyYuan ?? 0]),
      lineStyle: { color: c.line, width: 2 },
      itemStyle: { color: c.line },
      areaStyle: { color: fadeArea(c.line, 0.1, 0) },
      // 史低节点绿点光晕：价格刷新全序列最低值的拐点（tooltip 同标注）
      markPoint: {
        symbol: 'circle',
        symbolSize: 11,
        animation: false,
        itemStyle: {
          color: c.success,
          borderColor: withAlpha(c.success, 0.3),
          borderWidth: 5,
        },
        label: { show: false },
        data: [...lowEventIdx.value].map((i) => ({
          coord: [tsList.value[i], ys.value[i]],
        })),
      },
      z: 3,
    },
  ]
  // Key 店走线：绿色实线（现价横线覆盖全序列）+ 面积幕布
  if (keyPrice != null) {
    series.push({
      name: SERIES.keyStore,
      type: 'line',
      step: 'end',
      symbol: 'none',
      data: tsList.value.map((t) => [t, keyPrice]),
      lineStyle: { color: c.success, width: 2 },
      itemStyle: { color: c.success },
      areaStyle: { color: fadeArea(c.success, 0.14, 0) },
      z: 2,
    })
  }
  // 史低平线：黄色虚线（不铺幕布，模版语义就是一条参考线）
  if (low != null) {
    series.push({
      name: SERIES.lowest,
      type: 'line',
      symbol: 'none',
      data: tsList.value.map((t) => [t, low]),
      lineStyle: { color: c.warning, width: 1.5, type: [6, 4], opacity: 0.8 },
      itemStyle: { color: c.warning },
      z: 2,
    })
  }
  return {
    backgroundColor: 'transparent',
    // rAF 被后台窗口限流时入场动画永停首帧（汇率图同款坑），直接关掉
    animation: false,
    grid: { left: 12, right: 52, top: 16, bottom: 66 },
    // 悬停时贴在坐标轴上的指示线与标签框（走 tooltip 同款底/框；不配就走 echarts
    // 默认的灰底白字，两个主题下都突兀）
    axisPointer: axisPointerStyle(c, tip),
    tooltip: {
      trigger: 'axis',
      // 悬停窗走主题 token（echarts 默认白底黑字脱离双主题体系）
      backgroundColor: tip.bg,
      borderColor: tip.border,
      borderWidth: 1,
      borderRadius: 6,
      padding: [7, 11],
      textStyle: { color: tip.text, fontSize: 12 },
      extraCssText: `box-shadow: ${tip.shadow};`,
      formatter: (params: unknown) => {
        const arr = params as { dataIndex: number; seriesName: string }[]
        const i = arr?.[0]?.dataIndex ?? -1
        const p = ps[i]
        if (!p) return ''
        let html = `<b>${(p.timestamp ?? '').slice(0, 16).replace('T', ' ')}</b>`
        // 与图上 markPoint 同色（此前这里写死 #2ed573，而那个点用的是 --success——
        // 同一语义两个绿，且 #2ed573 在 token 表里根本不存在）
        if (lowEventIdx.value.has(i))
          html += ` · <span style="color:${c.success}">${t('trendChart.tip.lowNode')}</span>`
        // 现价一行：带原价时合并成**一条**带占位符的词条（`现价 ¥9.99（¥19.99）`），
        // 不拆成「标签 + 原价」两段拼——英文的括号与语序都不同，拼不出来
        const cur = (p.cnyYuan ?? 0).toFixed(2)
        html += `<br/>${
          p.formattedPrice
            ? t('trendChart.tip.currentWithOriginal', { v: cur, original: p.formattedPrice })
            : t('trendChart.tip.current', { v: cur })
        }`
        if (p.discount > 0) html += `<br/>${t('trendChart.tip.discount', { n: p.discount })}`
        if (keyPrice != null)
          html += `<br/><span style="color:${c.success}">${t('trendChart.tip.keyStore', { v: keyPrice.toFixed(2) })}</span>`
        if (low != null)
          html += `<br/><i style="color:${c.warning}">${t('trendChart.tip.lowest', { v: low.toFixed(2) })}</i>`
        return html
      },
    },
    xAxis: {
      // 时间轴（不是类别轴）：点距按真实时间成比例，稀疏段留白可见
      type: 'time',
      axisLine: { lineStyle: { color: c.axis } },
      axisTick: { show: false },
      axisLabel: {
        color: c.label,
        fontSize: 11,
        hideOverlap: true,
        formatter: (v: number) => tickFormatter.value(v),
      },
    },
    yAxis: {
      type: 'value',
      position: 'right',
      axisLabel: { color: c.label, fontSize: 11 },
      splitLine: { lineStyle: { color: c.grid } },
    },
    dataZoom: [
      {
        type: 'slider',
        xAxisIndex: 0,
        left: 12,
        right: 52,
        bottom: 8,
        height: 32,
        // 时间轴下 startValue/endValue 是**时间戳**（不是索引）
        startValue: windowStartTs(props.windowDays ?? 0),
        endValue: tsList.value[n - 1],
        brushSelect: false,
        borderColor: c.axis,
        fillerColor: withAlpha(c.line, 0.14),
        handleStyle: { color: c.line },
        moveHandleStyle: { color: c.line },
        dataBackground: {
          lineStyle: { color: withAlpha(c.line, 0.35) },
          areaStyle: { color: withAlpha(c.line, 0.1) },
        },
        selectedDataBackground: {
          lineStyle: { color: c.line },
          areaStyle: { color: withAlpha(c.line, 0.18) },
        },
        textStyle: { color: c.label, fontSize: 10 },
      },
    ],
    series,
  }
})

// ── 时间窗同步：父级预设 → 图表开窗；导航条拖拽 → 回传实际跨度 ──
const chartRef = ref<{ dispatchAction: (action: Record<string, unknown>) => void } | null>(null)
let applyingZoom = false

function applyWindow() {
  const inst = chartRef.value
  if (!inst || !hasPoints.value) return
  applyingZoom = true
  inst.dispatchAction({
    type: 'dataZoom',
    startValue: windowStartTs(props.windowDays ?? 0),
    endValue: tsList.value[pts.value.length - 1],
  })
  Promise.resolve().then(() => {
    applyingZoom = false
  })
}

/** 导航条拖拽期 datazoom 会连续触发；逐次回传会让父级 windowDays 高频变化，
 *  每次都重放整幅描线（还会落进 revealChart 的打断窗口）。停手后回传一次。 */
let zoomEmitTimer: number | undefined

interface ZoomPayload {
  startValue?: number
  endValue?: number
}

function onZoom(e: unknown) {
  if (applyingZoom) return // 自家 dispatch 的回声不回传
  const ev = e as { batch?: ZoomPayload[] } & ZoomPayload
  const z = ev.batch?.[0] ?? ev
  // 时间轴下不能再用「百分比 → 索引」回推时间：百分比映射的是时间跨度，
  // 而点数在跨度上分布不均（促销期密集），换算出的索引会指向错误的切片。
  // 事件里已经带了时间戳形态的 startValue/endValue，直接取。
  const t0 = typeof z?.startValue === 'number' ? z.startValue : null
  const t1 = typeof z?.endValue === 'number' ? z.endValue : null
  if (t0 == null || t1 == null) return
  const days = Math.max(1, Math.round((t1 - t0) / 86400_000))
  window.clearTimeout(zoomEmitTimer)
  zoomEmitTimer = window.setTimeout(() => emit('window-change', days), 160)
}

watch(() => props.windowDays, () => nextTick(applyWindow))
watch(() => props.payload, () => nextTick(applyWindow))
onMounted(() => nextTick(applyWindow))
onUnmounted(() => window.clearTimeout(zoomEmitTimer))

// ── 左→右描线入场：CSS clip-path 展开（文档时间线推进，后台标签页照样
//    走完，规避 rAF 后台限流把入场动画永停首帧的坑；echarts 保持
//    animation:false 同步落画布，展开的只是可视层）──
const wipe = ref(false)
let lastPayload: HistoryPayload | null = null
// 以挂载时的实际刻度为基线。此前恒为 0，导致「打开抽屉时已是 90 天 → 切回
// 全部」这条路径上 d=0 === lastWindowDays=0 而判定「没变」，确定不播动画。
let lastWindowDays = props.windowDays ?? 0

watch(
  () => props.payload,
  (p) => {
    // 仅「换序列对象」时展开（打开抽屉 / 切版本 / 切地区加载完成）；
    // windowDays 纯前端开窗不触发
    const changed = p !== lastPayload
    lastPayload = p
    if (changed && p && p.points.length) revealChart(wipe)
  },
  { immediate: true },
)

// 时间刻度切换（range chips）同样走展开动画——重放 wipe
watch(
  () => props.windowDays,
  (d) => {
    if (d !== lastWindowDays && hasPoints.value) revealChart(wipe)
    lastWindowDays = d
  },
)
</script>

<template>
  <div class="trend-chart-wrap">
    <VChart
      v-if="hasPoints"
      ref="chartRef"
      class="trend-chart"
      :class="{ 'hl-chart-wipe': wipe }"
      :style="{ height }"
      :option="chartOption"
      autoresize
      @datazoom="onZoom"
    />
    <div v-else class="trend-chart__empty" :style="{ height }">
      <slot name="empty">{{ t('trendChart.empty') }}</slot>
    </div>
    <!-- 图例（详情页大图随 key 线显示；抽屉窄图用自身的 pc-legend） -->
    <div v-if="hasPoints && withKeyLine" class="trend-chart__legend">
      <span class="lg-item"><i class="lg-steam"></i>{{ t('trendChart.legend.steam') }}</span>
      <span v-if="keyPriceYuan != null" class="lg-item"><i class="lg-key"></i>{{ t('trendChart.legend.keyStore') }}</span>
      <span v-if="lowYuan != null" class="lg-item"><i class="lg-low"></i>{{ t('trendChart.legend.lowest') }}</span>
    </div>
  </div>
</template>

<style scoped>
.trend-chart-wrap {
  display: flex;
  flex-direction: column;
}

.trend-chart {
  width: 100%;
}

/* 左→右描线入场的 keyframes 与降级已上移到 hl-framework.css（.hl-chart-wipe）：
   本文件与 rates/Index.vue 此前各持一份逐字相同的副本。 */

.trend-chart__legend {
  display: flex;
  gap: 14px;
  margin-top: 6px;
  padding-left: 4px;
}

.lg-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-muted);
}

.lg-steam {
  width: 16px;
  height: 0;
  border-top: 2px solid var(--accent);
}

.lg-key {
  width: 16px;
  height: 0;
  border-top: 2px solid var(--success);
}

.lg-low {
  width: 16px;
  height: 0;
  border-top: 2px dashed var(--warning);
}

.trend-chart__empty {
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  color: var(--text-muted);
  text-align: center;
  padding: 0 16px;
}
</style>
