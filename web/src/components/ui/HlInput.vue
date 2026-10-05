<script setup lang="ts">
import { computed, ref } from 'vue'

import HlIcon from './HlIcon.vue'
import { useI18n } from '@/locales'

const model = defineModel<string>({ default: '' })

const props = withDefaults(
  defineProps<{
    placeholder?: string
    /** 前缀图标名（ui/icons.ts 认可清单） */
    prefixIcon?: string
    /** 后缀图标名 */
    suffixIcon?: string
    disabled?: boolean
    type?: string
    /** 密钥形态：输入时始终掩码，右侧眼睛切换明文（切换不落库、只改本地视图） */
    showPassword?: boolean
  }>(),
  { placeholder: '', prefixIcon: '', suffixIcon: '', disabled: false, type: 'text', showPassword: false },
)

const { t } = useI18n()
const revealed = ref(false)
const inputType = computed(() =>
  props.showPassword ? (revealed.value ? 'text' : 'password') : (props.type ?? 'text'),
)
</script>

<template>
  <span class="hl-input-wrap" :class="{ 'is-focus': undefined }">
    <span v-if="prefixIcon" class="hl-iprefix"><HlIcon :name="prefixIcon" /></span>
    <input
      v-model="model"
      :type="inputType"
      :placeholder="placeholder"
      :disabled="disabled"
    />
    <button
      v-if="showPassword"
      type="button"
      class="hl-isuffix hl-input-eye"
      :aria-label="revealed ? t('common.mask') : t('common.reveal')"
      :title="revealed ? t('common.mask') : t('common.reveal')"
      @click.prevent="revealed = !revealed"
    >
      <HlIcon :name="revealed ? 'eye-off' : 'eye'" />
    </button>
    <span v-else-if="suffixIcon" class="hl-isuffix"><HlIcon :name="suffixIcon" /></span>
  </span>
</template>
