<script setup lang="ts">
/**
 * HlScrollList —— 纵向滚动列表容器（游戏商店 / 家庭库的「列表模式」用）。
 *
 * 职责边界：只管**容器**。滚动、进出视口的显隐、上下渐隐遮罩、选中态、键盘导航与
 * 无限滚动哨兵都在这里；列表项长什么样由 `#item` 插槽决定（本组件不碰条目内部）。
 *
 * 四处与本项目其它动效一致的约定：
 *  · 显隐用 `opacity + translateY`，**不用 scale**——缩放对前庭敏感用户不友好，
 *    且项目内其它出入场（.hl-pane-*、抽屉、弹层）都是位移+透明度这一套。
 *  · 入场动画**只播一次**（data-entered 只加不撤）：连续滚动容器里「滚出淡出、滚回重播」
 *    会把视口内条目全推进过渡态，观感是列表发虚而非有动效。
 *  · 时长一律 `calc(var(--duration-N) * var(--motion-scale))`，开着系统级
 *    「减少动态效果」时 --motion-scale 归零 → 瞬时到位，不需要另写 media query。
 *  · 键盘导航**必须显式开启**，且只在本容器持有焦点时生效。禁用挂 window + 对 Tab
 *    做 preventDefault 的写法——只要列表在页面上，整页的 Tab 焦点顺序就被劫持。
 *
 * 无限滚动：滚动区末尾有常驻哨兵，接近底部时 emit `endReached`；加载态 / 到底
 * 提示由消费方放进 `#footer` 插槽（渲染在条目与哨兵之间，随内容一起滚）。
 */
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 列表数据。仅用于遍历与键盘导航的边界判定 */
    items: unknown[]
    /** 上下渐隐遮罩 */
    showGradients?: boolean
    /** 自绘滚动条（关掉即隐藏，仍可滚动） */
    displayScrollbar?: boolean
    /** 方向键 / Enter 导航。默认关：见文件头关于 Tab 劫持的说明 */
    keyboardNav?: boolean
    /** 列表项额外 class */
    itemClass?: string
    /** 滚动区最大高度（CSS 长度） */
    maxHeight?: string
  }>(),
  {
    showGradients: true,
    displayScrollbar: true,
    keyboardNav: false,
    itemClass: '',
    maxHeight: '70vh',
  },
)

/** 选中项变化。用 emit 而不是 `onXxx` 回调 prop：后者是 React 的传参形制，
 *  Vue 侧的既有约定是 emits（同 HlSelect / HlPopconfirm）。 */
const emit = defineEmits<{
  select: [item: unknown, index: number]
  /** 滚动接近底部（尾部哨兵进入 240px 预判区）。消费方按 hasNextPage / loading 自行去重 */
  endReached: []
}>()

const scroller = ref<HTMLElement | null>(null)
const maskTop = ref<HTMLElement | null>(null)
const tail = ref<HTMLElement | null>(null)
const selectedIndex = ref(-1)
const topFade = ref(0)
const bottomFade = ref(0)

let observer: IntersectionObserver | null = null
let tailObserver: IntersectionObserver | null = null

/* 进出视口的显隐直接改 DOM 属性，不进响应式：滚动时每帧都在切换，
   走 Set + 响应式会触发整列表重渲染。选中态是低频的，才留在 ref 里。
   入场**只加不撤**（见文件头）：滚出的条目保持常亮，回滚不重播，连续滚动不闪。
   入场标记必须是 data 属性、不能是手工加的 class——Vue 对 class 绑定变化的
   patch 是整串覆写 className，会把手加的类抹掉（条目被点选后 is-selected
   变化即触发覆写，条目当场隐形且永不恢复）；vdom 之外的手工属性不受 patch 影响。 */
function setupObserver() {
  observer?.disconnect()
  const root = scroller.value
  if (!root) return
  observer = new IntersectionObserver(
    (entries) => {
      for (const e of entries) {
        if (e.isIntersecting) e.target.setAttribute('data-entered', '')
      }
    },
    { root, threshold: 0.01 },
  )
  root.querySelectorAll('.hl-scroll-list__item').forEach((el) => observer?.observe(el))
}

/* 尾部哨兵（无限滚动观察点）：进入预判区即 emit endReached。
   条目数组变化后必须重新 observe——新内容到达时哨兵可能仍停在视口内，
   IntersectionObserver 不会对同一目标重复上报同一个相交状态。 */
