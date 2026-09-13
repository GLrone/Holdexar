<script setup lang="ts">
import { computed } from 'vue'

/**
 * 标准按键 —— 艺术按键优先。
 * 传入 art 时渲染 .hl-btn-art（pattern 图案阴影 / outline 粗描边 / combo 深色波浪），
 * 否则渲染常规 .hl-btn 变体。视觉规范见 web/public/component-framework.html。
 */
const props = withDefaults(
  defineProps<{
    /** 常规变体（art 未设置时生效） */
    variant?: 'default' | 'primary' | 'success' | 'danger' | 'warning' | 'info' | 'ghost' | 'text'
    /** 艺术按键方案：设置后优先于 variant */
    art?: '' | 'pattern' | 'outline' | 'combo'
    /** 艺术配色 / 图案（pattern 支持 steam|game|premium|discount 图案） */
    tone?: '' | 'green' | 'blue' | 'pink' | 'dark' | 'wine' | 'red' | 'steam' | 'game' | 'premium' | 'discount'
    /** 艺术按键矩形圆角变体 */
    rect?: boolean
    size?: 'md' | 'sm' | 'lg'
    block?: boolean
    disabled?: boolean
    /** 异步进行中：内置转圈 + 强制禁用（配合 message.loading 气泡使用） */
    loading?: boolean
    type?: 'button' | 'submit' | 'reset'
  }>(),
  {
    variant: 'default',
    art: '',
    tone: '',
    rect: false,
    size: 'md',
    block: false,
    disabled: false,
    loading: false,
    type: 'button',
  },
)

const classes = computed(() => {
  if (props.art) {
    return [
      'hl-btn-art',
      `hl-btn-art--${props.art}`,
      props.tone ? `hl-btn-art--${props.tone}` : '',
      props.rect ? 'hl-btn-art--rect' : '',
      props.size === 'sm' ? 'hl-btn-art--sm' : '',
    ]
  }
  return [
    'hl-btn',
    props.variant !== 'default' ? `hl-btn--${props.variant}` : '',
    props.size === 'sm' ? 'hl-btn--sm' : '',
    props.size === 'lg' ? 'hl-btn--lg' : '',
    props.block ? 'hl-btn--block' : '',
  ]
})
</script>

<template>
  <button
    :type="type"
    :class="classes"
    :disabled="disabled || loading || undefined"
    :aria-busy="loading || undefined"
  >
    <span v-if="art" class="hl-btn-art__txt">
      <span v-if="loading" class="hl-spinner hl-spinner--inline" aria-hidden="true" /><slot />
    </span>
    <template v-else>
      <span v-if="loading" class="hl-spinner hl-spinner--inline" aria-hidden="true" /><slot />
    </template>
  </button>
</template>
