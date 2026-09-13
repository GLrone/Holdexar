<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, useId, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { APP_NAME } from '@/appInfo'
import { useI18n, type MessageKey } from '@/locales'
import { useSettingsStore } from '@/stores/settings'
import { useThemeStore } from '@/stores/theme'
import { HlButton, HlIcon } from '@/components/ui'
import type { IconName } from '@/components/ui/icons'

/* ═══ 产品导览（蒙层 + 聚光镂空 + 教练标记气泡）═══
   - 蒙层遮罩：全屏 SVG mask 暗化，在靶点元素处镂空聚光
   - 教练标记（Coachmark）：聚光框旁气泡，8 分向智能避让（优先下方/右上），
     永不与聚光框重叠、永不超出视口——高卡片不再遮挡按键
   - 焦点引导：靶点滚动进视野（聚光框完整可见优先），页面滚动/缩放实时跟随
   顺序编排（小白实测反馈修正）：
     代理 → 账号绑定 → 任务导入 → 价格提醒 → 收尾
   （不配代理 Steam 登录窗都加载不出，代理必须先行。）
   关闭兜底：打开即写 ui.onboarding_done 标志（幂等），关闭路径再补写一次。

   靶点选择器契约：
   - 框架层：data-tour 属性（侧边栏项，HlSideNav 按 to 派生）
   - 页面层：data-section 属性（分节卡片既有，零侵入复用）
     ⚠️ 迁移后该属性的值是**词条 key**（如 `proxies.section.clash`）而不是译文：
     锚点与语言无关，切语言时下面的选择器不会断。四个跨页步骤的 key 是与视图侧
     逐字对上的契约（见 .tmp-i18n-brief5.md 第一节），改名即静默选不中。
   重开约定：打开时 step 强制归 0（左上角 logo / 设置页重看都从头走）。 */

const model = defineModel<boolean>({ default: false })

const router = useRouter()
const route = useRoute()
const maskId = useId()
const themeStore = useThemeStore()
const { t } = useI18n()

/* 品牌图（与侧边栏同一来源：主题联动的双版本素材） */
const brandLogo = computed(() =>
  themeStore.isDark ? '/assets/logo_dark.ico' : '/assets/logo_light.ico',
)

interface TourStep {
  /** 需要先导航到的页面 */
  route?: string
  /** 聚光靶点选择器（缺省 = 全屏居中开场/收尾卡，不聚光） */
  target?: string
  /** 标题词条 key（**存 key 不存译文**：常量表在 setup 期求值一次，
      存译文会把语言冻在组件创建那一刻） */
  titleKey: MessageKey
  /** 标题的插值参数（品牌名等语言无关的常量，随表一起进常量） */
  params?: Record<string, string>
  paras: TourPara[]
  /** 底部提示条词条 key（可选） */
  hintKey?: MessageKey
  /** 框架区元素：不随页面滚动 */
  side?: boolean
}

/** 段落条目：普通段 { textKey }，或 { emphKey } 强调行（accent 色 + 图标 + 内嵌底）。
    与标题同理，这里存的是词条 key 不是译文；params 是语言无关的插值常量。 */
type TourPara =
  | { textKey: MessageKey; params?: Record<string, string> }
  | { emphKey: MessageKey; params?: Record<string, string>; icon?: IconName }

