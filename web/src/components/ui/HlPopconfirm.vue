<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from '@/locales'

defineProps<{ text?: string; confirmLabel?: string; cancelLabel?: string }>()

const emit = defineEmits<{ confirm: []; cancel: [] }>()

const { t } = useI18n()

const show = ref(false)
const root = ref<HTMLElement | null>(null)

/** 点外关闭（对齐 HlSelect）：气泡与触发钮都在 host 子树内，点外面才收起 */
function onDocClick(e: MouseEvent) {
  if (root.value?.contains(e.target as Node)) return
  show.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <span ref="root" class="hl-tip-host" @click="show = !show">
    <slot />
    <span class="hl-popconfirm" :class="{ 'is-show': show }" @click.stop>
      {{ text ?? t('common.confirmTip') }}
      <span class="hl-popconfirm__actions">
        <button
          type="button"
          class="hl-btn hl-btn--sm"
          @click="((show = false), emit('cancel'))"
        >
          {{ cancelLabel ?? t('common.cancel') }}
        </button>
        <button
          type="button"
          class="hl-btn hl-btn--sm hl-btn--primary"
          @click="((show = false), emit('confirm'))"
        >
          {{ confirmLabel ?? t('common.confirm') }}
        </button>
      </span>
    </span>
  </span>
</template>
