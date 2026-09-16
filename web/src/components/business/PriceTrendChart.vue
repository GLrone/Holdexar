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

/** 全量走势缩略条的**底图几何**——这条缩略条同时就是时间窗控制器：
 *  走势画在它身上，窗口暗幕与把手直接叠在这张走势图上（见模板的窗口控制层）。
 *
 *  此前这一层用的是 echarts dataZoom 内置的 dataBackground「数据影子」：它从该轴上
 *  **第一条可用序列**里挑一条当代表（echarts 源码注释即 "Find a representative
 *  series"），把该序列的**原始数据**画成一条不分级的折线，再按这条序列自身的极值
 *  重新标定。于是缩略条的曲线既不是主图任何一条线的形状（主图是阶梯线、共享值域、
 *  还有 Key 店线与史低线），也没有「它到底画的哪条序列」的确定语义——看图时就是一条
 *  来路不明的曲线。
 *
 *  现在口径与所选版本 × 地区的主图完全一致：
 *   · 同一批序列——Steam 阶梯线（step: 'end'）+ 幕布、Key 店横线、史低虚线；
 *   · 同一值域——三条序列的并集极值（主图 y 轴吃的也是这批极值）；
 *   · 恒为全量时间跨度——窗口只压暗不裁窄，条上永远看得到全部历史。
 *  响应式 computed：切版本 / 切地区拿到新 payload 即重算，不需要额外同步。 */
const THUMB_W = 1000
/** 与 .pc-range-svg 的 CSS 高度一致（36px，模版尺寸）——y 坐标即像素，好对账 */
const THUMB_H = 36
const THUMB_VIEWBOX = `0 0 ${THUMB_W} ${THUMB_H}`

const thumbGeo = computed(() => {
  const ts = tsList.value
  const vals = ys.value
  const n = ts.length
  if (!n) return null
  const key = keyPriceYuan.value
  const low = lowYuan.value
  const all = [...vals, ...(key != null ? [key] : []), ...(low != null ? [low] : [])]
  const vMin = Math.min(...all)
  const vMax = Math.max(...all)
  const vSpan = vMax - vMin
  const t0 = ts[0]
  const tSpan = ts[n - 1] - t0
  const px = (tsv: number) => (tSpan > 0 ? ((tsv - t0) / tSpan) * THUMB_W : 0)
  // 上下各留 3px，线不贴边；极值退化成一条水平线时落中线
  const py = (v: number) => (vSpan > 0 ? THUMB_H - 3 - ((v - vMin) / vSpan) * (THUMB_H - 6) : THUMB_H / 2)
  const r = (v: number) => Math.round(v * 100) / 100
  // 阶梯线（与主图 step: 'end' 同形）：先横走到下一时刻，再竖跳到新价
  let d = `M ${r(px(ts[0]))} ${r(py(vals[0]))}`
  for (let i = 1; i < n; i++) d += ` L ${r(px(ts[i]))} ${r(py(vals[i - 1]))} L ${r(px(ts[i]))} ${r(py(vals[i]))}`
  const area = `${d} L ${r(px(ts[n - 1]))} ${THUMB_H} L ${r(px(ts[0]))} ${THUMB_H} Z`
  return {
    line: d,
    area,
    keyY: key != null ? r(py(key)) : null,
    lowY: low != null ? r(py(low)) : null,
  }
})

const chartOption = computed(() => {
  const c = palette.value
  const tip = tipPalette.value
  const ps = pts.value
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
    // 导航条视图已移出画布（见 dataZoom 注释），底部只留轴标签的位置
    grid: { left: 12, right: 52, top: 16, bottom: 34 },
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
        // 只见状态、不见视图：导航条的可视部分就是下方的全量走势缩略条本身
        // （控制层画在那张走势图上，见 thumbGeo / winPct）。这里只留一个
        // 不渲染的 dataZoom 承载窗口状态，接收 dispatchAction。
        type: 'slider',
        show: false,
        xAxisIndex: 0,
        brushSelect: false,
      },
    ],
    series,
  }
})

