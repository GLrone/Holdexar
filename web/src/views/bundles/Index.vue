<script setup lang="ts">
/**
 * 捆绑包浏览视图。
 *
 * 功能范围：捆绑包卡片网格（折扣角标/撞库状态徽章/MPS·基础折扣标签/
 * 国区价/前三低价区/差价）、全区价格明细（锁区提示 + 部分锁区高亮）、
 * 补齐状态栏、包内游戏列表（归属着色 + 查看全部）、地区 AppID 差异
 * 分组（AGR）、赠礼地区分析、补齐计算器（自动排除 + 手动排除 +
 * 整包基础折扣）。
 *
 * 单品游戏联动（游戏卡 → 关联捆绑包 GPW 区块）不在此视图，已另有实现。
 *
 * 载体差异：PC 悬浮 popover / 移动端内联展开统一为非模态
 * HlDrawer（底层可交互，符合收口红线）；三级弹窗统一 HlDialog。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { bundlesApi, ownershipApi, type BundleDetail, type BundleGame, type BundleSummary, type OwnershipInfo } from '@/api/client'
import { regionSelectOptions } from '@/api/selectOptions'
import { HlButton, HlCheckbox, HlDialog, HlDrawer, HlSelect, HlSkeleton } from '@/components/ui'
import RegionFlag from '@/components/RegionFlag.vue'
import {
  autoExcludedAppIds,
  computeCompletion,
  formatForeignMinor,
  inferBundleOwnership,
  orderedRegionCodes,
} from '@/lib/bundleCalc'
import {
  ASSET_RETRY_MAX,
  assetRetryUrl,
  isAssetDead,
  markAssetDead,
  markAssetOk,
  resolveAssetUrl,
} from '@/lib/assetCache'
import { computeGiftingAnalysis, type GiftingAnalysis, type RegionPriceInfo } from '@/lib/gifting'
import { vStagger } from '@/lib/stagger'
import { useI18n, type MessageKey } from '@/locales'
import { useRegionsStore } from '@/stores/regions'
import { APP_SLUG } from '@/appInfo'

const regionsStore = useRegionsStore()
const { t } = useI18n()

// ─── 列表 ───
const bundles = ref<BundleSummary[]>([])
const loading = ref(true)
const errorMsg = ref('')
/** bundleId → 撞库推演结果 */
const ownershipMap = ref<Record<number, OwnershipInfo>>({})
const ownershipByBid = ref<Record<number, ReturnType<typeof inferBundleOwnership>>>({})

// ─── 导航栏（对齐游戏商店形态：搜索 + 排序下拉 + 地区维度 + 高级筛选 + 统计）───
// 排序走服务端预计算列（smart=智能评分 / diff=差价）；搜索按包名前端过滤。
// 无手动刷新键：路由切走再回来 onMounted 必然重新拉取，常驻刷新键无意义。
type BundleSortKey = 'smart' | 'diff' | 'discount'
const sortBy = ref<BundleSortKey>(
  localStorage.getItem(`${APP_SLUG}.bundles.sort`) === 'diff' ? 'diff' : 'smart',
)
const showSortMenu = ref(false)
const sortRef = ref<HTMLElement | null>(null)
/* 常量只存词条 key：t() 在模块求值时算一次会把语言冻死，模板里现取 */
const sortOptions: { key: BundleSortKey; labelKey: MessageKey }[] = [
  { key: 'smart', labelKey: 'navbar.sort.smart' },
  { key: 'diff', labelKey: 'navbar.sort.priceDiff' },
  { key: 'discount', labelKey: 'navbar.sort.discount' },
]
const currentSortLabel = computed(
  () => t(sortOptions.find((o) => o.key === sortBy.value)?.labelKey ?? 'navbar.sort.smart'),
)

const searchInput = ref('')
const committedSearch = ref('')

// ─── 地区维度：选区后差价重新锚定到该区（国区价 − 该区 cnyFen）。
// 列表载荷自带全区 cnyFen，锚定换算/过滤在前端做，无需新快照列；
// smart 评分保持服务端序——评分是「国区对全区最低」视角，与所选地区无关，
// 与 games 商店「smart 不挂地区前缀」同一取舍。
// 地区维度是**视图态不持久化**：切走再回来一律回默认（全区最低/全部）。
type RegionMode = 'all' | 'cheaper' | 'locked'
const region = ref('')
const regionMode = ref<RegionMode>('all')
/* 选项走 selectOptions 统一出口（旗帜由出口挂载，视图层不直连 flagUrl） */
const regionOptions = computed(() => [
  { value: '', label: t('navbar.regionOption.allLowest'), flag: undefined as string | undefined },
  ...regionSelectOptions(regionsStore.enabledCodes.map((c) => c.toUpperCase())),
])
const activeRegionMeta = computed(() => regionOptions.value.find((o) => o.value === region.value))
const regionLabel = computed(() => activeRegionMeta.value?.label ?? t('navbar.regionOption.allLowest'))
const showRegionMenu = ref(false)
const regionRef = ref<HTMLElement | null>(null)
const regionModes: { key: RegionMode; labelKey: MessageKey }[] = [
  { key: 'all', labelKey: 'bundles.regionMode.all' },
  { key: 'cheaper', labelKey: 'bundles.regionMode.cheaper' },
  { key: 'locked', labelKey: 'bundles.regionMode.locked' },
]

function selectRegion(code: string) {
  showRegionMenu.value = false
  if (code === region.value) return
  region.value = code
  renderLimit.value = RENDER_STEP
  // 进地区维度即回顶：工具栏是吸顶层（z-index 9000），滚动中途选区会把它
  // 钉在当前视口上、盖住正下方的卡片行（与游戏商店选排序回顶同一语义）
  ;(document.querySelector('.view-container') as HTMLElement | null)?.scrollTo({
    top: 0,
    behavior: 'smooth',
  })
}

function selectRegionMode(mode: RegionMode) {
  regionMode.value = mode
  renderLimit.value = RENDER_STEP
}

// ─── 高级筛选（复刻可实现的维度）：可补齐 / 国区最低 / 隐藏已拥有 /
// 家庭共享 / 国区价格区间 / 差价下限（选区时锚定该区差价）。持久化 localStorage。 ───
interface BundleFilters {
  completableOnly: boolean
  cnLowestOnly: boolean
  hideOwned: boolean
  hideFamily: boolean
  priceMin: string
  priceMax: string
  diffMin: string
}
const DEFAULT_FILTERS: BundleFilters = {
  completableOnly: false,
  cnLowestOnly: false,
  hideOwned: false,
  hideFamily: false,
  priceMin: '',
  priceMax: '',
  diffMin: '',
}
function loadFilters(): BundleFilters {
  try {
    return { ...DEFAULT_FILTERS, ...JSON.parse(localStorage.getItem(`${APP_SLUG}.bundles.filters`) ?? '{}') }
  } catch {
    return { ...DEFAULT_FILTERS }
  }
}
const filters = ref<BundleFilters>(loadFilters())
const filterOpen = ref(false)
watch(
  filters,
  (v) => localStorage.setItem(`${APP_SLUG}.bundles.filters`, JSON.stringify(v)),
  { deep: true },
)
const activeFilterCount = computed(() => {
  const f = filters.value
  const numeric = (s: string) => s !== '' && Number.isFinite(Number(s))
  return (
    (f.completableOnly ? 1 : 0) +
    (f.cnLowestOnly ? 1 : 0) +
    (f.hideOwned ? 1 : 0) +
    (f.hideFamily ? 1 : 0) +
    (numeric(f.priceMin) ? 1 : 0) +
    (numeric(f.priceMax) ? 1 : 0) +
    (numeric(f.diffMin) ? 1 : 0)
  )
})
function setFilter<K extends keyof BundleFilters>(key: K, value: BundleFilters[K]) {
  filters.value = { ...filters.value, [key]: value }
  renderLimit.value = RENDER_STEP
}
function resetFilters() {
  filters.value = { ...DEFAULT_FILTERS }
  renderLimit.value = RENDER_STEP
}

