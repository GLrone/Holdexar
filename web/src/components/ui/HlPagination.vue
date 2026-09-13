<script setup lang="ts">
import { computed } from 'vue'

const props = withDefaults(
  defineProps<{
    total: number
    pageSize?: number
    /** 页码省略前后保留数 */
    siblings?: number
  }>(),
  { pageSize: 10, siblings: 1 },
)

const model = defineModel<number>({ default: 1 })

const pageCount = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)))

/** 页码序列（含省略标记） */
const pages = computed<(number | '…')[]>(() => {
  const count = pageCount.value
  const cur = model.value
  const s = props.siblings
  if (count <= s * 2 + 3) return Array.from({ length: count }, (_, i) => i + 1)
  const set = new Set<number>([1, count, cur])
  for (let i = 1; i <= s; i++) {
    set.add(cur - i)
    set.add(cur + i)
  }
  const sorted = [...set].filter((n) => n >= 1 && n <= count).sort((a, b) => a - b)
  const out: (number | '…')[] = []
  let prev = 0
  for (const n of sorted) {
    if (n - prev > 1) out.push('…')
    out.push(n)
    prev = n
  }
  return out
})

function go(n: number) {
  model.value = Math.min(pageCount.value, Math.max(1, n))
}
</script>

<template>
  <div class="hl-pagination">
    <button :disabled="model <= 1" @click="go(model - 1)">‹</button>
    <template v-for="(p, i) in pages" :key="i">
      <span v-if="p === '…'" class="hl-gap">…</span>
      <button v-else :class="{ 'is-on': p === model }" @click="go(p)">{{ p }}</button>
    </template>
    <button :disabled="model >= pageCount" @click="go(model + 1)">›</button>
  </div>
</template>