const TOUR: TourStep[] = [
  {
    titleKey: 'productTour.intro.title',
    params: { app: APP_NAME },
    paras: [
      { textKey: 'productTour.intro.p1' },
      { textKey: 'productTour.intro.p2' },
      { textKey: 'productTour.intro.p3', params: { app: APP_NAME } },
    ],
  },
  {
    target: '[data-tour="sb-proxies"]',
    titleKey: 'productTour.stepProxy.title',
    paras: [
      { emphKey: 'productTour.stepProxy.emph', icon: 'warning' },
      { textKey: 'productTour.stepProxy.p1' },
      { textKey: 'productTour.stepProxy.p2' },
    ],
    side: true,
  },
  {
    route: '/proxies',
    target: '[data-section="proxies.section.clash"]',
    titleKey: 'productTour.stepClash.title',
    paras: [
      { textKey: 'productTour.stepClash.p1' },
      { textKey: 'productTour.stepClash.p2' },
      { textKey: 'productTour.stepClash.p3' },
      { textKey: 'productTour.stepClash.p4' },
    ],
    hintKey: 'productTour.stepClash.hint',
  },
  {
    target: '[data-tour="sb-settings"]',
    titleKey: 'productTour.stepAccount.title',
    paras: [{ textKey: 'productTour.stepAccount.p1' }],
    side: true,
  },
  {
    route: '/settings',
    target: '[data-section="settings.section.steamAccount"]',
    titleKey: 'productTour.stepBind.title',
    paras: [
      { emphKey: 'productTour.stepBind.emph', icon: 'zap' },
      { textKey: 'productTour.stepBind.p1' },
      { textKey: 'productTour.stepBind.p2' },
      { textKey: 'productTour.stepBind.p3' },
    ],
    hintKey: 'productTour.stepBind.hint',
  },
  {
    target: '[data-tour="sb-crawl"]',
    titleKey: 'productTour.stepTasks.title',
    paras: [{ textKey: 'productTour.stepTasks.p1' }],
    side: true,
  },
  {
    route: '/crawl',
    target: '[data-section="crawl.section.bulkImport"]',
    titleKey: 'productTour.stepImport.title',
    paras: [
      { textKey: 'productTour.stepImport.p1' },
      { textKey: 'productTour.stepImport.p2' },
      { textKey: 'productTour.stepImport.p3' },
      { emphKey: 'productTour.stepImport.emph', icon: 'zap' },
    ],
    hintKey: 'productTour.stepImport.hint',
  },
  {
    target: '[data-tour="sb-alerts"]',
    titleKey: 'productTour.stepAlerts.title',
    paras: [{ textKey: 'productTour.stepAlerts.p1' }],
    side: true,
  },
  {
    route: '/alerts',
    target: '[data-section="alerts.section.rules"]',
    titleKey: 'productTour.stepRules.title',
    paras: [
      { textKey: 'productTour.stepRules.p1' },
      { textKey: 'productTour.stepRules.p2' },
      { emphKey: 'productTour.stepRules.emph', icon: 'zap' },
      { textKey: 'productTour.stepRules.p3' },
    ],
    hintKey: 'productTour.stepRules.hint',
  },
  {
    titleKey: 'productTour.done.title',
    paras: [
      { emphKey: 'productTour.done.emph', icon: 'check-circle' },
      { textKey: 'productTour.done.p1' },
      { textKey: 'productTour.done.p2' },
      { textKey: 'productTour.done.p3', params: { app: APP_NAME } },
    ],
  },
]

const step = ref(0)
const current = computed(() => TOUR[step.value])
const isIntro = computed(() => !current.value.target)
const isLast = computed(() => step.value === TOUR.length - 1)

/* ── 段落类型判定（{ textKey } 普通段 / { emphKey } 强调段）──
   取文案一律在渲染期 t()：表里存的是 key，故切语言即重渲染。 */
function isEmph(p: TourPara): p is { emphKey: MessageKey; params?: Record<string, string>; icon?: IconName } {
  return 'emphKey' in p
}
function paraKey(p: TourPara): MessageKey {
  return isEmph(p) ? p.emphKey : p.textKey
}
function paraParams(p: TourPara): Record<string, string> | undefined {
  return p.params
}
function emphIcon(p: TourPara): IconName {
  return (isEmph(p) && p.icon) || 'zap'
}

/* ── 定位状态 ── */
const box = ref({ top: 0, left: 0, width: 0, height: 0 })
const viewport = ref({ w: 0, h: 0 })
/** 气泡方向（8 分向）：下 / 右下 / 右上 / 左下 / 左上 / 上 …智能避让结果 */
const popPlacement = ref<'bottom' | 'top' | 'right-bottom' | 'right-top' | 'left-bottom' | 'left-top' | 'center'>('bottom')
/** 靶点定位中（换页/异步渲染间隙）：蒙层常显、气泡保持显示（不闪断） */
const waiting = ref(false)
/** 靶点 4s 未出现：气泡降级居中，箭头隐藏 */
const missed = ref(false)
const finishing = ref(false)
/** 导览开始前的页面（打开瞬间记录），收尾还原 */
const capturedStart = ref('/')

