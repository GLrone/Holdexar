<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import HlIcon from './HlIcon.vue'
import { useI18n } from '@/locales'

export interface HlSelectOption {
  label: string
  value: string | number
  /** 左侧图标名（ui/icons.ts 认可清单，可选） */
  icon?: string
  /** 左侧旗帜图 URL（国徽/区旗，可选，优先于 icon） */
  flag?: string
  /** 视觉弱化（置灰提示，如「该地区暂无数据」）；纯视觉标记，不挡选择 */
  dimmed?: boolean
}

const props = withDefaults(
  defineProps<{
    options: HlSelectOption[]
    placeholder?: string
    disabled?: boolean
  }>(),
  // 留空而非写死中文：withDefaults 的默认值只在 defineProps 求值那刻算一次，
  // 会把语言冻结在组件创建时。缺省值在模板里用 t() 现取。
  { placeholder: '', disabled: false },
)

const { t } = useI18n()

const model = defineModel<string | number>({ default: '' })

const open = ref(false)
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLElement | null>(null)
const pos = ref({ top: 0, left: 0, minWidth: 0 })
/** 下方空间不足时向上弹（弹层 max-height 280，走两侧可用空间决定方向） */
const openUp = ref(false)

const current = computed(
  () => props.options.find((o) => o.value === model.value)?.label ?? '',
)

const currentOption = computed(() =>
  props.options.find((o) => o.value === model.value),
)

async function toggle() {
  if (props.disabled) return
  open.value = !open.value
  if (open.value) {
    await nextTick()
    const el = trigger.value?.getBoundingClientRect()
    if (el) {
      const below = window.innerHeight - el.bottom - 6
      const above = el.top - 6
      openUp.value = above > 200 && below < 200
      pos.value = {
        top: openUp.value ? el.top - 6 : el.bottom + 6,
        left: el.left,
        minWidth: el.width,
      }
    }
  }
}

function choose(value: string | number) {
  model.value = value
  open.value = false
}

function onDocClick(e: MouseEvent) {
  if (root.value && !root.value.contains(e.target as Node)) open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))

defineOptions({ name: 'HlSelect' })
</script>

<template>
  <div ref="root" class="hl-pop-host">
    <div
      ref="trigger"
      class="hl-select-wrap"
      :class="{ 'is-open': open }"
      role="combobox"
      :aria-expanded="open"
      @click="toggle"
    >
      <img
        v-if="currentOption?.flag"
        :src="currentOption.flag"
        :alt="currentOption.label"
        style="width: 18px; height: 13px; border-radius: 2px; object-fit: cover; flex-shrink: 0; box-shadow: 0 0 0 1px var(--line-1)"
      />
      <span
        class="hl-select-wrap__val"
        :style="!current ? 'color: var(--text-muted)' : currentOption?.dimmed ? 'color: var(--text-dim)' : ''"
      >
        {{ current || placeholder || t('common.select') }}
      </span>
      <span class="hl-select-wrap__caret"><HlIcon name="chevron-down" /></span>
    </div>
    <Teleport to="body">
      <Transition name="hl-pop">
        <div
          v-if="open"
          class="hl-dropdown-pop"
          :class="{ 'is-open-up': openUp }"
          style="position: fixed"
          :style="{
            top: pos.top + 'px',
            left: pos.left + 'px',
            minWidth: pos.minWidth + 'px',
          }"
        >
          <div
            v-for="opt in options"
            :key="opt.value"
            class="hl-dropdown-item"
            :class="{ 'is-selected': opt.value === model, 'is-dimmed': opt.dimmed }"
            @click="choose(opt.value)"
          >
            <img
              v-if="opt.flag"
              :src="opt.flag"
              :alt="opt.label"
              style="width: 18px; height: 13px; border-radius: 2px; object-fit: cover; flex-shrink: 0"
            />
            <HlIcon v-else-if="opt.icon" :name="opt.icon" />
            {{ opt.label }}
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
