<script setup lang="ts">
/**
 * HlSectionRail —— 页内分节定位轨（二级侧边栏）。
 *
 * 紧贴主侧边栏右缘的细刻度列：每个 [data-section] 分节一枚横向段落，
 * 激活段（scrollspy 判定）变长变主色，光标邻近段平滑生长；
 * 点击段落 = 平滑滚动到对应分节。
 *
 * 动效引擎：单个 rAF 循环驱动全部段落，段状态**不进响应式系统**——每帧把 0..1 写进
 * 各段自己的 CSS 自定义属性 `--effect`，长度/颜色/位移三条曲线同读这一个值
 * （见 hl-framework.css 的 `.hl-rail__seg`），所以一帧只碰 DOM 样式，不触发重渲染。
 * 缓动按两帧时间差做指数插值而非「每帧走固定比例」，故帧率无关——
 * 120Hz 与 60Hz 下跟手感一致，不会在慢机器上整体变迟钝。
 * 分节由视图层声明（data-section="分节名"），本组件自动发现；
 * MutationObserver 兜住晚挂载分节（game-detail 接口返回后才渲染等）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n, type MessageKey } from '@/locales'

/** 单个分节（自动扫描 [data-section] 生成） */
export interface HlSectionRailItem {
  label: string
  el: HTMLElement
}

const props = withDefaults(
  defineProps<{
    /** 滚动容器选择器（分节在该容器内扫描） */
    container?: string
    /** 分节锚点选择器（label 取 data-section 属性值） */
    selector?: string
    /** 光标邻近感应半径（px） */
    proximityRadius?: number
    /** 邻近动画半衰期（ms，越小越跟手） */
    halfLife?: number
    /** 邻近时段落位移上限（px） */
    maxShift?: number
  }>(),
  {
    container: '.view-container',
    selector: '[data-section]',
    proximityRadius: 48,
    halfLife: 70,
    maxShift: 3,
  },
)

/** scrollspy 阈值线：距滚动容器顶部的分节判定线（px） */
const SPY_THRESHOLD = 96

const sections = ref<HlSectionRailItem[]>([])
const activeIndex = ref(-1)
const railListEl = ref<HTMLElement | null>(null)

/* 引擎状态（非响应式：rAF 热路径不走 proxy） */
const targets: number[] = []
const currents: number[] = []
let rafId: number | null = null
let lastTime = 0
let containerEl: HTMLElement | null = null
let mutationObserver: MutationObserver | null = null
let mutationTimer: number | undefined
let scrollRaf = false

/* 点击粘滞：平滑滚动途中 spy 以中间位置覆盖点击目标，冻结到滚动落地 */
let stickyIdx = -1
let stickyTimer: number | undefined

/* 悬停弹窗（cf-status-tip 同款：跟随光标、鼠标离开/滚动即收） */
const tip = ref<{ label: string; x: number; y: number } | null>(null)
let tipHideTimer: number | undefined

const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)')
const route = useRoute()

const { t } = useI18n()

/* 分节标签：`data-section` 的值**优先按词条 key 解释**，取不到就原样显示。
 *
 * 属性值放 key 而不是直接放译文：`data-section` 同时是 ProductTour 的
 * querySelector 目标。写成 `:data-section="t('x.y')"` 会让锚点随语言变——
 * 切语言后 ProductTour 的选择器就选不中，引导高亮**静默落空**。写 key 则锚点
 * 与语言无关，显示仍是译文的。
 *
 * 兼容性：`t()` 的兜底链是「当前语言 → zh-CN → key 原文」，故仍是中文原文的
 * `data-section`（如 `data-section="添加提醒规则"`）不受影响，只是照原样显示。
 *
 * **在渲染期取**，不把结果写进 sections：那等于把语言冻在扫描那一刻，
 * 而这些分节是 MutationObserver 长期持有的（切语言后不会重扫）。 */
const labelOf = (s: { label: string }) =>
  s.label ? t(s.label as MessageKey) : t('rail.unnamed')

/** 遍历当前段落 li（v-for 子项，index 与 sections 对齐） */
function forEachItem(fn: (el: HTMLElement, i: number) => void) {
  const list = railListEl.value
  if (!list) return
  for (let i = 0; i < list.children.length; i++) fn(list.children[i] as HTMLElement, i)
}