// ── 时间窗：缩略条即控制器（拖动把手改宽度 / 拖窗口平移 / 点暗幕跳转）──
// 窗口状态由本组件持有（winStart/winEnd，ms），图表侧只被动接收 dispatchAction。
// 窗口不再写进 option：option 每次重算（切主题、换 payload）都会重放其中的
// startValue/endValue，用户拖出来的区间会被拽回「最后 N 天」的锚点。
const chartRef = ref<{ dispatchAction: (action: Record<string, unknown>) => void } | null>(null)

/** 当前时间窗（ms），控制层的位置与暗幕都由它算 */
const winStart = ref(0)
const winEnd = ref(0)

/** 缩略图 svg（描线入场用）与叠在它上面的滑块区（拖拽换算量它的实际像素宽度，
 *  不能用 viewBox 宽度）——两者同宽同高、完全重合，见 .pc-slider-zone 的负 margin */
const thumbRef = ref<SVGSVGElement | null>(null)
const zoneRef = ref<HTMLElement | null>(null)

function dispatchWindow() {
  const inst = chartRef.value
  if (!inst || !hasPoints.value) return
  inst.dispatchAction({ type: 'dataZoom', startValue: winStart.value, endValue: winEnd.value })
}

/** 父级给的是「跨度天数」，锚在最后一片切片上（与 windowStartTs 同一口径） */
function applyWindow() {
  const ts = tsList.value
  if (!ts.length) return
  winStart.value = windowStartTs(props.windowDays ?? 0)
  winEnd.value = ts[ts.length - 1]
  dispatchWindow()
}

/** 自 emit 回环：拖完回传的天数原样落回 props.windowDays，此时**不能**再按锚点
 *  重开窗口——那会把用户刚拖到的历史区间拽回「最后 N 天」。消费一次即清。 */
let lastEmittedDays: number | null = null

watch(
  () => props.windowDays,
  (d) => {
    if (lastEmittedDays !== null && d === lastEmittedDays) {
      lastEmittedDays = null
      return
    }
    nextTick(applyWindow)
  },
)
watch(() => props.payload, () => nextTick(applyWindow))
onMounted(() => nextTick(applyWindow))

/** 窗口在条上的占比（0..1）——与图形几何同一把时间尺子 */
const winPct = computed(() => {
  const ts = tsList.value
  if (ts.length < 2) return { start: 0, end: 1 }
  const t0 = ts[0]
  const span = ts[ts.length - 1] - t0 || 1
  const clamp = (v: number) => Math.max(0, Math.min(1, v))
  const a = clamp((winStart.value - t0) / span)
  const b = clamp((winEnd.value - t0) / span)
  return { start: Math.min(a, b), end: Math.max(a, b) }
})

/** 标签行右端：当前窗口的起止月份 + 跨度（整段 =「全部」，否则月数） */
const thumbRangeText = computed(() => {
  const ts = tsList.value
  if (ts.length < 2) return ''
  const full = ts[ts.length - 1] - ts[0] || 1
  const span = winEnd.value - winStart.value
  return t('trendChart.thumb.range', {
    start: fmtTick(winStart.value, true),
    end: fmtTick(winEnd.value, true),
    span:
      span >= full * 0.999
        ? t('trendChart.thumb.all')
        : t('trendChart.thumb.months', {
            n: Math.max(1, Math.round(span / (30.44 * 86400_000))),
          }),
  })
})

/** 信息行两端的锚点月份（与轴刻度同一 YYYY-MM 粒度） */
const thumbStartLabel = computed(() => (tsList.value.length ? fmtTick(winStart.value, true) : ''))
const thumbEndLabel = computed(() =>
  tsList.value.length ? fmtTick(winEnd.value, true) : '',
)

type DragMode = 'left' | 'right' | 'pan'
let drag: { mode: DragMode; x0: number; s0: number; e0: number; w: number } | null = null