// ─── 分批渲染：3,386 张卡片一次性挂载是页面打开慢的另一主因（数据接口
// 已有服务端缓存），先渲 RENDER_STEP 张、点按钮续批。筛选/排序仍在全量
// bundles 上做（纯 JS，微秒级），只有 DOM 挂载分批。───
const RENDER_STEP = 120
const renderLimit = ref(RENDER_STEP)
/** 展示管道：地区锚定换算 → 地区模式过滤 → 高级筛选 → 选区 diff 排序时
   按该区差价前端重排 → 搜索过滤 → 分批渲染切片 */
const enrichedBundles = computed(() =>
  bundles.value.map((b) => {
    let regionDiff: number | null = null
    let regionLocked = false
    const code = region.value.toUpperCase()
    if (code) {
      const rp = b.regionPrices[code]
      if (!rp || rp.cnyFen == null) regionLocked = true
      else if (b.cnCnyFen != null) regionDiff = b.cnCnyFen - rp.cnyFen
    }
    return { b, regionDiff, regionLocked }
  }),
)
const filteredBundles = computed(() => {
  let rows = enrichedBundles.value
  const code = region.value.toUpperCase()
  if (code && regionMode.value === 'cheaper') {
    rows = rows.filter((r) => r.regionDiff != null && r.regionDiff > 0)
  } else if (code && regionMode.value === 'locked') {
    rows = rows.filter((r) => r.regionLocked)
  }
  const f = filters.value
  if (f.completableOnly) rows = rows.filter((r) => r.b.mustPurchaseAsSet === 0)
  if (f.cnLowestOnly) rows = rows.filter((r) => r.b.lowestRegion === 'cn')
  const own = ownershipByBid.value
  if (f.hideOwned) rows = rows.filter((r) => own[r.b.bundleId]?.type !== 'owned')
  if (f.hideFamily) rows = rows.filter((r) => own[r.b.bundleId]?.type !== 'family')
  const numeric = (s: string) => (s !== '' && Number.isFinite(Number(s)) ? Number(s) : null)
  const pMin = numeric(f.priceMin)
  const pMax = numeric(f.priceMax)
  const dMin = numeric(f.diffMin)
  if (pMin != null) rows = rows.filter((r) => r.b.cnCnyFen != null && r.b.cnCnyFen >= pMin * 100)
  if (pMax != null) rows = rows.filter((r) => r.b.cnCnyFen != null && r.b.cnCnyFen <= pMax * 100)
  if (dMin != null) {
    rows = rows.filter((r) => {
      const d = code && r.regionDiff != null ? r.regionDiff : r.b.diffFen
      return d >= dMin * 100
    })
  }
  if (sortBy.value === 'discount') {
    // 折扣力度：现折扣% 降序（选区=该区折扣，未选区=全区最大），同折扣按差价
    rows = [...rows].sort(
      (a, b) => discountOf(b.b) - discountOf(a.b) || b.b.diffFen - a.b.diffFen,
    )
  } else if (code && sortBy.value === 'diff') {
    rows = [...rows].sort((a, b) => (b.regionDiff ?? -Infinity) - (a.regionDiff ?? -Infinity))
  } else if (code && sortBy.value === 'smart') {
    // 选区重锚：省钱因子按该区差价重算，其余三因子不变（真·因子驱动重排；
    // 未选区保持服务端评分序——同一公式，二者等价）
    rows = [...rows].sort(
      (a, b) => anchoredSmart(b.b, b.regionDiff) - anchoredSmart(a.b, a.regionDiff),
    )
  }
  const q = committedSearch.value
  if (q) rows = rows.filter((r) => r.b.name.toLowerCase().includes(q))
  return rows.map((r) => r.b)
})
const visibleBundles = computed(() => filteredBundles.value.slice(0, renderLimit.value))
const completableCount = computed(
  () => filteredBundles.value.filter((b) => b.mustPurchaseAsSet === 0).length,
)

function commitSearch() {
  committedSearch.value = searchInput.value.trim().toLowerCase()
  renderLimit.value = RENDER_STEP
}

// ─── 排序因子（与 games/scoring 同式，前端可算的都在这）───
/** 省钱因子（games/scoring.save_score 同式）：diffFen 分 → 元，对数压缩 ¥200 封顶 */
function saveScore(diffFen: number): number {
  const yuan = Math.max(0, diffFen) / 100
  return Math.min(1, Math.log1p(yuan / 20) / Math.log(11))
}
/** smart 评分的地区无关余量 = 评分 − save(快照差价)，即质量+时机+熟悉度三因子和 */
function smartRest(b: BundleSummary): number {
  return Math.max(0, (b.smartScore || 0) - saveScore(b.diffFen))
}
/** 选区视角的 smart 评分：省钱因子按该区差价重算，其余三因子与地区无关 */
function anchoredSmart(b: BundleSummary, regionDiff: number | null): number {
  return saveScore(regionDiff ?? 0) + smartRest(b)
}
/** 折扣力度因子：选区 = 该区现折扣%；未选区 = 各区最大现折扣 */
function discountOf(b: BundleSummary): number {
  const code = region.value.toUpperCase()
  if (!code) return maxDiscount(b)
  return b.regionPrices[code]?.discountPercent || 0
}

// ─── 布局模式（对齐游戏商店：网格 ⊞ / 列表 ☰，持久化）───
// 列表行 = 封面 128×60 + 名称标签 + 国区价 / 最低区 / 差价单行铺开，
// 复用网格卡的全部展示助手（fen / cheaperTop3 / diffOf / maxDiscount）。
type BundleLayout = 'grid' | 'list'
const layoutMode = ref<BundleLayout>(
  localStorage.getItem(`${APP_SLUG}.bundles.layout`) === 'list' ? 'list' : 'grid',
)
function setLayout(mode: BundleLayout) {
  layoutMode.value = mode
  localStorage.setItem(`${APP_SLUG}.bundles.layout`, mode)
}

/** 卡片差价角标：未选区 = 服务端快照 diffFen（对全区最低）；选区 = 该区相对
    国区的差价（与地区维度的过滤/排序同一锚点，展示与顺序不脱节） */
function diffOf(b: BundleSummary): number {
  const code = region.value.toUpperCase()
  if (!code) return b.diffFen
  const rp = b.regionPrices[code]
  if (!rp || rp.cnyFen == null || b.cnCnyFen == null) return 0
  return Math.max(b.cnCnyFen - rp.cnyFen, 0)
}

async function selectSort(key: BundleSortKey) {
  showSortMenu.value = false
  if (key === sortBy.value) return
  sortBy.value = key
  localStorage.setItem(`${APP_SLUG}.bundles.sort`, key)
  renderLimit.value = RENDER_STEP
  await load()
}

function onDocClick(e: MouseEvent) {
  if (sortRef.value && !sortRef.value.contains(e.target as Node)) showSortMenu.value = false
  if (regionRef.value && !regionRef.value.contains(e.target as Node)) showRegionMenu.value = false
}

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await bundlesApi.list(sortBy.value)
    bundles.value = res.bundles
    renderLimit.value = RENDER_STEP
    // 撞库推演：收集全部 appid 一次批量查归属
    const allIds = [...new Set(bundles.value.flatMap((b) => b.appIds))]
    if (allIds.length > 0) {
      try {
        const own = await ownershipApi.batch(allIds.slice(0, 200))
        ownershipMap.value = Object.fromEntries(
          Object.entries(own.ownerships).map(([k, v]) => [Number(k), v]),
        )
      } catch {
        ownershipMap.value = {}
      }
    }
    refreshOwnershipBadges()
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function refreshOwnershipBadges() {
  const next: Record<number, ReturnType<typeof inferBundleOwnership>> = {}
  for (const b of bundles.value) {
    next[b.bundleId] = inferBundleOwnership(b.appIds, null, ownershipMap.value)
  }
  ownershipByBid.value = next
}

/** 差价降幅角标：各区折扣最大值 */
function maxDiscount(b: BundleSummary): number {
  return Math.max(0, ...Object.values(b.regionPrices).map((r) => r.discountPercent || 0))
}

