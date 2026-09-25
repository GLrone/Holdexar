<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { gamesApi, invalidateGetCache, type GameListItem } from '@/api/client'
import { useCrawlStatusStore } from '@/stores/crawlStatus'
import { useFilterStore } from '@/stores/gamesFilter'
import { useRegionsStore } from '@/stores/regions'
import { usePasteAdd } from '@/composables/usePasteAdd'
import { canGift } from '@/lib/gifting'
import { mergeItemsByAppid } from '@/lib/priceRefresh'
import { vStagger } from '@/lib/stagger'
import { useI18n, useLocaleFormat } from '@/locales'
import HlNavbar from '@/components/business/HlNavbar.vue'
import HlFilterPanel from '@/components/business/HlFilterPanel.vue'
import HlGameCard from '@/components/business/HlGameCard.vue'
import { HlButton, HlEmpty, HlScrollList, HlSpinner } from '@/components/ui'

/**
 * 库视图完整实现：Navbar + 高级筛选 + 卡片网格 +
 * 无限滚动 + 客户端高级过滤（史低/差价/绝对低价/跨区送礼）。
 * 系列区块重组（applySeriesBlocks）依赖 seriesId 数据源，暂为空转。
 */
const store = useFilterStore()
const crawlStatus = useCrawlStatusStore()
const crawl = useCrawlStatusStore()
const regionsStore = useRegionsStore()
const router = useRouter()
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
    hideFamilySharing: store.hideFamilySharing || undefined,
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
    wishlistPriority: store.wishlistPriority || undefined,
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

// ─── 价格周期收敛后的原地刷新 ───
let refreshing = false

/** 重拉已加载区间，按 appid 就地替换条目数据。
 *
 * 触发单位是 Price Refresh Cycle（SSE price_cycle.completed），不是 crawl_job：
 * 一轮里有多个 job，按 job 刷会把一次刷新放大成多次请求。
 *
 * 只换数据不换结构：条目顺序与数量、分页游标、滚动位置、卡片上的弹层与价格
 * 走势抽屉都留在原地——重排列表会把这些交互状态一起卸载掉。重拉失败保留原
 * 数据，不把页面打成错误态：后台刷新失败不该让用户以为库坏了。 */
async function refreshInPlace() {
  const target = items.value.length
  if (!initialized.value || target === 0 || refreshing || isFetchingNext.value) return
  refreshing = true
  const fresh = new Map<number, GameListItem>()
  try {
    let after: string | null = null
    for (;;) {
      const res = await gamesApi.list({ ...baseParams(), limit: PAGE_SIZE, after })
      for (const item of res.items) fresh.set(item.appid, item)
      total.value = res.total
      hasNextPage.value = res.hasMore
      nextCursor.value = res.nextCursor
      after = res.nextCursor
      if (!res.hasMore || fresh.size >= target) break
    }
  } catch {
    return
  } finally {
    refreshing = false
  }
  items.value = mergeItemsByAppid(items.value, fresh)
}

// 价格周期收敛 → 精确失效 games 缓存（不等下一次写操作全量清）→ 原地刷新
watch(
  () => crawl.priceCycle?.cycleId ?? null,
  async (cycleId) => {
    if (cycleId === null) return
    invalidateGetCache('/games')
    await refreshInPlace()
  },
)

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
    store.hideFamilySharing,
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
    store.wishlistPriority,
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

// ─── 空态：区分「库里没有游戏」与「有条件但没匹配」───

/** 服务端全空且无搜索词才算「库空」，有筛选条件时不算（走「没有匹配结果」） */
const isLibraryEmpty = computed(
  () => initialized.value && !isError.value && total.value === 0 && !store.committedSearch,
)

/** 本轮刚添加的款数：>0 时空态改显「正在获取价格」——新导入行要等首轮
 *  取价补全后才出现在列表里，这个间隙必须让用户看到系统接住了 */
const addedPending = ref(0)

// 粘贴添加链（与仪表盘欢迎卡共用）：空库态直接添加，成功后列表自动出现
const { addOpen, addText, addMsg, addBusy, submitAdd } = usePasteAdd({
  onAdded: (added) => {
    addedPending.value = added
  },
  onSettled: () => load(),
})

// 首爬收敛（爬取从跑到停）→ 重取一次：条目补全后空态自然消失
watch(
  () => crawlStatus.running,
  (now, before) => {
    if (before && !now && addedPending.value > 0) {
      addedPending.value = 0
      void load()
    }
  },
)

/** 清除搜索与筛选（「没有匹配结果」的出路）：搜索词 + 全部筛选回默认 */
function clearFilters() {
  store.resetForLeave()
  void load()
}

// ─── 无限滚动 ───
// 两种模式各有一个触发点：
// · 列表模式：列表在 HlScrollList 内部滚动，加载哨兵挂在滚动区末尾（#footer
//   插槽），由容器的 endReached 事件驱动——页面上的哨兵在「列表内滚」时几何
//   位置不变，相交状态只在首屏变一次，是本页只能加载一到两页的旧病根。
// · 网格模式：整页滚动，哨兵留在文档流里，由 .view-container 的 observer 观察。

function handleObserver(entries: IntersectionObserverEntry[]) {
  const [entry] = entries
  if (entry?.isIntersecting && hasNextPage.value && !isFetchingNext.value) {
    load(false)
  }
}

