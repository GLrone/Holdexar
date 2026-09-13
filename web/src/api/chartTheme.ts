/* ECharts 图表主题化出口（图表红线配套）。
 *
 * 三个图表实例（PriceTrendChart / rates / bills）此前各写各的 option，于是同一个
 * 语义在三处取了三个色：tooltip 做了主题化，坐标轴 / 图例 / 系列色却是当年抄下来的
 * 字面量（#8f98a0 正是深色主题 --text-muted 的值，主题一切换就再不跟随；bills 更
 * 甚——柱色里混取了**浅色**的 --accent 与**深色**的 --danger，两边都不对）。
 *
 * 所以本出口不再只管 tooltip，而是把「一个图表要用到的全部颜色」一次性交出来：
 *   useTipPalette()    → tooltip 浮层（bg / border / text / accent / shadow）
 *   useChartPalette()  → 画布本体（网格 / 轴 / 轴文字 / 正负语义色 / 三条语义线）
 *
 * 注：`axisPointer` 不需要单独 `use()` —— echarts 的 TooltipComponent 安装时
 * 内部已 `use(installAxisPointer)`，且 `echarts/components` 并未导出
 * AxisPointerComponent。只要引了 TooltipComponent，option 里的 axisPointer 即生效。
 *   axisPointerStyle() → 悬停时贴在坐标轴上的指示线与标签框
 *
 * 红线：图表 option 内**不得出现颜色字面量**，一律从这里取（eslint error 级门禁）。
 *
 * 左→右描线入场（clip-path wipe）同在此处（见 revealChart 注释）。
 */
import { computed, nextTick, type Ref } from 'vue'

import { useThemeStore } from '@/stores/theme'

function cssVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v || fallback
}

export interface TipPalette {
  bg: string
  border: string
  text: string
  accent: string
  shadow: string
}

export interface ChartPalette {
  /** 网格线 */
  grid: string
  /** 轴线 */
  axis: string
  /** 轴文字（独立 token；此前借用 --text-muted，是硬编码的来源） */
  label: string
  /** 正/负语义色（柱状图的涨跌、差额） */
  positive: string
  negative: string
  /** 三条语义线：主序列（品牌色）/ 成功色 / 警示色。
   *  按 **token 的语义** 命名而不是按某张图的业务名（曾叫 key / low，那是走势图
   *  的「Key 店」「史低」）——否则 rates 的单序列、bills 的折线来取色时，
   *  读代码的人会以为汇率的线用了「Key 店的色」。 */
  line: string
  success: string
  warning: string
}

/** tooltip 主题化配色（hl-popper 同款 token）；主题切换 → 重算 */
export function useTipPalette() {
  const themeStore = useThemeStore()
  return computed<TipPalette>(() => {
    void themeStore.isDark // 依赖主题态，切换时重读 CSS 变量
    return {
      bg: cssVar('--bg-card', '#1b2838'),
      border: cssVar('--border-soft', 'rgba(102, 192, 244, 0.14)'),
      text: cssVar('--text-primary', '#dce6f2'),
      accent: cssVar('--accent', '#66c0f4'),
      shadow: cssVar('--shadow-lg', '0 14px 38px rgba(23, 32, 42, 0.12)'),
    }
  })
}

/** 画布本体配色（网格 / 轴 / 轴文字 / 语义色）；主题切换 → 重算 */
export function useChartPalette() {
  const themeStore = useThemeStore()
  return computed<ChartPalette>(() => {
    void themeStore.isDark
    return {
      grid: cssVar('--chart-grid', 'rgba(102, 192, 244, 0.1)'),
      axis: cssVar('--chart-axis', 'rgba(102, 192, 244, 0.3)'),
      label: cssVar('--chart-axis-label', '#8f98a0'),
      positive: cssVar('--chart-positive', '#66c0f4'),
      negative: cssVar('--chart-negative', '#e74c3c'),
      line: cssVar('--accent', '#66c0f4'),
      success: cssVar('--success', '#a4d007'),
      warning: cssVar('--warning', '#f59e0b'),
    }
  })
}

/**
 * 悬停时贴在坐标轴上的指示线与标签框。
 *
 * 这是 tooltip 主题化**唯一覆盖不到**的悬停元素：tooltip 是浮层，axisPointer 画在
 * 轴上。echarts 默认给 #555 指示线 + rgba(150,150,150,0.9) 灰底白字的标签块——
 * 浅色主题下是块脏灰，深色主题下比 tooltip 还亮，两个主题都突兀。这正是用户说的
 * 「鼠标悬停的 UI 窗口不符合主题」。
 *
 * 做成普通函数（不是 use*）以便在 option computed 内调用，随 palette 一起保持响应。
 */
export function axisPointerStyle(palette: ChartPalette, tip: TipPalette) {
  return {
    type: 'line' as const,
    lineStyle: { color: palette.axis, width: 1 },
    label: {
      // 与 tooltip 同底同框：悬停时轴标签与浮层是同一套语言的两种形态
      backgroundColor: tip.bg,
      borderColor: tip.border,
      borderWidth: 1,
      color: tip.text,
      fontSize: 11,
      padding: [3, 6],
    },
  }
}

/** 十六进制主题色 → 带 alpha（echarts 画布不认 color-mix，只能降级成 rgba） */
export function withAlpha(color: string, alpha: number): string {
  const h = color.trim()
  if (!h.startsWith('#')) return color
  const hex = h.slice(1)
  const full = hex.length === 3 ? hex.split('').map((x) => x + x).join('') : hex.slice(0, 6)
  const n = parseInt(full, 16)
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`
}

function prefersReducedMotion(): boolean {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true
}

/**
 * 描线入场的收尾定时器。**必须是模块级的**：连续调用要能取消上一个。
 *
 * 曾经的 bug：定时器句柄被丢弃在函数内部，无法取消。设第一次调用 t=0、第二次
 * t=T（动画 650ms、收尾定时器 750ms）：当 100ms < T < 750ms 时，第一次的定时器
 * 会在 T 之后的 (750−T) ms 处把 class 摘掉，而第二次的动画本该跑到 T+650 ——
 * 于是「新人」被「旧人」拦腰砍断，只播了一部分就定格，看起来就像动画没播。
 * 连点 range chips、拖 dataZoom 导航条后回传都会落进这个窗口。
 */
let wipeTimer: number | undefined

/** 触发一次图表左→右描线入场（摘挂 class 重启 CSS 动画）。 */
export async function revealChart(wipe: Ref<boolean>, durationMs = 750) {
  // 减弱动效：CSS 侧已 `animation: none`，class 挂了也没有视觉效果——直接短路，
  // 免得白排一个定时器、还参与上面那套打断时序。
  if (prefersReducedMotion()) {
    wipe.value = false
    return
  }
  window.clearTimeout(wipeTimer)
  wipe.value = false
  await nextTick()
  wipe.value = true
  wipeTimer = window.setTimeout(() => {
    wipe.value = false
  }, durationMs)
}