/* ── 气泡 8 分向定位（核心：永不遮挡聚光框、永不溢出视口）──
    方向优先级（按小白阅读动线）：
    1. 聚光框正下方 —— 阅读完卡片标题顺势往下看
    2. 框右侧偏下/偏上 —— 下方/上方都放不下时贴右边（侧边栏窄目标的主位）
    3. 聚光框正上方
    4. 全放不下 → 居中（missed 同款）
    高度两段式：首摆用 POP_H_EST 预算，气泡挂载后实测回填 popHeight，
    placePop 复跑——实测值参与下一轮方向决策与夹取。 */
const POP_W = 440
const POP_H_EST = 340
const MARGIN = 14

/** 气泡实测高度（0 = 未测，用预算值；挂载后 measurePop 回填） */
const popHeight = ref(0)
const popEl = ref<HTMLElement | null>(null)

function currentPopH(): number {
  return popHeight.value > 0 ? popHeight.value : POP_H_EST
}

/** 气泡挂载/内容变化 → 实测高度回填 + 按实测值复摆（防溢出的第二段） */
function measurePop() {
  const el = popEl.value
  if (!el) return
  const h = Math.ceil(el.getBoundingClientRect().height)
  if (h > 0 && h !== popHeight.value) {
    popHeight.value = h
    placePop()
  }
}

const popStyle = computed(() => {
  const vw = viewport.value.w
  const vh = viewport.value.h
  const w = Math.min(POP_W, vw - MARGIN * 2)
  if (isIntro.value || missed.value || popPlacement.value === 'center') {
    const cw = Math.min(480, vw - MARGIN * 2)
    return { top: '50%', left: '50%', transform: 'translate(-50%, -50%)', width: `${cw}px`, maxWidth: `${cw}px` }
  }
  const b = box.value
  const h = currentPopH()
  const left = Math.max(MARGIN, Math.min(b.left + b.width / 2 - w / 2, vw - w - MARGIN))
  const pos: Record<string, string> = { left: `${left}px`, width: `${w}px`, maxWidth: `${w}px` }
  if (popPlacement.value === 'bottom') pos.top = `${b.top + b.height + MARGIN}px`
  else if (popPlacement.value === 'top') pos.bottom = `${vh - b.top + MARGIN}px`
  else if (popPlacement.value === 'right-bottom') {
    pos.left = `${Math.min(b.left + b.width + MARGIN, vw - w - MARGIN)}px`
    pos.top = `${b.top + 12}px`
  } else if (popPlacement.value === 'right-top') {
    pos.left = `${Math.min(b.left + b.width + MARGIN, vw - w - MARGIN)}px`
    pos.bottom = `${Math.max(vh - b.top - b.height + 12, MARGIN)}px`
  } else if (popPlacement.value === 'left-bottom') {
    // 宽卡场景：气泡贴框左缘（框近乎占满宽，右/下都放不下）
    pos.left = `${Math.max(b.left - MARGIN - w, MARGIN)}px`
    pos.top = `${b.top + 12}px`
  } else if (popPlacement.value === 'left-top') {
    pos.left = `${Math.max(b.left - MARGIN - w, MARGIN)}px`
    pos.bottom = `${Math.max(vh - b.top - b.height + 12, MARGIN)}px`
  }
  return pos
})

/** 方向决策（纯函数化，resize/scroll 复用）：算完写 popPlacement */
function placePop() {
  const vw = viewport.value.w
  const vh = viewport.value.h
  const b = box.value
  const w = Math.min(POP_W, vw - MARGIN * 2)
  const h = currentPopH()
  // 1. 正下方：竖直预算够
  if (b.top + b.height + MARGIN + h <= vh) {
    popPlacement.value = 'bottom'
    return
  }
  // 2. 右侧：水平放得下（含气泡宽），竖直上/下至少一边预算够
  if (b.left + b.width + MARGIN + w <= vw - MARGIN) {
    if (b.top + 12 + h <= vh) {
      popPlacement.value = 'right-bottom'
      return
    }
    if (vh - (b.top + b.height) + 12 >= MARGIN + 60) {
      popPlacement.value = 'right-top'
      return
    }
  }
  // 3. 左侧：宽卡占满视口宽时，左缘空间常比右侧富余（居中卡左右的留白）
  if (b.left - MARGIN - w >= MARGIN - 8) {
    if (b.top + 12 + h <= vh) {
      popPlacement.value = 'left-bottom'
      return
    }
    if (vh - (b.top + b.height) + 12 >= MARGIN + 60) {
      popPlacement.value = 'left-top'
      return
    }
  }
  // 4. 正上方
  if (b.top - MARGIN - h >= 0) {
    popPlacement.value = 'top'
    return
  }
  // 5. 全放不下 → 居中
  popPlacement.value = 'center'
}

