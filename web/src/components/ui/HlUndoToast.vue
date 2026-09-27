<script setup lang="ts">
import { useI18n } from '@/locales'
import HlButton from './HlButton.vue'

/**
 * 撤销条：破坏性轻操作的「结果 + 后悔药」浮层（底部居中）。
 * 显隐由 show 驱动（出入场对称：位移 + 透明度，时长走动效总闸）；
 * 动作文案与撤销回调由调用方给定，本组件不关心业务语义。
 */
const props = defineProps<{
  show: boolean
  text: string
}>()

const emit = defineEmits<{ (e: 'undo'): void }>()

const { t } = useI18n()
</script>

<template>
  <Transition name="hl-undo-toast">
    <div v-if="props.show" class="hl-undo-toast">
      <span>{{ props.text }}</span>
      <HlButton variant="text" size="sm" @click="emit('undo')">
        {{ t('common.undo') }}
      </HlButton>
    </div>
  </Transition>
</template>

<style scoped>
.hl-undo-toast {
  position: fixed;
  left: 50%;
  bottom: 24px;
  transform: translateX(-50%);
  z-index: 60;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 8px 6px 14px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-md);
  font-size: 13px;
}
.hl-undo-toast-enter-active,
.hl-undo-toast-leave-active {
  transition:
    opacity calc(var(--duration-2) * var(--motion-scale)),
    transform calc(var(--duration-2) * var(--motion-scale));
}
.hl-undo-toast-enter-from,
.hl-undo-toast-leave-to {
  opacity: 0;
  transform: translateX(-50%) translateY(8px);
}
</style>