/** 全量扫描容器内的分节锚点（路由切换 / DOM 变更后调用） */
function scan() {
  if (!containerEl) return
  const els = Array.from(containerEl.querySelectorAll<HTMLElement>(props.selector))
  const next = els.map((el) => ({
    // 原样保留（可能为空）：兜底词条在渲染期取，见 labelOf
    label: (el.dataset.section ?? '').trim(),
    el,
  }))
  const changed =
    next.length !== sections.value.length ||
    next.some((s, i) => s.el !== sections.value[i]?.el)
  sections.value = next
  if (changed) {
    targets.fill(0)
    currents.length = 0
  }
  // 分节未变但数据加载改变分节高度时，激活态须重算（首屏高亮不等到首次滚动）
  updateSpy()
}

/** scrollspy：阈值线落在哪个分节跨距内；都不含时取跨距最近者（双列页确定性单高亮） */
function updateSpy() {
  if (stickyIdx >= 0) return // 点击粘滞期内不覆盖
  if (!containerEl || !sections.value.length) {
    if (activeIndex.value !== -1) setActive(-1)
    return
  }
  const cTop = containerEl.getBoundingClientRect().top
  let best = -1
  let bestDist = Infinity
  let bestHeight = Infinity
  for (let i = 0; i < sections.value.length; i++) {
    const el = sections.value[i].el
    if (!el.isConnected) continue
    const rect = el.getBoundingClientRect()
    const top = rect.top - cTop
    const bottom = top + rect.height
    // 阈值线到分节跨距 [top, bottom] 的距离：0 = 阈值线落在分节内
    const dist = Math.max(top - SPY_THRESHOLD, SPY_THRESHOLD - bottom, 0)
    // 平局（嵌套分节同时含阈值线）取跨距更小者 = 更内层分节，
    // 否则外层容器（如 family 的 Steam 家庭库包住入库许可证）恒占高亮
    if (dist < bestDist || (dist === bestDist && rect.height < bestHeight)) {
      bestDist = dist
      bestHeight = rect.height
      best = i
    }
  }
  if (best !== activeIndex.value) setActive(best)
}

function setActive(idx: number) {
  activeIndex.value = idx
  if (reducedMotion.matches) {
    // 降级：无插值，直接吸附激活态
    forEachItem((el, i) => el.style.setProperty('--effect', i === idx ? '1' : '0'))
  } else {
    ensureLoop()
  }
}

/** 逐帧把 --effect 收拢到 max(邻近值, 激活值)。
 *
 *  缓动用**半衰期**表述：每过 halfLife 毫秒，当前值与目标的差距减半，
 *  即 k = 1 - 2^(-dt/halfLife)。取半衰期而非指数的时间常数，是因为它能直接读懂：
 *  「70ms 追平一半差距」比「70ms 后到达 63%」少一次脑内换算。
 *
 *  dt 上限 50ms：页面切回前台或长时间掉帧后，两帧间隔可能是几秒，
 *  不夹住的话这一帧的 k 会≈1，段落直接跳到位、看不出过渡。 */
function runFrame(now: number) {
  const dt = Math.min((now - lastTime) / 1000, 0.05)
  lastTime = now
  const halfLife = Math.max(props.halfLife, 1) / 1000
  const k = 1 - Math.pow(2, -dt / halfLife)

  let moving = false
  const active = activeIndex.value
  forEachItem((el, i) => {
    const target = Math.max(targets[i] || 0, active === i ? 1 : 0)
    const cur = currents[i] || 0
    const next = cur + (target - cur) * k
    const settled = Math.abs(target - next) < 0.0015
    const value = settled ? target : next
    currents[i] = value
    el.style.setProperty('--effect', value.toFixed(4))
    if (!settled) moving = true
  })

  rafId = moving ? requestAnimationFrame(runFrame) : null
}

function ensureLoop() {
  if (reducedMotion.matches || rafId != null) return
  lastTime = performance.now()
  rafId = requestAnimationFrame(runFrame)
}

function onPointerMove(e: PointerEvent) {
  if (reducedMotion.matches) return
  const list = railListEl.value
  if (!list) return
  const pointerY = e.clientY - list.getBoundingClientRect().top
  forEachItem((el, i) => {
    const center = el.offsetTop + el.offsetHeight / 2
    /* 感应圈内的线性距离 → 0..1 的邻近度 */
    const p = Math.max(0, 1 - Math.abs(pointerY - center) / props.proximityRadius)
    /* 升余弦衰减 0.5·(1 - cos πp)：两端导数为 0，段进出感应圈时不会突然
       变亮/变暗；中段比 ease-out 陡，因为这条曲线的职责是**空间分辨**——
       让最近的那一段明显最亮。换成中段就冲到 ~0.9 的曲线，
       光标附近三四个段会一起接近满值，读起来像「几段同时激活」。 */
    targets[i] = 0.5 * (1 - Math.cos(Math.PI * p))
  })
  ensureLoop()
}

