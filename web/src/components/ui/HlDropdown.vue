<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import HlIcon from './HlIcon.vue'

export interface HlDropdownItem {
  label: string
  value?: string
  icon?: string
  danger?: boolean
  divided?: boolean
}

const props = withDefaults(
  defineProps<{
    items: HlDropdownItem[]
    /** 触发方式 */
    trigger?: 'hover' | 'click'
  }>(),
  { trigger: 'hover' },
)

const emit = defineEmits<{ select: [item: HlDropdownItem] }>()

const open = ref(false)
const root = ref<HTMLElement | null>(null)

const isClick = computed(() => props.trigger === 'click')

function onDocClick(e: MouseEvent) {
  if (root.value && !root.value.contains(e.target as Node)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div
    ref="root"
    class="hl-tip-host"
    @mouseenter="!isClick && (open = true)"
    @mouseleave="!isClick && (open = false)"
  >
    <slot />
    <Transition name="hl-pop">
      <div v-if="open" class="hl-dropdown-pop">
        <template v-for="(item, i) in items" :key="i">
          <div v-if="item.divided && i > 0" class="hl-dropdown-divider" />
          <div
            class="hl-dropdown-item"
            :style="item.danger ? 'color: var(--danger)' : ''"
            @click="((open = false), emit('select', item))"
          >
            <HlIcon v-if="item.icon" :name="item.icon" />
            {{ item.label }}
          </div>
        </template>
      </div>
    </Transition>
  </div>
</template>