/** 列表模式：滚动区已接近底部（去重交给 hasNextPage / isFetchingNext） */
function onEndReached() {
  if (hasNextPage.value && !isFetchingNext.value) load(false)
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

onBeforeUnmount(() => {
  observer?.disconnect()
  // 先关闸再重置：重置会触发上面的 watch（store 变更），
  // initialized=false 让回调直接跳过，避免卸载瞬间多发一次请求
  initialized.value = false
  store.resetForLeave()
})
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

      <!-- 空状态（挂 data-tour：ProductTour「找游戏」步的聚光锚点——空态
           正是那步要教的场景。不用 data-section：那会被 HlSectionRail 扫成
           分节刻度，找游戏页本无分节轨）
           两态分开：A 库里没有游戏（给添加动作）≠ C 有条件但没匹配（给清除入口）。
           添加只进目录并自动取一次价格，不需要 Steam 账号，也不经过任务页 -->
      <HlEmpty
        v-if="!isLoading && !isError && visibleGames.length === 0 && initialized"
        icon=""
        data-tour="lib-empty"
      >
        <!-- 分支优先级：搜索/筛选无结果（C）优先于「刚添加」（B）——用户已在
             搜索时，找到与否才是他当前的问题；无搜索时 B（刚添加）先于 A（没游戏） -->
        <template v-if="!isLibraryEmpty">
          <h3>{{ t('library.empty.filter.title') }}</h3>
          <p>{{ t('library.empty.filter.hint') }}</p>
          <div class="lib-empty-actions">
            <HlButton size="sm" @click="clearFilters">
              {{ t('library.empty.filter.clear') }}
            </HlButton>
          </div>
        </template>
        <template v-else-if="addedPending > 0">
          <h3>{{ t('library.empty.library.pending') }}</h3>
          <p>{{ t('library.empty.library.pendingHint') }}</p>
        </template>
        <template v-else>
          <h3>{{ t('library.empty.library.title') }}</h3>
          <p>{{ t('library.empty.library.hint') }}</p>
          <div class="lib-empty-actions">
            <HlButton size="sm" variant="primary" @click="addOpen = !addOpen">
              {{ t('library.empty.library.paste') }}
            </HlButton>
            <HlButton size="sm" @click="router.push('/pool?add=1')">
              {{ t('library.empty.library.import') }}
            </HlButton>
          </div>
          <!-- 内联粘贴区：粘贴即入库并自动取价（与仪表盘欢迎卡同一链路） -->
          <div v-if="addOpen" class="lib-empty-add">
            <HlTextarea v-model="addText" :rows="3" :placeholder="t('dashboard.welcome.pasteHint')" />
            <div class="lib-empty-actions" style="margin-top: 8px">
              <HlButton
                size="sm"
                variant="primary"
                :loading="addBusy"
                :disabled="addBusy || !addText.trim()"
                @click="submitAdd"
              >
                {{ addBusy ? t('dashboard.welcome.adding') : t('dashboard.welcome.add') }}
              </HlButton>
              <HlButton variant="text" size="sm" :disabled="addBusy" @click="addOpen = false">
                {{ t('common.cancel') }}
              </HlButton>
              <span v-if="addMsg" class="lib-empty-msg">{{ addMsg }}</span>
            </div>
          </div>
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
        @end-reached="onEndReached"
      >
        <template #item="{ item }">
          <HlGameCard
            :game="item as GameListItem"
            :layout-mode="'list'"
            :enabled-regions="regionsStore.enabledCodes"
            :show-top3="store.top3Check"
          />
        </template>
        <!-- 加载态随列表一起滚（不能挂在页面底部，列表内滚时永远看不到） -->
        <template #footer>
          <div v-if="isFetchingNext || !hasNextPage" class="loading-sentinel">
            <template v-if="isFetchingNext">
              <HlSpinner />
              {{ t('library.loadingMore') }}
            </template>
            <template v-else>{{ t('library.end') }}</template>
          </div>
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
          :show-top3="store.top3Check"
        />
      </div>

      <!-- 无限滚动哨兵（仅网格模式；列表模式的哨兵在 HlScrollList 滚动区末尾） -->
      <div
        v-if="visibleGames.length > 0 && store.layoutMode !== 'list'"
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
/* 空态引导按钮行（去配代理 / 去导入游戏）：HlEmpty slot 内的横向排布 */
.lib-empty-actions {
  display: flex;
  justify-content: center;
  gap: 8px;
  margin-top: 10px;
}
/* 内联粘贴区（空库态的添加动作展开处）与结果提示 */
.lib-empty-add {
  margin-top: 10px;
  width: min(420px, 100%);
  text-align: left;
  margin-inline: auto;
}
.lib-empty-msg {
  font-size: 12px;
  color: var(--text-secondary);
}

/* 加载骨架的仿形卡片（详见模板里「为什么不用 HlSkeleton 的 card 变体」那段）。
   高度**不写行内 style**：真卡列表模式的高度是 `.game-card.list-layout { height: 60px }`，
   行内样式会压过它——这正是此前列表模式下 8 张骨架卡仍是 400px 高的原因。
   写成本文件的类规则，选择器权重低于那条，两种模式各自取到正确的一档。 */
.lib-skel-card { height: 400px; opacity: 0.5; }
/* 列表模式复用真卡的 list-layout 几何（封面 128px 宽通高 + 右侧 5 列网格）。
   真卡右侧那一行是 5 列网格，塞三条占位块比留空更乱，故藏掉。 */
.lib-skel-card.list-layout .info { display: none; }
</style>
