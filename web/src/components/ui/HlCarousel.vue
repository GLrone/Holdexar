<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    /** 轮播帧数（内容用默认插槽按索引渲染，或 slide 插槽） */
    count: number
    /** 幻灯片底色（默认取 accent 渐变） */
    colors?: string[]
    /** 自动轮播间隔 ms（0 = 关闭，默认；hover 暂停） */
    interval?: number
  }>(),
  { count: 0, colors: () => [], interval: 0 },
)

const active = ref(0)
const paused = ref(false)

const trackStyle = computed(() => ({ transform: `translateX(-${active.value * 100}%)` }))

function go(i: number) {
  active.value = (i + props.count) % props.count
}

// ── 自动轮播（interval > 0 且帧数 ≥ 2 时启用；hover 暂停，卸载清理）──
let timer: number | null = null

function stopTimer() {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

function startTimer() {
  stopTimer()
  if (!props.interval || props.count < 2) return
  timer = window.setInterval(() => {
    if (!paused.value) go(active.value + 1)
  }, props.interval)
}

watch(() => [props.count, props.interval], startTimer)
onBeforeUnmount(stopTimer)

const slideBg = (i: number) =>
  props.colors[i]
    ? { background: props.colors[i] }
    : {
        background:
          'linear-gradient(135deg, var(--accent-deep), var(--accent) ' +
          (55 + i * 10) + '%, var(--success))',
      }
</script>

<template>
  <div class="hl-carousel" @mouseenter="paused = true" @mouseleave="paused = false">
    <div class="hl-carousel__track" :style="trackStyle">
      <div v-for="i in count" :key="i" class="hl-carousel__slide" :style="slideBg(i - 1)">
        <slot :index="i - 1" />
      </div>
    </div>
    <button
      v-if="count > 1"
      type="button"
      class="hl-carousel__arrow hl-carousel__arrow--prev"
      @click="go(active - 1)"
    >
      ‹
    </button>
    <button
      v-if="count > 1"
      type="button"
      class="hl-carousel__arrow hl-carousel__arrow--next"
      @click="go(active + 1)"
    >
      ›
    </button>
    <div v-if="count > 1" class="hl-carousel__dots">
      <button
        v-for="i in count"
        :key="i"
        type="button"
        class="hl-carousel__dot"
        :class="{ 'is-on': active === i - 1 }"
        @click="go(i - 1)"
      />
    </div>
  </div>
</template>
