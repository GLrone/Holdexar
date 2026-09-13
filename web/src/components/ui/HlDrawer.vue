<script setup lang="ts">
import { useAttrs, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    title?: string
    width?: string
    /** 点击遮罩关闭（仅 modal=true 时生效） */
    maskClosable?: boolean
    /** 是否模态（有遮罩+锁滚动），默认 true；非模态时底层可交互 */
    modal?: boolean
    /** 是否显示默认头部，默认 true；设为 false 时由 slot 完全自定义内容 */
    withHeader?: boolean
    /** 顶部偏移像素（如避开 sticky 导航栏），默认 0 */
    top?: number
  }>(),
  { title: '', width: '250px', maskClosable: true, modal: true, withHeader: true, top: 0 },
)

const attrs = useAttrs()
const model = defineModel<boolean>({ default: false })

// 仅模态时锁定 body 滚动；非模态不锁
watch(model, (v) => {
  if (props.modal) {
    document.body.style.overflow = v ? 'hidden' : ''
  }
})

defineOptions({ inheritAttrs: false })
</script>

<template>
  <Teleport to="body">
    <Transition name="hl-drawer-fade" appear>
      <div v-if="model && modal" class="hl-drawer-mask" @click="maskClosable && (model = false)" />
    </Transition>
    <Transition name="hl-drawer-slide" appear>
      <div
        v-if="model"
        class="hl-drawer"
        :class="[attrs.class, { 'hl-drawer--non-modal': !modal }]"
        :style="[
          attrs.style as Record<string, string>,
          {
            width,
            top: top ? top + 'px' : undefined,
            height: top ? `calc(100% - ${top}px)` : undefined,
          },
        ]"
      >
        <div v-if="withHeader" class="hl-drawer__head">
          {{ title }}
          <button type="button" class="hl-dialog__close" @click="model = false">✕</button>
        </div>
        <div class="hl-drawer__body" :class="{ 'hl-drawer__body--full': !withHeader }">
          <slot />
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