/* ── 重试代际 + 全局监听（滚动/缩放跟随）── */
let gen = 0
let retryTimer: ReturnType<typeof setTimeout> | null = null
let offScroll: (() => void) | null = null
let offGlobal: (() => void) | null = null

function invalidate() {
  gen++
  if (retryTimer !== null) {
    clearTimeout(retryTimer)
    retryTimer = null
  }
  offScroll?.()
  offScroll = null
  stopStabilize()
}

function attachGlobal() {
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'Escape') model.value = false
  }
  const onResize = () => {
    viewport.value = { w: window.innerWidth, h: window.innerHeight }
    measure()
  }
  window.addEventListener('keydown', onKey)
  window.addEventListener('resize', onResize)
  offGlobal = () => {
    window.removeEventListener('keydown', onKey)
    window.removeEventListener('resize', onResize)
  }
}

/** 靶点选择器安全构建（属性选择器值走 CSS.escape 防御，保留完整属性名） */
function resolveTarget(): HTMLElement | null {
  const raw = current.value.target
  if (!raw) return null
  const m = raw.match(/^\[([a-z-]+)="(.+)"\]$/)
  if (!m) return document.querySelector(raw)
  const [, attr, val] = m
  try {
    return document.querySelector(`[${attr}="${CSS.escape(val)}"]`)
  } catch {
    return null
  }
}

/** 纯测量：滚动/缩放/重排跟随只更新聚光框与气泡方向，不滚页面、不挂监听。
    返回本次几何（供稳定轮询对比位移）；靶点不在线返回 null。 */
function measure(): { top: number; left: number; width: number; height: number } | null {
  const el = resolveTarget()
  if (!el) return null
  const r = el.getBoundingClientRect()
  const gap = 6
  const next = {
    top: r.top - gap,
    left: r.left - gap,
    width: r.width + gap * 2,
    height: r.height + gap * 2,
  }
  box.value = next
  placePop()
  measurePop()
  return next
}

/** 靶点几何是否显著位移（数据加载重排/卡片展开等，>4px 即跟） */
function boxMoved(a: { top: number; left: number; width: number; height: number } | null, b: { top: number; left: number; width: number; height: number }): boolean {
  if (!a) return false
  return (
    Math.abs(a.top - b.top) > 4 ||
    Math.abs(a.left - b.left) > 4 ||
    Math.abs(a.width - b.width) > 4 ||
    Math.abs(a.height - b.height) > 4
  )
}

/** 靶点是否被内滚容器裁剪：元素在窗口视口内、但被可滚祖先（如侧栏
    .hl-sb-nav）的裁剪区切掉——矮视口下尾部分组常见，此时聚光框会落在
    一个视觉上是别的元素（如侧栏收起按键）的位置上。 */
function clippedByScroller(el: HTMLElement): boolean {
  let p: HTMLElement | null = el.parentElement
  while (p) {
    const st = getComputedStyle(p)
    if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && p.scrollHeight > p.clientHeight + 1) {
      const pr = p.getBoundingClientRect()
      const r = el.getBoundingClientRect()
      return r.bottom > pr.bottom + 1 || r.top < pr.top - 1
    }
    p = p.parentElement
  }
  return false
}

/** 聚焦靶点：滚动进视野 → 测量 → 滚动跟随 + 稳定期轮询（重排位移复测）。
    先同步尝试一次（多数步骤靶点已在 DOM，等待提示不该闪现）。
    滚动策略：聚光框完整可见优先——先把框底滚进视口，再看框顶。
    上滚腾位：框下方空间不够摆气泡时，把框顶滚到视口上缘，下方自然
    腾出完整空间（比居中降级可读性好得多）。
    侧栏项随 .hl-sb-nav 滚动容器校正（side 步骤不再跳过滚动）。
    靶点迟到宽容 10s：跨页步骤的锚点要等 route-fade 进场 + 页面数据
    门控（loading 态）+ 图片布局重排，4s 内可能不齐（弱网/慢机更久）。 */