/** 比国区便宜的前三区（升序）；国区自己最低时走"国区最低"行 */
const cheaperTop3 = (b: BundleSummary) => {
  const cn = b.cnCnyFen
  if (cn == null) return []
  return Object.entries(b.regionPrices)
    .filter(([code, rp]) => code !== 'CN' && rp.cnyFen != null && rp.cnyFen < cn)
    .sort((a, c) => (a[1].cnyFen ?? 0) - (c[1].cnyFen ?? 0))
    .slice(0, 3)
}

/** 全区最低（含国区）——"国区最低"兜底行的价格（对齐游戏卡 lowestPriceFen） */
function lowestFen(b: BundleSummary): number | null {
  const vals = Object.values(b.regionPrices)
    .map((rp) => rp.cnyFen)
    .filter((v): v is number => v != null)
  return vals.length > 0 ? Math.min(...vals) : null
}

/** 前三名奖牌（对齐游戏卡 TROPHIES：金银铜） */
const TROPHIES = ['/assets/trophy_gold.png', '/assets/trophy_silver.png', '/assets/trophy_copper.png']

// ─── 封面加载：失败随机延时重试（对齐游戏卡标准规则）───
// 最多 4 次，每次 0.8–3.2s 随机退避。与游戏卡同一套登记（assetCache）：重试 URL
// 稳定、不掺时间戳，「已放弃」跨重挂载记忆——否则下架包的封面会在每次切筛选时
// 重跑 5 次注定 404 的往返。
const coverSrc = ref<Record<number, string>>({})
const coverRetries = ref<Record<number, number>>({})
const coverTimers = new Map<number, number>()

/** 本轮已重试过的 URL（覆盖登记处的「裸 URL」结论）；未重试过则问登记处 */
function bundleCover(b: BundleSummary): string {
  return coverSrc.value[b.bundleId] ?? resolveAssetUrl(b.headerImage)
}

/** 已知失效且本轮未拿到可用 URL → 不渲染 img（占位 📦 常驻底层），一次网络都不发 */
function bundleDead(b: BundleSummary): boolean {
  return coverSrc.value[b.bundleId] === undefined && isAssetDead(b.headerImage)
}

function onCoverError(e: Event, b: BundleSummary) {
  const retries = coverRetries.value[b.bundleId] ?? 0
  if (retries >= ASSET_RETRY_MAX) {
    markAssetDead(b.headerImage)
    ;(e.target as HTMLImageElement).style.display = 'none'
    return
  }
  const next = retries + 1
  coverRetries.value = { ...coverRetries.value, [b.bundleId]: next }
  const delay = 800 + Math.random() * 2400
  const timer = window.setTimeout(() => {
    coverSrc.value = { ...coverSrc.value, [b.bundleId]: assetRetryUrl(b.headerImage, next) }
    coverTimers.delete(b.bundleId)
  }, delay)
  coverTimers.set(b.bundleId, timer)
}

function onCoverLoad(b: BundleSummary) {
  markAssetOk(b.headerImage, bundleCover(b))
}

const fen = (v: number | null | undefined, digits = 2) =>
  v == null ? '—' : `¥${(v / 100).toFixed(digits)}`

// ─── 详情抽屉（全区价格 / 补齐状态 / 游戏列表 / AGR / 赠礼） ───
const drawerOpen = ref(false)
/** 抽屉最高处须低于吸顶 navbar（高度随布局 68/50px 变化），打开时以量得值为准 */
const stackTop = ref(140)
const drawerBundle = ref<BundleSummary | null>(null)
const detail = ref<BundleDetail | null>(null)
const detailLoading = ref(false)
/** 详情加载失败原因（空串 = 正常；失败时抽屉内行内提示） */
const detailError = ref('')
const detailCache = new Map<number, BundleDetail>()

watch(drawerOpen, (open) => {
  if (!open) return
  let bottom = 0
  for (const sel of ['.app-header', '.navbar', '.filter-toolbar']) {
    const el = document.querySelector(sel)
    if (el) bottom = Math.max(bottom, el.getBoundingClientRect().bottom)
  }
  stackTop.value = Math.round(bottom + 6)
})

/** 展示顺序：CN 第一，其余按服务端区服表顺序 */
const drawerCodes = computed(() =>
  drawerBundle.value
    ? orderedRegionCodes(Object.keys(drawerBundle.value.regionPrices), regionsStore.metas)
    : [],
)

const hasPartialLock = computed(
  () =>
    !!drawerBundle.value &&
    drawerCodes.value.some((c) => (drawerBundle.value!.regionPrices[c]?.lockedCount ?? 0) > 0),
)

/** 该区有可展示的价格（行存在但全空 = 锁区） */
function hasPrice(code: string): boolean {
  const rp = drawerBundle.value?.regionPrices[code]
  return !!rp && (rp.cnyFen != null || rp.priceMinor != null)
}

/** 补齐状态文案（owned / family / 可补齐 / 未知四态）。
 *  t() 在 computed 里现取，切语言即重算。 */
const statusBar = computed(() => {
  const b = drawerBundle.value
  if (!b) return { cls: '', text: '' }
  const own = ownershipByBid.value[b.bundleId]
  if (own?.type === 'owned') return { cls: '', text: t('bundles.status.owned') }
  if (own?.type === 'family') return { cls: '', text: t('bundles.status.family') }
  if (b.mustPurchaseAsSet === 0) return { cls: 'completable', text: t('bundles.mps.completable') }
  return { cls: '', text: t('bundles.mps.unknown') }
})

async function openDrawer(b: BundleSummary) {
  drawerBundle.value = b
  giftTarget.value = null
  drawerOpen.value = true
  detailError.value = ''
  if (detailCache.has(b.bundleId)) {
    detail.value = detailCache.get(b.bundleId)!
    return
  }
  detailLoading.value = true
  try {
    const res = await bundlesApi.detail(b.bundleId)
    detail.value = res
    detailCache.set(b.bundleId, res)
  } catch (e) {
    detail.value = null
    detailError.value = e instanceof Error ? e.message : String(e)
  } finally {
    detailLoading.value = false
  }
}

/** 再点同包收回（抽屉开合开关语义）。
 *  封面命中徽标（appid-badge，SteamDB 外链）时忽略——外链点击不开抽屉。 */
function toggleDrawer(b: BundleSummary, e?: Event) {
  if (e && (e.target as HTMLElement).closest?.('.appid-badge')) return
  if (drawerOpen.value && drawerBundle.value?.bundleId === b.bundleId) {
    drawerOpen.value = false
    return
  }
  openDrawer(b)
}

/** 外链跳转（UI 按钮不能包 <a>，用真实锚点触发导航保持外链语义） */
function openExternal(url: string) {
  const a = document.createElement("a")
  a.href = url
  a.target = "_blank"
  a.rel = "noopener noreferrer"
  a.click()
}

/** 包内游戏归属 class（owned/family/wishlist 着色，bgc-* 前缀） */
function gameStatusClass(game: BundleGame): string {
  const info = ownershipMap.value[game.appid]
  if (info?.type === 'owned') return 'bgc-owned'
  if (info?.type === 'family') return 'bgc-family'
  if (info?.type === 'wishlist') return 'bgc-wishlist'
  return ''
}

/** 游戏名 + 归属徽标（模板里现取，故 t() 进来即可；含前导空格的词条见词典）。 */
function gameTitle(game: BundleGame): string {
  const info = ownershipMap.value[game.appid]
  const name = game.name ?? `AppID: ${game.appid}`
  const extra =
    info?.type === 'owned'
      ? t('bundles.gameTag.owned')
      : info?.type === 'family'
        ? t('bundles.gameTag.family', { owners: info.owners?.join(', ') ?? '' })
        : info?.type === 'wishlist'
          ? t('bundles.gameTag.wishlist', { owners: info.owners?.join(', ') ?? '' })
          : ''
  return name + extra
}

const drawerGames = computed(() => (detail.value?.games ?? []).slice(0, 4))