function setupTailObserver() {
  tailObserver?.disconnect()
  const root = scroller.value
  const el = tail.value
  if (!root || !el) return
  tailObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) emit('endReached')
    },
    { root, rootMargin: '240px 0px' },
  )
  tailObserver.observe(el)
}

/* 遮罩浓度按「滚过了多少」算，分母取遮罩自身的实际高度——两边写死同一个数字
   迟早会对不上（改 CSS 忘了改 JS），直接从元素上读就不存在这个问题。 */
function onScroll() {
  const el = scroller.value
  if (!el) return
  const maskH = maskTop.value?.offsetHeight || 1
  const { scrollTop, scrollHeight, clientHeight } = el
  topFade.value = Math.min(scrollTop / maskH, 1)
  const below = scrollHeight - (scrollTop + clientHeight)
  bottomFade.value = scrollHeight <= clientHeight ? 0 : Math.min(below / maskH, 1)
}

function select(i: number) {
  selectedIndex.value = i
  emit('select', props.items[i], i)
}

function onKeydown(e: KeyboardEvent) {
  if (!props.keyboardNav || !props.items.length) return
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault()
    const step = e.key === 'ArrowDown' ? 1 : -1
    const from = selectedIndex.value < 0 ? (step > 0 ? -1 : props.items.length) : selectedIndex.value
    select(Math.min(Math.max(from + step, 0), props.items.length - 1))
  } else if (e.key === 'Enter' && selectedIndex.value >= 0) {
    e.preventDefault()
    select(selectedIndex.value)
  }
}

/* 键盘移动选中项后把它带进视野。边距取遮罩高度：被上下渐隐盖住的条目等于看不见。 */
watch(selectedIndex, async (idx) => {
  if (!props.keyboardNav || idx < 0) return
  await nextTick()
  const el = scroller.value
  const item = el?.querySelector<HTMLElement>(`[data-index="${idx}"]`)
  if (!el || !item) return
  const margin = maskTop.value?.offsetHeight ?? 0
  const top = item.offsetTop
  const bottom = top + item.offsetHeight
  if (top < el.scrollTop + margin) {
    el.scrollTo({ top: Math.max(top - margin, 0), behavior: 'smooth' })
  } else if (bottom > el.scrollTop + el.clientHeight - margin) {
    el.scrollTo({ top: bottom - el.clientHeight + margin, behavior: 'smooth' })
  }
})

/* 条目数量变化（翻页加载更多）后要重新观察：IntersectionObserver 不会自动跟进
   新插入的 DOM。deep 关掉——只有数组本身被替换时才需要重扫。 */
watch(
  () => props.items,
  () =>
    nextTick(() => {
      setupObserver()
      setupTailObserver()
    }),
)

onMounted(() => {
  setupObserver()
  setupTailObserver()
  onScroll()
})

onBeforeUnmount(() => {
  observer?.disconnect()
  tailObserver?.disconnect()
})
</script>

<template>
  <div class="hl-scroll-list-container" :style="{ '--list-max-h': maxHeight }">
    <div
      ref="scroller"
      class="hl-scroll-list"
      :class="{ 'is-plain-scroll': !displayScrollbar }"
      :tabindex="keyboardNav ? 0 : undefined"
      @scroll="onScroll"
      @keydown="onKeydown"
    >
      <div
        v-for="(item, index) in items"
        :key="index"
        :data-index="index"
        class="hl-scroll-list__item"
        :class="[itemClass, { 'is-selected': selectedIndex === index }]"
        @click="select(index)"
      >
        <slot name="item" :item="item" :index="index" :selected="selectedIndex === index">
          <p class="hl-scroll-list__text">{{ typeof item === 'string' ? item : String(item) }}</p>
        </slot>
      </div>

      <!-- 追加内容插槽（加载态 / 到底提示）与无限滚动哨兵：都在滚动区内、条目之后 -->
      <slot name="footer" />
      <div ref="tail" class="hl-scroll-list__tail" aria-hidden="true" />
    </div>

    <template v-if="showGradients">
      <div ref="maskTop" class="hl-scroll-list__mask hl-scroll-list__mask--top" :style="{ opacity: topFade }" />
      <div class="hl-scroll-list__mask hl-scroll-list__mask--bottom" :style="{ opacity: bottomFade }" />
    </template>
  </div>
</template>
