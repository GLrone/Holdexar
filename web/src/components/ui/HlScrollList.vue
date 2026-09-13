<script setup lang="ts">
/**
 * HlScrollList —— 纵向滚动列表容器（家庭库的「列表模式」用）。
 *
 * **本组件是独立实现，不是任何上游组件的翻译/移植。** 它的前身
 * `components/AnimatedList.vue` 自述为某外部库的 Vue 翻译版，连同组件名、props 名与
 * 默认值一起照搬了一套形制；本次按本项目的实际需要整体重写并更名为 `HlScrollList`
 * （同名的旧实现只有一份零引用的空壳，已删）。关于交互形式的参考与署名见
 * 应用内「关于」页致谢名单（许可清单随安装包分发）。
 *
 * 职责边界：只管**容器**。滚动、进出视口的显隐、上下渐隐遮罩、选中态与键盘导航都在
 * 这里；列表项长什么样由 `#item` 插槽决定（本组件不碰条目内部）。
 *
 * 三处与本项目其它动效一致的约定：
 *  · 显隐用 `opacity + translateY`，**不用 scale**——缩放对前庭敏感用户不友好，
 *    且项目内其它出入场（.hl-pane-*、抽屉、弹层）都是位移+透明度这一套。
 *  · 时长一律 `calc(var(--duration-N) * var(--motion-scale))`，开着系统级
 *    「减少动态效果」时 --motion-scale 归零 → 瞬时到位，不需要另写 media query。
 *  · 键盘导航**必须显式开启**，且只在本容器持有焦点时生效。旧实现挂在 window 上并
 *    对 Tab 做 preventDefault——只要列表在页面上，整页的 Tab 焦点顺序就被劫持。
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
const emit = defineEmits<{ select: [item: unknown, index: number] }>()

const scroller = ref<HTMLElement | null>(null)
const maskTop = ref<HTMLElement | null>(null)
const selectedIndex = ref(-1)
const topFade = ref(0)
const bottomFade = ref(0)

let observer: IntersectionObserver | null = null

/* 进出视口的显隐直接改 class，不进响应式：滚动时每帧都在切换，
   走 Set + 响应式会触发整列表重渲染。选中态是低频的，才留在 ref 里。 */
function setupObserver() {
  observer?.disconnect()
  const root = scroller.value
  if (!root) return
  observer = new IntersectionObserver(
    (entries) => {
      for (const e of entries) e.target.classList.toggle('is-in', e.isIntersecting)
    },
    { root, threshold: 0.01 },
  )
  root.querySelectorAll('.hl-scroll-list__item').forEach((el) => observer?.observe(el))
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
  () => nextTick(setupObserver),
)

onMounted(() => {
  setupObserver()
  onScroll()
})

onBeforeUnmount(() => observer?.disconnect())
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
    </div>

    <template v-if="showGradients">
      <div ref="maskTop" class="hl-scroll-list__mask hl-scroll-list__mask--top" :style="{ opacity: topFade }" />
      <div class="hl-scroll-list__mask hl-scroll-list__mask--bottom" :style="{ opacity: bottomFade }" />
    </template>
  </div>
</template>
