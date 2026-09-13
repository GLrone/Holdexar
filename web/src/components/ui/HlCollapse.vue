<script setup lang="ts">
import { ref } from 'vue'

export interface HlCollapseItem {
  title: string
  content: string
}

defineProps<{ items: HlCollapseItem[] }>()

const activeIndex = ref<number | null>(null)

function toggle(i: number) {
  activeIndex.value = activeIndex.value === i ? null : i
}
</script>

<template>
  <div class="hl-collapse">
    <div
      v-for="(item, i) in items"
      :key="i"
      class="hl-collapse-item"
      :class="{ 'is-on': activeIndex === i }"
    >
      <button type="button" class="hl-collapse-item__head" @click="toggle(i)">
        {{ item.title }}
        <i class="hl-collapse-item__arrow">›</i>
      </button>
      <div class="hl-collapse-item__body">
        <div class="hl-collapse-item__content">{{ item.content }}</div>
      </div>
    </div>
  </div>
</template>
