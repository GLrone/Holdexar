<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { gamesApi, familyApi, type GameListItem, type GameSeriesInfo, type GameVersionPrices, type LinkedBundle } from '@/api/client'
import { compactRegionName, flagUrl } from '@/api/regions'
import { normalizeAvatarUrl } from '@/api/avatar'
import { useI18n, useLocaleFormat, type MessageKey } from '@/locales'
import { useLocaleStore } from '@/stores/locale'
import { useRegionsStore } from '@/stores/regions'
import { useOwnershipStore, type OwnershipType } from '@/stores/ownership'
import { useFollowsStore } from '@/stores/follows'
import { message } from '@/components/ui'
import { computeGiftingAnalysis, giftPayAmountFen, isReceiverPriced, type GiftingAnalysis, type RegionPriceInfo } from '@/lib/gifting'
import {
  ASSET_RETRY_MAX,
  assetRetryUrl,
  isAssetDead,
  markAssetDead,
  markAssetOk,
  resolveAssetUrl,
} from '@/lib/assetCache'
import { useTrendDrawerStore } from '@/stores/trendDrawer'
import { isPermChangeRecent } from '@/lib/priceFlag'
import { priceDataView } from '@/lib/priceDataView'
import { selectableVariants, versionSelectOptions } from '@/lib/versions'
import HlSelect from '@/components/ui/HlSelect.vue'
import PriceTrendDrawer from './PriceTrendDrawer.vue'

function formatCnyText(fen: number): string {
  return `¥${(fen / 100).toFixed(2)}`
}

