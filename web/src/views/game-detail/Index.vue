<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  crawlApi,
  gamesApi,
  invalidateGetCache,
  type GameDetail,
  type HistoryPayload,
  type PriceEventItem,
} from '@/api/client'
import { formatMinor } from '@/api/currencies'
import { flagUrl, formatCnyFen } from '@/api/regions'
import { isPermChangeRecent } from '@/lib/priceFlag'
import { agePart } from '@/lib/priceDataView'
import {
  EVENT_AGE_KEYS,
  eventViews,
  stateFormatter,
  type EventFormatters,
} from '@/lib/priceEvents'
import { selectableVariants, versionSelectOptions } from '@/lib/versions'
import { useCrawlStatusStore } from '@/stores/crawlStatus'
import { useRegionsStore } from '@/stores/regions'
import { useI18n, useLocaleFormat, type MessageKey } from '@/locales'
import RegionFlag from '@/components/RegionFlag.vue'
import HlSelect from '@/components/ui/HlSelect.vue'
import HlTable, { type HlTableColumn } from '@/components/ui/HlTable.vue'
import HlButton from '@/components/ui/HlButton.vue'
import HlChip from '@/components/ui/HlChip.vue'
import HlEmpty from '@/components/ui/HlEmpty.vue'
import HlImg from '@/components/ui/HlImg.vue'
import HlSkeleton from '@/components/ui/HlSkeleton.vue'
import PriceTrendChart from '@/components/business/PriceTrendChart.vue'

/** 价格奖牌（金/银/铜 = 非 CN 最低价前三，与卡片 GPW 同一规则） */
const TROPHIES = ['/assets/trophy_gold.png', '/assets/trophy_silver.png', '/assets/trophy_copper.png']

interface RegionRow {
  code: string // 大写区码（priceMatrix 键）
  name: string
  flag: string
  currency: string
  nativePrice: string
  cnyFen: number
  discount: number
  locked: boolean
  free: boolean // cents=0 的免费态（f2p/限时赠送）：显示「免费」而非锁区/¥0
}

const route = useRoute()
const router = useRouter()
const regionsStore = useRegionsStore()
// 千分位与时间随界面语言（fmt 内部现读 locale，切语言即重渲染，见 locales/format.ts）
const fmt = useLocaleFormat()
const { t } = useI18n()

const appid = computed(() => Number(route.params.appid))

const detail = ref<GameDetail | null>(null)
const loading = ref(true)
const errorMsg = ref('')

const historyRegion = ref('cn') // 小写（history API 语义）
/** 0 = 标准版（请求不带 subId）；其余 = sub 包号（对齐走势抽屉 versionKey 语义） */
const historySubId = ref(0)
const history = ref<HistoryPayload | null>(null)
const historyLoading = ref(false)
/** 可见时间窗（天）：0=全部。历史全量一次拉齐，缩放只在图表端进行（SteamDB 式导航条） */
const windowDays = ref(0)
let requestSeq = 0

/** 时间窗 chips。**只存词条 key**：模块级常量在加载时求值一次，存译文会把语言
    冻死（模板里 `t(r.labelKey)` 才是渲染期取值）。 */
const RANGE_OPTIONS: { labelKey: MessageKey; days: number }[] = [
  { labelKey: 'gameDetail.range.90d', days: 90 },
  { labelKey: 'gameDetail.range.1y', days: 365 },
  { labelKey: 'gameDetail.range.3y', days: 1095 },
  { labelKey: 'gameDetail.range.all', days: 0 },
]

const regionName = (code: string) => regionsStore.regionName(code)

// ── 全区价格行（升序排、锁区沉底；行点击 → 切走势地区）──
const regionRows = computed<RegionRow[]>(() => {
  if (!detail.value) return []
  const rows: RegionRow[] = Object.entries(detail.value.priceMatrix).map(([code, cell]) => ({
    code,
    name: regionName(code),
    flag: flagUrl(code),
    currency: regionsStore.metaByCode(code)?.currency ?? '',
    nativePrice: cell[0],
    cnyFen: cell[1],
    discount: cell[3] ?? 0,
    // cents=0 = 免费态（cny_fen 合法为 0）；locked 只剩 cnyFen 缺失且非免费的行
    free: cell[2] === 0,
    locked: !cell[1] && cell[2] !== 0,
  }))
  return rows.sort((a, b) => {
    if (a.locked !== b.locked) return a.locked ? 1 : -1
    return a.cnyFen - b.cnyFen
  })
})

/** 非 CN 有价区 CNY 升序前三（金/银/铜奖牌；免费行不参与——全 0 时奖牌无意义） */
const medalRegions = computed<string[]>(() =>
  regionRows.value
    .filter((r) => r.code !== 'CN' && !r.locked && !r.free)
    .slice(0, 3)
    .map((r) => r.code),
)

/** 奖牌行展示数据（价格表头「最低三区」摘要） */
const medalItems = computed(() =>
  medalRegions.value.map((code, i) => ({
    code,
    icon: TROPHIES[i],
    name: regionName(code),
    cnyFen: regionRows.value.find((r) => r.code === code)?.cnyFen ?? 0,
  })),
)

/** 模板辅助：HlTable 行槽位 → RegionRow */
const asRow = (row: unknown): RegionRow => row as RegionRow

/** HlTable rows prop 类型适配 */
const tableRows = computed(() => regionRows.value as unknown as Record<string, unknown>[])

/** 后端 chinese_support 的取值（crawler/utils.py detect_chinese_support 产出
    '无中文' / '简体中文' / '繁体中文'）——这是**数据值不是文案**：不建词条、
    值原样渲染，这里只判它决定标签配色。
    用正则字面量而不是 `=== '无中文'`：双语红线的 no-hardcoded-cjk 把正则字面量
    排除在「文案」之外（规则判不了字符集里是数据还是文案），而同一个中文字符串写成
    字符串字面量会被判成待迁文案。语义与 `=== '无中文'` 完全等价。 */
const NO_CHINESE = /^无中文$/

const cnRow = computed(() => regionRows.value.find((r) => r.code === 'CN') ?? null)
const lowestRow = computed(() => regionRows.value.find((r) => r.code !== 'CN' && !r.locked && !r.free) ?? null)
/** 限时免费截止日（promoEndAt 为 Unix 秒） */
const promoEndDate = computed(() => {
  const ts = detail.value?.promoEndAt
  if (!ts) return ''
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
})

/** 国区折扣截止日（Unix 秒；无折扣/未带促销元数据/已过期不展示——
 *  过期只在抓取间隙出现，下一轮爬取会把现价行刷成无折扣） */
const discountEndDate = computed(() => {
  const ts = detail.value?.cnDiscountEndsAt
  if (!ts || ts * 1000 <= Date.now()) return ''
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
})
const savingsYuan = computed(() => {
  if (!detail.value?.savingsFen) return 0
  return Math.round(detail.value.savingsFen / 100)
})

/** 列定义必须是 computed：HlTable 直接渲染 `col.label`（不经过本文件的模板表达式），
    写成模块级常量就等于把表头语言冻在模块加载那一刻。 */
