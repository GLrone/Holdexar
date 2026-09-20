<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useFilterStore } from '@/stores/gamesFilter'
import { flagUrl, type FilterMode, type SortKey } from '@/api/regions'
import { useRegionsStore } from '@/stores/regions'
import { useI18n, type MessageKey } from '@/locales'

/**
 * 库页导航栏组件：
 * 搜索 / 排序下拉 / 地区下拉（含子分类工具栏）/ 布局切换 / 统计 / 高级筛选。
 * 品牌 logo 区按需求移除，搜索框占满空位。
 */
const store = useFilterStore()
const regionsStore = useRegionsStore()
const { t } = useI18n()

const showSortMenu = ref(false)
const showRegionMenu = ref(false)
const mobileNavOpen = ref(false)
const isScrolled = ref(false)
const navbarEl = ref<HTMLElement | null>(null)
const sortRef = ref<HTMLElement | null>(null)
const regionRef = ref<HTMLElement | null>(null)
let scrollEl: HTMLElement | null = null

/* 模块级常量表只存**键**：t() 在模块加载时求值一次会把语言冻死，
   显示文本一律在模板 / computed 里现取。 */
const sortOptions: { key: SortKey; labelKey: MessageKey }[] = [
  { key: 'default', labelKey: 'navbar.sort.default' },
  { key: 'smart', labelKey: 'navbar.sort.smart' },
  { key: 'rate', labelKey: 'navbar.sort.rating' },
  { key: 'diff', labelKey: 'navbar.sort.priceDiff' },
  { key: 'discount', labelKey: 'navbar.sort.discount' },
  { key: 'top100', labelKey: 'navbar.sort.top100' },
  { key: 'new2026', labelKey: 'navbar.sort.new2026' },
]

const currentSortLabel = computed(() => {
  const opt = sortOptions.find((o) => o.key === store.sortBy)
  return t(opt?.labelKey ?? 'navbar.sort.default')
})

const isLockedRegion = computed(() => store.region === 'locked')
const isCNRegion = computed(() => store.region.toLowerCase() === 'cn')
const hasRegionFilter = computed(() => !!store.region && !isLockedRegion.value)
const showFilterToolbar = computed(() => hasRegionFilter.value && !isCNRegion.value)

const regionLabel = computed(() => {
  if (!store.region) return t('navbar.region.allLowest')
  if (isLockedRegion.value) return t('navbar.region.locked')
  return regionsStore.regionName(store.region)
})

/* 地区筛选选项 = 国区 + 启用区（追踪区）+ 特殊项。只列启用区：GPW/低价
   数据本来就只按启用区抓取，未启用区选了必空结果（全量 41 区列出来是误导） */
const regionOptions = computed(() => [
  { code: '', name: t('navbar.regionOption.allLowest'), flag: null as string | null },
  { code: 'cn', name: t('navbar.regionOption.cn'), flag: flagUrl('cn') },
  ...regionsStore.metas
    .filter((c) => c.code !== 'CN' && regionsStore.enabledCodes.includes(c.code.toLowerCase()))
    .map((c) => ({
      code: c.code.toLowerCase(),
      name: c.name,
      flag: flagUrl(c.code),
    })),
  { code: 'locked', name: t('navbar.regionOption.locked'), flag: null as string | null },
])

function handleSelectRegion(code: string) {
  store.region = code
  if (code.toLowerCase() === 'cn') store.filterMode = 'global' as FilterMode
  showRegionMenu.value = false
  // 进地区维度即回顶：吸顶工具栏（z-index 9000）在滚动中途出现会钉在视口上
  // 盖住正下方的卡片行——与选排序回顶同一语义
  scrollEl?.scrollTo({ top: 0, behavior: 'smooth' })
}

function selectSort(key: SortKey) {
  store.sortBy = key
  showSortMenu.value = false
  // 智能排序是默认态（「回到常态推荐」）：选它即回到全区最低视角。
  // 旧算法（default）现为普通选项，不再触发重置。
  if (key === 'smart') {
    store.region = ''
    store.filterMode = 'global'
  }
  scrollEl?.scrollTo({ top: 0, behavior: 'smooth' })
}

function onDocClick(e: MouseEvent) {
  if (sortRef.value && !sortRef.value.contains(e.target as Node)) showSortMenu.value = false
  if (regionRef.value && !regionRef.value.contains(e.target as Node)) showRegionMenu.value = false
}

function onScroll() {
  if (scrollEl) isScrolled.value = scrollEl.scrollTop > 80
}

/** navbar 实际高度 → CSS 变量（filter-toolbar 吸顶基准 / 抽屉 top 对齐用；
    高度随窗口宽度和换行变化，CSS 静态值不可靠）。 */
function syncNavbarHeight() {
  document.documentElement.style.setProperty('--navbar-h', `${navbarEl.value?.offsetHeight ?? 68}px`)
}

onMounted(() => {
  document.addEventListener('mousedown', onDocClick)
  // 页面滚动发生在 App 外壳的 .view-container 内
  scrollEl = document.querySelector('.view-container')
  scrollEl?.addEventListener('scroll', onScroll, { passive: true })
  syncNavbarHeight()
  window.addEventListener('resize', syncNavbarHeight)
})

onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocClick)
  scrollEl?.removeEventListener('scroll', onScroll)
  window.removeEventListener('resize', syncNavbarHeight)
})
</script>

<template>
  <!-- 多根 fragment：navbar/filter-toolbar 直接进父级文档流。
      ⚠️ 勿再加包裹根元素——sticky 元素被等高父级包住时吸附行程归零，
      navbar 会随滚动消失（修过，勿回退） -->
  <nav ref="navbarEl" class="navbar" :class="{ scrolled: isScrolled }">
      <button class="hamburger" @click="mobileNavOpen = !mobileNavOpen">☰</button>

      <div class="nav-controls" :class="{ open: mobileNavOpen }">
        <input
          type="text"
          class="search-box"
          style="flex: 1; min-width: 200px; max-width: none"
          :placeholder="t('navbar.search.placeholder')"
          :value="store.search"
          @input="store.search = ($event.target as HTMLInputElement).value"
          @keydown.enter.prevent="store.commitSearch()"
        />

        <div class="sort-buttons">
          <!-- 排序下拉 -->
          <div ref="sortRef" class="region-dropdown">
            <button
              class="sort-dropdown-btn"
              :class="{ active: store.sortBy !== 'smart' }"
              @click="showSortMenu = !showSortMenu"
            >
              {{ currentSortLabel }} ▼
            </button>
            <div class="region-dropdown-menu" :class="{ show: showSortMenu }">
              <div
                v-for="opt in sortOptions"
                :key="opt.key"
                class="region-option"
                :class="{ active: store.sortBy === opt.key }"
                @click="selectSort(opt.key)"
              >
                <span class="name">{{ t(opt.labelKey) }}</span>
              </div>
            </div>
          </div>

          <!-- 地区低价 -->
          <div ref="regionRef" class="region-dropdown">
            <button
              class="region-dropdown-btn"
              :class="{ active: !!store.region }"
              @click="showRegionMenu = !showRegionMenu"
            >
              📉 {{ regionLabel }} ▼
            </button>
            <div class="region-dropdown-menu" :class="{ show: showRegionMenu }">
              <div
                v-for="opt in regionOptions"
                :key="opt.code || 'all'"
                class="region-option"
                :class="{ active: store.region === opt.code }"
                @click="handleSelectRegion(opt.code)"
              >
                <img v-if="opt.flag" :src="opt.flag" class="flag" alt="" />
                <span class="name">{{ opt.name }}</span>
              </div>
            </div>
          </div>

          <!-- 布局切换 -->
          <div class="layout-switch">
            <button
              class="layout-switch__btn"
              :class="{ active: store.layoutMode === 'grid' }"
              :title="t('navbar.layout.grid')"
              @click="store.layoutMode = 'grid'"
            >
              ⊞
            </button>
            <button
              class="layout-switch__btn"
              :class="{ active: store.layoutMode === 'list' }"
              :title="t('navbar.layout.list')"
              @click="store.layoutMode = 'list'"
            >
              ☰
            </button>
          </div>
        </div>

        <span class="stats"><slot name="stats" /></span>

        <!-- 高级筛选 -->
        <button
          class="filter-main-btn"
          :class="{ active: store.showAdvancedFilter }"
          @click="store.showAdvancedFilter = !store.showAdvancedFilter"
        >
          {{ t('navbar.advancedFilter') }}
          <span v-if="store.activeFilterCount() > 0" class="filter-badge">
            {{ store.activeFilterCount() }}
          </span>
        </button>
      </div>
    </nav>

    <!-- 地区筛选子分类工具栏 -->
    <div v-if="showFilterToolbar" class="filter-toolbar active">
      <span class="filter-toolbar-label">
        <img
          v-if="store.region && !isLockedRegion && !isCNRegion"
          :src="flagUrl(store.region)"
          style="width: 18px; height: 13px"
          alt=""
        />
        {{ regionLabel }}
      </span>
      <button
        v-for="mode in [
          { value: 'global', label: t('navbar.filterMode.global') },
          { value: 'cheaper', label: t('navbar.filterMode.cheaper') },
          { value: 'highdiff', label: t('navbar.filterMode.highDiff') },
        ]"
        :key="mode.value"
        class="filter-mode-btn"
        :class="{ active: store.filterMode === mode.value }"
        @click="store.filterMode = mode.value as FilterMode"
      >
        {{ mode.label }}
      </button>
      <button class="filter-close-btn" @click="store.resetFilters()">
        {{ t('navbar.filter.clear') }}
      </button>
    </div>
  </template>

<style scoped>
.layout-switch {
  display: flex;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  overflow: hidden;
}
</style>

<style scoped>
.layout-switch {
  display: flex;
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  overflow: hidden;
}

.layout-switch__btn {
  border: none;
  background: transparent;
  color: var(--text-muted);
  padding: 6px 10px;
  font-size: 14px;
  cursor: pointer;
  transition: all var(--transition);
}

.layout-switch__btn + .layout-switch__btn {
  border-left: 1px solid var(--border-soft);
}

.layout-switch__btn.active {
  background: var(--accent-fill);
  color: var(--on-accent-fill);
}
</style>