/** 地区 AppID 差异分组（各区 appids 签名分组，>1 组才展示） */
const agrGroups = computed(() => {
  const b = drawerBundle.value
  if (!b) return []
  const groups: Record<string, { codes: string[]; aids: number[] }> = {}
  for (const [code, rp] of Object.entries(b.regionPrices)) {
    const key = [...rp.appIds].sort((x, y) => x - y).join(',')
    if (!groups[key]) groups[key] = { codes: [], aids: rp.appIds }
    groups[key].codes.push(code)
  }
  return Object.values(groups).sort((a, c) => c.aids.length - a.aids.length)
})

// ─── 赠礼地区分析（复用 gifting.ts 引擎） ───
const giftTarget = ref<string | null>(null)

const giftAnalysis = computed<GiftingAnalysis | null>(() => {
  const b = drawerBundle.value
  if (!b || !giftTarget.value) return null
  const regionPrices: RegionPriceInfo[] = drawerCodes.value
    .filter((code) => code !== giftTarget.value)
    .map((code) => {
      const rp = b.regionPrices[code]
      const locked = !rp || rp.cnyFen == null || rp.cnyFen <= 0
      return {
        code: code.toLowerCase(),
        nameZh: regionsStore.regionName(code),
        cnyFen: locked ? 0 : rp!.cnyFen!,
        locked,
      }
    })
  const targetRp = b.regionPrices[giftTarget.value]
  if (!targetRp || targetRp.cnyFen == null || targetRp.cnyFen <= 0) return null
  regionPrices.unshift({
    code: giftTarget.value.toLowerCase(),
    nameZh: regionsStore.regionName(giftTarget.value),
    cnyFen: targetRp.cnyFen,
    locked: false,
  })
  return computeGiftingAnalysis(regionPrices, giftTarget.value.toLowerCase())
})

function toggleGift(code: string) {
  giftTarget.value = giftTarget.value === code ? null : code
}

// ─── 补齐计算器 ───
const calcOpen = ref(false)
const calcBundle = ref<BundleSummary | null>(null)
const calcDetail = ref<BundleDetail | null>(null)
const calcRegion = ref('')
const calcExcluded = ref<Set<number>>(new Set())
const calcResult = ref<ReturnType<typeof computeCompletion> | null>(null)

const calcRegionOptions = computed(() =>
  calcBundle.value
    ? regionSelectOptions(orderedRegionCodes(Object.keys(calcBundle.value.regionPrices), regionsStore.metas))
    : [],
)

const calcGames = computed(() => {
  const b = calcBundle.value
  const d = calcDetail.value
  if (!b || !d || !calcRegion.value) return []
  const rp = b.regionPrices[calcRegion.value.toUpperCase()]
  if (!rp) return []
  const aids = new Set(rp.appIds.map(Number))
  return d.games.filter((g) => aids.has(g.appid))
})

function openCalculator(b: BundleSummary) {
  calcBundle.value = b
  calcResult.value = null
  const codes = orderedRegionCodes(Object.keys(b.regionPrices), regionsStore.metas)
  calcRegion.value = codes[0]?.toLowerCase() ?? ''
  // 详情缓存复用抽屉拉取结果；未拉过时静默补拉
  calcDetail.value = detailCache.get(b.bundleId) ?? null
  if (!calcDetail.value) {
    bundlesApi
      .detail(b.bundleId)
      .then((res) => {
        calcDetail.value = res
        detailCache.set(b.bundleId, res)
      })
      .catch(() => {
        calcDetail.value = null
      })
  }
  calcOpen.value = true
  // 计算器数据到手后执行自动排除（无数据 + 已拥有）
  const applyAuto = (d: BundleDetail | null) => {
    if (!d) return
    calcExcluded.value = autoExcludedAppIds(d, ownershipMap.value)
  }
  if (calcDetail.value) applyAuto(calcDetail.value)
  else {
    const timer = window.setInterval(() => {
      if (calcDetail.value) {
        applyAuto(calcDetail.value)
        window.clearInterval(timer)
      }
    }, 300)
    window.setTimeout(() => window.clearInterval(timer), 5000)
  }
}

function toggleExclude(appid: number) {
  const next = new Set(calcExcluded.value)
  if (next.has(appid)) next.delete(appid)
  else next.add(appid)
  calcExcluded.value = next
  calcResult.value = null
}

// 切区清空结果
watch(calcRegion, () => {
  calcResult.value = null
})

function runCalculate() {
  const b = calcBundle.value
  if (!b || !calcRegion.value) return
  calcResult.value = computeCompletion(
    calcDetail.value ?? { ...b, games: [] },
    calcRegion.value,
    calcExcluded.value,
  )
}

const calcForeignText = computed(() => {
  const r = calcResult.value
  const b = calcBundle.value
  if (!r || !b) return '-'
  if (!r.official && r.validCount === 0) return t('bundles.calc.noValidItems')
  const rp = b.regionPrices[calcRegion.value.toUpperCase()]
  return formatForeignMinor(r.foreignMinor, rp?.currency ?? null) || t('bundles.calc.noPrice')
})

const calcCnyText = computed(() => {
  const r = calcResult.value
  if (!r) return '-'
  if (!r.official && r.validCount === 0) return '¥ 0.00'
  return r.cnyFen != null ? fen(r.cnyFen) : '—'
})

// ─── 游戏全览 / AGR 详情弹窗 ───
const allGamesOpen = ref(false)
/** 弹窗标题的**参数**（不是已译文案）：标题在 computed 里 t() 现取，切语言跟随。
 *  直接 `allGamesTitle.value = t(...)` 是一次性写进 ref 的译文——语言会冻死在
 *  打开弹窗那一刻，且区名也会冻住（regionName 随 store 变化）。 */
const allGamesScope = ref<{ codes: string[] | null; count: number } | null>(null)
const allGamesTitle = computed(() => {
  const s = allGamesScope.value
  if (!s) return ''
  if (s.codes === null) return t('bundles.dialog.allGamesTitle', { n: s.count })
  const regions = s.codes.map((c) => regionsStore.regionName(c)).join('/')
  return t('bundles.dialog.agrTitle', { regions, n: s.count })
})
const allGames = ref<{ game: BundleGame; missing: boolean }[]>([])

function showAllGames() {
  const d = detail.value
  if (!d) return
  allGamesScope.value = { codes: null, count: d.games.length }
  allGames.value = d.games.map((g) => ({ game: g, missing: false }))
  allGamesOpen.value = true
}

function showAgrGroup(group: { codes: string[]; aids: number[] }) {
  const d = detail.value
  if (!d) return
  const aids = new Set(group.aids.map(Number))
  allGamesScope.value = { codes: group.codes, count: group.aids.length }
  allGames.value = d.games.map((g) => ({ game: g, missing: !aids.has(g.appid) }))
  allGamesOpen.value = true
}

onMounted(() => {
  document.addEventListener('mousedown', onDocClick)
  load()
})
onBeforeUnmount(() => document.removeEventListener('mousedown', onDocClick))
</script>