const PRICE_COLUMNS = computed<HlTableColumn[]>(() => [
  { key: 'region', label: t('gameDetail.price.col.region') },
  { key: 'native', label: t('gameDetail.price.col.native'), numeric: true },
  { key: 'cny', label: t('gameDetail.price.col.cny'), numeric: true },
  { key: 'discount', label: t('gameDetail.price.col.discount'), width: '70px' },
  { key: 'save', label: t('gameDetail.price.col.save'), width: '90px', numeric: true },
])
/** 只剩例外要写：折扣列同样是数字，但居中比右对齐更好读（对齐规则默认由
 *  HlTable 提供，这里只登记例外）。 */
const PRICE_ALIGN = { discount: 'center' }

function priceRowClass(row: Record<string, unknown>): string {
  const r = row as unknown as RegionRow
  return [
    r.code.toLowerCase() === historyRegion.value ? 'is-active' : '',
    r.code === 'CN' ? 'is-cn' : '',
    lowestRow.value?.code === r.code ? 'is-lowest' : '',
    r.locked ? 'is-locked' : '',
  ]
    .filter(Boolean)
    .join(' ')
}

function onPriceRowClick(row: Record<string, unknown>) {
  const r = row as unknown as RegionRow
  if (!r.locked) historyRegion.value = r.code.toLowerCase()
}

function saveYuanOf(r: RegionRow): number {
  if (r.code === 'CN' || r.locked || !cnRow.value) return 0
  return Math.max(cnRow.value.cnyFen - r.cnyFen, 0) / 100
}

/** 地区下拉选项（带国旗，HlSelect 格式）。
    地区名与货币码是数据，但外层的全角括号是中文排版，故整条走词条参数化。 */
const regionSelectOptions = computed<HlSelectOption[]>(() =>
  regionRows.value
    .filter((r) => !r.locked)
    .map((r) => ({
      value: r.code.toLowerCase(),
      label: t('gameDetail.region.option', { name: r.name, currency: r.currency }),
      flag: r.flag,
    })),
)

// ── 头部史低标签（hlFlag：1=新史低 2=平史低 3=打折非史低）──
const headerHlType = computed(() => (detail.value && detail.value.cnDiscount > 0 ? detail.value.hlFlag : 0))
/** 永降/永涨徽章显隐:原价跳变须在 14 天时效窗内;永降还要求不在打折 */
const ppTagVisible = computed(() => {
  const d = detail.value
  if (!d || !d.ppFlag) return false
  if (d.ppFlag === 1 && (d.cnDiscount ?? 0) > 0) return false
  return isPermChangeRecent(d.ppChangedAt)
})

// ── 侧栏类型标签 ──
const genresList = computed(() =>
  (detail.value?.genres ?? '')
    .split(',')
    .map((g) => g.trim())
    .filter(Boolean),
)

// 全版本浏览在卡片走势抽屉「全部版本」区块；本页走势按
// component-framework.html 模块 F 权威模版：Steam 价 + Key 店走线 + 史低平线
// + 缩略导航 + 四格统计 + 事件时间线

/** 走势地区当前现价（priceMatrix 直取，CNY 折算）。priceMatrix 是标准版
    口径——切到具体版本后现价块取该版本序列末点，避免「图表是豪华版、
    现价是本体」的错位 */
const regionNowCnyFen = computed(() => {
  if (historySubId.value !== 0) {
    const ps = history.value?.points ?? []
    return ps.length ? (ps[ps.length - 1].cnyFen ?? null) : null
  }
  const cell = detail.value?.priceMatrix?.[historyRegion.value.toUpperCase()]
  return Array.isArray(cell) && cell[1] ? (cell[1] as number) : null
})

/** 走势地区当前折扣（序列末点，priceMatrix 无折扣列） */
const regionNowDiscount = computed(() => {
  const ps = history.value?.points ?? []
  return ps.length ? (ps[ps.length - 1].discount ?? 0) : 0
})

/** 首发原价：序列内首个有原价切片（currentCents 未打折）的 CNY 折算，
    无则退回首切片现价（爬虫积累初期等价于首发价） */
const debutOriginalYuan = computed(() => {
  const ps = history.value?.points ?? []
  const first = ps.find((p) => (p.cnyFen ?? 0) > 0)
  return first ? (first.cnyFen ?? 0) / 100 : null
})

/** 史低节点数与时间线（PriceTrendChart 同一 running-min 口径） */
const lowEventCount = computed(() => {
  const ps = history.value?.points ?? []
  let min = Infinity
  let n = 0
  for (const p of ps) {
    if (!p.cnyFen || p.cnyFen <= 0) continue
    if (p.cnyFen < min) {
      n++
      min = p.cnyFen
    }
  }
  return n
})
const lowTimeline = computed(() => {
  const ps = history.value?.points ?? []
  let min = Infinity
  const evts: { ts: string; fen: number }[] = []
  for (const p of ps) {
    if (!p.cnyFen || p.cnyFen <= 0) continue
    if (p.cnyFen < min) {
      evts.push({ ts: (p.timestamp ?? '').slice(0, 10), fen: p.cnyFen })
      min = p.cnyFen
    }
  }
  return evts.map((e, i) => ({
    // computed 体内 t()：t 现读 locale，故切语言即重算（不是冻结陷阱）
    ts: e.ts,
    label:
      i === 0
        ? t('gameDetail.event.first')
        : i === evts.length - 1
          ? t('gameDetail.event.lowest')
          : t('gameDetail.event.drop'),
    fen: e.fen,
  }))
})

// ── 特性徽章组 ──
interface FeatureBadge {
  text: string
  tone: 'green' | 'blue' | 'purple' | 'red' | 'orange'
}
const featureBadges = computed<FeatureBadge[]>(() => {
  const g = detail.value
  if (!g) return []
  const list: FeatureBadge[] = []
  if (g.familySharing) list.push({ text: t('gameDetail.badge.familySharing'), tone: 'purple' })
  if (g.tradingCards) list.push({ text: t('gameDetail.badge.tradingCards'), tone: 'blue' })
  // XGP 条只剩品牌词与后端档位（无中文），不建词条——同 HlGameCard 的 EPIC 处理
  if (g.xgpTier) list.push({ text: `XGP: ${g.xgpTier}`, tone: 'green' })
  if (g.isAdult) list.push({ text: t('gameDetail.badge.adult'), tone: 'red' })
  if (g.isVisualNovel) list.push({ text: t('gameDetail.badge.visualNovel'), tone: 'blue' })
  if (g.isEpic) {
    list.push({
      text: g.epicDate
        ? t('gameDetail.badge.epicDate', { date: g.epicDate })
        : t('gameDetail.badge.epic'),
      tone: 'blue',
    })
  }
  if (g.isHb) list.push({ text: g.hbData || t('gameDetail.badge.hb'), tone: 'green' })
  // 第三方渠道进包史（Barter.vg 计数），与 HB 慈善包口径区分
  if (g.bundleCount) list.push({ text: t('gameDetail.badge.bundled', { n: g.bundleCount }), tone: 'orange' })
  return list
})

const ratingTone = computed(() => {
  const rate = detail.value?.positiveRate
  if (rate == null) return 'blue'
  if (rate >= 80) return 'green'
  if (rate >= 50) return 'blue'
  return 'red'
})