function onPointerLeave() {
  if (reducedMotion.matches) return
  targets.fill(0)
  ensureLoop()
}

function jump(i: number) {
  const s = sections.value[i]
  if (!s || !s.el.isConnected) return
  // 点击粘滞：立即高亮目标段并冻结 spy，避免平滑滚动途中被中间位置覆盖
  window.clearTimeout(stickyTimer)
  stickyIdx = i
  setActive(i)
  stickyTimer = window.setTimeout(() => {
    stickyIdx = -1
    updateSpy()
  }, 700)
  s.el.scrollIntoView({
    behavior: reducedMotion.matches ? 'auto' : 'smooth',
    block: 'start',
  })
}

/** 悬停弹窗：段按键右侧、垂直居中对齐（cf-status-tip 同款视觉），离开 120ms 后收起 */
function showTip(i: number, e: PointerEvent) {
  const s = sections.value[i]
  if (!s) return
  window.clearTimeout(tipHideTimer)
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  tip.value = { label: labelOf(s), x: rect.right + 8, y: rect.top + rect.height / 2 }
}

function scheduleHideTip() {
  window.clearTimeout(tipHideTimer)
  tipHideTimer = window.setTimeout(() => (tip.value = null), 120)
}

function onScroll() {
  tip.value = null // 弹窗锚定视口坐标，滚动即收
  if (scrollRaf) return
  scrollRaf = true
  requestAnimationFrame(() => {
    scrollRaf = false
    updateSpy()
  })
}

/** 晚挂载分节兜底：game-detail / toolbox 等接口返回后才渲染的区块 */
function scheduleScan() {
  window.clearTimeout(mutationTimer)
  mutationTimer = window.setTimeout(scan, 200)
}

onMounted(() => {
  // .view-container 是兄弟节点，须等整树挂完再取
  nextTick(() => {
    containerEl = document.querySelector<HTMLElement>(props.container)
    if (containerEl) {
      containerEl.addEventListener('scroll', onScroll, { passive: true })
      mutationObserver = new MutationObserver(scheduleScan)
      mutationObserver.observe(containerEl, { childList: true, subtree: true })
    }
    scan()
  })
})

watch(
  () => route.fullPath,
  () => nextTick(scan),
)

onBeforeUnmount(() => {
  containerEl?.removeEventListener('scroll', onScroll)
  mutationObserver?.disconnect()
  window.clearTimeout(mutationTimer)
  window.clearTimeout(stickyTimer)
  window.clearTimeout(tipHideTimer)
  if (rafId != null) cancelAnimationFrame(rafId)
  rafId = null
})

defineOptions({ name: 'HlSectionRail' })
</script>

<template>
  <!-- 单分节 / 无分节页面不渲染轨道（不占宽度） -->
  <nav
    v-if="sections.length > 1"
    class="hl-rail"
    :style="{ '--max-shift': `${maxShift}px` }"
    :aria-label="t('rail.navLabel')"
  >
    <ul
      ref="railListEl"
      class="hl-rail__list"
      @pointermove="onPointerMove"
      @pointerleave="onPointerLeave"
    >
      <li
        v-for="(s, i) in sections"
        :key="`${i}-${s.label}`"
        class="hl-rail__item"
        :class="{ 'is-active': i === activeIndex }"
      >
        <button
          type="button"
          class="hl-rail__btn"
          :aria-label="t('rail.jumpTo', { label: labelOf(s) })"
          :aria-current="i === activeIndex ? 'true' : undefined"
          @click="jump(i)"
          @pointerenter="showTip(i, $event)"
          @pointerleave="scheduleHideTip()"
        >
          <span class="hl-rail__seg" aria-hidden="true" />
        </button>
      </li>
    </ul>
  </nav>

  <!-- 悬停弹窗（cf-status-tip 同款视觉：毛玻璃浮层 + 蓝边 + 上浮入场） -->
  <Teleport to="body">
    <div
      v-if="tip"
      class="hl-rail-tip"
      :style="{ left: `${tip.x}px`, top: `${tip.y}px` }"
      role="tooltip"
    >
      {{ tip.label }}
    </div>
  </Teleport>
</template>
