<script setup lang="ts">
/* 屏外分节延迟挂载：先只放一个同锚点的占位块，等它接近视口再挂真内容。
 *
 *  成就殿堂分节多且重（热力图、称号墙等），一次性全渲染会明显拖慢切页；
 *  屏外分节在用户滚动到之前用不上，却要一起等渲染。
 *
 *  两条关键设计：
 *  ① **锚点条件挂载**：占位块先带 `data-section`，真内容挂载后（它自带同锚点的
 *     `<section>`）就把占位块的属性撤掉——页面里任何时刻同一个锚点只有一个，
 *     侧边定位轨不会出现重复项，点击轨道滚动到占位块也照样能触发挂载。
 *  ② **min-height 占位**：占位期给一个估高，避免挂载瞬间把下方内容整体推移、
 *     把用户正在滚的位置顶跑；挂载后放开。
 */
import { onBeforeUnmount, onMounted, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 分节锚点（与真内容自带的 data-section 同值） */
    anchor: string
    /** 占位高度（px） */
    minHeight?: number
    /** 提前挂载余量：滚到附近就先把内容备好，减少"看到空白再跳出内容" */
    margin?: number
  }>(),
  { minHeight: 260, margin: 320 },
)

const host = ref<HTMLElement | null>(null)
const shown = ref(false)
let io: IntersectionObserver | null = null

onMounted(() => {
  // 无 IntersectionObserver（极老环境）→ 退回立即挂载，功能不受影响
  if (typeof IntersectionObserver === 'undefined' || !host.value) {
    shown.value = true
    return
  }
  io = new IntersectionObserver(
    (entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        shown.value = true
        io?.disconnect()
        io = null
      }
    },
    { rootMargin: `${props.margin}px 0px` },
  )
  io.observe(host.value)
})

onBeforeUnmount(() => {
  io?.disconnect()
  io = null
})
</script>

<template>
  <div
    ref="host"
    class="achv-defer"
    :data-section="shown ? undefined : anchor"
    :style="shown ? undefined : { minHeight: `${minHeight}px` }"
  >
    <slot v-if="shown" />
  </div>
</template>

<style scoped>
/* 已挂载且仍在屏外时跳过布局与绘制（浏览器原生支持，省的是 native 那一段）。
   contain-intrinsic-size 给个估值，滚动条长度在跳过布局时也不至于乱跳。 */
.achv-defer {
  display: flex;
  flex-direction: column;
  content-visibility: auto;
  contain-intrinsic-size: auto 480px;
}
</style>