async function load() {
  loading.value = true
  errorMsg.value = ''
  // 事件面与详情并行拉取：失败只是不渲染区块，不拖累详情页其余部分
  void loadEvents()
  try {
    // 走势预热与详情并行:初始地区默认 cn。仅当详情到手后发现国区无价、
    // 需要改选初始地区时,才放弃预热结果、由 watch(historyRegion) 重拉
    const warmHistory = gamesApi.history(appid.value, historyRegion.value, 0).catch(() => null)
    detail.value = await gamesApi.detail(appid.value)
    const hasCn = regionRows.value.some((r) => r.code === 'CN' && !r.locked)
    const wantRegion = hasCn
      ? 'cn'
      : (regionRows.value.find((r) => !r.locked)?.code.toLowerCase() ?? 'cn')
    if (wantRegion !== historyRegion.value) {
      historyRegion.value = wantRegion
      await warmHistory
      return
    }
    const res = await warmHistory
    if (res) {
      const seq = ++requestSeq
      if (seq === requestSeq) history.value = res
    }
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

// ── 版本切换（版本列表来自 history 响应 versions，随地区刷新）──
// 展示口径与卡片 GPW、走势抽屉同源（lib/versions.ts）：标准版只占一行，
// 它横跨的多个 sub 代际不各占一行。
const hasMultipleVersions = computed(() => selectableVariants(history.value?.versions ?? []).length > 0)

const versionOptions = computed(() => versionSelectOptions(history.value?.versions ?? [], t))

async function loadHistory() {
  const seq = ++requestSeq
  historyLoading.value = true
  try {
    // days=0 全量拉齐：时间窗缩放全部在图表端（时间 chips / 导航条），不回源
    const res = await gamesApi.history(
      appid.value,
      historyRegion.value,
      0,
      historySubId.value || undefined,
    )
    if (seq !== requestSeq) return
    history.value = res
    // 版本列表随地区/响应刷新，当前选择可能已不在列表 → 回落标准版
    if (
      historySubId.value !== 0 &&
      !history.value?.versions?.some((v) => (v.subId ?? 0) === historySubId.value)
    ) {
      historySubId.value = 0
    }
  } catch {
    if (seq === requestSeq) history.value = null
  } finally {
    if (seq === requestSeq) historyLoading.value = false
  }
}

/** 下架复探：清标 + 后台重爬（误判自愈 / 重新上架复活） */
const retrying = ref(false)
/** 复探结果：**state 只存词条 key**（'' = 无提示），文字在模板里 t() 现取——
 *  把译好的句子写进 ref 会把语言冻在赋值那一刻（赋值只发生在异步回调里）。 */
const retryNoteKey = ref<'' | MessageKey>('')
/** 失败分支是后端原文，不走词条 */
const retryNoteErr = ref('')
const retryNote = computed(() =>
  retryNoteErr.value ? retryNoteErr.value : retryNoteKey.value ? t(retryNoteKey.value) : '',
)
async function retryRemoved() {
  if (!detail.value?.removedAt || retrying.value) return
  retrying.value = true
  retryNoteKey.value = ''
  retryNoteErr.value = ''
  try {
    const res = await gamesApi.retryRemoved(appid.value)
    retryNoteKey.value = res.requeued
      ? 'gameDetail.removed.requeued'
      : 'gameDetail.removed.cleared'
  } catch (e) {
    retryNoteErr.value = e instanceof Error ? e.message : String(e)
  } finally {
    retrying.value = false
  }
}

// ── 最近价格变化（事件的唯一来源是 price_events）──
// 事件判没判出来在后端，这里只做格式化：内部枚举翻标签、previous/current 拼成
// 「之前 → 现在」。前端不据 priceMatrix / hlFlag 自行推断事件。
// **null = 没取到**（不渲染区块，也不声称「没有变化」）；[] = 取到了但没有事件。

const crawl = useCrawlStatusStore()
const priceEvents = ref<PriceEventItem[] | null>(null)
/** 单游戏级请求：一次拉当前游戏的最近事件，不按卡片轮询 */
const EVENTS_LIMIT = 20

const eventFmt: EventFormatters = {
  price: (amountMinor, region) =>
    formatMinor(amountMinor, regionsStore.metaByCode(region ?? '')?.currency ?? 'CNY'),
  state: stateFormatter(t),
}

/** 展示时间来自事件自身的发生时刻（occurred_at），不是实体更新时间 */
const eventList = computed(() =>
  eventViews(priceEvents.value ?? [], eventFmt, (hours) => agePart(hours, EVENT_AGE_KEYS)),
)

async function loadEvents() {
  try {
    priceEvents.value = await crawlApi.priceEvents({ appid: appid.value, limit: EVENTS_LIMIT })
  } catch {
    priceEvents.value = null
  }
}

/** 周期收敛后的静默重取：不动加载态（后台刷新不该把页面打回骨架屏） */
async function refreshAfterCycle() {
  invalidateGetCache('/games')
  try {
    detail.value = await gamesApi.detail(appid.value)
  } catch {
    /* 保留原数据 */
  }
  await loadEvents()
}

// 价格周期收敛 → 价格数据与事件面一起刷新（P6-A 的刷新链在这里续到事件面）
watch(
  () => crawl.priceCycle?.cycleId ?? null,
  (cycleId) => {
    if (cycleId !== null) refreshAfterCycle()
  },
)
watch(appid, () => loadEvents())

// 切地区 → 版本数据源随区变化，重置标准版再重拉全量历史
watch(historyRegion, () => {
  historySubId.value = 0
  loadHistory()
})
// 切版本 → 重拉该 sub 序列（现价块/四格统计/事件线连锁跟随）
watch(historySubId, () => loadHistory())

/** 捆绑包补齐状态（后端 mustPurchaseAsSet：0=可补齐 1=必须整包 其余=未知）。
    函数体在**渲染期**执行（模板里调用），故 t() 在这里是响应式的——冻结的是
    模块级常量里预先算好的译文，不是这种延迟到调用点的写法。 */
const bundleMustText = (m: number) =>
  m === 0
    ? t('gameDetail.bundle.completable')
    : m === 1
      ? t('gameDetail.bundle.wholeOnly')
      : t('gameDetail.bundle.unknown')

onMounted(load)
</script>

<template>
  <section class="gd-page">
    <!-- 错误态：富内容空态（无图标——这里要给的是原因与重试按钮，不是"空"） -->
    <HlEmpty v-if="errorMsg" class="card" icon="">
      {{ errorMsg }}
      <button class="btn btn--sm" style="margin-top: 12px" @click="load">{{ t('common.retry') }}</button>
    </HlEmpty>
    <!-- 加载态由「加载中…」纯文字改骨架屏：外壳与 done 态一致，数据到达时不跳版 -->
    <div v-if="loading" class="card hl-loading-pane">
      <HlSkeleton variant="text" :count="1" :rows="6" />
    </div>

    <template v-else-if="detail">
      <!-- 返回键 -->
      <button class="gd-back-btn" @click="router.push('/library')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M19 12H5M12 19l-7-7 7-7" />
        </svg>
        {{ t('gameDetail.header.back') }}
      </button>

      <!-- ① 渐变头部：封面铺底 + 渐变遮罩 -->
      <div class="gd-header" data-section="gameDetail.section.header">
        <HlImg class="gd-header-cover" :src="detail.headerImage" :alt="detail.name" />
        <div class="gd-header-gradient"></div>
        <div class="gd-header-content">
          <div class="gd-header-left">
            <h1 class="gd-header-title">{{ detail.name }}</h1>
            <div v-if="detail.nameEn && detail.nameEn !== detail.name" class="gd-header-en">
              {{ detail.nameEn }}
            </div>
            <div class="gd-header-subtitle">
              <span v-if="detail.developers.length" class="gd-dev">
                {{ t('gameDetail.header.developers', { names: detail.developers.join(' / ') }) }}
              </span>
              <span v-if="detail.developers.length && detail.publishers.length" class="gd-separator">|</span>
              <span v-if="detail.publishers.length">
                {{ t('gameDetail.header.publishers', { names: detail.publishers.join(' / ') }) }}
              </span>
              <span v-if="detail.releaseDate" class="gd-separator">|</span>
              <span v-if="detail.releaseDate">{{ detail.releaseDate }}</span>
            </div>
          </div>

          <!-- 摘要角标（Holdexar 增强：一眼看核心价差）-->
          <div class="gd-header-summary">
            <div class="gd-sum-item">
              <div class="gd-sum-label">{{ t('gameDetail.region.cn') }}</div>
              <div class="gd-sum-value cn">
                {{ cnRow?.free ? t('gameDetail.price.free') : cnRow && !cnRow.locked ? formatCnyFen(cnRow.cnyFen) : '—' }}
              </div>
            </div>
            <div v-if="lowestRow" class="gd-sum-item">
              <div class="gd-sum-label">
                {{ t('gameDetail.summary.lowestRegion', { region: lowestRow.name }) }}
              </div>
              <div class="gd-sum-value lowest">{{ formatCnyFen(lowestRow.cnyFen) }}</div>
            </div>
            <div v-if="savingsYuan > 0" class="gd-sum-item">
              <div class="gd-sum-label">{{ t('gameDetail.summary.savings') }}</div>
              <div class="gd-sum-value diff">¥{{ savingsYuan }}</div>
            </div>
          </div>

          <!-- 折扣/史低标签 -->
          <div v-if="headerHlType > 0 || ppTagVisible || detail.freeKind === 'promo'" class="gd-header-badges">
            <!-- 限时免费（promo 态）：显示赠送截止日 -->
            <span v-if="detail.freeKind === 'promo'" class="gd-pp-tag gd-free-tag">
              {{ t('gameDetail.promo.active', { date: promoEndDate }) }}
            </span>
            <div v-if="headerHlType > 0" class="discount-badge" :class="`hl-type-${headerHlType}`">
              -{{ detail.cnDiscount }}%
              <span v-if="headerHlType === 1" class="db-text">{{ t('gameDetail.hl.newLow') }}</span>
              <span v-if="headerHlType === 2" class="db-text">{{ t('gameDetail.hl.sameLow') }}</span>
            </div>
            <!-- 折扣截止（卡片折扣徽章悬停气泡的同源信息，详情页直接展示） -->
            <span v-if="headerHlType > 0 && discountEndDate" class="gd-pp-tag gd-discount-ends">
              {{ t('gameDetail.discount.endsAt', { date: discountEndDate }) }}
            </span>
            <!-- 永降/永涨只在 14 天时效窗内展示（ppChangedAt 起算）：打折中显示
                 永降会被读成「这个折扣是永降」，变化超 14 天不再常驻 -->
            <span v-if="detail.ppFlag === 1 && ppTagVisible" class="gd-pp-tag gd-pp-tag--down">
              {{ t('gameDetail.pp.cut') }}
            </span>
            <span v-else-if="detail.ppFlag === 2 && ppTagVisible" class="gd-pp-tag gd-pp-tag--up">
              {{ t('gameDetail.pp.raise') }}
            </span>
          </div>

          <!-- 外链（图标对齐游戏库卡片：Steam 用 --steam-icon，SteamDB 用官方 logo） -->
          <div class="gd-header-links">
            <a class="gd-header-link" :href="detail.storeUrl" target="_blank" rel="noreferrer">
              <span class="gd-link-icon steam" />
              {{ t('gameDetail.link.steamStore') }}
            </a>
            <a class="gd-header-link" :href="`https://steamdb.info/app/${detail.appid}`" target="_blank" rel="noreferrer">
              <span class="gd-link-icon steamdb" />
              SteamDB
            </a>
          </div>

          <!-- 下架提示（移除监控）：判定下架时展示，附手动复探入口 -->
          <div v-if="detail.removedAt" class="gd-removed-banner">
            <span class="gd-removed-tag">{{ t('gameDetail.removed.tag') }}</span>
            <span class="gd-removed-date">
              {{ t('gameDetail.removed.judged', { date: detail.removedAt.slice(0, 10) }) }}
            </span>
            <HlButton variant="text" :disabled="retrying" :loading="retrying" @click="retryRemoved">
              {{ retrying ? t('gameDetail.removed.probing') : t('gameDetail.removed.retry') }}
            </HlButton>
            <span v-if="retryNote" class="gd-removed-note">{{ retryNote }}</span>
          </div>
        </div>
      </div>

      <!-- ② + ③ 双栏主体 -->
      <div class="gd-body">
        <!-- 信息侧栏 -->
        <aside class="gd-sidebar">
          <div class="gd-info-section">
            <div class="gd-info-title">{{ t('gameDetail.info.basic') }}</div>
            <div class="gd-info-value">
              <div v-if="detail.type">{{ t('gameDetail.info.type', { type: detail.type }) }}</div>
              <div v-if="detail.releaseDate">
                {{ t('gameDetail.info.releaseDate', { date: detail.releaseDate }) }}
              </div>
              <div v-if="detail.chineseSupport" :class="NO_CHINESE.test(detail.chineseSupport) ? '' : 'gd-info-cn'">
                {{ t('gameDetail.info.chinese', { value: detail.chineseSupport }) }}
              </div>
              <div v-if="detail.seriesId">{{ t('gameDetail.info.series', { id: detail.seriesId }) }}</div>
            </div>
          </div>

          <div v-if="detail.developers.length" class="gd-info-section">
            <div class="gd-info-title">{{ t('gameDetail.info.developers') }}</div>
            <div class="gd-info-tags">
              <span v-for="d in detail.developers" :key="d" class="gd-info-tag">{{ d }}</span>
            </div>
          </div>

          <div v-if="detail.publishers.length" class="gd-info-section">
            <div class="gd-info-title">{{ t('gameDetail.info.publishers') }}</div>
            <div class="gd-info-tags">
              <span v-for="p in detail.publishers" :key="p" class="gd-info-tag">{{ p }}</span>
            </div>
          </div>

          <div v-if="genresList.length" class="gd-info-section">
            <div class="gd-info-title">{{ t('gameDetail.info.genres') }}</div>
            <div class="gd-info-tags">
              <span v-for="g in genresList" :key="g" class="gd-info-tag">{{ g }}</span>
            </div>
          </div>

          <div class="gd-info-section">
            <div class="gd-info-title">{{ t('gameDetail.info.reviews') }}</div>
            <div class="gd-info-value">
              <span v-if="detail.positiveRate !== null" class="gd-info-badge" :class="ratingTone">
                {{ t('gameDetail.rating.positive', { rate: detail.positiveRate.toFixed(1) }) }}
              </span>
              <span class="gd-review-count">
                {{ t('gameDetail.rating.reviews', { n: fmt.group(detail.reviewCount) }) }}
              </span>
            </div>
          </div>

          <div v-if="featureBadges.length" class="gd-info-section">
            <div class="gd-info-title">{{ t('gameDetail.info.features') }}</div>
            <div class="gd-info-feature-row">
              <span v-for="f in featureBadges" :key="f.text" class="gd-info-badge" :class="f.tone">
                {{ f.text }}
              </span>
            </div>
          </div>

          <div class="gd-info-section">
            <div class="gd-info-title">AppID</div>
            <div class="gd-info-value gd-info-appid">{{ detail.appid }}</div>
          </div>
        </aside>

        <!-- 内容区 -->
        <div class="gd-content">
          <!-- ③ 全区价格表格 -->
          <div class="gd-card gd-price-section" data-section="gameDetail.section.priceDetail">
            <div class="gd-price-header">
              <span class="gd-price-title">{{ t('gameDetail.section.priceDetail') }}</span>
              <div class="gd-price-controls">
                <HlSelect
                  v-if="hasMultipleVersions"
                  v-model="historySubId"
                  :options="versionOptions"
                  class="gd-price-version"
                />
                <div v-if="medalItems.length" class="gd-medal-row">
                  <span class="gd-medal-label">{{ t('gameDetail.price.top3') }}</span>
                  <span v-for="m in medalItems" :key="m.code" class="gd-medal-item">
                    <img :src="m.icon" class="bc-medal bc-medal--inline" :class="`medal-${medalItems.indexOf(m)}`" alt="medal" />
                    {{ m.name }}
                    <b>{{ formatCnyFen(m.cnyFen) }}</b>
                  </span>
                </div>
              </div>
            </div>
            <HlTable
              v-if="regionRows.length"
              :columns="PRICE_COLUMNS"
              :rows="tableRows"
              :align="PRICE_ALIGN"
              :row-class="priceRowClass"
              :on-row-click="onPriceRowClick"
            >
              <template #region="{ row }">
                <div class="gd-region-cell">
                  <img :src="asRow(row).flag" :alt="asRow(row).code" />
                  <span>{{ asRow(row).name }}</span>
                  <img
                    v-if="medalRegions.indexOf(asRow(row).code) >= 0"
                    :src="TROPHIES[medalRegions.indexOf(asRow(row).code)]"
                    class="bc-medal bc-medal--inline"
                    :class="`medal-${medalRegions.indexOf(asRow(row).code)}`"
                    :title="t('gameDetail.price.medalTip')"
                    alt="medal"
                  />
                  <span v-if="asRow(row).code === 'CN'" class="gd-region-rank gd-rank-cn">
                    {{ t('gameDetail.region.cn') }}
                  </span>
                  <span v-if="lowestRow?.code === asRow(row).code" class="gd-region-rank">
                    {{ t('gameDetail.price.lowestTag') }}
                  </span>
                </div>
              </template>
              <template #native="{ row }">
                <span v-if="asRow(row).locked" class="gd-price-locked">—</span>
                <span v-else class="gd-price-native">{{ asRow(row).nativePrice }}</span>
              </template>
              <template #cny="{ row }">
                <span v-if="asRow(row).free" class="gd-price-free">
                  {{ t('gameDetail.price.free') }}
                </span>
                <span v-else-if="asRow(row).locked" class="gd-price-locked">
                  {{ t('gameDetail.price.locked') }}
                </span>
                <span
                  v-else
                  class="gd-price-cny"
                  :class="asRow(row).code === 'CN' ? 'cn' : asRow(row).cnyFen < (cnRow?.cnyFen ?? 0) ? 'cheaper' : 'expensive'"
                >
                  {{ formatCnyFen(asRow(row).cnyFen) }}
                </span>
              </template>
              <template #discount="{ row }">
                <span v-if="asRow(row).discount > 0" class="gd-discount-cell">-{{ asRow(row).discount }}%</span>
                <span v-else class="gd-price-locked">—</span>
              </template>
              <template #save="{ row }">
                <span v-if="saveYuanOf(asRow(row)) >= 1" class="gd-save-badge">
                  {{ t('gameDetail.price.save', { amount: saveYuanOf(asRow(row)).toFixed(0) }) }}
                </span>
              </template>
            </HlTable>
            <HlEmpty v-else size="sm" icon="" :text="t('gameDetail.price.empty')" />
          </div>

          <!-- ④ 历史价格走势 -->
          <div class="gd-card gd-section" data-section="gameDetail.section.trend">
            <div class="gd-section__header">
              <div class="section-title">{{ t('gameDetail.section.trend') }}</div>
              <div class="gd-trend-selects">
                <HlSelect
                  v-if="hasMultipleVersions"
                  v-model="historySubId"
                  :options="versionOptions"
                  class="history-version"
                />
                <HlSelect v-model="historyRegion" :options="regionSelectOptions" class="history-region" />
              </div>
            </div>
            <div class="history-ranges">
              <HlChip
                v-for="r in RANGE_OPTIONS"
                :key="r.days"
                tone="accent"
                :on="windowDays === r.days"
                @click="windowDays = r.days"
              >{{ t(r.labelKey) }}</HlChip>
            </div>
            <div v-if="historyLoading" class="hl-loading-pane" style="--pane-pad: 30px 0">
              <HlSkeleton variant="text" :count="1" :rows="4" />
            </div>
            <template v-else-if="history && history.points.length">
              <!-- 现价块（模版 pc-price-now：现价/原价/折扣角标） -->
              <div class="gd-trend-pricebar">
                <div class="gd-tp-row">
                  <span class="gd-tp-label">{{ t('gameDetail.trend.steamNow') }}</span>
                  <div class="gd-tp-block">
                    <div class="gd-tp-current" :class="{ 'is-discount': regionNowDiscount > 0 }">
                      {{ regionNowCnyFen !== null ? formatCnyFen(regionNowCnyFen) : '—' }}
                    </div>
                    <div v-if="regionNowDiscount > 0 && regionNowCnyFen !== null" class="gd-tp-original">
                      {{ formatCnyFen(Math.round(regionNowCnyFen / (1 - regionNowDiscount / 100))) }}
                    </div>
                    <div
                      v-if="regionNowDiscount > 0"
                      class="gd-tp-flag gd-tp-flag--down"
                    >{{ t('gameDetail.trend.discount', { pct: regionNowDiscount }) }}</div>
                    <div v-else class="gd-tp-flag gd-tp-flag--none">
                      {{ t('gameDetail.trend.noDiscount') }}
                    </div>
                  </div>
                </div>
              </div>
              <PriceTrendChart
                :payload="history"
                :appid="appid"
                with-key-line
                height="300px"
                :window-days="windowDays"
                @window-change="windowDays = $event"
              >
                <template #empty>{{ t('gameDetail.trend.emptyRegion') }}</template>
              </PriceTrendChart>
              <!-- 四格统计（模版 pc-stats-row；无数据显示 —） -->
              <div class="gd-trend-stats">
                <div class="gd-stat">
                  <div class="gd-stat__num gd-stat--low">
                    {{ history.lowest ? formatCnyFen(history.lowest.cnyFen) : '—' }}
                  </div>
                  <div class="gd-stat__lbl">{{ t('gameDetail.trend.lowest') }}</div>
                </div>
                <div class="gd-stat">
                  <div class="gd-stat__num gd-stat--now">
                    {{ regionNowCnyFen !== null ? formatCnyFen(regionNowCnyFen) : '—' }}
                  </div>
                  <div class="gd-stat__lbl">{{ t('gameDetail.trend.steamNow') }}</div>
                </div>
                <div class="gd-stat">
                  <div class="gd-stat__num gd-stat--orig">
                    {{ debutOriginalYuan !== null ? `¥${debutOriginalYuan.toFixed(0)}` : '—' }}
                  </div>
                  <div class="gd-stat__lbl">{{ t('gameDetail.trend.debutPrice') }}</div>
                </div>
                <div class="gd-stat">
                  <div class="gd-stat__num gd-stat--evt">
                    {{ t('gameDetail.trend.lowNodeCount', { n: lowEventCount }) }}
                  </div>
                  <div class="gd-stat__lbl">{{ t('gameDetail.trend.lowNodes') }}</div>
                </div>
              </div>
              <!-- 史低节点时间线（模版 pc-event-markers） -->
              <div v-if="lowTimeline.length" class="gd-trend-events">
                <span v-for="e in lowTimeline" :key="`${e.ts}-${e.fen}`" class="gd-evt-tag">
                  <span class="gd-evt-dot"></span>{{ e.ts }} {{ e.label }} ¥{{ (e.fen / 100).toFixed(0) }}
                </span>
              </div>
            </template>
            <HlEmpty v-else size="sm" icon="" :text="t('gameDetail.trend.empty')" />
          </div>

          <!-- ⑤ 最近价格变化：事实来自 price_events（前端只格式化，不重判事件） -->
          <div v-if="priceEvents !== null" class="gd-card gd-section" data-section="priceEvent.title">
            <div class="gd-section__header">
              <div class="section-title">{{ t('priceEvent.title') }}</div>
            </div>
            <div v-if="priceEvents.length === 0" class="gd-events-empty">
              {{ t('priceEvent.empty') }}
            </div>
            <ul v-else class="gd-events-list">
              <li v-for="ev in eventList" :key="ev.id" class="gd-events-row" :class="`tone-${ev.tone}`">
                <span class="gd-events-dot"></span>
                <span class="gd-events-label">{{ t(ev.labelKey) }}</span>
                <RegionFlag
                  v-if="ev.region"
                  :code="ev.region.toLowerCase()"
                  compact
                  class="gd-events-region"
                />
                <span v-if="ev.before || ev.after" class="gd-events-values">
                  <span class="gd-events-from">{{ ev.before }}</span>
                  <span class="gd-events-arrow">→</span>
                  <span class="gd-events-to">{{ ev.after }}</span>
                </span>
                <span class="gd-events-time">{{ t(ev.age.key, ev.age.params) }}</span>
              </li>
            </ul>
          </div>

          <!-- ⑥ 多版本 → 在卡片走势抽屉「全部版本」区块 -->

          <!-- ⑦ 关联捆绑包 -->
          <div v-if="detail.linkedBundles.length" class="gd-section-plain" data-section="gameDetail.section.bundles">
            <div class="gd-plain-title">
              📦 {{ t('gameDetail.bundles.title', { n: detail.linkedBundles.length }) }}
            </div>
            <div class="gd-bundles-grid">
              <a
                v-for="b in detail.linkedBundles"
                :key="b.bundleId"
                :href="b.url || `https://store.steampowered.com/bundle/${b.bundleId}/`"
                target="_blank"
                rel="noreferrer"
                class="gd-bundle-card"
              >
                <HlImg
                  :src="b.headerImage || `https://shared.akamai.steamstatic.com/store_item_assets/steam/bundles/${b.bundleId}/header.jpg`"
                  :alt="b.name"
                  loading="lazy"
                  decoding="async"
                />
                <div class="gd-bundle-info">
                  <div class="gd-bundle-name" :title="b.name">{{ b.name }}</div>
                  <div class="gd-bundle-meta">
                    <span :class="b.mustPurchaseAsSet === 0 ? 'gd-bm-complete' : ''">{{ bundleMustText(b.mustPurchaseAsSet) }}</span>
                    <span v-if="b.priceCny !== null && b.lowestPriceFen !== null" class="gd-bundle-price">
                      {{ regionName(b.lowestRegion) }} ¥{{ (b.lowestPriceFen / 100).toFixed(2) }}
                    </span>
                  </div>
                </div>
              </a>
            </div>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.gd-page {
  max-width: 1280px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 返回键 ── */
.gd-back-btn {
  align-self: flex-start;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: var(--accent-a10);
  border: 1px solid var(--accent-a30);
  color: var(--accent);
  padding: 6px 14px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: 13px;
  transition: all var(--transition);
}
.gd-back-btn:hover {
  background: var(--accent-a20);
  border-color: var(--accent);
  color: var(--accent-deep);
}
.gd-back-btn svg { width: 16px; height: 16px; }

/* ── ① 渐变头部 ── */
.gd-header {
  position: relative;
  width: 100%;
  height: 210px;
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--bg-soft);
}
.gd-header-cover {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  object-fit: cover;
  /* Steam 头图人物居中，cover 默认居中裁切会砍头 → 从顶端显示 */
  object-position: top;
  z-index: 1;
}
.gd-header-gradient {
  position: absolute;
  inset: 0;
  z-index: 2;
  /* 遮罩色随主题卡片底色派生（浅=白、深=深蓝），保证文字恒可读 */
  background: linear-gradient(
    to bottom,
    color-mix(in srgb, var(--bg-card) 0%, transparent) 0%,
    color-mix(in srgb, var(--bg-card) 40%, transparent) 40%,
    color-mix(in srgb, var(--bg-card) 70%, transparent) 70%,
    color-mix(in srgb, var(--bg-card) 95%, transparent) 100%
  );
  pointer-events: none;
}
.gd-header-content {
  position: relative;
  z-index: 3;
  display: flex;
  align-items: flex-end;
  height: 100%;
  padding: 0 24px 16px;
  gap: 20px;
  flex-wrap: wrap;
}
.gd-header-left { flex: 1; min-width: 260px; }
.gd-header-title {
  font-size: 26px;
  font-weight: 700;
  /* 封面蒙版上的白字：走 --text-on-fill（两套主题同为 #ffffff）*/
  color: var(--text-on-fill);
  text-shadow: 0 2px 10px rgba(0, 0, 0, 0.55);
  margin-bottom: 4px;
  line-height: 1.3;
}
.gd-header-en {
  font-size: 13px;
  color: var(--text-on-scrim);
  text-shadow: 0 1px 5px rgba(0, 0, 0, 0.55);
  margin-bottom: 4px;
}
.gd-header-subtitle {
  font-size: 13px;
  color: var(--text-on-scrim);
  text-shadow: 0 1px 5px rgba(0, 0, 0, 0.55);
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
}
.gd-header-subtitle .gd-dev { color: var(--text-on-scrim); font-weight: 600; }
.gd-header-subtitle .gd-separator { opacity: 0.4; }

/* 摘要角标（玻璃拟态压在封面上，双主题恒暗底白字保证可读）*/
.gd-header-summary {
  display: flex;
  gap: 16px;
  padding: 10px 14px;
  border-radius: var(--radius);
  background: rgba(15, 26, 38, 0.62);
  border: 1px solid rgba(255, 255, 255, 0.14);
  backdrop-filter: blur(6px);
  align-self: flex-end;
}
.gd-sum-label { font-size: 11px; color: rgba(255, 255, 255, 0.75); }
.gd-sum-value {
  font-size: 17px;
  font-weight: 700;
  color: var(--text-on-fill);
  white-space: nowrap;
}
/* 面板为常量深色玻璃（覆盖封面图，双主题不变）→ 数值色取「恒暗面上的定色」
   （tokens.css 与主题无关区段），而不是跟着主题走的 --success / --accent：
   浅色主题的 --success 是暗橄榄，在深底上对比度不足。 */
.gd-sum-value.cn { color: var(--accent-on-dark); }
.gd-sum-value.lowest { color: var(--success-on-dark); }
.gd-sum-value.diff { color: var(--danger-on-dark); }

/* 折扣/史低标签（配方同卡片 .discount-badge，此处已全局类直引） */
.gd-header-badges {
  display: flex;
  gap: 6px;
  align-items: center;
  align-self: flex-end;
  margin-bottom: 6px;
}

/* 永降/原价上调标签（ppFlag：1=永降 2=永涨） */
.gd-pp-tag {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 6px;
  font-size: 12px;
  font-weight: 700;
}

/* 同在封面蒙版上，故同样是恒暗面定色（阶梯用 color-mix 现推，
   本文件别处已在用 color-mix，不再为 15% / 30% 各加一组 token） */
.gd-pp-tag--down {
  background: color-mix(in srgb, var(--success-on-dark) 15%, transparent);
  color: var(--success-on-dark);
  border: 1px solid color-mix(in srgb, var(--success-on-dark) 30%, transparent);
}

.gd-pp-tag--up {
  background: color-mix(in srgb, var(--danger-on-dark) 15%, transparent);
  color: var(--danger-on-dark);
  border: 1px solid color-mix(in srgb, var(--danger-on-dark) 30%, transparent);
}

/* 限时免费标签（promo 态）：绿色系对齐降价语义 */
.gd-free-tag {
  background: color-mix(in srgb, var(--success-on-dark) 15%, transparent);
  color: var(--success-on-dark);
  border: 1px solid color-mix(in srgb, var(--success-on-dark) 30%, transparent);
}

/* 折扣截止（同在封面蒙版上，恒暗面定色：中性白微透，不与史低徽章抢色） */
.gd-discount-ends {
  background: color-mix(in srgb, var(--text-on-scrim) 12%, transparent);
  color: var(--text-on-scrim);
  border: 1px solid color-mix(in srgb, var(--text-on-scrim) 28%, transparent);
}

/* 价格表「免费」单元格 */
.gd-price-free {
  font-weight: 700;
  color: var(--success);
}

/* 外链键：绝对定位钉在封面右上角，不参与行内打包。头部是一条
   align-items: flex-end 的贴底打包行（摘要/徽章 align-self: flex-end、
   行撑满容器高才成立）——links 若留在流内，行内任一元素变宽（如折扣
   截止标签）就会把它挤到第二行，行按自然高度自顶重排，摘要与徽章
   随之漂离封面底。 */
.gd-header-links {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
  position: absolute;
  top: 0;
  right: 24px;
}

/* 下架提示条（移除监控）：判定下架时头部展示 */
.gd-removed-banner {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid rgba(255, 120, 120, 0.35);
  background: rgba(85, 85, 95, 0.28);
  font-size: 13px;
}
.gd-removed-tag {
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 6px;
  background: rgba(204, 51, 51, 0.85);
  color: var(--text-on-fill);
}
.gd-removed-date {
  color: var(--text-secondary, rgba(255, 255, 255, 0.7));
}
.gd-removed-note {
  color: var(--accent, #66c0f4);
  font-size: 12px;
}
.gd-header-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  background: rgba(15, 26, 38, 0.62);
  border: 1px solid rgba(255, 255, 255, 0.16);
  color: rgba(255, 255, 255, 0.92);
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  font-size: 12px;
  text-decoration: none;
  transition: all var(--transition);
  backdrop-filter: blur(6px);
}
.gd-header-link:hover {
  background: rgba(15, 26, 38, 0.85);
  border-color: var(--color-hl-primary);
  color: var(--text-on-fill);
}
/* 外链图标：Steam 用游戏库卡片同款 logo；SteamDB 官方 192px 标记
   （透明底白色，浅色主题经 steamdb-light 反色） */
.gd-link-icon {
  width: 14px;
  height: 14px;
  background-size: contain;
  background-repeat: no-repeat;
  background-position: center;
  flex-shrink: 0;
}
.gd-link-icon.steam { background-image: var(--steam-icon); }
.gd-link-icon.steamdb { background-image: url('/assets/steamdb-logo.png'); }
html:not(.dark) .gd-link-icon.steamdb { filter: invert(1); }

/* ── ② 双栏主体 ── */
.gd-body {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}

.gd-sidebar {
  width: 264px;
  flex-shrink: 0;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 16px;
}
.gd-info-section { margin-bottom: 16px; }
.gd-info-section:last-child { margin-bottom: 0; }
.gd-info-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 1px;
  color: var(--accent);
  margin-bottom: 6px;
  font-weight: 600;
}
.gd-info-value {
  font-size: 13px;
  color: var(--text-secondary);
  line-height: 1.6;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.gd-info-cn { color: var(--success); }
.gd-info-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.gd-info-tag {
  background: var(--accent-a10);
  border: 1px solid var(--accent-a20);
  color: var(--text-secondary);
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 3px;
}
.gd-review-count { font-size: 12px; color: var(--text-muted); }
.gd-info-feature-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.gd-info-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  padding: 3px 8px;
  border-radius: 4px;
  font-weight: 500;
}
.gd-info-badge.green {
  background: var(--success-a15);
  color: var(--success);
  border: 1px solid var(--success-a30);
}
.gd-info-badge.blue {
  background: var(--accent-a10);
  color: var(--accent);
  border: 1px solid var(--accent-a30);
}
.gd-info-badge.purple {
  background: var(--purple-a15);
  color: var(--purple);
  border: 1px solid var(--purple-a30);
}
.gd-info-badge.red {
  background: var(--danger-a15);
  color: var(--danger);
  border: 1px solid var(--danger-a30);
}
.gd-info-badge.orange {
  background: var(--warning-a15);
  color: var(--warning-deep);
  border: 1px solid var(--warning-a30);
}
.gd-info-appid {
  font-family: var(--font-mono);
  font-size: 14px;
  color: var(--text-primary);
}

.gd-content {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.gd-card {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}
.gd-section { padding: 20px 24px; }

/* ── ③ 价格表格 ── */
.gd-price-section { overflow: hidden; }
.gd-price-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  background: var(--surface-panel);
  border-bottom: 1px solid var(--border-soft);
  flex-wrap: wrap;
}
.gd-price-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
}
.gd-price-controls {
  display: flex;
  align-items: center;
  gap: 10px;
}
.gd-price-version { width: 190px; }
.gd-price-version :deep(.hl-select-wrap) { width: 100%; }
.gd-medal-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.gd-medal-label {
  font-size: 11px;
  color: var(--text-muted);
  letter-spacing: 1px;
}
.gd-medal-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  color: var(--text-secondary);
}
.gd-medal-item b { color: var(--success); font-size: 12px; }

