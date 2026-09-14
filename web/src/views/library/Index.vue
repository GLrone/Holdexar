<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { crawlApi, gamesApi, watchPoolApi, type GameListItem } from '@/api/client'
import { useFilterStore } from '@/stores/gamesFilter'
import { useRegionsStore } from '@/stores/regions'
import { canGift } from '@/lib/gifting'
import { vStagger } from '@/lib/stagger'
import { useI18n, useLocaleFormat } from '@/locales'
import HlNavbar from '@/components/business/HlNavbar.vue'
import HlFilterPanel from '@/components/business/HlFilterPanel.vue'
import HlGameCard from '@/components/business/HlGameCard.vue'
import { HlEmpty, HlScrollList, HlSpinner } from '@/components/ui'

/**
 * 库视图完整实现：Navbar + 高级筛选 + 卡片网格 +
 * 无限滚动 + 客户端高级过滤（史低/差价/绝对低价/跨区送礼）。
 * 系列区块重组（applySeriesBlocks）依赖 seriesId 数据源，暂为空转。
 */
const store = useFilterStore()
const regionsStore = useRegionsStore()
// 千分位随界面语言：模板里每渲染一行都会重算，且 `fmt` 内部现读 locale，
// 故切语言即重渲染（详见 locales/format.ts 的说明）
const fmt = useLocaleFormat()
const { t } = useI18n()

const items = ref<GameListItem[]>([])
const total = ref(0)
const hasNextPage = ref(false)
const nextCursor = ref<string | null>(null)
const isLoading = ref(false)
const isFetchingNext = ref(false)
const isError = ref(false)
const errorMsg = ref('')
const initialized = ref(false)

const sentinel = ref<HTMLElement | null>(null)
let observer: IntersectionObserver | null = null
let scrollEl: HTMLElement | null = null

const PAGE_SIZE = 40

// ─── 服务端参数（来自 store）───

function num(v: string): number | undefined {
  return v === '' ? undefined : Number(v)
}

/** 差价区间换算：absolute 元→分；percent 取整百分值（后端 diff 参数均为整数） */
function diffBound(v: string): number | undefined {
  if (v === '') return undefined
  const n = Number(v)
  if (!Number.isFinite(n)) return undefined
  return Math.round(store.diffType === 'percent' ? n : n * 100)
}

function baseParams() {
  return {
    q: store.committedSearch || undefined,
    sort: store.sortBy,
    region: store.region,
    filterMode: store.filterMode,
    onlyDiscounted: store.onlyDiscounted,
    isLowest: store.isLowest,
    onlyHb: store.onlyHb,
    onlyEpic: store.onlyEpic,
    onlyXgp: store.onlyXgp,
    hideOwned: store.hideOwned || undefined,
    minRating: num(store.minRating) ?? 0,
    maxRating: num(store.maxRating),
    minReviews: num(store.minReviews) ?? 0,
    maxReviews: num(store.maxReviews),
    minPrice: num(store.minPrice),
    maxPrice: num(store.maxPrice),
    diffMin: diffBound(store.diffMin),
    diffMax: diffBound(store.diffMax),
    diffType: store.diffType === 'percent' ? 'percent' : undefined,
    strictLowest: store.strictLowest || undefined,
    // 游戏商店默认隐藏 DLC（excludeDlc=true）；白名单豁免个别常驻 DLC
    excludeDlc: store.excludeDlc,
    // 绝对低价勾选时容差随输入（0 → 严格任何分差）；未勾选走后端默认 5 元近似容差
    toleranceFen:
      store.strictLowest && store.tolerance !== ''
        ? Math.max(0, Math.round(Number(store.tolerance || '0') * 100))
        : undefined,
  }
}

async function load(reset: boolean) {
  if (reset) {
    isLoading.value = true
    isError.value = false
  } else {
    isFetchingNext.value = true
  }
  try {
    const res = await gamesApi.list({
      ...baseParams(),
      limit: PAGE_SIZE,
      after: reset ? null : nextCursor.value,
    })
    items.value = reset ? res.items : [...items.value, ...res.items]
    total.value = res.total
    hasNextPage.value = res.hasMore
    nextCursor.value = res.nextCursor
    initialized.value = true
  } catch (e) {
    isError.value = true
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    isLoading.value = false
    isFetchingNext.value = false
  }
}

// 服务端维度变化 → 重新拉取
watch(
  () => [
    store.committedSearch,
    store.sortBy,
    store.region,
    store.filterMode,
    store.onlyDiscounted,
    store.isLowest,
    store.onlyHb,
    store.onlyEpic,
    store.onlyXgp,
    store.hideOwned,
    store.minPrice,
    store.maxPrice,
    store.minRating,
    store.maxRating,
    store.minReviews,
    store.maxReviews,
    store.diffMin,
    store.diffMax,
    store.diffType,
    store.strictLowest,
    store.tolerance,
    store.excludeDlc,
  ],
  () => {
    if (initialized.value) load(true)
  },
)

