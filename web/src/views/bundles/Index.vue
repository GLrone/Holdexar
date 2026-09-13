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
import { computed, onMounted, ref, watch } from 'vue'

import { bundlesApi, ownershipApi, type BundleDetail, type BundleGame, type BundleSummary, type OwnershipInfo } from '@/api/client'
import { regionSelectOptions } from '@/api/selectOptions'
import { HlButton, HlDialog, HlDrawer, HlSelect, HlSkeleton } from '@/components/ui'
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
import { useI18n } from '@/locales'
import { useRegionsStore } from '@/stores/regions'

const regionsStore = useRegionsStore()
const { t } = useI18n()

// ─── 列表 ───
const bundles = ref<BundleSummary[]>([])
const loading = ref(true)
const errorMsg = ref('')
/** bundleId → 撞库推演结果 */
const ownershipMap = ref<Record<number, OwnershipInfo>>({})
const ownershipByBid = ref<Record<number, ReturnType<typeof inferBundleOwnership>>>({})

// ─── 分批渲染：3,386 张卡片一次性挂载是页面打开慢的另一主因（数据接口
// 已有服务端缓存），先渲 RENDER_STEP 张、点按钮续批。筛选/排序仍在全量
// bundles 上做（纯 JS，微秒级），只有 DOM 挂载分批。───
const RENDER_STEP = 120
const renderLimit = ref(RENDER_STEP)
const visibleBundles = computed(() => bundles.value.slice(0, renderLimit.value))

async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    const res = await bundlesApi.list()
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
/** 抽屉最高处须低于吸顶 navbar（高度随布局 68/50px 变化），打开时以实测为准 */
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

/** 该区有可展示的价格（行存在但全空 = 原版语义的"锁区"） */
function hasPrice(code: string): boolean {
  const rp = drawerBundle.value?.regionPrices[code]
  return !!rp && (rp.cnyFen != null || rp.priceMinor != null)
}

/** 补齐状态文案（对齐原版 statusBarHtml 分支）。
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

/** 再点同包收回（对齐原版 toggleBundleDetails 的开关语义）。
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

/** 包内游戏归属 class（owned/family/wishlist 着色，对齐原版 bgc-*） */
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
  // 计算器数据到手后执行自动排除（对齐原版：无数据 + 已拥有）
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

// 切区清空结果（对齐原版 onCalcRegionChange → clearCalcResult）
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

onMounted(load)
</script>

<template>
  <div class="bundles-view">
    <div class="bundles-head">
      <div class="bundles-stats">
        {{ t('bundles.head.total', { n: bundles.length }) }}
        <template v-if="bundles.length > 0">
          {{ t('bundles.head.completable', { n: bundles.filter((b) => b.mustPurchaseAsSet === 0).length }) }}
        </template>
      </div>
      <HlButton variant="text" @click="load">{{ t('common.refresh') }}</HlButton>
    </div>

    <div v-if="errorMsg" class="bundles-error">{{ errorMsg }}</div>
    <HlSkeleton v-else-if="loading" variant="card" :count="8" />
    <div v-else-if="bundles.length === 0" class="bundles-empty">
      {{ t('bundles.empty.noData') }}
    </div>

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
                <span v-if="b.diffFen > 0" class="diff-badge positive">
                  <span class="align-text-up">{{ t('bundles.price.save', { amt: fen(b.diffFen, 0) }) }}</span>
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

    <div v-if="!loading && bundles.length > visibleBundles.length" class="bundles-load-more">
      <HlButton @click="renderLimit += RENDER_STEP">
        {{ t('bundles.list.loadMore', { n: bundles.length - visibleBundles.length }) }}
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
</style>