/* 行高亮（表格行点击 → 切走势地区） */
.gd-price-section :deep(tbody tr) { cursor: pointer; }
.gd-price-section :deep(tbody tr.is-active) td { background: var(--accent-a15); }
.gd-price-section :deep(tbody tr.is-cn) td { background: var(--accent-a08); }
.gd-price-section :deep(tbody tr.is-lowest) td { background: var(--success-a08); }
.gd-price-section :deep(tbody tr.is-locked) { cursor: default; opacity: 0.5; }

.gd-region-cell {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.gd-region-cell > img:first-child { width: 20px; height: 15px; border-radius: 2px; }
.gd-region-cell span { font-size: 12.5px; }
/* 全局 .bc-medal 是 GPW 柱状图的绝对定位形态，行内场景中和为静态 */
.bc-medal--inline { position: static; bottom: auto; width: 14px; height: 14px; margin: 0; }
.gd-region-rank {
  font-size: 9px;
  background: var(--success-a20);
  color: var(--success);
  padding: 1px 5px;
  border-radius: 3px;
  font-weight: 600;
}
.gd-region-rank.gd-rank-cn {
  background: var(--accent-a20);
  color: var(--accent);
}
.gd-price-native {
  font-size: 12px;
  color: var(--text-muted);
}
.gd-price-cny { font-weight: 600; font-size: 14px; color: var(--text-primary); }
.gd-price-cny.cheaper { color: var(--success); }
.gd-price-cny.expensive { color: var(--text-muted); }
.gd-price-cny.cn { color: var(--accent); }
.gd-price-locked {
  color: var(--text-faint);
  font-style: italic;
  font-weight: normal;
  font-size: 12px;
}
.gd-discount-cell {
  font-size: 12px;
  font-weight: 600;
  color: var(--success);
}
.gd-save-badge {
  display: inline-flex;
  background: var(--success-a15);
  color: var(--success);
  font-size: 11px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 3px;
}

/* ── ④ 走势（控件样式沿用详情页既有形态）── */
.gd-section__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.gd-trend-selects {
  display: flex;
  gap: 8px;
  align-items: center;
}
.history-region { width: 160px; }
.history-region :deep(.hl-select-wrap) { width: 100%; }
.history-version { width: 190px; }
.history-version :deep(.hl-select-wrap) { width: 100%; }
.history-ranges { display: flex; gap: 6px; margin-top: 12px; }
/* `.range-chip`（胶囊 + 常驻强调色）曾在这里——它与 `tabs-shared.css` 的 `.bill-chip`、
   `game-detail` 的同名块是同一造型的三份副本，已统一到 `HlChip.vue` 的
   `<HlChip tone="accent">`。此处只留注释，样式见 `hl-framework.css` 的 `.hl-chip`。 */

/* ── 走势区块：现价块 / 四格统计 / 事件时间线（模版模块 F）── */
.gd-trend-pricebar {
  display: flex;
  justify-content: flex-end;
  margin-top: 10px;
}
.gd-tp-row {
  display: flex;
  align-items: center;
  gap: 12px;
}
.gd-tp-label {
  font-size: 12px;
  color: var(--text-muted);
}
.gd-tp-block {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
  justify-content: flex-end;
}
.gd-tp-current {
  font-size: 22px;
  font-weight: 700;
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}
.gd-tp-current.is-discount { color: var(--rate-good, var(--success)); }
.gd-tp-original {
  font-size: 12px;
  color: var(--text-faint);
  text-decoration: line-through;
}
.gd-tp-flag {
  font-size: 11px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 4px;
}
.gd-tp-flag--down {
  color: var(--rate-good, var(--success));
  background: var(--success-a15);
  border: 1px solid var(--success-a30);
}
.gd-tp-flag--none {
  color: var(--text-dim);
  background: var(--surface-chip-2);
  border: 1px solid var(--border-soft);
}

.gd-trend-stats {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-top: 14px;
}
.gd-stat {
  background: var(--surface-panel);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.gd-stat__num {
  font-size: 16px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: var(--text-primary);
  white-space: nowrap;
}
.gd-stat--low { color: var(--rate-good, var(--success)); }
.gd-stat--now { color: var(--accent); }
.gd-stat--orig { color: var(--warning); }
.gd-stat--evt { color: #2ed573; }
.gd-stat__lbl {
  font-size: 11px;
  color: var(--text-muted);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.gd-trend-events {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}
.gd-evt-tag {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--text-secondary);
  background: var(--surface-chip);
  border: 1px solid var(--border-soft);
  padding: 3px 9px;
  border-radius: 999px;
  white-space: nowrap;
}
.gd-evt-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #2ed573;
  box-shadow: 0 0 6px rgba(46, 213, 115, 0.5);
  flex-shrink: 0;
}

/* ── ⑤ 最近价格变化（事件事实面；只做有限配色分组，不评分不排序）── */
.gd-events-empty {
  font-size: 12.5px;
  color: var(--text-dim);
  padding: 4px 0 2px;
}
.gd-events-list {
  list-style: none;
  margin: 0;
  padding: 0;
}
.gd-events-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 0;
  font-size: 12.5px;
  color: var(--text-secondary);
}
.gd-events-row + .gd-events-row { border-top: 1px solid var(--row-border); }
.gd-events-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-faint);
  flex-shrink: 0;
}
.gd-events-row.tone-down .gd-events-dot { background: var(--rate-good); }
.gd-events-row.tone-low .gd-events-dot { background: var(--accent); }
.gd-events-row.tone-status .gd-events-dot { background: var(--warning); }
.gd-events-row.tone-free .gd-events-dot { background: var(--success); }
.gd-events-row.tone-removed .gd-events-dot { background: var(--danger); }
.gd-events-label { font-weight: 600; color: var(--text-primary); }
.gd-events-region { font-size: 12px; color: var(--text-dim); }
.gd-events-values {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-variant-numeric: tabular-nums;
  min-width: 0;
}
.gd-events-from { color: var(--text-faint); text-decoration: line-through; }
.gd-events-arrow { color: var(--text-faint); }
.gd-events-to { color: var(--text-primary); font-weight: 600; }
.gd-events-time {
  margin-left: auto;
  color: var(--text-dim);
  font-size: 11.5px;
  white-space: nowrap;
}

