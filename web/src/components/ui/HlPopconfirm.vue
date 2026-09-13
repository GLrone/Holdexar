<script setup lang="ts">
import { ref } from 'vue'
import { useI18n } from '@/locales'

defineProps<{ text?: string; confirmLabel?: string; cancelLabel?: string }>()

const emit = defineEmits<{ confirm: []; cancel: [] }>()

const { t } = useI18n()

const show = ref(false)
</script>

<template>
  <span class="hl-tip-host" @click="show = !show">
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
