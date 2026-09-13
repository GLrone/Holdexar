<script setup lang="ts">
import { watch } from 'vue'

const props = withDefaults(
  defineProps<{
    title?: string
    /** 点击遮罩关闭 */
    maskClosable?: boolean
    /** 弹窗最大宽度（px）；0 = 用框架默认 460 */
    width?: number
  }>(),
  { title: '', maskClosable: true, width: 0 },
)

const model = defineModel<boolean>({ default: false })

watch(model, (v) => {
  document.body.style.overflow = v ? 'hidden' : ''
})
</script>

<template>
  <Teleport to="body">
    <Transition name="hl-dialog-pop" appear>
      <div
        v-if="model"
        class="hl-overlay"
        @click="maskClosable && (model = false)"
      >
        <div class="hl-dialog" :style="props.width > 0 ? { maxWidth: `${props.width}px` } : undefined" @click.stop>
          <div class="hl-dialog__head">
            <span class="hl-dialog__title">{{ title }}</span>
            <button type="button" class="hl-dialog__close" @click="model = false">✕</button>
          </div>
          <div class="hl-dialog__body"><slot /></div>
          <div v-if="$slots.footer" class="hl-dialog__foot"><slot name="footer" /></div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
