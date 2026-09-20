<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import HlIcon from './HlIcon.vue'
import { useI18n } from '@/locales'

export interface HlSelectOption {
  label: string
  value: string | number
  /** 左侧图标名（ui/icons.ts 认可清单，可选） */
  icon?: string
  /** 左侧旗帜图 URL（国徽/区旗，可选，优先于 icon） */
  flag?: string
  /** 左侧头像图 URL（圆形象素，介于 flag 与 icon 之间，可选） */
  avatar?: string
  /** 名称后的弱化说明小字（行内等宽，如币种 ISO 代号）；纯展示，不参与搜索匹配 */
  meta?: string
  /** 视觉弱化（置灰提示，如「该地区暂无数据」）；纯视觉标记，不挡选择 */
  dimmed?: boolean
}

const props = withDefaults(
  defineProps<{
    options: HlSelectOption[]
    placeholder?: string
    disabled?: boolean
    /** 多选：触发器显示 chips，点选项切换选中且弹层不收起 */
    multiple?: boolean
    /** 弹层顶部搜索栏：输入实时过滤选项（label / value 包含匹配） */
    searchable?: boolean
    /** 多选触发器最多直显的 chips 数，超出折叠为「+N」 */
    maxTags?: number
  }>(),
  // 留空而非写死中文：withDefaults 的默认值只在 defineProps 求值那刻算一次，
  // 会把语言冻结在组件创建时。缺省值在模板里用 t() 现取。
  { placeholder: '', disabled: false, multiple: false, searchable: false, maxTags: 3 },
)

const { t } = useI18n()

const model = defineModel<string | number | Array<string | number>>({ default: '' })

const open = ref(false)
const root = ref<HTMLElement | null>(null)
const trigger = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)
const searchRef = ref<HTMLInputElement | null>(null)
const query = ref('')
const pos = ref({ top: 0, left: 0, minWidth: 0 })
/** 下方空间不足时向上弹（弹层 max-height 280，走两侧可用空间决定方向） */
const openUp = ref(false)

const current = computed(
  () => props.options.find((o) => o.value === model.value)?.label ?? '',
)

const currentOption = computed(() =>
  props.options.find((o) => o.value === model.value),
)

/** 选中判断：单选比单值，多选比数组包含（model 可能来自未初始化的非数组） */
function isSelected(value: string | number): boolean {
  if (!props.multiple) return model.value === value
  return Array.isArray(model.value) ? model.value.includes(value) : false
}

/** 多选触发器 chips：按 options 顺序取已选项，超出 maxTags 折叠为「+N」 */
const selectedOptions = computed(() =>
  props.multiple ? props.options.filter((o) => isSelected(o.value)) : [],
)
const shownTags = computed(() => selectedOptions.value.slice(0, props.maxTags))
const extraCount = computed(() =>
  Math.max(0, selectedOptions.value.length - shownTags.value.length),
)

/** 搜索过滤：label / value 包含匹配，不分大小写 */
const filteredOptions = computed(() => {
  const q = query.value.trim().toLowerCase()
  if (!q) return props.options
  return props.options.filter(
    (o) => o.label.toLowerCase().includes(q) || String(o.value).toLowerCase().includes(q),
  )
})

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
    if (props.searchable) searchRef.value?.focus()
  }
}

function choose(value: string | number) {
  if (props.multiple) {
    const arr = Array.isArray(model.value) ? [...model.value] : []
    const i = arr.indexOf(value)
    if (i >= 0) arr.splice(i, 1)
    else arr.push(value)
    model.value = arr
    return // 多选保持弹层展开，支持连续勾选
  }
  model.value = value
  open.value = false
}

function onDocClick(e: MouseEvent) {
  const target = e.target as Node
  if (root.value?.contains(target)) return
  // 弹层 teleport 到 body，不在 root 子树内——点弹层内部（搜索栏/选项）不收起
  if (panel.value?.contains(target)) return
  open.value = false
}

/* 收起即清搜索词，下次展开不残留上一次的过滤 */
watch(open, (v) => {
  if (!v) query.value = ''
})

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))