/** 拖拽中每帧都重算窗口：只发 dispatchAction，不重算 option（option 里没有窗口） */
function onDragMove(ev: PointerEvent) {
  const ts = tsList.value
  if (!drag || ts.length < 2) return
  const t0 = ts[0]
  const t1 = ts[ts.length - 1]
  const span = t1 - t0 || 1
  const dt = ((ev.clientX - drag.x0) / drag.w) * span
  // 最短窗口：两天（价格切片本身就是按天记的，再窄没有可读信息）
  const minSpan = Math.min(span, Math.max(2 * 86400_000, span * 0.01))
  if (drag.mode === 'pan') {
    let s = drag.s0 + dt
    let e = drag.e0 + dt
    if (s < t0) {
      e += t0 - s
      s = t0
    }
    if (e > t1) {
      s -= e - t1
      e = t1
    }
    winStart.value = Math.max(t0, s)
    winEnd.value = Math.min(t1, e)
  } else if (drag.mode === 'left') {
    winStart.value = Math.min(Math.max(t0, drag.s0 + dt), drag.e0 - minSpan)
  } else {
    winEnd.value = Math.max(Math.min(t1, drag.e0 + dt), drag.s0 + minSpan)
  }
  dispatchWindow()
}

function endDrag() {
  if (!drag) return
  drag = null
  window.removeEventListener('pointermove', onDragMove)
  window.removeEventListener('pointerup', endDrag)
  window.removeEventListener('pointercancel', endDrag)
  // 落点才回传（拖拽期逐帧回传会让父级 windowDays 高频变化、反复重放描线动画）
  const days = Math.max(1, Math.round((winEnd.value - winStart.value) / 86400_000))
  lastEmittedDays = days
  emit('window-change', days)
}

function beginDrag(ev: PointerEvent, mode: DragMode) {
  const el = zoneRef.value
  if (!el || tsList.value.length < 2) return
  const rect = el.getBoundingClientRect()
  if (!rect.width) return
  ev.preventDefault()
  drag = { mode, x0: ev.clientX, s0: winStart.value, e0: winEnd.value, w: rect.width }
  window.addEventListener('pointermove', onDragMove)
  window.addEventListener('pointerup', endDrag)
  window.addEventListener('pointercancel', endDrag)
}

/** 点窗口之外（滑块区里的暗幕是 pointer-events:none，点击落到缩略图本体上，
 *  与模版 `.pc-range-svg{cursor:crosshair}` 同一语义）→ 窗口中心跳到该处、
 *  跨度不变，随后可继续拖 = 平移 */
function onStripDown(ev: PointerEvent) {
  const el = zoneRef.value
  const ts = tsList.value
  if (!el || ts.length < 2) return
  const rect = el.getBoundingClientRect()
  if (!rect.width) return
  const t0 = ts[0]
  const t1 = ts[ts.length - 1]
  const span = t1 - t0 || 1
  const hit = t0 + Math.max(0, Math.min(1, (ev.clientX - rect.left) / rect.width)) * span
  const width = winEnd.value - winStart.value
  const s = Math.max(t0, Math.min(t1 - width, hit - width / 2))
  winStart.value = s
  winEnd.value = s + width
  dispatchWindow()
  beginDrag(ev, 'pan')
}