// ─── 客户端高级过滤（对齐 GameGrid useMemo）───
// 差价区间/绝对低价已服务端化（分页正确性：客户端过滤会丢页内条目），
// 本地仅保留无服务端维度的：史低状态、跨区送礼。

function passesAdvanced(g: GameListItem): boolean {
  // 1. 史低状态（hlFlag 数据源未接入：恒为 0，仅"非史低"可匹配）
  if (store.hlNew || store.hlEqual || store.hlNon) {
    const hlType = g.discount > 0 ? g.hlFlag : 0
    const matchNew = store.hlNew && hlType === 1
    const matchEqual = store.hlEqual && hlType === 2
    const matchNon = store.hlNon && (hlType === 3 || hlType === 0)
    if (!(matchNew || matchEqual || matchNon)) return false
  }

  // 2. 仅显示可跨区送礼：CN 与任一解锁区（新政策下价格倍率只影响实付口径，不限资格）
  if (store.giftFilter) {
    const cnCny = g.basePriceFen
    if (cnCny === null || cnCny <= 0) return false
    const giftable = Object.entries(g.priceMatrix).some(([code, cell]) => {
      if (code === 'CN') return false
      const cnyFen = cell[1]
      if (!cnyFen || cnyFen === -1) return false
      return canGift(cnCny, cnyFen) || canGift(cnyFen, cnCny)
    })
    if (!giftable) return false
  }

  return true
}

const visibleGames = computed(() => items.value.filter(passesAdvanced))

// ─── 空库诊断（total=0 时探测监控池与爬虫，给引导文案）───

const emptyDiag = ref<{
  checked: boolean
  poolCount: number | null
  crawlRunning: boolean | null
}>({ checked: false, poolCount: null, crawlRunning: null })

/** 服务端全空且无搜索词才算「库空」，有筛选条件时不算（走「没有匹配结果」） */
const isLibraryEmpty = computed(
  () => initialized.value && !isError.value && total.value === 0 && !store.committedSearch,
)

watch(isLibraryEmpty, async (empty) => {
  if (!empty || emptyDiag.value.checked) return
  emptyDiag.value.checked = true
  const [pool, active] = await Promise.allSettled([
    watchPoolApi.accounts(),
    crawlApi.active(),
  ])
  if (pool.status === 'fulfilled') {
    emptyDiag.value.poolCount = pool.value.reduce(
      (sum, acc) => sum + (acc.itemCount ?? 0),
      0,
    )
  }
  if (active.status === 'fulfilled') {
    emptyDiag.value.crawlRunning = active.value.activeJobId !== null
  }
})

// ─── 无限滚动 ───

function handleObserver(entries: IntersectionObserverEntry[]) {
  const [entry] = entries
  if (entry?.isIntersecting && hasNextPage.value && !isFetchingNext.value) {
    load(false)
  }
}

function setupObserver() {
  observer?.disconnect()
  if (!sentinel.value) return
  observer = new IntersectionObserver(handleObserver, {
    root: scrollEl,
    rootMargin: '200px',
    threshold: 0,
  })
  observer.observe(sentinel.value)
}

watch(sentinel, () => setupObserver())

onMounted(() => {
  scrollEl = document.querySelector('.view-container')
  load(true)
})

onBeforeUnmount(() => observer?.disconnect())
</script>