defineOptions({ name: 'HlSelect' })
</script>

<template>
  <div ref="root" class="hl-pop-host">
    <div
      ref="trigger"
      class="hl-select-wrap"
      :class="{ 'is-open': open, 'hl-select-wrap--multi': multiple }"
      role="combobox"
      :aria-expanded="open"
      @click="toggle"
    >
      <!-- 多选：已选项 chips（超出 maxTags 折叠），未选时退化为占位文案 -->
      <template v-if="multiple">
        <span v-for="opt in shownTags" :key="opt.value" class="hl-select-tag">
          <img
            v-if="opt.flag"
            :src="opt.flag"
            :alt="opt.label"
          />
          <span class="hl-select-tag__label">{{ opt.label }}</span>
        </span>
        <span v-if="extraCount > 0" class="hl-select-tag hl-select-tag--more">+{{ extraCount }}</span>
        <span v-if="!selectedOptions.length" class="hl-select-wrap__val hl-select-wrap__val--empty">
          {{ placeholder || t('common.select') }}
        </span>
      </template>
      <template v-else>
        <img
          v-if="currentOption?.flag"
          :src="currentOption.flag"
          :alt="currentOption.label"
          style="width: 18px; height: 13px; border-radius: 2px; object-fit: cover; flex-shrink: 0; box-shadow: 0 0 0 1px var(--line-1)"
        />
        <img
          v-else-if="currentOption?.avatar"
          :src="currentOption.avatar"
          :alt="currentOption.label"
          style="width: 20px; height: 20px; border-radius: 50%; object-fit: cover; flex-shrink: 0; box-shadow: 0 0 0 1px var(--line-1)"
        />
        <span
          class="hl-select-wrap__val"
          :style="!current ? 'color: var(--text-muted)' : currentOption?.dimmed ? 'color: var(--text-dim)' : ''"
        >
          {{ current || placeholder || t('common.select') }}
        </span>
      </template>
      <span class="hl-select-wrap__caret"><HlIcon name="chevron-down" /></span>
    </div>
    <Teleport to="body">
      <Transition name="hl-pop">
        <div
          v-if="open"
          ref="panel"
          class="hl-dropdown-pop"
          :class="{ 'is-open-up': openUp }"
          style="position: fixed"
          data-hl-select-pop
          :style="{
            top: pos.top + 'px',
            left: pos.left + 'px',
            minWidth: pos.minWidth + 'px',
          }"
        >
          <!-- 搜索栏：sticky 钉在滚动面板顶部，选项多时滚动不带走输入框 -->
          <div v-if="searchable" class="hl-select-search">
            <HlIcon name="search" />
            <input
              ref="searchRef"
              v-model="query"
              class="hl-select-search__input"
              :placeholder="t('common.search')"
              @keydown.esc.stop.prevent="query = ''"
            />
          </div>
          <div
            v-for="opt in filteredOptions"
            :key="opt.value"
            class="hl-dropdown-item"
            :class="{ 'is-selected': isSelected(opt.value), 'is-dimmed': opt.dimmed }"
            @click="choose(opt.value)"
          >
            <img
              v-if="opt.flag"
              :src="opt.flag"
              :alt="opt.label"
              style="width: 18px; height: 13px; border-radius: 2px; object-fit: cover; flex-shrink: 0"
            />
            <img
              v-else-if="opt.avatar"
              :src="opt.avatar"
              :alt="opt.label"
              style="width: 20px; height: 20px; border-radius: 50%; object-fit: cover; flex-shrink: 0; box-shadow: 0 0 0 1px var(--line-1)"
            />
            <HlIcon v-else-if="opt.icon" :name="opt.icon" />
            {{ opt.label }}
            <span v-if="opt.meta" class="hl-dropdown-item__meta">{{ opt.meta }}</span>
            <HlIcon v-if="multiple && isSelected(opt.value)" name="check" class="hl-dropdown-item__check" />
          </div>
          <div v-if="!filteredOptions.length" class="hl-dropdown-empty">{{ t('common.noMatch') }}</div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