onUnmounted(() => {
  window.removeEventListener('pointermove', onDragMove)
  window.removeEventListener('pointerup', endDrag)
  window.removeEventListener('pointercancel', endDrag)
})

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
    />
    <div v-else class="trend-chart__empty" :style="{ height }">
      <slot name="empty">{{ t('trendChart.empty') }}</slot>
    </div>
    <!-- 全量走势缩略条＝时间窗控制器（二合一）。结构与形态继承模块 F 的 pc-range
         四段式：标签行 / 缩略图 / 叠在缩略图上的滑块区 / 区间信息行。
         底图是所选版本 × 地区的**全部**时间跨度、与主图同一批序列与值域
         （几何见 thumbGeo），随 payload 变更实时重算；滑块区就是窗口控制：
         拖把手改跨度、拖窗口平移、点窗口外跳转。与主图共用一次左→右描线入场。 -->
    <div v-if="hasPoints && thumbGeo" class="pc-range-wrap">
      <div class="pc-range-label">
        <span>{{ t('trendChart.thumb.title') }}</span>
        <span>{{ thumbRangeText }}</span>
      </div>
      <svg
        ref="thumbRef"
        class="pc-range-svg"
        :class="{ 'hl-chart-wipe': wipe }"
        :viewBox="THUMB_VIEWBOX"
        preserveAspectRatio="none"
        aria-hidden="true"
        @pointerdown.prevent="onStripDown"
      >
        <!-- z 序对齐主图：Key 店幕布 → Steam 幕布 → 史低虚线 → Key 店线 → Steam 线 -->
        <rect
          v-if="thumbGeo.keyY !== null"
          class="tt-key-area"
          x="0"
          :y="thumbGeo.keyY"
          :width="THUMB_W"
          :height="Math.max(0, THUMB_H - thumbGeo.keyY)"
        />
        <path class="tt-steam-area" :d="thumbGeo.area" />
        <line
          v-if="thumbGeo.lowY !== null"
          class="tt-low"
          x1="0"
          :y1="thumbGeo.lowY"
          :x2="THUMB_W"
          :y2="thumbGeo.lowY"
        />
        <line
          v-if="thumbGeo.keyY !== null"
          class="tt-key"
          x1="0"
          :y1="thumbGeo.keyY"
          :x2="THUMB_W"
          :y2="thumbGeo.keyY"
        />
        <path class="tt-steam" :d="thumbGeo.line" />
      </svg>
      <!-- 滑块区：负 margin 完全压在缩略图上（模版同款）。自身不拦点击，
           只有把手与窗口命中层接收指针——点窗口外会落到下面的缩略图上。 -->
      <div ref="zoneRef" class="pc-slider-zone">
        <div
          class="pc-slider-mask"
          :style="{ left: '0%', width: `${winPct.start * 100}%` }"
        />
        <div
          class="pc-slider-mask"
          :style="{ left: `${winPct.end * 100}%`, width: `${(1 - winPct.end) * 100}%` }"
        />
        <div
          class="pc-slider-window"
          :style="{
            left: `${winPct.start * 100}%`,
            width: `${(winPct.end - winPct.start) * 100}%`,
          }"
          @pointerdown.prevent="beginDrag($event, 'pan')"
        />
        <div
          class="pc-slider-handle"
          :style="{ left: `${winPct.start * 100}%` }"
          @pointerdown.prevent="beginDrag($event, 'left')"
        />
        <div
          class="pc-slider-handle"
          :style="{ left: `${winPct.end * 100}%` }"
          @pointerdown.prevent="beginDrag($event, 'right')"
        />
      </div>
      <div class="pc-range-info">
        <span>{{ thumbStartLabel }}</span>
        <span>{{ t('trendChart.thumb.hint') }}</span>
        <span>{{ thumbEndLabel }}</span>
      </div>
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

/* ─ 全量走势缩略条＝时间窗控制器（pc-range 四段式）──
   骨架沿用模块 F 的 pc-range 一套：标签行 10px 灰字、缩略图 36px、滑块区用
   负 margin 完全压回缩略图上、信息行等宽字体。
   控制层三件（暗幕 / 滑块 / 两端把手）在本文件里定形，一律走令牌：
   · 暗幕 = --surface-mask（窗口外压暗，模版此处写的是 rgba 字面量）；
   · 滑块 = 半透明强调色底 + 描边 + 居中抓握纹——窗口是可拖的实体，不是隐形命中层；
   · 把手 = 实心强调色抓手块 + 两道竖向抓握纹（不用圆点，圆点在 36px 条上看不出
     是抓手，也点不中）。
   preserveAspectRatio="none" 让图形横向铺满（viewBox 是 1000×36 的虚拟坐标系，
   容器变窄也不重算几何）；笔画用 non-scaling-stroke 免被横向拉伸，
   拖拽换算永远走 getBoundingClientRect 的实际像素宽度，与 viewBox 无关。 */