function focusTarget() {
  const myGen = ++gen
  missed.value = false
  const started = performance.now()
  const attempt = async () => {
    if (myGen !== gen || !model.value) return
    const el = resolveTarget()
    if (el) {
      waiting.value = false
      // 框完整可见优先：先确保框底在视口内（下滚），再确保框顶在内（上滚）。
      // side 步骤同理——侧栏导航条（.hl-sb-nav）自身可滚，"我"等尾部分组
      // 在矮视口下滚出视界时聚光框会落到收起按键上，scrollIntoView 的
      // 最近可滚祖先即 .hl-sb-nav，直接把侧栏项滚回视界。
      // clippedByScroller 补盲：元素在窗口视口内但被内滚容器裁剪
      // （视觉上压在别的元素上），同样要滚最近的内滚容器。
      const r = el.getBoundingClientRect()
      const vh = window.innerHeight
      const outOfView = r.bottom > vh - 8 || r.top < 8
      const clipped = !outOfView && clippedByScroller(el)
      if (outOfView || clipped) {
        el.scrollIntoView({ block: clipped ? 'center' : 'nearest' })
      }
      if (!current.value.side) {
        const popH = currentPopH()
        const needRoom = (rect: DOMRect) => {
          const below = window.innerHeight - (rect.bottom + MARGIN)
          const gain = rect.top - MARGIN // 上滚可腾出的下方空间（框顶到视口顶）
          return below < popH && below + gain >= popH && gain > 0
        }
        if (!outOfView) {
          if (needRoom(r)) {
            // 下方空间不够摆气泡、但上滚能腾够：把框顶滚到视口上缘
            el.scrollIntoView({ block: 'start' })
          }
        } else {
          // 刚做了 nearest 校正后重新读几何，仍留下方空间不足时补一次 start
          await nextTick()
          const r2 = el.getBoundingClientRect()
          if (needRoom(r2)) {
            el.scrollIntoView({ block: 'start' })
          }
        }
      }
      measure()
      attachFollow()
      attachStabilize()
      return
    }
    if (performance.now() - started > 10000) {
      waiting.value = false
      missed.value = true
      popPlacement.value = 'center'
      return
    }
    waiting.value = true
    retryTimer = setTimeout(attempt, 120)
  }
  attempt()
}

/** 页面滚动跟随（含内滚容器）：scroll capture 阶段能收到窗口与内滚容器
    （.hl-sb-nav 等）两种滚动——side 步骤同样要跟随（用户滚侧栏时聚光框咬住） */
function attachFollow() {
  if (offScroll) return
  const onScroll = () => measure()
  window.addEventListener('scroll', onScroll, { passive: true, capture: true })
  offScroll = () => window.removeEventListener('scroll', onScroll, { capture: true })
}

/** 稳定期轮询：页面异步数据加载 → 靶点重排位移（含 scrollIntoView 平滑
    滚动落地前）持续复测，聚光框/气泡一直咬住靶点（"不跟随"的根治）。
    与上一轮几何对比位移（measure 已写 box.value，自比较无意义）；
    咬合稳定（连续 ~1.6s 无位移）自动停，省电不空转。 */
let stabilizeTimer: ReturnType<typeof setInterval> | null = null
let stableRounds = 0
let lastBox: { top: number; left: number; width: number; height: number } | null = null

function stopStabilize() {
  if (stabilizeTimer !== null) {
    clearInterval(stabilizeTimer)
    stabilizeTimer = null
  }
}

function attachStabilize() {
  if (stabilizeTimer !== null) return
  const myGen = gen
  stableRounds = 0
  lastBox = { ...box.value }
  stabilizeTimer = setInterval(() => {
    if (myGen !== gen || !model.value) {
      stopStabilize()
      return
    }
    const next = measure()
    if (boxMoved(next, lastBox)) {
      stableRounds = 0
      lastBox = next ? { ...next } : null
    } else {
      stableRounds++
    }
    // 1.6s 无位移（8 轮 × 200ms）即视为咬合稳定
    if (stableRounds >= 8) stopStabilize()
  }, 200)
}