<template>
  <div class="bundles-view">
    <!-- 导航栏（对齐游戏商店形态：搜索 + 排序 + 地区维度 + 高级筛选 + 统计）。
         复用全局 navbar 样式族；无手动刷新键——路由切走再回来 onMounted
         必然重新拉取，常驻刷新键无意义。-->
    <nav class="navbar">
      <div class="nav-controls">
        <input
          v-model="searchInput"
          type="text"
          class="search-box"
          style="flex: 1; min-width: 200px; max-width: none"
          :placeholder="t('bundles.nav.search')"
          @keydown.enter.prevent="commitSearch"
        />

        <div class="sort-buttons">
          <!-- 排序下拉（智能评分 / 差价最大，服务端预计算列） -->
          <div ref="sortRef" class="region-dropdown">
            <button
              class="sort-dropdown-btn"
              :class="{ active: sortBy !== 'smart' }"
              @click="showSortMenu = !showSortMenu"
            >
              {{ currentSortLabel }} ▼
            </button>
            <div class="region-dropdown-menu" :class="{ show: showSortMenu }">
              <div
                v-for="opt in sortOptions"
                :key="opt.key"
                class="region-option"
                :class="{ active: sortBy === opt.key }"
                @click="selectSort(opt.key)"
              >
                <span class="name">{{ t(opt.labelKey) }}</span>
              </div>
            </div>
          </div>

          <!-- 地区维度：选区后差价重新锚定到该区（卡片自带全区价格明细） -->
          <div ref="regionRef" class="region-dropdown">
            <button
              class="region-dropdown-btn"
              :class="{ active: !!region }"
              @click="showRegionMenu = !showRegionMenu"
            >
              📉 {{ regionLabel }} ▼
            </button>
            <div class="region-dropdown-menu" :class="{ show: showRegionMenu }">
              <div
                v-for="opt in regionOptions"
                :key="opt.value || 'all'"
                class="region-option"
                :class="{ active: region === opt.value }"
                @click="selectRegion(opt.value)"
              >
                <img v-if="opt.flag" :src="opt.flag" class="flag" alt="" />
                <span class="name">{{ opt.label }}</span>
              </div>
            </div>
          </div>

          <!-- 布局切换（网格 ⊞ / 列表 ☰） -->
          <div class="layout-switch">
            <button
              class="layout-switch__btn"
              :class="{ active: layoutMode === 'grid' }"
              :title="t('navbar.layout.grid')"
              @click="setLayout('grid')"
            >
              ⊞
            </button>
            <button
              class="layout-switch__btn"
              :class="{ active: layoutMode === 'list' }"
              :title="t('navbar.layout.list')"
              @click="setLayout('list')"
            >
              ☰
            </button>
          </div>
        </div>

        <span class="stats">
          {{ t('bundles.head.total', { n: filteredBundles.length }) }}
          <template v-if="filteredBundles.length > 0">
            {{ t('bundles.head.completable', { n: completableCount }) }}
          </template>
        </span>

        <!-- 高级筛选（复刻可实现的维度） -->
        <button class="filter-main-btn" :class="{ active: filterOpen }" @click="filterOpen = !filterOpen">
          {{ t('navbar.advancedFilter') }}
          <span v-if="activeFilterCount > 0" class="filter-badge">{{ activeFilterCount }}</span>
        </button>
      </div>
    </nav>

    <!-- 地区子分类工具栏（选区后出现，对齐游戏商店交互） -->
    <div v-if="region" class="filter-toolbar active">
      <span class="filter-toolbar-label">
        <img
          v-if="activeRegionMeta?.flag"
          :src="activeRegionMeta.flag"
          style="width: 18px; height: 13px"
          alt=""
        />
        {{ regionLabel }}
      </span>
      <button
        v-for="m in regionModes"
        :key="m.key"
        class="filter-mode-btn"
        :class="{ active: regionMode === m.key }"
        @click="selectRegionMode(m.key)"
      >
        {{ t(m.labelKey) }}
      </button>
      <button class="filter-close-btn" @click="selectRegion('')">{{ t('common.clear') }}</button>
    </div>

    <!-- 高级筛选抽屉（对齐游戏商店 hl-fp 标准结构，只放捆绑包有数据源的维度） -->
    <HlDrawer v-model="filterOpen" :title="t('navbar.advancedFilter')" width="400px" :mask-closable="true">
      <div class="hl-fp-body" style="padding: 0">
        <div>
          <div class="hl-fp-sec-title">{{ t('bundles.filter.section.basic') }}</div>
          <div class="hl-fp-check">
            <HlCheckbox
              :model-value="filters.completableOnly"
              :label="t('bundles.filter.completableOnly')"
              @update:model-value="(v: boolean) => setFilter('completableOnly', v)"
            />
            <HlCheckbox
              :model-value="filters.cnLowestOnly"
              :label="t('bundles.filter.cnLowestOnly')"
              @update:model-value="(v: boolean) => setFilter('cnLowestOnly', v)"
            />
          </div>
        </div>

        <div>
          <div class="hl-fp-sec-title">{{ t('bundles.filter.section.ownership') }}</div>
          <div class="hl-fp-check">
            <HlCheckbox
              :model-value="filters.hideOwned"
              :label="t('bundles.filter.hideOwned')"
              @update:model-value="(v: boolean) => setFilter('hideOwned', v)"
            />
            <HlCheckbox
              :model-value="filters.hideFamily"
              :label="t('bundles.filter.hideFamily')"
              @update:model-value="(v: boolean) => setFilter('hideFamily', v)"
            />
          </div>
        </div>

        <div>
          <div class="hl-fp-sec-title">{{ t('bundles.filter.section.price') }}</div>
          <div class="hl-fp-inline">
            <span>{{ t('bundles.filter.price') }}</span>
            <input
              type="number"
              class="hl-fp-tol"
              :value="filters.priceMin"
              :placeholder="t('bundles.filter.min')"
              min="0"
              step="1"
              @input="setFilter('priceMin', ($event.target as HTMLInputElement).value)"
            />
            <span>—</span>
            <input
              type="number"
              class="hl-fp-tol"
              :value="filters.priceMax"
              :placeholder="t('bundles.filter.max')"
              min="0"
              step="1"
              @input="setFilter('priceMax', ($event.target as HTMLInputElement).value)"
            />
            <span>{{ t('bundles.filter.cny') }}</span>
          </div>
          <div class="hl-fp-inline">
            <span>{{ t('bundles.filter.diffMinLabel') }}</span>
            <input
              type="number"
              class="hl-fp-tol"
              :value="filters.diffMin"
              :placeholder="t('bundles.filter.min')"
              min="0"
              step="1"
              @input="setFilter('diffMin', ($event.target as HTMLInputElement).value)"
            />
            <span>{{ t('bundles.filter.cny') }}</span>
            <span v-if="region" class="hl-fp-hint">{{ t('bundles.filter.diffMinRegionHint') }}</span>
          </div>
        </div>

        <div style="margin-top: 12px">
          <HlButton variant="text" size="sm" @click="resetFilters">{{ t('common.clear') }}</HlButton>
        </div>
      </div>
    </HlDrawer>

    <div v-if="errorMsg" class="bundles-error">{{ errorMsg }}</div>
    <HlSkeleton v-else-if="loading" variant="card" :count="8" />
    <div v-else-if="bundles.length === 0" class="bundles-empty">
      {{ t('bundles.empty.noData') }}
    </div>
    <div v-else-if="filteredBundles.length === 0" class="bundles-empty">
      {{ t('bundles.empty.noMatch') }}
    </div>

    <template v-else>
      <!-- 列表模式：单列紧凑行（对齐游戏商店列表几何：封面 128×60 通高 +
           名称标签 + 国区价 / 最低区 / 差价 单行铺开），展示助手与网格卡同源 -->
      <div v-if="layoutMode === 'list'" class="bundle-list">
        <div
          v-for="b in visibleBundles"
          :key="b.bundleId"
          class="bundle-list-row"
          :class="{
            owned: ownershipByBid[b.bundleId]?.type === 'owned',
            family: ownershipByBid[b.bundleId]?.type === 'family',
          }"
          @click="toggleDrawer(b, $event)"
        >
          <div class="blr-cover">
            <span class="cover-placeholder">📦</span>
            <img
              v-if="b.headerImage && !bundleDead(b)"
              class="cover"
              :src="bundleCover(b)"
              :alt="b.name"
              loading="lazy"
              decoding="async"
              @error="onCoverError($event, b)"
              @load="onCoverLoad(b)"
            />
            <span v-if="maxDiscount(b) > 0" class="blr-discount">-{{ maxDiscount(b) }}%</span>
          </div>

          <div class="blr-main">
            <div class="blr-name" :title="b.name">{{ b.name }}</div>
            <div class="blr-tags">
              <span
                v-if="ownershipByBid[b.bundleId]?.type"
                class="blr-own"
                :class="ownershipByBid[b.bundleId]!.type"
              >
                {{ ownershipByBid[b.bundleId]!.type === 'owned' ? t('bundles.badge.owned') : t('bundles.badge.family') }}
              </span>
              <span
                v-if="b.mustPurchaseAsSet === 0"
                class="bundle-mps-tag completable"
                :title="t('bundles.mps.completableTip')"
              >{{ t('bundles.mps.completable') }}</span>
              <span
                v-else-if="b.mustPurchaseAsSet === 1"
                class="bundle-mps-tag must-buy"
                :title="t('bundles.mps.setOnlyTip')"
              >{{ t('bundles.mps.setOnly') }}</span>
              <span
                v-if="(Object.values(b.regionPrices)[0]?.baseDiscount ?? 0) > 0"
                class="bundle-base-discount-tag"
              >{{ t('bundles.tag.baseDiscount', { pct: Object.values(b.regionPrices)[0].baseDiscount }) }}</span>
            </div>
          </div>

          <div class="blr-col">
            <span class="blr-label"><RegionFlag code="CN" /></span>
            <span class="blr-value cn">{{ b.cnCnyFen != null ? fen(b.cnCnyFen) : t('bundles.price.none') }}</span>
          </div>
          <div class="blr-col">
            <span class="blr-label">
              <RegionFlag v-if="cheaperTop3(b).length > 0" :code="cheaperTop3(b)[0]![0]" />
              <RegionFlag v-else-if="b.cnCnyFen != null" code="CN" />
            </span>
            <span class="blr-value lowest">
              {{
                cheaperTop3(b).length > 0
                  ? fen(cheaperTop3(b)[0]![1].cnyFen)
                  : b.cnCnyFen != null
                    ? fen(lowestFen(b))
                    : '—'
              }}
            </span>
          </div>
          <div class="blr-col">
            <span class="blr-label">{{ t('bundles.price.diff') }}</span>
            <span v-if="diffOf(b) > 0" class="diff-badge positive">
              <span class="align-text-up">{{ t('bundles.price.save', { amt: fen(diffOf(b), 0) }) }}</span>
            </span>
            <span v-else class="diff-badge"><span class="align-text-up">{{ t('bundles.price.noDiff') }}</span></span>
          </div>

          <!-- 商店链接不触发行点击的详情抽屉：外层挡冒泡（<a> 本身不带事件，
              收口规则禁止无 href 的 <a> 当按键） -->
          <span class="blr-link" @click.stop>
            <a class="steam-link" :href="b.url" target="_blank" rel="noreferrer">
              <span class="steam-icon"></span>
              {{ t('bundles.link.store') }}
            </a>
          </span>
        </div>
      </div>

      <!-- 网格模式：原始布局（hl-stagger：逐卡级联入场，见 hl-framework.css） -->
      <div v-else v-stagger class="bundle-grid hl-stagger">
      <div
        v-for="b in visibleBundles"
        :key="b.bundleId"
        class="game-card bundle-card"
        :class="{
          owned: ownershipByBid[b.bundleId]?.type === 'owned',
          family: ownershipByBid[b.bundleId]?.type === 'family',
        }"
      >
        <div class="cover-wrapper" @click="toggleDrawer(b, $event)">
          <div
            v-if="ownershipByBid[b.bundleId]?.type"
            class="status-badge"
            :class="ownershipByBid[b.bundleId]!.type"
            :data-owners="
              JSON.stringify(
                ownershipByBid[b.bundleId]!.type === 'owned'
                  ? [t('bundles.badge.me')]
                  : [ownershipByBid[b.bundleId]!.account ?? ''],
              )
            "
          >
            {{ ownershipByBid[b.bundleId]!.type === 'owned' ? t('bundles.badge.owned') : t('bundles.badge.family') }}
            <span class="sb-q">?</span>
          </div>
          <span class="cover-placeholder">📦</span>
          <img
            v-if="b.headerImage && !bundleDead(b)"
            class="cover"
            :src="bundleCover(b)"
            :alt="b.name"
            loading="lazy"
            decoding="async"
            @error="onCoverError($event, b)"
            @load="onCoverLoad(b)"
          />
          <div v-if="maxDiscount(b) > 0" class="discount-badges-container">
            <span class="discount-badge">-{{ maxDiscount(b) }}%</span>
          </div>
          <!-- BundleID 徽标 = SteamDB 外链：链接形态走 itemKind（sub / bundle），
               与购买语义（mustPurchaseAsSet）解耦——bundle 形态但必须整包的包真实存在。
               点击不冒泡到封面抽屉（toggleDrawer 内按事件目标忽略徽标命中） -->
          <a
            class="appid-badge"
            :href="`https://steamdb.info/${b.itemKind === 1 ? 'sub' : 'bundle'}/${b.bundleId}/`"
            target="_blank"
            rel="noreferrer"
          >{{ b.bundleId }}</a>
        </div>

        <div class="info">
          <div class="title-row">
            <h3 class="game-title" :title="b.name">{{ b.name }}</h3>
          </div>

          <div class="tags-row">
            <span
              v-if="b.mustPurchaseAsSet === 0"
              class="bundle-mps-tag completable"
              :title="t('bundles.mps.completableTip')"
            >
              {{ t('bundles.mps.completable') }}
            </span>
            <span
              v-else-if="b.mustPurchaseAsSet === 1"
              class="bundle-mps-tag must-buy"
              :title="t('bundles.mps.setOnlyTip')"
            >
              {{ t('bundles.mps.setOnly') }}
            </span>
            <span v-else class="bundle-mps-tag unknown">{{ t('bundles.mps.unknown') }}</span>
            <span
              v-if="(Object.values(b.regionPrices)[0]?.baseDiscount ?? 0) > 0"
              class="bundle-base-discount-tag"
            >
              {{ t('bundles.tag.baseDiscount', { pct: Object.values(b.regionPrices)[0].baseDiscount }) }}
            </span>
            <a
              class="steam-link"
              :href="b.url"
              target="_blank"
              rel="noreferrer"
            >
              <span class="steam-icon"></span>
              {{ t('bundles.link.store') }}
            </a>
          </div>

          <div class="price-section">
            <div class="price-row">
              <RegionFlag code="CN" class="price-label" />
              <span class="price-value cn">{{ b.cnCnyFen != null ? fen(b.cnCnyFen) : t('bundles.price.none') }}</span>
            </div>
            <template v-if="b.cnCnyFen != null">
              <!-- 前三低价区：对齐游戏卡 topRegions（RegionFlag 自带旗+名，
                   名字只渲染一次——手写 top3-name 会与组件内 region-flag__name
                   叠加成双地名，且把奖牌挤出可视列） -->
              <div v-for="([code, rp], i) in cheaperTop3(b)" :key="code" class="price-row top3-row">
                <span class="price-label">
                  <RegionFlag :code="code" />
                  <img
                    :src="TROPHIES[i]"
                    class="bc-medal top3-medal"
                    :class="`medal-${i}`"
                    alt="medal"
                  />
                </span>
                <span class="price-value lowest">{{ fen(rp.cnyFen) }}</span>
              </div>
              <!-- 国区自己最低：走游戏卡同款「国区最低」行 -->
              <div v-if="cheaperTop3(b).length === 0" class="price-row">
                <span class="price-label">
                  <RegionFlag code="CN" />
                  <span class="top3-name">{{ t('bundles.price.lowest') }}</span>
                </span>
                <span class="price-value lowest">{{ fen(lowestFen(b)) }}</span>
              </div>
            </template>
            <div class="price-row">
              <span class="price-label">{{ t('bundles.price.diff') }}</span>
              <div class="price-diff">
                <span v-if="diffOf(b) > 0" class="diff-badge positive">
                  <span class="align-text-up">{{ t('bundles.price.save', { amt: fen(diffOf(b), 0) }) }}</span>
                </span>
                <span v-else class="diff-badge"><span class="align-text-up">{{ t('bundles.price.noDiff') }}</span></span>
              </div>
            </div>
          </div>

          <HlButton class="details-btn" @click.stop="toggleDrawer(b)">
            <span class="arrow">▼</span> {{ t('bundles.action.allRegionPrices') }}
          </HlButton>
        </div>
      </div>
      </div>
    </template>

    <div v-if="!loading && filteredBundles.length > visibleBundles.length" class="bundles-load-more">
      <HlButton @click="renderLimit += RENDER_STEP">
        {{ t('bundles.list.loadMore', { n: filteredBundles.length - visibleBundles.length }) }}
      </HlButton>
    </div>

    <!-- 全区价格详情抽屉（非模态，底层可交互） -->
    <HlDrawer v-model="drawerOpen" :modal="false" :with-header="false" width="520px" :top="stackTop" class="bundle-drawer">
      <template v-if="drawerBundle">
        <div class="bundle-detail-header">
          <HlImg :src="drawerBundle.headerImage" :alt="drawerBundle.name" />
          <div style="min-width: 0">
            <div class="bdh-name">{{ drawerBundle.name }}</div>
            <div class="bdh-sub">
              BundleID {{ drawerBundle.bundleId }}
              <HlButton size="sm" variant="text" @click="openExternal(drawerBundle.url)">{{ t('bundles.action.steamStore') }} ↗</HlButton>
            </div>
          </div>
          <HlButton class="bd-close" variant="text" @click="drawerOpen = false">✕</HlButton>
        </div>

        <div class="bundle-status-bar">
          <span class="bundle-status-label">{{ t('bundles.drawer.statusLabel') }}</span>
          <span class="bundle-status-value" :class="statusBar.cls">{{ statusBar.text }}</span>
          <HlButton
            v-if="drawerBundle.mustPurchaseAsSet === 0"
            size="sm"
            art="pattern"
            tone="blue"
            style="margin-left: auto"
            @click="openCalculator(drawerBundle)"
          >
            {{ t('bundles.action.calc') }}
          </HlButton>
        </div>

        <div v-if="hasPartialLock" class="bundle-lock-hint">
          {{ t('bundles.drawer.lockHint') }}
        </div>

        <HlSkeleton v-if="detailLoading" variant="text" :count="1" :rows="5" />
        <div v-else-if="detailError" class="bundles-error">
          {{ t('bundles.drawer.detailError', { err: detailError }) }}
        </div>

        <div v-else class="bd-price-grid">
          <div
            v-for="code in drawerCodes"
            :key="code"
            class="bd-price-item"
            :class="{
              'lowest-region': code.toLowerCase() === drawerBundle!.lowestRegion,
              'partial-lock': (drawerBundle!.regionPrices[code]?.lockedCount ?? 0) > 0,
            }"
            :title="t('bundles.drawer.giftTooltip', { region: regionsStore.regionName(code) })"
            @click="toggleGift(code)"
          >
            <RegionFlag :code="code" class="bd-region" />
            <div class="bd-prices">
              <template v-if="hasPrice(code)">
                <span class="bd-orig">{{ drawerBundle!.regionPrices[code].formatted }}</span>
                <span
                  class="bd-cny"
                  :class="{
                    cheaper:
                      drawerBundle!.cnCnyFen != null &&
                      drawerBundle!.regionPrices[code].cnyFen != null &&
                      drawerBundle!.regionPrices[code].cnyFen! < drawerBundle!.cnCnyFen! - 100,
                    expensive:
                      drawerBundle!.cnCnyFen != null &&
                      drawerBundle!.regionPrices[code].cnyFen != null &&
                      drawerBundle!.regionPrices[code].cnyFen! > drawerBundle!.cnCnyFen! + 100,
                  }"
                >
                  {{ fen(drawerBundle!.regionPrices[code].cnyFen) }}
                </span>
                <span
                  v-if="(drawerBundle!.regionPrices[code].lockedCount ?? 0) > 0"
                  class="bd-locked"
                  :title="t('bundles.drawer.lockedTip', { n: drawerBundle!.regionPrices[code].lockedCount })"
                >
                  {{ t('bundles.drawer.lockedBadge', { n: drawerBundle!.regionPrices[code].lockedCount }) }}
                </span>
              </template>
              <span v-else class="bd-locked">{{ t('bundles.drawer.locked') }}</span>
            </div>
          </div>
        </div>

        <!-- 赠礼地区分析（点价格行展开，复用 gifting 引擎） -->
        <div v-if="giftAnalysis" class="gift-analysis">
          <div class="gift-head">
            {{ t('bundles.gift.head', { region: giftAnalysis.targetRegion.nameZh }) }}
            <HlButton variant="text" size="small" @click="giftTarget = null">{{ t('bundles.gift.collapse') }}</HlButton>
          </div>
          <div class="gift-cols">
            <div class="gift-col">
              <div class="gift-col-title can">{{ t('bundles.gift.canGive') }}</div>
              <RegionFlag
                v-for="r in giftAnalysis.canGiveTo"
                :key="r.code"
                :code="r.code"
                class="gift-region"
              />
              <div v-if="giftAnalysis.canGiveTo.length === 0" class="gift-none">—</div>
            </div>
            <div class="gift-col">
              <div class="gift-col-title cannot">{{ t('bundles.gift.cannotGive') }}</div>
              <RegionFlag
                v-for="r in giftAnalysis.cannotGiveTo"
                :key="r.code"
                :code="r.code"
                class="gift-region"
              />
              <div v-if="giftAnalysis.cannotGiveTo.length === 0" class="gift-none">—</div>
            </div>
            <div class="gift-col">
              <div class="gift-col-title can">{{ t('bundles.gift.canReceive') }}</div>
              <RegionFlag
                v-for="r in giftAnalysis.canReceiveFrom"
                :key="r.code"
                :code="r.code"
                class="gift-region"
              />
              <div v-if="giftAnalysis.canReceiveFrom.length === 0" class="gift-none">—</div>
            </div>
          </div>
        </div>

        <!-- 包内游戏 -->
        <div class="bundle-game-section">
          <div class="bundle-game-title">{{ t('bundles.games.title') }}</div>
          <div v-if="detail && detail.games.length > 0" class="bundle-game-list">
            <div
              v-for="g in drawerGames"
              :key="g.appid"
              class="bundle-game-card"
              :class="gameStatusClass(g)"
              :title="gameTitle(g)"
            >
              <HlImg
                :src="g.headerImage"
                :alt="g.name ?? ''"
                loading="lazy"
              />
              <div class="bgc-name">{{ g.name ?? `AppID: ${g.appid}` }}</div>
            </div>
          </div>
          <div v-else-if="!detailLoading" class="bd-empty-hint">{{ t('bundles.games.empty') }}</div>
          <HlButton
            v-if="detail && detail.games.length > 4"
            variant="text"
            size="small"
            style="margin: 8px auto 0; display: block"
            @click="showAllGames()"
          >
            {{ t('bundles.games.viewAll', { n: detail.games.length }) }}
          </HlButton>
        </div>

        <!-- 地区 AppID 差异 -->
        <div v-if="agrGroups.length > 1" class="agr-section">
          <div class="agr-section-title">{{ t('bundles.agr.title') }}</div>
          <div
            v-for="(g, i) in agrGroups"
            :key="i"
            class="appid-group-row"
            @click="showAgrGroup(g)"
          >
            <div class="agr-flags">
              <RegionFlag v-for="c in g.codes" :key="c" :code="c" :show-flag="true" class="agr-flag" />
            </div>
            <span class="agr-count">{{ t('bundles.agr.count', { n: g.aids.length }) }}</span>
          </div>
        </div>
      </template>
    </HlDrawer>

    <!-- 补齐计算器 -->
    <HlDialog v-model="calcOpen" :title="t('bundles.calc.title')" :width="420">
      <template v-if="calcBundle">
        <div class="calc-row">
          <span class="calc-label">{{ calcBundle.name }}</span>
        </div>
        <div class="calc-row">
          <span class="calc-label">{{ t('bundles.calc.region') }}</span>
          <HlSelect v-model="calcRegion" :options="calcRegionOptions" style="width: 200px" />
        </div>
        <div class="calc-row">
          <span class="calc-label">{{ t('bundles.calc.foreignTotal') }}</span>
          <span class="calc-result-foreign" :class="{ 'calc-result-none': calcResult && !calcResult.official && calcResult.validCount === 0 }">
            {{ calcForeignText }}
          </span>
        </div>
        <div class="calc-row">
          <span class="calc-label">{{ t('bundles.calc.cnyTotal') }}</span>
          <span style="display: flex; align-items: center; gap: 10px">
            <span
              class="calc-result-cny"
              :class="{ 'calc-result-none': calcResult && !calcResult.official && calcResult.validCount === 0 }"
            >
              {{ calcCnyText }}
            </span>
            <HlButton art="pattern" tone="blue" size="sm" @click="runCalculate">{{ t('bundles.calc.run') }}</HlButton>
          </span>
        </div>

        <!-- 整句一条词条，行内强调由词条自带的 <em> 承担（见 zh-CN/bundles.ts 的说明）。
             拆成三段的写法会让译文被迫把被强调的词放在句尾，英文拼出来是残句。
             词条是应用自有静态文案（非用户输入），v-html 无注入面。
             强调样式在全局表 hl-bundles.css：scoped 到不了 v-html 的内容。
             vue/no-v-html 未启用（flat/essential 不含），与 HlBanner 同款写法。 -->
        <div class="calc-hint" v-html="t('bundles.calc.excludeHint')"></div>
        <div class="calc-hint-sub">{{ t('bundles.calc.autoPreselect') }}</div>

        <div v-if="calcGames.length > 0" class="calc-games-grid" style="margin-top: 10px">
          <div
            v-for="g in calcGames"
            :key="g.appid"
            class="bundle-game-card"
            :class="gameStatusClass(g)"
            :title="gameTitle(g)"
            style="cursor: pointer"
            @click="toggleExclude(g.appid)"
          >
            <HlImg
              :src="g.headerImage"
              :alt="g.name ?? ''"
              loading="lazy"
            />
            <div class="bgc-name">{{ g.name ?? `AppID: ${g.appid}` }}</div>
            <div v-if="calcExcluded.has(g.appid)" class="exclusion-mask exclude">
              <span>❌</span>
            </div>
          </div>
        </div>
        <div v-else class="calc-hint" style="border-top: none">
          {{ t('bundles.calc.noGames') }}
        </div>
      </template>
    </HlDialog>

    <!-- 游戏全览 / AGR 差异详情 -->
    <HlDialog v-model="allGamesOpen" :title="allGamesTitle" :width="640">
      <div class="calc-games-grid" style="max-height: 60vh">
        <div
          v-for="{ game, missing } in allGames"
          :key="game.appid"
          class="bundle-game-card"
          :class="gameStatusClass(game)"
          :title="gameTitle(game)"
        >
          <HlImg
            :src="game.headerImage"
            :alt="game.name ?? ''"
            loading="lazy"
          />
          <div class="bgc-name">{{ game.name ?? `AppID: ${game.appid}` }}</div>
          <div v-if="missing" class="exclusion-mask region-locked"><span>{{ t('bundles.mask.regionLocked') }}</span></div>
        </div>
      </div>
    </HlDialog>
  </div>