.pc-range-wrap {
  margin-top: 6px;
  padding: 8px 4px 4px;
}

.pc-range-label {
  display: flex;
  justify-content: space-between;
  margin-bottom: 4px;
  font-size: 10px;
  color: var(--text-dim);
}

.pc-range-svg {
  display: block;
  width: 100%;
  height: 36px;
  cursor: crosshair;
  touch-action: none; /* 触屏上拖把手不要被页面滚动抢走 */
}

.pc-slider-zone {
  position: relative;
  height: 36px;
  margin-top: -36px;
  /* 自身不拦指针：只有把手与窗口命中层接指针，其余位置落到底下的缩略图上
     （模版里暗幕即 pointer-events:none，点击语义在 svg 的 crosshair 上） */
  pointer-events: none;
}

/* 窗口外暗幕（模版 rgba(0,0,0,.35) → 令牌 --surface-mask） */
.pc-slider-mask {
  position: absolute;
  top: 0;
  height: 36px;
  background: var(--surface-mask);
  pointer-events: none;
}

/* 窗口本体＝可拖的滑块（拇指）：半透明强调色底 + 描边 + 居中的抓握纹，
   底下的走势仍看得见（不是盖死的不透明条）。拖动 = 平移，不改跨度。
   抓握纹一段 13×10 的 1px 竖线排，居中不重复——窗口窄到放不下时自然截断。 */
.pc-slider-window {
  position: absolute;
  top: 4px;
  height: 28px;
  border-radius: 3px;
  background-color: var(--accent-a15);
  background-image: repeating-linear-gradient(
    90deg,
    var(--accent-a50) 0 1px,
    transparent 1px 4px
  );
  background-size: 13px 10px;
  background-position: center;
  background-repeat: no-repeat;
  border: 1px solid var(--accent-a30);
  box-sizing: border-box;
  pointer-events: auto;
  cursor: grab;
}

.pc-slider-window:active {
  cursor: grabbing;
}

/* 两端把手：实心强调色抓手块 + 两道竖向抓握纹（不是圆点）。
   块比滑块高、外沿压住窗口边界，读起来是「这一段的两端 + 中间一段可拖」。
   命中区就是块本身。 */
.pc-slider-handle {
  position: absolute;
  top: 2px;
  width: 10px;
  height: 32px;
  margin-left: -5px; /* 块心对准窗口边界 */
  border-radius: 2px;
  background-color: var(--accent);
  background-image: repeating-linear-gradient(
    90deg,
    color-mix(in srgb, var(--bg-card) 55%, transparent) 0 1px,
    transparent 1px 3px
  );
  background-size: 5px 12px;
  background-position: center;
  background-repeat: no-repeat;
  box-shadow: var(--shadow-sm);
  cursor: ew-resize;
  pointer-events: auto;
  z-index: 5;
}

.pc-range-info {
  display: flex;
  justify-content: space-between;
  margin-top: 4px;
  font-size: 10px;
  color: var(--text-dim);
  font-family: var(--font-mono);
}

.tt-steam {
  fill: none;
  stroke: var(--accent);
  stroke-width: 1.6;
  stroke-linejoin: round;
  vector-effect: non-scaling-stroke;
}

.tt-steam-area {
  fill: var(--accent);
  fill-opacity: 0.12;
  stroke: none;
}

.tt-key {
  stroke: var(--success);
  stroke-width: 1.2;
  vector-effect: non-scaling-stroke;
}

.tt-key-area {
  fill: var(--success);
  fill-opacity: 0.1;
  stroke: none;
}

.tt-low {
  stroke: var(--warning);
  stroke-width: 1.2;
  stroke-dasharray: 6 4;
  opacity: 0.75;
  vector-effect: non-scaling-stroke;
}

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
