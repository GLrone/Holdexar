<script setup lang="ts">
export interface HlTimelineItem {
  time?: string
  title: string
  desc?: string
  tone?: 'default' | 'success' | 'danger'
}

withDefaults(defineProps<{ items: HlTimelineItem[] }>(), { items: () => [] })
</script>

<template>
  <div class="hl-timeline">
    <div
      v-for="(item, i) in items"
      :key="i"
      class="hl-timeline-item"
      :class="item.tone && item.tone !== 'default' ? `hl-timeline-item--${item.tone}` : ''"
    >
      <span class="hl-timeline-item__dot" />
      <div>
        <div v-if="item.time" class="hl-timeline-item__time">{{ item.time }}</div>
        <div class="hl-timeline-item__title">{{ item.title }}</div>
        <div v-if="item.desc" class="hl-timeline-item__desc">{{ item.desc }}</div>
      </div>
    </div>
  </div>
</template>