</template>

<style scoped>
.bundles-view {
  padding: 16px 18px 40px;
}
.bundles-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.bundles-load-more {
  display: flex;
  justify-content: center;
  margin: 18px 0 26px;
}
.bundles-stats {
  font-size: 13px;
  color: var(--text-secondary);
}
/* `.bundles-loading` 随加载态改骨架屏删除（描述文字不再是「加载中…」样式） */
.bundles-empty,
.bundles-error {
  text-align: center;
  padding: 40px 0;
  color: var(--text-muted);
}
.bundles-error {
  color: var(--danger);
}
.lowest-flag {
  font-size: 10px;
  color: var(--success);
  border: 1px solid var(--success-a40);
  border-radius: 3px;
  padding: 0 4px;
  margin-left: 6px;
}
/* 前三低价区行：对齐游戏卡 price-row 结构（label = 旗+名+奖牌 flex 内联） */
.top3-row .price-label,
.price-row .price-label.is-top3 {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  font-size: 11px;
  color: var(--text-dim);
}
.top3-row :deep(.region-flag__icon) {
  width: 18px;
  height: 13px;
}
.top3-row .top3-name {
  font-size: 11px;
  color: var(--text-dim);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.top3-medal {
  width: 16px;
  height: 16px;
  margin-left: 4px;
  flex-shrink: 0;
}
.medal-0 { filter: drop-shadow(0 0 5px rgba(255, 215, 0, 0.4)); }
.medal-1 { filter: drop-shadow(0 0 5px rgba(192, 192, 192, 0.4)); }
.medal-2 { filter: drop-shadow(0 0 5px rgba(205, 127, 50, 0.4)); }
.bd-close {
  margin-left: auto;
}
.bdh-sub a {
  color: var(--accent);
}
.gift-analysis {
  margin-top: 10px;
  padding: 10px;
  border-radius: 6px;
  background: var(--surface-inset);
  border: 1px solid var(--row-border);
}
.gift-head {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.gift-cols {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}
.gift-col-title {
  font-size: 11px;
  margin-bottom: 4px;
}
.gift-col-title.can {
  color: var(--success);
}
.gift-col-title.cannot {
  color: var(--danger);
}
.gift-region {
  display: flex;
  font-size: 11px;
  padding: 2px 0;
}
.gift-none {
  font-size: 11px;
  color: var(--text-muted);
}
.bd-empty-hint {
  font-size: 12px;
  color: var(--text-muted);
  padding: 8px 0;
}
.agr-flag {
  margin-right: 2px;
}
.agr-flag :deep(img) {
  width: 16px;
  height: 12px;
  object-fit: cover;
  border-radius: 2px;
}
.agr-flag :deep(.region-flag__name) {
  display: none;
}

/* ─── 布局切换（⊞/☰）：类名与 HlNavbar 同款，但那份是组件 scoped 私有，
     此处独立声明（几何一致，颜色走令牌） ─── */
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

/* ─── 列表模式：单列紧凑行（几何对齐游戏商店列表：封面 128×60 通高）─── */
.bundle-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bundle-list-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 12px 6px 6px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-md, 10px);
  background: var(--bg-card);
  cursor: pointer;
  min-width: 0;
}
.bundle-list-row:hover {
  border-color: var(--border-strong);
}
.bundle-list-row.owned {
  border-color: var(--success, #27ae60);
}
.bundle-list-row.family {
  border-color: var(--accent);
}

.blr-cover {
  position: relative;
  width: 128px;
  height: 60px;
  flex-shrink: 0;
  border-radius: 6px;
  overflow: hidden;
  background: var(--surface-inset, transparent);
}
.blr-cover .cover {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}
.blr-cover .cover-placeholder {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  color: var(--text-faint);
}
.blr-discount {
  position: absolute;
  top: 2px;
  right: 2px;
  padding: 1px 4px;
  border-radius: 4px;
  background: var(--accent-fill);
  color: var(--on-accent-fill);
  font-size: 10px;
  font-weight: 700;
}

.blr-main {
  flex: 1 1 240px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.blr-name {
  font-size: 13.5px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.blr-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  min-width: 0;
}
.blr-own {
  padding: 0 6px;
  border-radius: 4px;
  font-size: 10.5px;
  line-height: 16px;
  border: 1px solid var(--success, #27ae60);
  color: var(--success, #27ae60);
}
.blr-own.family {
  border-color: var(--accent);
  color: var(--accent);
}

/* 价格三列：固定列宽（列位置跨行一致，不随内容漂移——对齐卡片统计列契约） */
.blr-col {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
  flex-shrink: 0;
}
.blr-col:nth-of-type(2) {
  width: 96px;
}
.blr-col:nth-of-type(3) {
  width: 96px;
}
.blr-col:nth-of-type(4) {
  width: 88px;
  align-items: flex-end;
}
.blr-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-muted);
}
.blr-value {
  font-size: 12.5px;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.blr-value.lowest {
  color: var(--success, #27ae60);
  font-weight: 600;
}
.blr-link {
  flex-shrink: 0;
}

</style>