/* ── 步进：路由先行 → 等靶点挂载 ── */
watch(
  () => [model.value, step.value] as const,
  async ([open]) => {
    if (!open) return
    invalidate()
    viewport.value = { w: window.innerWidth, h: window.innerHeight }
    offGlobal?.()
    attachGlobal()
    const s = current.value
    if (s.route && route.path !== s.route) {
      await router.push(s.route)
      await nextTick()
    }
    if (s.target) {
      focusTarget()
    } else {
      waiting.value = false
      missed.value = false
      box.value = { top: 0, left: 0, width: 0, height: 0 }
      stopStabilize()
    }
    // 换步后气泡内容/方向可能变化：清预算 → 挂载后实测回填（两段式定位）
    popHeight.value = 0
    await nextTick()
    requestAnimationFrame(() => measurePop())
  },
  { immediate: true },
)

/* ── 开/关生命周期 ──
   开（false→true）：**立刻记账**（幂等）——用户哪怕直接关窗口/强杀进程
   也算看过，下次启动不再打扰；step 归 0 + 记录起始页。
   关（true→false，任何路径）：再记一次（兜底，首次写失败时补上）+ 回到起始页。 */
watch(model, async (v, was) => {
  if (v && !was) {
    step.value = 0
    capturedStart.value = route.fullPath
    // 打开即写标志：导览入口长期在（左上角 logo / 我页「新手教程」），
    // 「看过一次就不再自动弹」比「必须点完才算完成」更贴合实际使用。
    void useSettingsStore().markOnboardingDone()
    return
  }
  if (!v || !was) return
  invalidate()
  stopStabilize()
  offGlobal?.()
  offGlobal = null
  const back = capturedStart.value
  if (route.path !== back) {
    void router.push(back)
  }
  finishing.value = true
  await useSettingsStore().markOnboardingDone()
  finishing.value = false
})

function next() {
  if (step.value < TOUR.length - 1) {
    step.value += 1
    return
  }
  model.value = false
}

onUnmounted(() => {
  invalidate()
  stopStabilize()
  offGlobal?.()
  offGlobal = null
})
</script>

