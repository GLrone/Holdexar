<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import {
  ASSET_RETRY_MAX,
  assetRetryUrl,
  isAssetDead,
  markAssetDead,
  markAssetOk,
  resolveAssetUrl,
} from '@/lib/assetCache'

/**
 * 外链图片统一出口：素材复用登记处（assetCache）的组件化。
 *
 * - 首发取登记过的「曾成功 URL」（resolveAssetUrl），命中跨挂载复用；
 * - 加载失败自动换 `?hlretry=N` 稳定 query 重试（绕开坏缓存），共 ASSET_RETRY_MAX 次；
 * - 重试耗尽登记死图（10 分钟窗口内不再发请求），渲染 #fallback 插槽——
 *   无该插槽时整个 img 消失，由父级布局决定空位形态；
 * - 任意一次成功（含重试）登记实际生效的 URL 并撤销死图。
 *
 * class/style/loading 等属性透传到 img；alt 用 prop。
 */
defineOptions({ inheritAttrs: false })

const props = defineProps<{
  /** 外链图片 URL；空值或死图窗口内不渲染 img，落 #fallback */
  src?: string | null
  alt?: string
}>()

const attempt = ref(0)
const dead = ref(false)

watch(
  () => props.src,
  (v) => {
    attempt.value = 0
    dead.value = isAssetDead(v)
  },
  { immediate: true },
)

const liveSrc = computed(() => {
  if (!props.src || dead.value) return ''
  // 重试基于裸 URL 换 query（hlretry 叠加在干净序列上）；首发吃登记的成功 URL
  return attempt.value > 0 ? assetRetryUrl(props.src, attempt.value) : resolveAssetUrl(props.src)
})

function onLoaded() {
  dead.value = false
  markAssetOk(props.src, liveSrc.value)
}

function onFailed() {
  if (!props.src) return
  if (attempt.value < ASSET_RETRY_MAX) {
    attempt.value += 1
  } else {
    dead.value = true
    markAssetDead(props.src)
  }
}
</script>

<template>
  <img
    v-if="liveSrc"
    :src="liveSrc"
    :alt="alt ?? ''"
    v-bind="$attrs"
    @load="onLoaded"
    @error="onFailed"
  />
  <slot v-else name="fallback" />
</template>
