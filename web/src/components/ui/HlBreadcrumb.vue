<script setup lang="ts">
export interface HlBreadcrumbItem {
  label: string
  /** 点击回调名（路由跳转由视图层处理） */
  to?: string
}

withDefaults(defineProps<{ items: HlBreadcrumbItem[] }>(), { items: () => [] })

const emit = defineEmits<{ select: [item: HlBreadcrumbItem, index: number] }>()
</script>

<template>
  <div class="hl-breadcrumb">
    <template v-for="(item, i) in items" :key="i">
      <span
        class="hl-breadcrumb__item"
        :class="{ 'is-current': i === items.length - 1 }"
        @click="i < items.length - 1 && emit('select', item, i)"
      >
        {{ item.label }}
      </span>
      <span v-if="i < items.length - 1" class="hl-breadcrumb__sep">/</span>
    </template>
  </div>
</template>
