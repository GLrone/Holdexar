<script setup lang="ts">
export interface HlDescriptionItem {
  label: string
  value: string
}

withDefaults(defineProps<{ items: HlDescriptionItem[] }>(), { items: () => [] })
</script>

<template>
  <div class="hl-descriptions">
    <div class="hl-descriptions__grid">
      <template v-for="(item, i) in items" :key="i">
        <span class="hl-descriptions__label">{{ item.label }}</span>
        <span class="hl-descriptions__val">
          <slot :item="item">{{ item.value }}</slot>
        </span>
      </template>
    </div>
  </div>
</template>