<template>
  <Teleport to="body">
    <div v-if="model" class="pt-tour">
      <!-- 蒙层 + 聚光镂空：SVG mask 一次成型。镂空缘双层柔化（外层半透环
           近似 blur，规避 mask 内 filter 边界渗暗） -->
      <svg class="pt-mask" :width="viewport.w" :height="viewport.h" aria-hidden="true">
        <defs>
          <mask :id="maskId">
            <rect fill="#fff" x="0" y="0" :width="viewport.w" :height="viewport.h" />
            <template v-if="current.target && !waiting && !missed">
              <rect
                fill="#000"
                fill-opacity="0.55"
                :x="box.left"
                :y="box.top"
                :width="Math.max(box.width, 0)"
                :height="Math.max(box.height, 0)"
                rx="12"
              />
              <rect
                fill="#000"
                :x="box.left + 4"
                :y="box.top + 4"
                :width="Math.max(box.width - 8, 0)"
                :height="Math.max(box.height - 8, 0)"
                rx="9"
              />
            </template>
          </mask>
        </defs>
        <rect
          class="pt-mask__dim"
          x="0"
          y="0"
          :width="viewport.w"
          :height="viewport.h"
          :mask="`url(#${maskId})`"
        />
      </svg>

      <!-- 聚光描边框（呼吸发光，纯视觉不挡交互） -->
      <div
        v-if="current.target && !waiting && !missed"
        class="pt-spot-frame"
        :style="{
          top: `${box.top}px`,
          left: `${box.left}px`,
          width: `${box.width}px`,
          height: `${box.height}px`,
        }"
      />

      <!-- 开场/收尾：全屏居中卡（不聚光） -->
      <div v-if="isIntro" class="pt-intro">
        <img class="pt-intro__brand" :src="brandLogo" :alt="APP_NAME" />
        <h2 class="pt-intro__title">{{ t(current.titleKey, current.params) }}</h2>
        <template v-for="(p, i) in current.paras" :key="i">
          <p v-if="isEmph(p)" class="pt-pop__p pt-pop__p--emph">
            <HlIcon :name="emphIcon(p)" :size="15" />
            <span>{{ t(paraKey(p), paraParams(p)) }}</span>
          </p>
          <p v-else class="pt-pop__p">{{ t(paraKey(p), paraParams(p)) }}</p>
        </template>
        <p v-if="current.hintKey" class="pt-pop__hint">{{ t(current.hintKey) }}</p>
        <div class="pt-pop__foot">
          <HlButton size="sm" variant="text" :disabled="finishing" @click="model = false">{{ t('productTour.action.skip') }}</HlButton>
          <span class="pt-spring" />
          <HlButton v-if="step > 0" size="sm" @click="step -= 1">{{ t('common.prev') }}</HlButton>
          <HlButton size="sm" art="outline" tone="blue" :disabled="finishing" @click="next">
            <HlIcon v-if="isLast" name="check" :size="14" />
            {{ isLast ? t('common.finish') : t('common.next') }}
          </HlButton>
        </div>
      </div>

      <!-- 教练标记气泡（Coachmark）：
           waiting = 靶点定位中（气泡照常显示，角标转圈提示）
           missed = 靶点超时未出现（降级居中，无箭头） -->
      <div
        v-else
        ref="popEl"
        class="pt-pop"
        :class="[
          `pt-pop--${popPlacement}`,
          { 'pt-pop--missed': missed, 'is-waiting': waiting },
        ]"
        :style="popStyle"
      >
        <div class="pt-pop__step">
          <span v-if="waiting" class="hl-spinner hl-spinner--inline" aria-hidden="true" />
          {{ t('productTour.progress', { current: step + 1, total: TOUR.length }) }}
        </div>
        <h3 class="pt-pop__title">{{ t(current.titleKey, current.params) }}</h3>
        <template v-for="(p, i) in current.paras" :key="i">
          <p v-if="isEmph(p)" class="pt-pop__p pt-pop__p--emph">
            <HlIcon :name="emphIcon(p)" :size="15" />
            <span>{{ t(paraKey(p), paraParams(p)) }}</span>
          </p>
          <p v-else class="pt-pop__p">{{ t(paraKey(p), paraParams(p)) }}</p>
        </template>
        <p v-if="current.hintKey" class="pt-pop__hint">{{ t(current.hintKey) }}</p>
        <div class="pt-pop__foot">
          <HlButton size="sm" variant="text" :disabled="finishing" @click="model = false">{{ t('productTour.action.skip') }}</HlButton>
          <span class="pt-spring" />
          <HlButton v-if="step > 0" size="sm" @click="step -= 1">{{ t('common.prev') }}</HlButton>
          <HlButton size="sm" art="outline" tone="blue" :disabled="finishing" @click="next">
            <HlIcon v-if="isLast" name="check" :size="14" />
            {{ isLast ? t('common.finish') : t('common.next') }}
          </HlButton>
        </div>
        <!-- 箭头只挂四向摆位（居中降级无指向意义，不渲染） -->
        <span
          v-if="!missed && !waiting && popPlacement !== 'center'"
          class="pt-pop__arrow"
          :class="`pt-pop__arrow--${popPlacement}`"
        />
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.pt-tour {
  position: fixed;
  inset: 0;
  z-index: 2000;
}

.pt-mask {
  position: absolute;
  inset: 0;
}

.pt-mask__dim {
  fill: rgb(0 0 0 / 0.62);
}

/* 聚光描边框：呼吸发光指向靶点 */
.pt-spot-frame {
  position: absolute;
  border: 2px solid var(--accent);
  border-radius: 12px;
  animation: pt-breath 2.4s ease-in-out infinite;
  pointer-events: none;
  transition:
    top 0.3s var(--ease-spring),
    left 0.3s var(--ease-spring),
    width 0.3s var(--ease-spring),
    height 0.3s var(--ease-spring);
}

@keyframes pt-breath {
  0%,
  100% {
    box-shadow:
      0 0 0 4px color-mix(in srgb, var(--accent) 20%, transparent),
      0 0 22px color-mix(in srgb, var(--accent) 45%, transparent);
  }
  50% {
    box-shadow:
      0 0 0 7px color-mix(in srgb, var(--accent) 30%, transparent),
      0 0 36px color-mix(in srgb, var(--accent) 60%, transparent);
  }
}

/* ═══ 教练标记气泡（Coachmark）═══
   max-height 40vh + 内滚动：内容再多也不溢出视口/不遮挡按键 */
.pt-pop {
  position: absolute;
  max-height: min(430px, 72vh);
  overflow-y: auto;
  padding: 14px 16px 12px;
  border-radius: 12px;
  background: var(--bg-card);
  border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
  box-shadow:
    var(--shadow-xl),
    0 0 30px color-mix(in srgb, var(--accent) 16%, transparent);
  animation: pt-pop-in 0.25s var(--ease-spring);
}