/** Unix 秒 → YYYY-MM-DD（ISO 文本中英同形，与 removedAt.slice 同一口径） */
function formatDateTs(ts: number): string {
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

/** CDK 第三方平台状态（跨域查价的终端化承接，数据由后端 cdk_fetcher 提供）*/
interface CdkPlatform {
  listed: boolean
  price: string | null
  url: string
  error?: string
}

/**
 * 游戏卡片完整组件：
 * 卡片 + 徽章体系（折扣/史低/EPIC/HB/XGP/家庭共享/卡牌，网格与列表双布局）
 * + 左上角归属状态徽章（已拥有/家庭共享/愿望单，悬停弹归属账号）
 * + 全区价格弹窗（列表/柱状图）+ 关联捆绑包 + CDK 入口 + 赠礼分析弹窗 + 徽章 tooltip。
 * GPW 只展示「我」页启用的地区；top100 无数据源暂未渲染。
 */
const props = defineProps<{
  game: GameListItem
  layoutMode: 'grid' | 'list'
  /** 「我」页启用的地区（小写）；null = 全部 */
  enabledRegions?: string[] | null
}>()

const regionsStore = useRegionsStore()
const ownershipStore = useOwnershipStore()
const followsStore = useFollowsStore()
const localeStore = useLocaleStore()
const router = useRouter()

const { t } = useI18n()
const fmt = useLocaleFormat()

const showGpw = ref(false)
const activeTab = ref<'list' | 'chart'>('list')
const trendStore = useTrendDrawerStore()
/** 走势抽屉开关：全局 store 驱动，同游戏点击关闭，异游戏点击切换 */
const trendOpen = computed({
  get: () => trendStore.activeAppid === props.game.appid,
  set: (v: boolean) => {
    if (!v && trendStore.activeAppid === props.game.appid) trendStore.close()
  },
})
const cardRef = ref<HTMLElement | null>(null)
const coverRef = ref<HTMLElement | null>(null)
const gpwBtnRef = ref<HTMLElement | null>(null)
const gpwPopoverRef = ref<HTMLElement | null>(null)
const popoverPos = ref<{ top: number; left: number } | null>(null)
/** 弹窗自身尺寸监听（内容异步到位后重算落位），随开关挂卸 */
let popoverResizeObserver: ResizeObserver | null = null

// ─── 封面加载：失败随机延时重试（标准规则，见 component-framework.html）───
// 最多 4 次，每次 0.8–3.2s 随机退避。重试 URL 走 assetCache 的**稳定**形态
// （拼 `Date.now()` 会让每次重试都是全新 URL，浏览器缓存与 CDN 边缘缓存双双失效）。
// 「已放弃」状态记在模块级登记处而**不是**组件 ref：卡片会随列表重挂载，ref
// 归零会让一张下架封面在每次切板块时重跑 5 次注定 404 的往返。
const coverSrc = ref(resolveAssetUrl(props.game.headerImage))
const coverRetries = ref(0)
/** 初值问登记处：重挂载时已知失效的封面直接落占位，一次网络都不发 */
const coverBroken = ref(isAssetDead(props.game.headerImage))
let coverTimer: number | null = null
/**
 * 当前正在加载的那张图的**登记键**。必须跟着请求走、而不是现读 `props.game.headerImage`：
 * 列表项复用同一张卡片实例时，旧请求的 `load` 可能在换图之后才到，现读 props 会把
 * 新游戏的 URL 登记成「这张图加载成功过」——那是一条会污染整个会话的错误缓存。
 */
let coverBase = props.game.headerImage

function onCoverError() {
  if (coverRetries.value >= ASSET_RETRY_MAX) {
    coverBroken.value = true
    markAssetDead(coverBase)
    return
  }
  coverRetries.value += 1
  const attempt = coverRetries.value
  const delay = 800 + Math.random() * 2400
  coverTimer = window.setTimeout(() => {
    coverSrc.value = assetRetryUrl(coverBase, attempt)
  }, delay)
}

function onCoverLoad() {
  coverRetries.value = 0
  coverBroken.value = false
  // 记下实际生效的 URL：该图若只在带后缀时可达，下次挂载直接用它
  markAssetOk(coverBase, coverSrc.value)
}

watch(
  () => props.game.headerImage,
  (v) => {
    if (coverTimer) window.clearTimeout(coverTimer)
    coverBase = v
    coverSrc.value = resolveAssetUrl(v)
    coverRetries.value = 0
    // 换图时同样先问登记处：新游戏可能也是已知失效的那一张
    coverBroken.value = isAssetDead(v)
  },
)

const giftRegion = ref<string | null>(null)
const giftPopupPos = ref<{ top: number; left: number } | null>(null)
const giftPopupRef = ref<HTMLElement | null>(null)
/** 赠礼弹窗的锚点行（被点的那条地区）；行被重挂（换版本）后即失效 */
const giftAnchorRef = ref<HTMLElement | null>(null)

// ─── 徽章体系（折扣/归属/标题三组徽章）───

/** hlFlag：0=无 1=新史低 2=平史低 3=打折非史低；不打折恒为 0 */
const hlType = computed(() => (props.game.discount > 0 ? props.game.hlFlag : 0))

/** 折扣标签内嵌文字：1=新史低 2=平史低 3=非史低；hlFlag=0/4+ 兜底为非史低 */
const hlText = computed(() => {
  if (hlType.value === 1) return t('gameCard.hl.newLow')
  if (hlType.value === 2) return t('gameCard.hl.sameLow')
  return t('gameCard.hl.notLow')
})

/** 无日期时只剩品牌词 EPIC，不建词条；整句走 epic.given 一条（{date} 占位） */
const epicText = computed(() =>
  props.game.epicDate ? t('gameCard.epic.given', { date: props.game.epicDate }) : 'EPIC',
)
const hbText = computed(() => props.game.hbData || t('gameCard.hb.bundle'))

/** 下架角标文案：判定下架（removedAt 非空）时显示，tooltip 带判定日期 */
const isRemoved = computed(() => !!props.game.removedAt)
const removedText = computed(() =>
  props.game.removedAt
    ? t('gameCard.removed.tip', { date: props.game.removedAt.slice(0, 10) })
    : '',
)

/** 折扣截止 tooltip（悬停折扣徽章弹出）：无数据或已过期（抓取间隙）不展示 */
const discountEndsTip = computed(() => {
  const ts = props.game.discountEndsAt
  if (props.game.discount <= 0 || !ts || ts * 1000 <= Date.now()) return ''
  return t('gameCard.discount.endsAt', { date: formatDateTs(ts) })
})

// ─── 价格数据状态（观察时间 / 新鲜度 / 本轮覆盖）───
// 观察时间是**价格**维度，与 updatedAt（实体更新时间）不同源。覆盖率由后端按
// Cycle 冻结的期望集算出，卡片只展示；措辞规则在 lib/priceDataView。
const priceView = computed(() => priceDataView(props.game.priceData))
const priceAgeText = computed(() => t(priceView.value.age.key, priceView.value.age.params))
const coverageText = computed(() => {
  const cov = priceView.value.coverage
  return cov ? t(cov.key, cov.params) : ''
})
const priceDataTip = computed(() =>
  priceView.value.tips.map((part) => t(part.key, part.params)).join(' · '),
)

// ─── 左上角归属状态徽章 + 游戏归属弹窗（状态徽章 + 悬停弹窗）───

/** 徽章类型 → 词条键 / 弹窗标题键 / 标题色 / 边框色（与账号悬停卡同源配色）*/
const STATUS_META: Record<
  OwnershipType,
  { badgeKey: MessageKey; titleKey: MessageKey; color: string; border: string }
> = {
  // 取令牌而非字面量：色值走 CSS 变量才能跟随主题（写死 html.dark 下的取值
  // 会让浅色主题下徽章一直挂着深色的色）。令牌写法在 :style 里同样生效。
  //
  // 文案同理**存键不存值**：常量表在模块加载时求值一次，直接写 t() 的结果会把
  // 语言冻死在首次加载那一刻（同 PriceTrendDrawer 的 RANGE_OPTIONS）。显示文本
  // 在模板/computed 里 t(badgeKey) 现取，切语言即跟随。
  owned: {
    badgeKey: 'gameCard.status.owned',
    titleKey: 'gameCard.status.ownedTitle',
    color: 'var(--success)',
    border: 'var(--success-a40)',
  },
  family: {
    badgeKey: 'gameCard.status.family',
    titleKey: 'gameCard.status.familyTitle',
    color: 'var(--purple)',
    border: 'var(--purple-a30)',
  },
  wishlist: {
    badgeKey: 'gameCard.status.wishlist',
    titleKey: 'gameCard.status.wishlistTitle',
    color: 'var(--accent)',
    border: 'var(--accent-a40)',
  },
}

const ownership = computed(() => ownershipStore.map[props.game.appid] ?? null)

/** 多账号共同愿望单 → 愿望单 +N（口径同展示文案）*/
const statusBadgeText = computed(() => {
  if (!ownership.value) return ''
  if (ownership.value.type === 'wishlist' && ownership.value.owners.length > 1) {
    return t('gameCard.status.wishlistMore', { n: ownership.value.owners.length - 1 })
  }
  return t(STATUS_META[ownership.value.type].badgeKey)
})

/** 卡片边框/底色随归属状态（.game-card.owned / .family / .wishlist）*/
const statusClass = computed(() => ownership.value?.type ?? null)

// ─── 关注星标（金色高亮 + 排序置顶的开关；状态存后端追踪池 manual 条目）───

const isFollowed = computed(() => followsStore.has(props.game.appid))

/** 列表模式且无折扣徽章时，星标自己承担「推到行尾」的 auto 边距：网格模式
 *  由 .title-tags 的 margin-left:auto 推，列表模式由折扣徽章的行内 auto 推，
 *  两者都不在（列表 + 未打折）时星标会贴着标题，补一个。 */
const starPush = computed(() => props.layoutMode === 'list' && !(props.game.discount > 0))

/** 切换关注。失败回滚后弹后端 detail（如未绑定 SteamID 的指引），
 *  无 detail（网络断等）回落通用词条。 */
async function onToggleFollow() {
  try {
    await followsStore.toggle(props.game.appid)
  } catch (e) {
    message.error(e instanceof Error && e.message ? e.message : t('gameCard.follow.fail'))
  }
}

/** GPW 弹窗刚被同一点击的 mousedown 关闭（onDocClick 先于 click 触发），本次点击不应进详情 */
let gpwJustClosedByDoc = false

/** 封面区点击 = 进入详情页；GPW 弹窗开着时首次点击仅关闭弹窗（防误触），不跳转 */
function onCoverClick() {
  const wasGpwOpen = showGpw.value
  if (wasGpwOpen) showGpw.value = false
  if (wasGpwOpen || gpwJustClosedByDoc) {
    gpwJustClosedByDoc = false
    return
  }
  router.push(`/game/${props.game.appid}`)
}

const statusTip = ref<{
  type: OwnershipType
  owners: string[]
  avatars: string[]
  left: number
  top: number
} | null>(null)
let statusTipTimer: number | null = null

function statusTipEnter(e: MouseEvent) {
  if (!ownership.value) return
  if (statusTipTimer !== null) {
    window.clearTimeout(statusTipTimer)
    statusTipTimer = null
  }
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  let left = rect.left
  if (left + 160 > window.innerWidth) left = window.innerWidth - 170
  const owners = ownership.value.owners
  statusTip.value = {
    type: ownership.value.type,
    owners: owners.length > 0 ? owners : [t('gameCard.owner.unknown')],
    // 与 owners 按下标对齐；后端未返 ownerAvatars（旧缓存）或越位时落空串=首字符占位
    avatars:
      owners.length > 0
        ? owners.map((_, i) => ownership.value?.ownerAvatars?.[i] || '')
        : [''],
    left: Math.max(8, left),
    top: rect.bottom + 6,
  }
}

function statusTipLeave() {
  if (statusTipTimer !== null) window.clearTimeout(statusTipTimer)
  statusTipTimer = window.setTimeout(() => {
    statusTip.value = null
    statusTipTimer = null
  }, 120)
}

/** 徽章悬停 tooltip（默认悬停跟随定位在徽章上方；below=true 落徽章下方——
 *  折扣徽章贴封面顶，上方放不下。文本为空（无数据可展示）不弹。 */
const hoveredBadge = ref<{ text: string; left: number; top: number; below?: boolean } | null>(null)

function badgeEnter(e: MouseEvent, text: string, below = false) {
  if (!text) return
  const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
  let left = rect.left
  if (left + 180 > window.innerWidth) left = window.innerWidth - 188
  hoveredBadge.value = {
    text,
    left: Math.max(8, left),
    top: below ? rect.bottom + 6 : rect.top,
    below,
  }
}
function badgeLeave() {
  hoveredBadge.value = null
}

// ─── 价格矩阵加工（价格矩阵三视角加工）───

interface RegionPrice {
  code: string
  name: string
  flag: string
  nativePrice: string
  cnyFen: number
  nativeCents: number
  locked: boolean
  /** cents=0 的免费态（f2p/限时赠送）：显示「免费」而非锁区/¥0.00 */
  free: boolean
  /** 爬过但未抓到价格（missing/blocked）：黄框「待更新」，区别于真锁区 */
  unavailable: boolean
}

/** priceMatrix 为区服键控对象；展示顺序 = 服务端下发区服顺序 */
const regionPrices = computed<RegionPrice[]>(() =>
  regionsStore.metas.map((region) => {
    const cell = props.game.priceMatrix[region.code]
    const unavailable =
      !cell && (props.game.unavailableRegions || []).includes(region.code.toUpperCase())
    const base = {
      code: region.code.toLowerCase(),
      name: region.name,
      flag: flagUrl(region.code),
      nativePrice: '-',
      cnyFen: 0,
      nativeCents: 0,
      locked: true,
      free: false,
      unavailable,
    }
    if (!cell) return base
    return {
      ...base,
      nativePrice: cell[0],
      cnyFen: cell[1],
      nativeCents: cell[2],
      // cnyFen=0 且 cents=0 = 免费态；cnyFen=0 但 cents>0 = 汇率缺失（沿用锁区显示）
      free: cell[2] === 0,
      locked: cell[1] === -1 || (cell[1] === 0 && cell[2] !== 0),
    }
  }),
)

const cnRegion = computed(() => regionPrices.value.find((r) => r.code === 'cn') ?? null)
const cnFree = computed(() => cnRegion.value?.free ?? false)

/** 「我」页启用的地区（未启用不进 GPW 与低价计算） */
function isEnabledCode(code: string): boolean {
  return props.enabledRegions === null || props.enabledRegions === undefined
    ? true
    : props.enabledRegions.includes(code)
}

const cnPriceFen = computed(() => props.game.basePriceFen ?? 0)

/** 列表模式价格列固定 70px（国旗+奖牌占位），超过 4 字的地区名走简写。
 *  简写后缀随语言（zh `区` / en 空串 = 不简写，交给 CSS 截断），见
 *  `api/regions.ts` 的 compactRegionName 与 common.regionSuffix 的注。 */
function priceRowRegionName(name: string): string {
  return props.layoutMode === 'list' ? compactRegionName(name, t('common.regionSuffix')) : name
}

const topRegions = computed(() => {
  const filtered = sortedRegions.value.filter(
    (p) => cnPriceFen.value === 0 || p.cnyFen < cnPriceFen.value,
  )
  if (props.layoutMode === 'list' && filtered.length > 0) {
    const minPrice = filtered[0]!.cnyFen
    const lowestList = filtered.filter((p) => p.cnyFen === minPrice)
    lowestList.sort((a, b) => a.code.localeCompare(b.code))
    return [lowestList[0]!]
  }
  return filtered.slice(0, 3)
})

const lowestPriceFen = computed(() =>
  sortedRegions.value.length > 0 ? sortedRegions.value[0]!.cnyFen : cnPriceFen.value,
)
const diffYuan = computed(() => {
  const diff = cnPriceFen.value - lowestPriceFen.value
  return diff > 0 ? Math.round(diff / 100) : 0
})

function ratingClass(rate: number | null): string {
  if (rate === null) return 'low'
  if (rate >= 80) return ''
  if (rate >= 60) return 'medium'
  return 'low'
}

/** 量级缩写格式化器缓存（按语言；Intl 构造开销远大于格式化本身）*/
const reviewFmt = new Map<string, Intl.NumberFormat>()

/**
 * 评测数展示。原按量级分三档（万+评测 / k+评测 / 评测），但量级本身是
 * **数字格式**而非文案：中文用「万」、英文用「K」，档位与除数都不同。写进
 * 词典会逼出「中文词混进英文」或按语言各存一条的重复词条，故用 Intl 的
 * compact 现取语言（zh → 1.2万 ／ en → 12K），文本只留 gameCard.reviewCount
 * 一条。`format.ts` 的 useLocaleFormat 没有 compact 档，就地缓存一个格式化器；
 * 语言在渲染期现读（localeStore.locale），切语言即重算。
 */
function formatReviews(count: number): string {
  let nf = reviewFmt.get(localeStore.locale)
  if (!nf) {
    nf = new Intl.NumberFormat(localeStore.locale, {
      notation: 'compact',
      maximumFractionDigits: 1,
    })
    reviewFmt.set(localeStore.locale, nf)
  }
  const compact = nf.format(count)
  // compact 没缩写（纯数字：中文万以下的档位）→ 退回带千分位的原样数字
  const n = /^\d+$/.test(compact) ? fmt.group(count) : `${compact}+`
  return t('gameCard.reviewCount', { n })
}

// ─── GPW 弹窗 ──

/** 弹窗量得宽度（未挂载时按 CSS 声明兜底：列表 360 / 柱状图 480）。
 *  写死 320 会误判：CSS 声明是 360，右侧明明放得下也会翻到左边。 */
function popoverWidth(): number {
  return gpwPopoverRef.value?.offsetWidth || (activeTab.value === 'chart' ? 480 : 360)
}

/**
 * 弹窗落位：网格模式**顶对齐卡片顶** + 贴卡片右侧（右侧放不下翻左侧）；
 * 列表模式吸附「全区价格」按键**左侧**（右缘距按键左缘 8px、顶随按键顶）——
 * 行高只有 60px 且通栏，贴卡片右侧必溢出→翻边→被夹到屏幕最左，弹窗与按键
 * 隔着整行内容。
 *
 * 竖向**不夹视口**——夹住的话「贴着卡片」只在卡片处于屏幕上中部时成立：卡片落到
 * 下半屏弹窗被上提一截，继续滚动又会被钉在屏幕边缘不动，用户看到的就是「滚轮一转，
 * 弹窗就不贴着卡片了」。锚点（卡片/按键）滚到哪弹窗跟到哪（超出视口的部分交给
 * 弹窗自身的 max-height 80vh + 内滚动），这才是「吸附」的完整语义。
 *
 * 坐标用文档坐标（rect + window.scrollY）：页面滚动发生在应用级滚动容器
 * `.view-container` 里、文档自身不滚，所以跟随必须靠自己重算（见 onViewportChange）。
 */
function computePopoverPos() {
  if (!cardRef.value) return
  const w = popoverWidth()
  if (props.layoutMode === 'list' && gpwBtnRef.value) {
    const btnRect = gpwBtnRef.value.getBoundingClientRect()
    let left = btnRect.left - w - 8
    if (left < window.scrollX + 8) left = window.scrollX + 8
    popoverPos.value = { top: btnRect.top + window.scrollY, left }
    return
  }
  const rect = cardRef.value.getBoundingClientRect()
  let left = rect.right + 12 + window.scrollX
  if (rect.right + 12 + w > window.innerWidth) {
    left = rect.left - w - 12 + window.scrollX
  }
  if (left < window.scrollX + 8) left = window.scrollX + 8
  popoverPos.value = { top: rect.top + window.scrollY, left }
}

/**
 * 跟住卡片：滚动（含内层滚动容器）与视口变化时重算位置。
 *
 * scroll 事件不冒泡，所以在 document 上**捕获**阶段监听——一处覆盖 window 滚动
 * 与任意内层滚动容器；弹窗自己内部的滚动（`.gpw-popover-body`）跳过，那种滚动
 * 卡片没动、重算纯属白干。用文档坐标落位的弹窗在内层容器滚动时不会自己跟着走，
 * 这就是「滚轮一转，弹窗就不贴着卡片了」的成因。
 */
function onViewportChange(e?: Event) {
  if (!showGpw.value || !cardRef.value) return
  if (e?.type === 'scroll' && e.target instanceof Node && gpwPopoverRef.value?.contains(e.target)) {
    return
  }
  computePopoverPos()
  computeGiftPopupPos()
}

function toggleGpw() {
  if (!showGpw.value) {
    computePopoverPos()
    loadCdkForVersion()
    loadBundles()
    loadGpwVersions()
    loadSeries()
    showGpw.value = true
    // 挂载后按真实尺寸再校一次（打开前量不到 DOM，只能按兜底尺寸估）
    void nextTick(() => {
      if (showGpw.value) computePopoverPos()
      watchPopoverResize()
    })
    return
  }
  giftRegion.value = null
  showGpw.value = false
}

/** 弹窗内容（CDK / 捆绑包 / 版本表 / 图片）异步到位会改变自身高度：满高后文档
 *  被撑出滚动条、视口窄 8px，锚点整体平移而弹窗没跟着走——落位是在内容到位前
 *  算的，差的就是这一截。尺寸一变即重算，弹窗才始终贴住锚点。 */
function watchPopoverResize() {
  popoverResizeObserver?.disconnect()
  const el = gpwPopoverRef.value
  if (!el) return
  popoverResizeObserver = new ResizeObserver(() => {
    if (showGpw.value) computePopoverPos()
  })
  popoverResizeObserver.observe(el)
}

watch(showGpw, (v) => {
  if (!v) popoverResizeObserver?.disconnect()
})

/** 列表 / 柱状图 tab 的宽度不同（360 / 480），切换后按新尺寸重排一次 */
watch(activeTab, () => {
  if (showGpw.value) void nextTick(computePopoverPos)
})

// ─── GPW 版本选择（同价格走势的 versionKey 语义）───
const gpwVersionKey = ref(0)
const gpwVersions = ref<GameVersionPrices[] | null>(null)
let gpwVersionsSeq = 0

/** 标准版（value 0）与各变体；标准版的多个 sub 代际不重复成行，见 lib/versions.ts */
const gpwVersionOptions = computed(() => versionSelectOptions(gpwVersions.value ?? [], t))

const gpwHasMultipleVersions = computed(() => selectableVariants(gpwVersions.value ?? []).length > 0)

const gpwSelectedVersion = computed(() => {
  if (gpwVersionKey.value === 0 || !gpwVersions.value) return null
  return gpwVersions.value.find((v) => v.subId === gpwVersionKey.value) ?? null
})

/** 版本感知的地区价格：选中具体版本时从版本数据取，否则回落标准版 `regionPrices` */
const gpwDisplayPrices = computed<RegionPrice[]>(() => {
  const ver = gpwSelectedVersion.value
  if (!ver) return regionPrices.value
  return regionsStore.metas.map((region) => {
    const vrp = ver.regions[region.code.toUpperCase()]
    const unavailable =
      !vrp && (props.game.unavailableRegions || []).includes(region.code.toUpperCase())
    const base = {
      code: region.code.toLowerCase(),
      name: region.name,
      flag: flagUrl(region.code),
      nativePrice: '-',
      cnyFen: 0,
      nativeCents: 0,
      locked: true,
      unavailable,
    }
    if (!vrp) return base
    return {
      ...base,
      nativePrice: vrp.formatted,
      cnyFen: vrp.cnyFen ?? 0,
      nativeCents: vrp.cents,
      locked: (vrp.cnyFen ?? 0) <= 0,
    }
  })
})

async function loadGpwVersions() {
  const seq = ++gpwVersionsSeq
  try {
    const res = await gamesApi.versions(props.game.appid)
    if (seq !== gpwVersionsSeq) return
    gpwVersions.value = res.versions
    // 如果当前选择的版本已不在列表 → 回落标准版
    if (gpwVersionKey.value !== 0 && !res.versions.some((v) => v.subId === gpwVersionKey.value)) {
      gpwVersionKey.value = 0
    }
  } catch {
    if (seq === gpwVersionsSeq) gpwVersions.value = null
  }
}

/** gpwRegions = 由版本数据决定展示内容（而非直取 regionPrices） */
const gpwRegions = computed(() => gpwDisplayPrices.value.filter((p) => isEnabledCode(p.code)))

const sortedRegions = computed(() =>
  gpwDisplayPrices.value
    .filter((p) => !p.locked && isEnabledCode(p.code))
    .sort((a, b) => a.cnyFen - b.cnyFen),
)

const chartRegions = computed(() =>
  gpwDisplayPrices.value
    .filter((p) => isEnabledCode(p.code))
    .sort((a, b) => {
      if (a.locked && !b.locked) return -1
      if (!a.locked && b.locked) return 1
      if (a.locked && b.locked) return 0
      return a.cnyFen - b.cnyFen
    }),
)

/** 切换版本 → 地区价格区块随 gpwDisplayPrices 联动，CDK 改取该版本的价格 */
watch(gpwVersionKey, (subId, prev) => {
  if (subId === prev || !showGpw.value) return
  loadCdkForVersion()
})

// ─── CDK 第三方平台 ───

const cdkLoading = ref(false)
const cdkSteampy = ref<CdkPlatform | null>(null)
const cdkCici = ref<CdkPlatform | null>(null)
/** 前端 per-version CDK 缓存（每个版本一分钟只请求一次外部接口） */
const CDK_CACHE_TTL = 60_000
const cdkVersionCache = new Map<string, { ts: number; steampy: CdkPlatform; cici: CdkPlatform }>()

/** 按当前选中版本查 CDK：标准版不传 subId（后端从库内解析），变体传各自 subId */
function loadCdkForVersion() {
  loadCdk(gpwVersionKey.value === 0 ? undefined : gpwVersionKey.value)
}

async function loadCdk(subId?: number) {
  const key = subId !== undefined ? String(subId) : 'default'
  const cached = cdkVersionCache.get(key)
  if (cached && Date.now() - cached.ts < CDK_CACHE_TTL) {
    cdkSteampy.value = cached.steampy
    cdkCici.value = cached.cici
    return
  }
  cdkLoading.value = true
  try {
    const res = await gamesApi.cdk(props.game.appid, subId)
    const plat = { steampy: res.steampy, cici: res.steamcici }
    cdkVersionCache.set(key, { ts: Date.now(), ...plat })
    cdkSteampy.value = res.steampy
    cdkCici.value = res.steamcici
  } catch {
    // 失败不清缓存：旧值还可展示
  } finally {
    cdkLoading.value = false
  }
}

// ─── 关联捆绑包（GPW 区块，打开时懒加载）───

const linkedBundles = ref<LinkedBundle[]>([])
const bundlesLoading = ref(false)
let bundlesFetched = false

async function loadBundles() {
  if (bundlesFetched) return
  bundlesFetched = true
  bundlesLoading.value = true
  try {
    const res = await gamesApi.bundles(props.game.appid)
    linkedBundles.value = res.bundles
  } catch {
    bundlesFetched = false
  } finally {
    bundlesLoading.value = false
  }
}

// ─── 同系列游戏（GPW 区块，打开时懒加载）───

const seriesInfo = ref<GameSeriesInfo | null>(null)
const seriesLoading = ref(false)
let seriesFetched = false

async function loadSeries() {
  if (seriesFetched) return
  seriesFetched = true
  seriesLoading.value = true
  try {
    seriesInfo.value = await gamesApi.seriesInfo(props.game.appid)
  } catch {
    // 404（未识别到系列）与其他失败同语义：不渲染区块
    seriesInfo.value = null
  } finally {
    seriesLoading.value = false
  }
}

/** 点系列成员：进详情页（GPW 弹窗随路由离开自然销毁） */
function openSeriesMember(appid: number) {
  if (appid === props.game.appid) return
  showGpw.value = false
  giftRegion.value = null
  router.push(`/game/${appid}`)
}

/** 系列成员按「本作在前 + 国区现价升序」排：同系列比价先看在售的 */
const seriesMembers = computed(() => {
  const members = seriesInfo.value?.members ?? []
  return [...members].sort((a, b) => {
    if (a.isSelf !== b.isSelf) return a.isSelf ? -1 : 1
    return (a.cnPriceFen ?? Number.MAX_SAFE_INTEGER) - (b.cnPriceFen ?? Number.MAX_SAFE_INTEGER)
  })
})

function lowestRegionName(code: string): string {
  return regionsStore.regionName(code)
}

/**
 * 换游戏时清掉**按游戏取数**的那几样：列表布局的卡片实例按 index 复用
 * （HlScrollList 的 `:key="index"`，排序/筛选后同一实例会挂到另一款游戏上），
 * 版本选择、CDK 结果与其临时缓存都只对上一款游戏成立——不清的话弹窗会先把
 * 上一款的挂牌价/版本当成这一款的显示出来。家庭组赠礼数据不在此列（成员与
 * 区服跟游戏无关，区价每次渲染现取）。
 */
watch(
  () => props.game.appid,
  () => {
    gpwVersionKey.value = 0
    gpwVersions.value = null
    cdkVersionCache.clear()
    cdkSteampy.value = null
    cdkCici.value = null
    bundlesFetched = false
    linkedBundles.value = []
    seriesFetched = false
    seriesInfo.value = null
    // 弹窗还开着（列表在弹窗下方重排）：立刻按新游戏重取，别停在空白/上一款的数据上
    if (showGpw.value) {
      loadCdkForVersion()
      loadBundles()
      loadGpwVersions()
      loadSeries()
    }
  },
)

/** 点击是否落在下拉选项弹层里。选项弹层由 HlSelect teleport 到 body，DOM 上
 *  不在弹窗内（版本下拉的选项全在那儿）；不放行的话，点选项会先被 onDocClick
 *  当成「外部点击」把整个弹窗关掉，选择永远落不了地。 */
function isSelectPopup(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest('[data-hl-select-pop]') !== null
}

function onDocClick(e: MouseEvent) {
  if (!showGpw.value) return
  if (
    gpwPopoverRef.value &&
    !gpwPopoverRef.value.contains(e.target as Node) &&
    !isSelectPopup(e.target) &&
    gpwBtnRef.value &&
    !gpwBtnRef.value.contains(e.target as Node)
  ) {
    if (giftPopupRef.value && giftPopupRef.value.contains(e.target as Node)) return
    showGpw.value = false
    giftRegion.value = null
    // 弹窗由本次 mousedown 关闭：随后到达封面的 click 不再触发进详情
    if (coverRef.value && coverRef.value.contains(e.target as Node)) gpwJustClosedByDoc = true
  }
  if (giftRegion.value && giftPopupRef.value && !giftPopupRef.value.contains(e.target as Node)) {
    giftRegion.value = null
  }
}

onMounted(() => {
  document.addEventListener('mousedown', onDocClick)
  // 捕获阶段监听：一处覆盖 window 滚动与任意内层滚动容器（scroll 不冒泡）
  document.addEventListener('scroll', onViewportChange, { capture: true, passive: true })
  window.addEventListener('resize', onViewportChange)
  ownershipStore.ensure(props.game.appid)
  followsStore.ensure()
})
onBeforeUnmount(() => {
  document.removeEventListener('mousedown', onDocClick)
  document.removeEventListener('scroll', onViewportChange, { capture: true })
  window.removeEventListener('resize', onViewportChange)
  popoverResizeObserver?.disconnect()
  if (coverTimer) window.clearTimeout(coverTimer)
  if (statusTipTimer !== null) window.clearTimeout(statusTipTimer)
})

// ─── 赠礼分析 ───

const giftRegionPrices = computed<RegionPriceInfo[]>(() =>
  gpwRegions.value.map((rp) => ({
    code: rp.code,
    nameZh: rp.name,
    cnyFen: rp.cnyFen,
    locked: rp.locked,
  })),
)

const giftAnalysis = computed<GiftingAnalysis | null>(() => {
  if (!giftRegion.value) return null
  return computeGiftingAnalysis(giftRegionPrices.value, giftRegion.value)
})

// ─── 好友个性化赠礼分析（家庭组成员，对齐元脚本「好友互动分析」）───
// 新政策双轨实付：收方区价 ≤ 主账号区价×1.15 → 付主账号区价；超出 → 付收方区价。

interface FriendGiftRow {
  steamid: string
  /** 成员显示名；空串 = 无名档，展示文本走 memberName() 的兜底词条 */
  name: string
  avatarUrl: string
  region: string
  regionName: string
  /** 主账号区价（分）；null = 主账号区锁区/无价 */
  primaryFen: number | null
  /** 主账号送给 TA 的实付（分）；null = TA 区锁区/无价不可送 */
  sendToFen: number | null
  /** 主账号送 TA 是否走收方计价轨道（超出 ×1.15） */
  sendReceiverPriced: boolean
  /** TA 送给主账号的实付（分）；null = 主账号区锁区/无价不可送 */
  receiveFromFen: number | null
  /** TA 送主账号是否走收方计价轨道 */
  receiveReceiverPriced: boolean
}

const giftFriends = ref<FriendGiftRow[]>([])
let giftFriendsFetched = false

/**
 * 成员显示名：无名档用 steamid 尾号兜底。
 *
 * 兜底文本**在渲染期取**（t 在模板/computed 里现读语言），不写进 rows ——
 * rows 是一次拉取的产物、常驻到下次拉取，中途切语言不会重算，写进去等于
 * 把语言冻在打开弹窗那一刻（同 STATUS_META 的模块级常量坑）。
 */
function memberName(row: FriendGiftRow): string {
  return row.name || t('gameCard.gift.member', { id: row.steamid.slice(-4) })
}

function friendRegionFen(code: string): number | null {
  const rp = regionPrices.value.find((r) => r.code === code)
  if (!rp || rp.locked || rp.cnyFen <= 0) return null
  return rp.cnyFen
}

/** 家庭组快照（成员 + 各自区服 + 主账号钱包区）——赠礼弹窗首次打开时拉一次 */
async function loadGiftFriends() {
  if (giftFriendsFetched) return
  giftFriendsFetched = true
  try {
    const st = await familyApi.status()
    if (!st.joined || !st.members?.length) {
      giftFriends.value = []
      return
    }
    const saved = st.memberRegions ?? {}
    const walletRegion = st.walletRegion || ''
    const rows: FriendGiftRow[] = st.members.map((m, i) => {
      // 区服优先级对齐 family/Index.vue：主账号钱包判定 > 手动选择 > 默认 cn
      const isPrimary = (st.steamid && m.steamid === st.steamid) || i === 0
      const region = (isPrimary && walletRegion) || saved[m.steamid] || 'cn'
      return {
        steamid: m.steamid,
        name: m.personaName || '',
        avatarUrl: normalizeAvatarUrl(m.avatarUrl),
        region,
        regionName: regionsStore.regionName(region),
        primaryFen: null,
        sendToFen: null,
        sendReceiverPriced: false,
        receiveFromFen: null,
        receiveReceiverPriced: false,
      }
    })
    const primaryRow = rows.find((r) => r.steamid === st.steamid) ?? rows[0]!
    giftPrimarySteamid.value = primaryRow.steamid
    const primaryFen = friendRegionFen(primaryRow.region)
    for (const r of rows) {
      r.primaryFen = primaryFen
      if (r.steamid === primaryRow.steamid) continue
      // 主账号 → 成员：实付双轨
      const memberFen = friendRegionFen(r.region)
      r.sendToFen = giftPayAmountFen(primaryFen, memberFen)
      r.sendReceiverPriced = isReceiverPriced(primaryFen, memberFen)
      // 成员 → 主账号：实付双轨
      r.receiveFromFen = giftPayAmountFen(memberFen, primaryFen)
      r.receiveReceiverPriced = isReceiverPriced(memberFen, primaryFen)
    }
    giftFriends.value = rows
  } catch {
    giftFriendsFetched = false
  }
}

/** 主账号 SteamID（loadGiftFriends 解析后回填，弹窗列表头用） */
const giftPrimarySteamid = ref('')

/** 主账号（发送方视角基准） */
const giftPrimaryRow = computed(() => {
  const rows = giftFriends.value
  return rows.length ? rows.find((r) => r.steamid === giftPrimarySteamid.value) ?? rows[0] : null
})

/** 主账号可送出的成员列表（按实付升序，无法计算排尾） */
const giftFriendSendRows = computed(() =>
  giftFriends.value
    .filter((r) => r.sendToFen !== null)
    .sort((a, b) => (a.sendToFen ?? 0) - (b.sendToFen ?? 0)),
)

/** 可送主账号礼的成员列表（按 TA 实付升序） */
const giftFriendReceiveRows = computed(() =>
  giftFriends.value
    .filter((r) => r.receiveFromFen !== null)
    .sort((a, b) => (a.receiveFromFen ?? 0) - (b.receiveFromFen ?? 0)),
)

/** 赠礼弹窗落位：贴被点击的地区行右侧、顶对齐该行（口径同主弹窗：不夹视口，随滚动重算）。 */
function computeGiftPopupPos() {
  const anchor = giftAnchorRef.value
  if (!giftRegion.value || !anchor?.isConnected) {
    // 锚点行没了（换版本把行重挂了）：与其让它漂在旧位置，不如关掉
    giftRegion.value = null
    return
  }
  const rect = anchor.getBoundingClientRect()
  const popupW = giftPopupRef.value?.offsetWidth || 380
  let left = rect.right + 8 + window.scrollX
  if (rect.right + 8 + popupW > window.innerWidth) {
    left = rect.left - popupW - 8 + window.scrollX
  }
  if (left < window.scrollX + 8) left = window.scrollX + 8
  giftPopupPos.value = { top: rect.top + window.scrollY, left }
}

function handleRegionClick(regionCode: string, e: MouseEvent) {
  e.stopPropagation()
  if (giftRegion.value === regionCode) {
    giftRegion.value = null
    return
  }
  void loadGiftFriends()
  giftAnchorRef.value = e.currentTarget as HTMLElement
  giftRegion.value = regionCode
  computeGiftPopupPos()
}

function priceClass(rec: RegionPrice): string {
  if (rec.locked) return 'same'
  return rec.cnyFen < cnPriceFen.value
    ? 'cheaper'
    : rec.cnyFen > cnPriceFen.value
      ? 'expensive'
      : 'same'
}

// ─── GPW 图表 tab 数据（纯 CSS 柱状图，口径同 bc-bar 柱状图）───

const validMax = computed(() => {
  const valid = regionPrices.value.filter((p) => !p.locked).map((p) => p.cnyFen)
  return valid.length > 0 ? Math.max(...valid) : 1
})

const TROPHIES = ['/assets/trophy_gold.png', '/assets/trophy_silver.png', '/assets/trophy_copper.png']
</script>

<template>
  <div
    ref="cardRef"
    class="game-card"
    :class="[statusClass, { 'list-layout': layoutMode === 'list', favorite: isFollowed }]"
  >
    <!-- 封面（点击进入详情；加载失败随机延时重试，全部失败显示占位） -->
    <div ref="coverRef" class="cover-wrapper" @click="onCoverClick">
      <span v-if="coverBroken" class="cover-placeholder" style="display: block">🎮</span>
      <img
        v-else
        :src="coverSrc"
        :alt="game.name"
        class="cover"
        loading="lazy"
        @error="onCoverError"
        @load="onCoverLoad"
      />

      <!-- 左上角归属状态徽章（已拥有/家庭共享/愿望单，悬停弹归属账号） -->
      <div
        v-if="ownership"
        class="status-badge"
        :class="ownership.type"
        @mouseenter="statusTipEnter"
        @mouseleave="statusTipLeave"
      >
        {{ statusBadgeText }}<span class="sb-q">?</span>
      </div>

      <!-- 折扣 & 史低标签（仅网格模式，列表模式进标题行）；悬停弹折扣截止 -->
      <div
        v-if="game.discount > 0 && layoutMode !== 'list'"
        class="discount-badges-container"
      >
        <div
          class="discount-badge"
          :class="`hl-type-${hlType}`"
          @mouseenter="badgeEnter($event, discountEndsTip, true)"
          @mouseleave="badgeLeave"
        >
          <span>-{{ game.discount }}%</span>
          <span class="db-text">{{ hlText }}</span>
        </div>
      </div>

      <!-- 下架角标（封面左下，判定 removedAt 非空；网格/列表两模式） -->
      <div v-if="isRemoved" class="bottom-left-badges">
        <div
          class="removed-badge"
          @mouseenter="badgeEnter($event, removedText)"
          @mouseleave="badgeLeave"
        >
          {{ t('gameCard.removed.tag') }}
        </div>
      </div>

      <!-- EPIC/HB 徽章（封面左下，仅网格模式） -->
      <div
        v-if="(game.isEpic || game.isHb) && layoutMode !== 'list'"
        class="bottom-left-badges grid-only"
      >
        <div
          v-if="game.isEpic"
          class="epic-badge"
          @mouseenter="badgeEnter($event, epicText)"
          @mouseleave="badgeLeave"
        >
          <span class="epic-icon"></span>
          {{ epicText }}
        </div>
        <div
          v-if="game.isHb"
          class="hb-badge"
          @mouseenter="badgeEnter($event, hbText)"
          @mouseleave="badgeLeave"
        >
          <span class="hb-icon"></span>
          {{ hbText }}
        </div>
      </div>

      <!-- AppID 徽标 = SteamDB 外链（桌面端 target=_blank 由 pywebview 转交系统浏览器） -->
      <a
        class="appid-badge"
        :href="`https://steamdb.info/app/${game.appid}/`"
        target="_blank"
        rel="noreferrer"
        @click.stop
      >{{ game.appid }}</a>
    </div>

    <!-- 信息区 -->
    <div class="info">
      <div class="title-row">
        <div class="title-interactive-wrap">
          <router-link :to="`/game/${game.appid}`" @click.stop>
            <h3 class="game-title">{{ game.name }}</h3>
          </router-link>
        </div>

        <!-- 列表模式：折扣标签进标题行右侧 -->
        <div
          v-if="layoutMode === 'list' && game.discount > 0"
          style="display: flex; gap: 4px; align-items: center; margin-left: auto; padding-left: 8px; flex-shrink: 0"
        >
          <div
            class="discount-badge"
            :class="`hl-type-${hlType}`"
            @mouseenter="badgeEnter($event, discountEndsTip, true)"
            @mouseleave="badgeLeave"
          >
            <span>-{{ game.discount }}%</span>
            <span class="db-text">{{ hlText }}</span>
          </div>
        </div>

        <!-- 网格模式：XGP/家庭共享/卡牌 属性 tags -->
        <div v-if="layoutMode !== 'list'" class="title-tags" style="flex-shrink: 0">
          <span
            v-if="game.xgpTier"
            class="tag xgp"
            @mouseenter="badgeEnter($event, `Xbox Game Pass: ${game.xgpTier}`)"
            @mouseleave="badgeLeave"
          >
            <span class="tag-icon"></span>
          </span>
          <span
            v-if="game.familySharing"
            class="tag family-share"
            @mouseenter="badgeEnter($event, t('gameCard.tag.familySharing'))"
            @mouseleave="badgeLeave"
          >
            <span class="tag-icon"></span>
          </span>
          <span
            v-if="game.tradingCards"
            class="tag trading-card"
            @mouseenter="badgeEnter($event, t('gameCard.tag.tradingCards'))"
            @mouseleave="badgeLeave"
          >
            <span class="tag-icon"></span>
          </span>
        </div>

        <!-- 关注星标（☆/★；金色高亮 = .game-card.favorite，置顶在服务端排序完成） -->
        <button
          class="star-btn"
          :class="{ active: isFollowed, 'star-push': starPush }"
          :title="t('gameCard.follow.tip')"
          @click.stop="onToggleFollow"
        >
          <span class="star-empty">☆</span>
          <span class="star-filled">★</span>
        </button>
      </div>

      <div class="tags-row">
        <div class="ratings-group" style="display: flex; gap: 6px; align-items: center; flex-wrap: wrap">
          <!-- 列表模式：属性 tags + EPIC/HB logo 收进评分组 -->
          <template v-if="layoutMode === 'list'">
            <span
              v-if="game.xgpTier"
              class="tag xgp"
              @mouseenter="badgeEnter($event, `Xbox Game Pass: ${game.xgpTier}`)"
              @mouseleave="badgeLeave"
            >
              <span class="tag-icon"></span>
            </span>
            <span
              v-if="game.familySharing"
              class="tag family-share"
              @mouseenter="badgeEnter($event, t('gameCard.tag.familySharing'))"
              @mouseleave="badgeLeave"
            >
              <span class="tag-icon"></span>
            </span>
            <span
              v-if="game.tradingCards"
              class="tag trading-card"
              @mouseenter="badgeEnter($event, t('gameCard.tag.tradingCards'))"
              @mouseleave="badgeLeave"
            >
              <span class="tag-icon"></span>
            </span>
            <div v-if="game.isEpic || game.isHb" class="list-only-badges" style="display: flex; gap: 4px">
              <span
                v-if="game.isEpic"
                class="tag epic-logo"
                style="padding: 0 4px; cursor: pointer"
                @mouseenter="badgeEnter($event, epicText)"
                @mouseleave="badgeLeave"
              >
                <span
                  class="epic-icon"
                  style="margin: 0; display: inline-block; width: 14px; height: 14px; background-image: var(--epic-icon); background-size: contain; background-repeat: no-repeat; background-position: center"
                ></span>
              </span>
              <span
                v-if="game.isHb"
                class="tag hb-logo"
                style="padding: 0 4px; cursor: pointer"
                @mouseenter="badgeEnter($event, hbText)"
                @mouseleave="badgeLeave"
              >
                <span
                  class="hb-icon"
                  style="margin: 0; display: inline-block; width: 14px; height: 14px; background-image: var(--hb-icon); background-size: contain; background-repeat: no-repeat; background-position: center"
                ></span>
              </span>
            </div>
          </template>

          <!-- 永降只在不在打折且在 14 天时效窗内展示：打折中价格下降的原因是
               促销；变化发生 14 天后徽章自动隐藏（isPermChangeRecent） -->
          <span
            v-if="game.ppFlag === 1 && game.discount === 0 && isPermChangeRecent(game.ppChangedAt)"
            class="tag pp-cut"
            @mouseenter="badgeEnter($event, t('gameCard.tag.ppCutTip'))"
            @mouseleave="badgeLeave"
          >
            {{ t('gameCard.tag.ppCut') }}
          </span>
          <span class="tag rating" :class="ratingClass(game.positiveRate)">
            {{
              game.positiveRate !== null
                ? t('gameCard.rating.positive', { rate: `${game.positiveRate.toFixed(1)}%` })
                : t('gameCard.rating.none')
            }}
          </span>
          <span class="tag reviews">{{ formatReviews(game.reviewCount) }}</span>
        </div>

        <a
          class="steam-link"
          :href="`https://store.steampowered.com/app/${game.appid}/`"
          target="_blank"
          rel="noreferrer"
          @click.stop
        >
          <span class="steam-icon"></span>
          {{ t('gameCard.steamLink') }}
        </a>
      </div>

      <!-- 价格区 -->
      <div class="price-section">
        <div class="price-row">
          <span class="price-label">
            <img :src="flagUrl('cn')" class="flag-icon" alt="CN" />
            {{ t('gameCard.price.cn') }}
          </span>
          <span class="price-value cn" :class="{ 'is-free': cnFree }">
            {{ cnFree ? t('gameCard.price.free') : regionPrices.find((r) => r.code === 'cn')?.locked ? '—' : formatCnyText(cnPriceFen) }}
          </span>
        </div>

        <div v-for="(rec, i) in topRegions" :key="rec.code" class="price-row">
          <span class="price-label">
            <img :src="rec.flag" class="flag-icon" :alt="rec.code" />
            <span :title="rec.name">{{ priceRowRegionName(rec.name) }}</span>
            <img
              v-if="i < 3"
              :src="TROPHIES[i]"
              class="bc-medal"
              :class="`medal-${i}`"
              alt="medal"
              style="position: static; width: 14px; height: 14px; margin-left: 4px"
            />
          </span>
          <span class="price-value lowest">¥{{ (rec.cnyFen / 100).toFixed(2) }}</span>
        </div>

        <div v-if="topRegions.length === 0" class="price-row">
          <span class="price-label">
            <img :src="flagUrl('cn')" class="flag-icon" alt="CN" />
            {{ t('gameCard.price.cnLowest') }}
          </span>
          <span v-if="cnFree" class="price-value lowest is-free">{{ t('gameCard.price.free') }}</span>
          <span v-else class="price-value lowest">¥{{ (cnPriceFen / 100).toFixed(2) }}</span>
        </div>

        <div class="price-row">
          <span class="price-label">{{ t('gameCard.price.diff') }}</span>
          <span v-if="diffYuan > 0" class="diff-badge positive">
            {{ t('gameCard.price.save', { amount: diffYuan }) }}
          </span>
          <span v-else class="diff-badge">{{ t('gameCard.price.noDiff') }}</span>
        </div>

        <div v-if="game.priceData" class="price-row">
          <span class="price-label">{{ t('gameCard.priceData.label') }}</span>
          <span
            class="price-data"
            :title="priceDataTip"
            :class="[
              priceView.freshness ? `is-${priceView.freshness}` : '',
              priceView.coverage?.partial ? 'is-partial' : '',
            ]"
          >
            <span>{{ priceAgeText }}</span>
            <span v-if="coverageText" class="coverage">{{ coverageText }}</span>
          </span>
        </div>
      </div>

      <!-- 卡片操作行：全区价格 + 价格走势入口 -->
      <div class="card-action-row">
        <button ref="gpwBtnRef" class="details-btn" :class="{ open: showGpw }" @click="toggleGpw">
          {{ t('gameCard.regionPrice.title') }}
          <span class="arrow">▲</span>
        </button>
        <button
          class="details-btn trend-btn"
          :title="t('gameCard.trend.tip')"
          @click="trendStore.toggle(game.appid)"
        >
          📈 {{ t('gameCard.trend.label') }}
        </button>
      </div>
    </div>

    <!-- 徽章 tooltip（悬停跟随定位） -->
    <Teleport to="body">
      <!-- 徽章浮字。原为十属性行内 style，其中 background 是深色 --surface-float 的取值、
           color 是 #fff、border 是深色 --row-border —— 浅色主题下白底浮窗配白字。
           移入 .cf-badge-tip 走令牌，留行内只做定位。below 态落徽章下方，transform 复位。 -->
      <div
        v-if="hoveredBadge"
        class="cf-badge-tip"
        :class="{ below: hoveredBadge.below }"
        :style="{
          left: hoveredBadge.left + 'px',
          top: (hoveredBadge.below ? hoveredBadge.top : hoveredBadge.top - 4) + 'px',
        }"
      >
        {{ hoveredBadge.text }}
      </div>

      <!-- 游戏归属弹窗：归属账号 / 家庭共享来源 / 愿望单所属 -->
      <div
        v-if="statusTip"
        class="cf-status-tip"
        :style="{ left: statusTip.left + 'px', top: statusTip.top + 'px', borderColor: STATUS_META[statusTip.type].border }"
      >
        <div
          class="cf-status-tip__title"
          :style="{ color: STATUS_META[statusTip.type].color }"
        >
          {{ t(STATUS_META[statusTip.type].titleKey) }}
        </div>
        <div class="cf-status-tip__list">
          <div
            v-for="(name, i) in statusTip.owners"
            :key="`${name}-${i}`"
            class="cf-status-tip__row"
          >
            <!-- 头像空缺时兜底首字符 = 显示名首字。字母源自**后端下发的主账号显示名**
                 （wishlist/service.py 的 display()，主账号是「我」），不是组件文案，
                 不能也不该在这里 t()——经 HlAvatar 的 name prop 现取。 -->
            <HlAvatar :src="statusTip.avatars[i]" :name="name" size="sm" square />
            <span class="cf-status-tip__name">{{ name }}</span>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- GPW 弹窗（列表 + 柱状图）-->
    <Teleport to="body">
      <div
        v-if="showGpw"
        ref="gpwPopoverRef"
        class="gpw-popover"
        :class="{ 'chart-mode': activeTab === 'chart' }"
        :style="popoverPos ? { top: popoverPos.top + 'px', left: popoverPos.left + 'px' } : undefined"
      >
        <div class="gpw-popover-header">
          <HlImg :src="game.headerImage" alt="cover" class="gpw-popover-cover" />
          <span class="gpw-title">{{ game.name }}</span>
          <button class="gpw-popover-close" @click="showGpw = false">✕</button>
        </div>

        <div class="gpw-popover-body">
          <div class="gpw-popover-subhead">
            <span class="subhead-title">{{ t('gameCard.regionPrice.title') }}</span>
            <div class="gpw-subhead-controls">
              <HlSelect
                v-if="gpwHasMultipleVersions"
                v-model="gpwVersionKey"
                :options="gpwVersionOptions"
                class="gpw-version-select"
              />
              <div class="gpw-popover-tabs">
                <button
                  class="gpw-tab-btn"
                  :class="{ active: activeTab === 'list' }"
                  :title="t('gameCard.tabs.list')"
                  @click="activeTab = 'list'"
                >
                  <svg viewBox="0 0 24 24"><path d="M3 4h18v2H3V4zm0 7h18v2H3v-2zm0 7h18v2H3v-2z" /></svg>
                </button>
                <button
                  class="gpw-tab-btn"
                  :class="{ active: activeTab === 'chart' }"
                  :title="t('gameCard.tabs.chart')"
                  @click="activeTab = 'chart'"
                >
                  <svg viewBox="0 0 24 24"><path d="M5 19h14v2H5v-2zm10-14h2v12h-2V5zm-4 4h2v8h-2V9zm-4 4h2v4H7v-4z" /></svg>
                </button>
              </div>
            </div>
          </div>

          <!-- 列表 tab -->
          <div v-if="activeTab === 'list'" class="gpw-view active details-grid">
            <div
              v-for="rec in gpwRegions"
              :key="rec.code"
              class="detail-item"
              :class="{
                'cn-region': rec.code === 'cn',
                'lowest-region': sortedRegions.length > 0 && rec.code === sortedRegions[0]?.code,
                'unavailable-region': rec.unavailable,
              }"
              style="cursor: pointer"
              :title="
                rec.unavailable
                  ? t('gameCard.region.unavailableTip')
                  : t('gameCard.region.clickGiftTip')
              "
              @click="handleRegionClick(rec.code, $event)"
            >
              <img :src="rec.flag" class="flag" :alt="rec.code" />
              <span class="name">{{ rec.name }}</span>
              <span v-if="rec.locked" class="locked">{{
                rec.unavailable ? t('gameCard.region.pending') : t('gameCard.region.locked')
              }}</span>
              <span v-else-if="rec.free" class="free-tag">{{ t('gameCard.price.free') }}</span>
              <div v-else class="prices">
                <span class="orig">{{ rec.nativePrice }}</span>
                <span class="cny" :class="priceClass(rec)">¥{{ (rec.cnyFen / 100).toFixed(2) }}</span>
              </div>
            </div>
          </div>

          <!-- 柱状图 tab -->
          <div v-else class="gpw-view active bc-chart-area">
            <div
              v-for="rec in chartRegions"
              :key="rec.code"
              class="bc-bar-group"
              :class="{ locked: rec.locked, 'unavailable-region': rec.unavailable }"
              :title="
                rec.unavailable ? t('gameCard.region.unavailableTip') : undefined
              "
            >
              <div class="bc-bar-value">{{
                rec.unavailable ? t('gameCard.region.pending') : `¥${(rec.cnyFen / 100).toFixed(0)}`
              }}</div>
              <div
                class="bc-bar"
                :class="rec.cnyFen > cnPriceFen ? 'bc-bar-red' : 'bc-bar-green'"
                :style="{
                  height: rec.locked ? 0 : (rec.cnyFen / validMax) * 100 + '%',
                  background:
                    sortedRegions.length > 0 && rec.code === sortedRegions[0]?.code
                      ? 'linear-gradient(to top, var(--success-a50), var(--success))'
                      : undefined,
                  borderColor:
                    sortedRegions.length > 0 && rec.code === sortedRegions[0]?.code
                      ? 'var(--success)'
                      : undefined,
                }"
              />
              <div class="bc-bar-label">
                <img :src="rec.flag" class="flag" :alt="rec.code" />
                <span class="name">{{ rec.name }}</span>
              </div>
              <img
                v-if="sortedRegions.indexOf(rec) >= 0 && sortedRegions.indexOf(rec) < 3"
                :src="TROPHIES[sortedRegions.indexOf(rec)]"
                class="bc-medal"
                :class="`medal-${sortedRegions.indexOf(rec)}`"
                alt="medal"
              />
            </div>
          </div>

          <!-- 同系列游戏（服务端名称聚类，打开 GPW 时懒加载；未识别到系列不渲染） -->
          <div v-if="seriesInfo" class="series-section">
            <div class="series-head">
              <span class="series-title">🧬 {{ seriesInfo.seriesName }}</span>
              <span class="series-count">{{ t('gameCard.series.count', { n: seriesMembers.length }) }}</span>
            </div>
            <div class="series-list">
              <button
                v-for="m in seriesMembers"
                :key="m.appid"
                class="series-item"
                :class="{ self: m.isSelf }"
                :title="m.isSelf ? undefined : t('gameCard.series.openTip')"
                @click="openSeriesMember(m.appid)"
              >
                <HlImg :src="m.headerImage" :alt="m.name" class="series-cover" />
                <span class="series-name">{{ m.name }}</span>
                <span v-if="m.isSelf" class="series-self-chip">{{ t('gameCard.series.self') }}</span>
                <span class="series-prices">
                  <span v-if="m.cnPriceFen" class="series-cn">¥{{ (m.cnPriceFen / 100).toFixed(0) }}</span>
                  <span v-else class="series-noprice">-</span>
                  <span
                    v-if="m.savingsFen > 0"
                    class="series-save"
                  >{{ t('gameCard.price.save', { amount: (m.savingsFen / 100).toFixed(0) }) }}</span>
                </span>
              </button>
            </div>
          </div>

          <!-- 关联捆绑包（第三方平台之前） -->
          <div
            v-if="linkedBundles.length > 0"
            class="bundle-recommendation-section"
            style="margin-top: 12px; padding: 10px; background: var(--accent-a08); border-radius: 6px; border: 1px solid var(--accent-a15)"
          >
            <div
              style="font-size: 12px; font-weight: bold; color: var(--accent); margin-bottom: 8px; display: flex; align-items: center; gap: 4px"
            >
              📦 {{ t('gameCard.bundles.title', { n: linkedBundles.length }) }}
            </div>
            <a
              v-for="b in linkedBundles.slice(0, 3)"
              :key="b.bundleId"
              :href="b.url"
              target="_blank"
              rel="noopener noreferrer"
              class="bundle-rec-item"
              style="display: flex; gap: 8px; padding: 6px; border-radius: 4px; background: var(--surface-inset); margin-bottom: 4px; text-decoration: none; border: 1px solid var(--row-border); transition: border-color 0.2s"
            >
              <HlImg
                :src="b.headerImage"
                :alt="b.name"
                style="width: 60px; height: 28px; border-radius: 3px; object-fit: cover; flex-shrink: 0"
              />
              <div style="flex: 1; min-width: 0">
                <div
                  style="font-size: 11px; color: var(--text-primary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis"
                >
                  {{ b.name }}
                </div>
                <div style="display: flex; gap: 8px; align-items: center; margin-top: 2px">
                  <span
                    v-if="b.mustPurchaseAsSet === 0"
                    style="font-size: 9px; color: var(--success); background: var(--success-a15); padding: 1px 4px; border-radius: 2px"
                  >
                    {{ t('gameCard.bundles.completable') }}
                  </span>
                  <span v-if="b.priceCny !== null" style="font-size: 10px; color: var(--text-muted)">
                    {{ t('gameCard.bundles.cnPrice', { amount: (b.priceCny / 100).toFixed(0) }) }}
                  </span>
                  <span
                    v-if="b.lowestPriceFen !== null"
                    :style="{ fontSize: '10px', color: b.diffFen > 0 ? 'var(--success)' : 'var(--text-muted)' }"
                  >
                    {{ lowestRegionName(b.lowestRegion) }} ¥{{ (b.lowestPriceFen / 100).toFixed(0) }}
                  </span>
                  <span
                    v-if="b.diffFen > 0"
                    style="font-size: 10px; color: var(--success); font-weight: bold"
                  >
                    {{ t('gameCard.price.save', { amount: (b.diffFen / 100).toFixed(0) }) }}
                  </span>
                </div>
              </div>
            </a>
          </div>

          <!-- 第三方平台入口（带平台 logo）；标题行右侧挂进包史 chip（Barter.vg 计数） -->
          <div class="cdk-section-container">
            <div class="cdk-section-title">
              <span>🎰 {{ t('gameCard.cdk.title') }}</span>
              <span
                v-if="game.bundleCount"
                class="cdk-bundled-chip"
                @mouseenter="badgeEnter($event, t('gameCard.bundled.tip', { n: game.bundleCount }))"
                @mouseleave="badgeLeave"
              >📦 {{ t('gameCard.bundled.tag', { n: game.bundleCount }) }}</span>
            </div>
            <div class="cdk-buttons-grid">
              <a
                :href="cdkSteampy?.listed ? cdkSteampy.url : `https://steampy.com/game/${game.appid}`"
                target="_blank"
                rel="noopener noreferrer"
                class="cdk-btn"
              >
                <img
                  src="/assets/logo_steampy.png"
                  alt="SteamPY"
                  class="cdk-icon"
                  style="background: var(--py-icon); background-size: contain; background-repeat: no-repeat; background-position: center"
                  onerror="this.remove()"
                />
                SteamPY
                <span
                  class="cdk-status"
                  :style="cdkSteampy?.listed ? 'color: var(--success)' : ''"
                >
                  {{
                    cdkLoading
                      ? t('gameCard.cdk.loading')
                      : (cdkSteampy?.price ?? t('gameCard.cdk.notListed'))
                  }}
                </span>
              </a>
              <a
                :href="cdkCici?.url ?? 'https://www.steamcici.com/'"
                target="_blank"
                rel="noopener noreferrer"
                class="cdk-btn"
              >
                <img
                  src="/assets/logo_steamcici.jpg"
                  alt="SteamCICI"
                  class="cdk-icon"
                  style="background: var(--cici-icon); background-size: contain; background-repeat: no-repeat; background-position: center"
                  onerror="this.remove()"
                />
                SteamCICI
                <span
                  class="cdk-status"
                  :style="cdkCici?.listed ? 'color: var(--success)' : ''"
                >
                  {{
                    cdkLoading
                      ? t('gameCard.cdk.loading')
                      : (cdkCici?.price ?? t('gameCard.cdk.notListed'))
                  }}
                </span>
              </a>
            </div>
          </div>
        </div>
      </div>

      <!-- 赠礼分析弹窗 -->
      <div
        v-if="giftRegion && giftPopupPos && giftAnalysis"
        ref="giftPopupRef"
        class="gifting-popup"
        :style="{ top: giftPopupPos.top + 'px', left: giftPopupPos.left + 'px' }"
      >
        <div class="gifting-popup-header">
          <span class="gifting-popup-title">🎁 {{ t('gameCard.gift.title') }}</span>
          <button class="gifting-popup-close" @click="giftRegion = null">✕</button>
        </div>

        <div class="gifting-popup-target">
          <img
            :src="flagUrl(giftAnalysis.targetRegion.code)"
            style="width: 18px; height: 13px; border-radius: 2px"
            alt=""
          />
          <span class="gifting-target-name">{{ giftAnalysis.targetRegion.nameZh }}</span>
          <span class="gifting-target-price">¥{{ (giftAnalysis.targetRegion.cnyFen / 100).toFixed(2) }}</span>
          <span class="gifting-threshold">
            × 1.15 = ¥{{ ((giftAnalysis.targetRegion.cnyFen * 1.15) / 100).toFixed(2) }}
          </span>
        </div>

        <!-- 好友个性化分析（家庭组成员；双轨实付） -->
        <div v-if="giftFriends.length > 0 && giftPrimaryRow" class="gifting-popup-section">
          <div class="gifting-popup-subtitle">
            <!-- 整句（含姓名/区名/基准价三个插值）走一条词条：中英的语序与括号位置不同，
                 拆成片段拼不出英文。有价/无价是两条，不是「基准价」片段 + 两种尾巴。 -->
            👥
            {{
              giftPrimaryRow.primaryFen !== null
                ? t('gameCard.gift.friends', {
                    name: memberName(giftPrimaryRow),
                    region: giftPrimaryRow.regionName,
                    price: ((giftPrimaryRow.primaryFen ?? 0) / 100).toFixed(0),
                  })
                : t('gameCard.gift.friendsNoPrice', {
                    name: memberName(giftPrimaryRow),
                    region: giftPrimaryRow.regionName,
                  })
            }}
          </div>
          <div
            v-if="giftPrimaryRow.primaryFen === null"
            class="gifting-row"
            style="color: var(--text-muted)"
          >
            {{ t('gameCard.gift.noPrimaryPrice', { region: giftPrimaryRow.regionName }) }}
          </div>
          <div class="gifting-row">
            <span class="gift-status-green" style="min-width: 36px">{{
              t('gameCard.gift.sendLabel')
            }}</span>
            <div class="gifting-region-list gift-status-green">
              <span
                v-for="f in giftFriendSendRows"
                :key="f.steamid"
                class="gifting-region-item"
                :class="{ 'gift-status-yellow': f.sendReceiverPriced }"
                :title="
                  f.sendReceiverPriced
                    ? t('gameCard.gift.sendReceiverPriced', {
                        from: memberName(giftPrimaryRow),
                        to: memberName(f),
                        region: f.regionName,
                        amount: ((f.sendToFen ?? 0) / 100).toFixed(2),
                      })
                    : t('gameCard.gift.sendBase', {
                        from: memberName(giftPrimaryRow),
                        to: memberName(f),
                        amount: ((f.sendToFen ?? 0) / 100).toFixed(2),
                      })
                "
              >
                <HlImg
                  :src="f.avatarUrl"
                  style="width: 14px; height: 14px; border-radius: 3px; object-fit: cover"
                  alt=""
                />
                {{ memberName(f) }}
                <span style="font-size: 10px; color: var(--text-muted); margin-left: 2px">
                  ¥{{ ((f.sendToFen ?? 0) / 100).toFixed(0) }}
                </span>
                <span v-if="f.sendReceiverPriced" style="font-size: 9px">↗</span>
              </span>
              <span v-if="giftFriendSendRows.length === 0" style="color: var(--text-muted)">-</span>
            </div>
          </div>
          <div class="gifting-row">
            <span class="gift-status-green" style="min-width: 36px">{{
              t('gameCard.gift.receiveLabel')
            }}</span>
            <div class="gifting-region-list gift-status-green">
              <span
                v-for="f in giftFriendReceiveRows"
                :key="f.steamid"
                class="gifting-region-item"
                :class="{ 'gift-status-yellow': f.receiveReceiverPriced }"
                :title="
                  f.receiveReceiverPriced
                    ? t('gameCard.gift.receiveReceiverPriced', {
                        from: memberName(f),
                        to: memberName(giftPrimaryRow),
                        region: f.regionName,
                        amount: ((f.receiveFromFen ?? 0) / 100).toFixed(2),
                      })
                    : t('gameCard.gift.receiveBase', {
                        from: memberName(f),
                        to: memberName(giftPrimaryRow),
                        region: f.regionName,
                        amount: ((f.receiveFromFen ?? 0) / 100).toFixed(2),
                      })
                "
              >
                <HlImg
                  :src="f.avatarUrl"
                  style="width: 14px; height: 14px; border-radius: 3px; object-fit: cover"
                  alt=""
                />
                {{ memberName(f) }}
                <span style="font-size: 10px; color: var(--text-muted); margin-left: 2px">
                  ¥{{ ((f.receiveFromFen ?? 0) / 100).toFixed(0) }}
                </span>
                <span v-if="f.receiveReceiverPriced" style="font-size: 9px">↗</span>
              </span>
              <span v-if="giftFriendReceiveRows.length === 0" style="color: var(--text-muted)">-</span>
            </div>
          </div>
        </div>

        <div class="gifting-popup-section">
          <div class="gifting-popup-subtitle">🌍 {{ t('gameCard.gift.globalSend') }}</div>
          <div class="gifting-row">
            <span class="gift-status-green" style="min-width: 36px">{{
              t('gameCard.gift.canSendLabel')
            }}</span>
            <div class="gifting-region-list gift-status-green">
              <span
                v-for="r in giftAnalysis.canGiveTo"
                :key="r.code"
                class="gifting-region-item"
                :class="{ 'gift-status-yellow': r.receiverPriced }"
                :title="
                  r.receiverPriced
                    ? t('gameCard.gift.sendGlobalPriced', {
                        region: r.nameZh,
                        amount: (r.payFen / 100).toFixed(2),
                      })
                    : t('gameCard.gift.sendGlobalBase', {
                        region: r.nameZh,
                        amount: (r.payFen / 100).toFixed(2),
                      })
                "
              >
                <img
                  :src="flagUrl(r.code)"
                  style="width: 16px; height: 12px; border-radius: 2px; vertical-align: middle; margin-right: 4px"
                  alt=""
                />
                {{ r.nameZh }}
                <span style="font-size: 10px; color: var(--text-muted); margin-left: 2px">
                  ¥{{ (r.payFen / 100).toFixed(0) }}
                </span>
                <span v-if="r.receiverPriced" style="font-size: 9px">↗</span>
              </span>
              <span v-if="giftAnalysis.canGiveTo.length === 0" style="color: var(--text-muted)">-</span>
            </div>
          </div>
          <div class="gifting-row">
            <span class="gift-status-red" style="min-width: 36px">{{
              t('gameCard.gift.cannotSendLabel')
            }}</span>
            <div class="gifting-region-list gift-status-red">
              <span
                v-for="r in giftAnalysis.cannotGiveTo"
                :key="r.code"
                class="gifting-region-item"
              >
                <img
                  :src="flagUrl(r.code)"
                  style="width: 16px; height: 12px; border-radius: 2px; vertical-align: middle; margin-right: 4px"
                  alt=""
                />
                {{ r.locked ? t('gameCard.region.locked') : `¥${(r.cnyFen / 100).toFixed(0)}` }}
              </span>
              <span v-if="giftAnalysis.cannotGiveTo.length === 0" style="color: var(--text-muted)">-</span>
            </div>
          </div>
        </div>

        <div class="gifting-popup-section">
          <div class="gifting-popup-subtitle">🌍 {{ t('gameCard.gift.globalReceive') }}</div>
          <div class="gifting-row">
            <span class="gift-status-green" style="min-width: 36px">{{
              t('gameCard.gift.canReceiveLabel')
            }}</span>
            <div class="gifting-region-list gift-status-green">
              <span
                v-for="r in giftAnalysis.canReceiveFrom"
                :key="r.code"
                class="gifting-region-item"
                :class="{ 'gift-status-yellow': r.receiverPriced }"
                :title="
                  r.receiverPriced
                    ? t('gameCard.gift.receiveGlobalPriced', {
                        region: r.nameZh,
                        amount: (r.payFen / 100).toFixed(2),
                      })
                    : t('gameCard.gift.receiveGlobalBase', {
                        region: r.nameZh,
                        amount: (r.payFen / 100).toFixed(2),
                      })
                "
              >
                <img
                  :src="flagUrl(r.code)"
                  style="width: 16px; height: 12px; border-radius: 2px; vertical-align: middle; margin-right: 4px"
                  alt=""
                />
                {{ r.nameZh }}
                <span style="font-size: 10px; color: var(--text-muted); margin-left: 2px">
                  ¥{{ (r.payFen / 100).toFixed(0) }}
                </span>
                <span v-if="r.receiverPriced" style="font-size: 9px">↗</span>
              </span>
              <span v-if="giftAnalysis.canReceiveFrom.length === 0" style="color: var(--text-muted)">-</span>
            </div>
          </div>
        </div>

        <div class="gifting-popup-rule">
          {{ t('gameCard.gift.rule') }}
        </div>
      </div>
    </Teleport>

    <!-- 价格走势抽屉：外壳常驻（内部 v-if 控制挂载），否则外层 v-if 瞬间卸载会吞掉 HlDrawer 的滑出动画 -->
    <PriceTrendDrawer v-model="trendOpen" :game="game" />
  </div>
</template>
