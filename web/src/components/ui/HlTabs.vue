<script setup lang="ts">
export interface HlTabItem {
  key: string
  label: string
}

defineProps<{ tabs: HlTabItem[] }>()

const model = defineModel<string>({ default: '' })
</script>

<template>
  <div>
    <div class="hl-tabs__head">
      <button
        v-for="t in tabs"
        :key="t.key"
        type="button"
        class="hl-tabs__item"
        :class="{ 'is-on': model === t.key }"
        @click="model = t.key"
      >
        {{ t.label }}
      </button>
    </div>
    <div class="hl-tabs__body">
      <slot :active="model" />
    </div>
  </div>
</template>
