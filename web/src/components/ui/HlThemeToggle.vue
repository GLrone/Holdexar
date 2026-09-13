<script setup lang="ts">
import { computed } from 'vue'
import HlIcon from './HlIcon.vue'
import { useI18n } from '@/locales'

const props = withDefaults(defineProps<{ dark?: boolean }>(), { dark: true })

const emit = defineEmits<{ toggle: [] }>()

const { t } = useI18n()

// 文案显示**切换后**的状态（与语言钮同理）；t 在渲染期读 locale，切语言即重算
const title = computed(() => t(props.dark ? 'shell.theme.toLight' : 'shell.theme.toDark'))

defineOptions({ name: 'HlThemeToggle' })
</script>

<template>
  <!-- 纯图标圆钮：悬停旋转放大，深色下图标 180° 翻转（见 .hl-theme-btn） -->
  <button
    type="button"
    class="hl-theme-btn"
    :title="title"
    :aria-label="title"
    @click="emit('toggle')"
  >
    <HlIcon :name="dark ? 'sun' : 'moon'" :size="17" />
  </button>
</template>