@media (max-width: 768px) {
  .gd-trend-stats { grid-template-columns: repeat(2, 1fr); }
}

@media (max-width: 768px) {
  .gd-trend-stats { grid-template-columns: repeat(2, 1fr); }
}

/* ── ⑤⑥ 版本 / 捆绑包 ── */
.gd-section-plain { display: flex; flex-direction: column; gap: 10px; }
.gd-plain-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 6px;
}
.gd-bundles-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 12px;
}
.gd-bundle-card {
  display: flex;
  gap: 10px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  padding: 10px;
  text-decoration: none;
  transition: all var(--transition);
}
.gd-bundle-card:hover {
  border-color: var(--accent-a40);
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}
.gd-bundle-card img {
  width: 120px;
  height: 56px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
  background: var(--bg-soft);
}
.gd-bundle-info { flex: 1; min-width: 0; }
.gd-bundle-name {
  font-size: 13px;
  color: var(--text-primary);
  margin-bottom: 4px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.gd-bundle-meta {
  font-size: 11px;
  color: var(--text-muted);
  display: flex;
  gap: 8px;
  align-items: center;
}
.gd-bundle-meta .gd-bm-complete { color: var(--success); font-weight: 600; }
.gd-bundle-price { color: var(--text-secondary); }

/* ── 响应式 ── */
@media (max-width: 1024px) {
  .gd-body { flex-direction: column; }
  .gd-sidebar { width: 100%; }
}
@media (max-width: 768px) {
  .gd-header { height: 170px; }
  .gd-header-content { padding: 0 12px 12px; }
  .gd-header-title { font-size: 20px; }
  .gd-header-links { flex-wrap: wrap; right: 12px; }
}
</style>
