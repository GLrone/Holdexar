<script setup lang="ts">
import { computed } from 'vue'
import HlIcon from './HlIcon.vue'
import { useI18n } from '@/locales'
import type { Locale } from '@/stores/locale'

const props = withDefaults(defineProps<{ locale?: Locale }>(), {
  locale: 'zh-CN',
})

const emit = defineEmits<{ toggle: [] }>()

const { t } = useI18n()

// 文案显示**目标语言**（当前中文 → EN；当前英文 → 中），与主题钮"展示切换后状态"同理。
// 短码是语言自称、title 是句子，两者在两种界面下同值——理由见 shell.ts 的说明。
const label = computed(() =>
  t(props.locale === 'zh-CN' ? 'shell.lang.targetEn' : 'shell.lang.targetZh'),
)
const title = computed(() =>
  t(props.locale === 'zh-CN' ? 'shell.lang.toEn' : 'shell.lang.toZh'),
)

defineOptions({ name: 'HlLangToggle' })
</script>

<template>
  <!-- 语言切换钮：地球图标 + 目标语言短码，悬停放大，样式见 .hl-lang-btn -->
  <button
    type="button"
    class="hl-lang-btn"
    :title="title"
    :aria-label="title"
    @click="emit('toggle')"
  >
    <HlIcon name="globe" :size="15" />
    <span>{{ label }}</span>
  </button>
</template>