/* 入场只渐透明度：气泡/开场卡的定位都依赖 transform（居中模式），
   keyframes 里再写 transform 会把基础变换顶掉（函数列表不可插值） */
@keyframes pt-pop-in {
  from {
    opacity: 0;
  }
}

.pt-pop--missed,
.pt-pop--center {
  border-color: var(--border-soft);
}

.pt-pop.is-waiting {
  opacity: 0.92;
}

.pt-pop__step {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--accent);
  padding: 1px 8px;
  border: 1px solid color-mix(in srgb, var(--accent) 40%, transparent);
  border-radius: 999px;
  margin-bottom: 8px;
  background: color-mix(in srgb, var(--accent) 8%, transparent);
}

.pt-pop__title {
  font-size: 14.5px;
  font-weight: 700;
  color: var(--text-primary);
  margin: 0 0 8px;
}

.pt-pop__p {
  margin: 6px 0;
  font-size: 13px;
  line-height: 1.75;
  color: var(--text-secondary);
}

/* 强调段：本步必须注意的事——accent 色加粗 + 图标点缀 + 内嵌底色，
   与普通段（同字号灰字）拉开视觉层级（小白实测反馈：关键句易被略过） */
.pt-pop__p--emph {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  margin: 9px 0;
  padding: 8px 10px;
  border-radius: 8px;
  font-weight: 600;
  font-size: 13px;
  line-height: 1.65;
  color: var(--accent);
  background: var(--accent-soft, color-mix(in srgb, var(--accent) 10%, transparent));
  border: 1px solid color-mix(in srgb, var(--accent) 28%, transparent);
}

.pt-pop__p--emph svg {
  flex-shrink: 0;
  margin-top: 3px;
}

.pt-pop__hint {
  margin: 10px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.65;
  color: var(--text-muted);
  background: var(--surface-inset, color-mix(in srgb, var(--text-primary) 4%, transparent));
  border-left: 3px solid var(--accent);
}

.pt-pop__foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px solid var(--line-1);
}

.pt-spring {
  flex: 1;
}

/* 指向箭头：按气泡方向挂在对应边 */
.pt-pop__arrow {
  position: absolute;
  width: 12px;
  height: 12px;
  background: var(--bg-card);
  border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
}

/* 气泡在框下方：箭头贴气泡顶缘（水平指向框中心由对称近似） */
.pt-pop__arrow--bottom {
  top: -7px;
  left: 50%;
  margin-left: -6px;
  border-right: none;
  border-bottom: none;
  transform: rotate(45deg);
}

/* 气泡在框上方：箭头贴气泡底缘 */
.pt-pop__arrow--top {
  bottom: -7px;
  left: 50%;
  margin-left: -6px;
  border-left: none;
  border-top: none;
  transform: rotate(45deg);
}

/* 气泡在框右侧：箭头贴气泡左缘 */
.pt-pop__arrow--right-bottom,
.pt-pop__arrow--right-top {
  left: -7px;
  top: 24px;
  border-left: none;
  border-bottom: none;
  transform: rotate(45deg);
}

/* 气泡在框左侧：箭头贴气泡右缘 */
.pt-pop__arrow--left-bottom,
.pt-pop__arrow--left-top {
  right: -7px;
  top: 24px;
  border-right: none;
  border-top: none;
  transform: rotate(45deg);
}

/* ═══ 开场/收尾：全屏居中卡 ═══ */
.pt-intro {
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  width: min(480px, calc(100vw - 48px));
  padding: 26px 28px 20px;
  border-radius: 16px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  box-shadow: var(--shadow-xl);
  animation: pt-pop-in 0.3s var(--ease-spring);
}

/* 品牌图片（与侧边栏 logo 同源双版本，主题联动；不用文字字标） */
.pt-intro__brand {
  width: 58px;
  height: 58px;
  margin-bottom: 12px;
  border-radius: 16px;
  object-fit: contain;
  box-shadow: var(--shadow-glow);
}

.pt-intro__title {
  font-size: 18px;
  font-weight: 800;
  color: var(--text-primary);
  margin: 0 0 10px;
}
</style>