<template>
  <div>
    <HlNavbar>
      <template #stats>
        {{ t('library.stats', { total: fmt.group(total), loaded: items.length }) }}
      </template>
    </HlNavbar>

    <!-- 高级筛选面板：外壳常驻（内部 v-if 控制挂载），否则外层 v-if 瞬间卸载会吞掉 HlDrawer 的滑出动画 -->
    <HlFilterPanel />

    <!-- 主内容区 -->
    <div class="container">
      <!-- 错误状态 -->
      <HlEmpty v-if="isError" icon="" style="--pane-pad: 40px 24px">
        <h3>{{ t('library.error.title') }}</h3>
        <p>{{ errorMsg || t('library.error.network') }}</p>
        <button class="sort-btn" @click="load(true)">{{ t('common.retry') }}</button>
      </HlEmpty>

      <!-- 加载骨架屏。**为什么不用 HlSkeleton 的 card 变体**：那个变体是按
           family / toolbox 的 ~140px 小卡做的（封面 78px + 两行字），而本页真实卡片是
           400px 高（`.game-card` 的 contain-intrinsic-size）——套上去骨架比真卡矮一大截，
           数据到达时整页跳版，正是骨架屏要消除的东西。故这里保留手搓的等高仿形，
           只把「等哪一档高」随布局模式走：列表模式真卡只有 60px（`.game-card.list-layout`），
           此前骨架仍固定 400px × 8 行，是实打实的错版。 -->
      <div v-if="isLoading" class="card-grid" :class="{ 'list-grid': store.layoutMode === 'list' }">
        <div
          v-for="i in 8"
          :key="i"
          class="game-card lib-skel-card"
          :class="{ 'list-layout': store.layoutMode === 'list' }"
        >
          <div class="cover-wrapper">
            <div class="skeleton" style="width: 100%; height: 100%; position: absolute; top: 0; left: 0" />
          </div>
          <div class="info">
            <div class="skeleton" style="height: 16px; width: 80%; margin-bottom: 10px" />
            <div class="skeleton" style="height: 12px; width: 50%; margin-bottom: 15px" />
            <div class="skeleton" style="height: 100px; width: 100%" />
          </div>
        </div>
      </div>

      <!-- 空状态 -->
      <HlEmpty v-if="!isLoading && !isError && visibleGames.length === 0 && initialized" icon="">
        <!-- 库本身为空：诊断监控池与爬虫状态，给引导 -->
        <template v-if="isLibraryEmpty">
          <h3>{{ t('library.empty.library.title') }}</h3>
          <template v-if="emptyDiag.poolCount !== null && emptyDiag.poolCount > 0">
            <p>{{ t('library.empty.library.pool', { n: emptyDiag.poolCount }) }}</p>
            <p v-if="emptyDiag.crawlRunning">{{ t('library.empty.library.crawling') }}</p>
            <p v-else>{{ t('library.empty.library.startHint') }}</p>
          </template>
          <template v-else-if="emptyDiag.checked">
            <p>{{ t('library.empty.library.noPool1') }}</p>
            <p>{{ t('library.empty.library.noPool2') }}</p>
          </template>
          <p v-else>{{ t('library.empty.library.checking') }}</p>
        </template>
        <!-- 有筛选/搜索：常规无结果 -->
        <template v-else>
          <h3>{{ t('library.empty.filter.title') }}</h3>
          <p>{{ t('library.empty.filter.hint') }}</p>
        </template>
      </HlEmpty>

      <!-- 游戏卡片网格 -->
      <!-- 列表模式：滚动容器（HlScrollList）。
           条目的网格几何由容器自己的 `.hl-scroll-list` 提供（单列 + 8px 间距），
           此前是往容器上挂 `card-grid list-grid` 再靠 `.animated-list-wrapper .scroll-list`
           补回同一套值——两个来源写同一件事。 -->
      <HlScrollList
        v-if="visibleGames.length > 0 && store.layoutMode === 'list'"
        :items="visibleGames"
        max-height="80vh"
      >
        <template #item="{ item }">
          <HlGameCard
            :game="item as GameListItem"
            :layout-mode="'list'"
            :enabled-regions="regionsStore.enabledCodes"
          />
        </template>
      </HlScrollList>

      <!-- 网格模式：原始布局（hl-stagger：逐卡级联入场，见 hl-framework.css） -->
      <div
        v-if="visibleGames.length > 0 && store.layoutMode !== 'list'"
        v-stagger
        class="card-grid hl-stagger"
      >
        <HlGameCard
          v-for="game in visibleGames"
          :key="game.appid"
          :game="game"
          :layout-mode="store.layoutMode"
          :enabled-regions="regionsStore.enabledCodes"
        />
      </div>

      <!-- 无限滚动哨兵（两种模式共用） -->
      <div
        v-if="visibleGames.length > 0"
        ref="sentinel"
        class="loading-sentinel"
      >
        <template v-if="isFetchingNext">
          <HlSpinner />
          {{ t('library.loadingMore') }}
        </template>
        <template v-else-if="!hasNextPage">{{ t('library.end') }}</template>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 加载骨架的仿形卡片（详见模板里「为什么不用 HlSkeleton 的 card 变体」那段）。
   高度**不写行内 style**：真卡列表模式的高度是 `.game-card.list-layout { height: 60px }`，
   行内样式会压过它——这正是此前列表模式下 8 张骨架卡仍是 400px 高的原因。
   写成本文件的类规则，选择器权重低于那条，两种模式各自取到正确的一档。 */
.lib-skel-card { height: 400px; opacity: 0.5; }
/* 列表模式复用真卡的 list-layout 几何（封面 128px 宽通高 + 右侧 5 列网格）。
   真卡右侧那一行是 5 列网格，塞三条占位块比留空更乱，故藏掉。 */
.lib-skel-card.list-layout .info { display: none; }
</style>
