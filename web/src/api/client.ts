/** API 客户端：统一请求封装 + 各资源 API。 */

import type { MessageKey } from '@/locales'

const BASE = '/api/v1'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// ─── GET 时间窗缓存 + inflight 去重 ──────────────────────
// 各视图 onMounted 无条件重拉是板块切换延迟的主因之一；这里在唯一请求
// 出口收口：切换往返（< TTL）直接复用上次响应，并发同 URL 共享同一请求。

const GET_CACHE_TTL = 60_000
const GET_CACHE_MAX = 128
/** Map 插入序即 LRU 序：命中时摘下重插，超限淘汰最老一条 */
const getCache = new Map<string, { data: unknown; ts: number }>()
const inflightGets = new Map<string, Promise<unknown>>()

/** 实时性敏感端点（轮询/进度/状态）：响应每次都可能变，不进时间窗缓存。
 * 匹配按路径段精确前缀（'/account' 不误伤 '/accounts'）。新增轮询端点
 * 要么登记在这里，要么调用 request 时显式传 { noCache: true }。 */
const NO_CACHE_PATHS = [
  '/account', // 顶栏每分钟轮转（在线状态/游戏中）
  '/proxies/stats', // 仪表盘 30s 轮询
  '/bills/sync', // 账单同步进行中的快照轮询
  '/crawl/active', // 任务状态（页内刷新）
  '/crawl/jobs',
  '/system/logs', // 用户点「刷新」要看新日志
  '/system/update-progress', // 更新下载进度 800ms 轮询
  '/system/update-pending',
  '/proxies/clash/install/progress',
]

function isNoCachePath(path: string): boolean {
  return NO_CACHE_PATHS.some((p) => path === p || path.startsWith(`${p}?`) || path.startsWith(`${p}/`))
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  opts?: { noCache?: boolean },
): Promise<T> {
  const cacheable = method === 'GET' && !opts?.noCache && !isNoCachePath(path)
  if (cacheable) {
    const hit = getCache.get(path)
    if (hit && Date.now() - hit.ts < GET_CACHE_TTL) {
      getCache.delete(path)
      getCache.set(path, hit)
      return hit.data as T
    }
    const running = inflightGets.get(path)
    if (running) return running as Promise<T>
  }

  const promise = (async (): Promise<T> => {
    const response = await fetch(`${BASE}${path}`, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
    if (!response.ok) {
      let detail = `HTTP ${response.status}`
      try {
        const data = await response.json()
        if (data?.detail) detail = String(data.detail)
      } catch {
        /* 忽略解析失败 */
      }
      throw new ApiError(response.status, detail)
    }
    return (await response.json()) as T
  })()

  if (cacheable) {
    inflightGets.set(path, promise)
    promise.then(
      (data) => {
        inflightGets.delete(path)
        getCache.delete(path)
        getCache.set(path, { data, ts: Date.now() })
        if (getCache.size > GET_CACHE_MAX) {
          const oldest = getCache.keys().next().value
          if (oldest !== undefined) getCache.delete(oldest)
        }
      },
      () => {
        // 失败不缓存，只摘 inflight 让下次重试
        inflightGets.delete(path)
      },
    )
  } else if (method !== 'GET') {
    // 写操作成功后全量失效：保守侧宁可多拉一次，不给页面留旧数据
    promise.then(() => getCache.clear(), () => {})
  }
  return promise
}

// ─── 类型 ────────────────────────────────────────────────

export interface RegionInfo {
  code: string
  name: string
  currency: string
}

export interface SettingsPayload {
  account: {
    steam_id: string
    steam_api_key: string
    has_api_key: boolean
  }
  /** 新手教程完成标志：false = 首次启动（自动弹教程） */
  onboarding_done: boolean
  /** 自动价格链总开关：false = 定时爬价与失败修复停转，只留手动爬取 */
  auto_price: boolean
  /** 已主动提示过的版本号：启动告知按「一次一版本」去重的锚点 */
  update_notified: string
}

// ─── regions（区服元数据单一来源：服务端下发，前端零硬编码）───

export interface RegionInfo {
  code: string
  name: string
  currency: string
  enabled: boolean
  sort: number
}

export const regionsApi = {
  list: () => request<{ regions: RegionInfo[]; ownedRegions: string[] | null }>(
    'GET',
    '/regions',
  ),
  setEnabled: (enabled: string[] | null) =>
    request<{ regions: RegionInfo[] }>('PUT', '/regions/enabled', { enabled }),
  // 已购游戏抓取区：null = 跟随启用集；否则为自定义子集
  setOwned: (regions: string[] | null) =>
    request<{ ownedRegions: string[] | null }>('PUT', '/regions/owned', { regions }),
}

// ─── settings ────────────────────────────────────────────

export const settingsApi = {
  get: () => request<SettingsPayload>('GET', '/settings'),
  update: (payload: {
    account?: { steam_id?: string; steam_api_key?: string }
    onboarding_done?: boolean
    auto_price?: boolean
    /** 记录已主动提示过的版本号（启动告知去重用） */
    update_notified?: string
  }) => request<SettingsPayload>('PUT', '/settings', payload),
}

// ─── account（Steam 账户绑定 / 钱包余额）────────────────────

export interface WalletSnapshot {
  balance: number
  balance_display: string
  currency_code: string
  currency_symbol: string
  currency_id: number
  region_code: string
  country_code: string
  checked_at: string | null
  check_ok: boolean
  error: string
}

export interface AccountProfile {
  persona_name: string
  avatar_url: string
  steam_id: string
  fetched_at: string | null
}

/** 多账号列表行（绑定顺序，第一个即主账号；不含 Cookie 明文） */
export interface SteamAccountItem {
  steam_id: string
  friend_code: string
  persona_name: string
  avatar_url: string
  is_active: boolean
  is_primary: boolean
  bound_at: string | null
  wallet: WalletSnapshot | null
  wallet_error: string
  wishlist_count: number
  game_count: number
  /** 30 分钟窗口内已用激活次数（后端进程计数） */
  redeem_used: number
  /** Steam 真实在线状态（每分钟轮转刷新） */
  is_online: boolean
  /** 正在玩的游戏名（在线且非空 = 游戏中） */
  in_game: string
}

export interface AccountStatus {
  has_cookie: boolean
  cookie_steam_id: string
  bound_steam_id: string
  mismatch: boolean
  profile: AccountProfile | null
  wallet: WalletSnapshot | null
  /** 最近一次同步的错误（空串 = 正常）*/
  sync_error?: string
  /** 换绑一致性提示（如 SteamID 不匹配警告）*/
  message?: string
  /** 多账号列表（active 的钱包在顶层 wallet；这里每个账号各自的钱包） */
  accounts: SteamAccountItem[]
  /** 主账号 SteamID64（第一个绑定的） */
  primary_steam_id: string
  /** 当前账号 Steam 真实在线状态（顶栏头像 dot 数据源） */
  is_online: boolean
  /** 当前账号正在玩的游戏名 */
  in_game: string
}

export const accountApi = {
  status: () => request<AccountStatus>('GET', '/account'),
  list: () => request<SteamAccountItem[]>('GET', '/account/list'),
  bindCookies: (cookies: string) =>
    request<AccountStatus>('PUT', '/account/cookies', { cookies }),
  setActive: (steamId: string) =>
    request<AccountStatus>('PUT', '/account/active', { steam_id: steamId }),
  removeAccount: (steamId: string) =>
    request<AccountStatus>('DELETE', `/account/cookies/${steamId}`),
  /** 解绑全部账号（清账号表；手填 SteamID64 / API Key 不动） */
  unbind: () => request<AccountStatus>('DELETE', '/account/cookies'),
  sync: () => request<AccountStatus>('POST', '/account/sync'),
}

// ─── family（Steam 家庭组发现 / 成员管理）─────────────────

export interface FamilyResolveResult {
  steamid: string
  kind: 'friend_code' | 'steamid64' | 'vanity'
  personaName: string
  avatarUrl: string
}

export interface FamilyMemberItem {
  steamid: string
  role: string
  personaName: string
  avatarUrl: string
}

export interface FamilyStatus {
  bound: boolean
  joined: boolean | null
  steamid?: string
  familyName?: string | null
  familyGroupid?: string | null
  members: FamilyMemberItem[]
  message?: string
  lastError?: string | null
  updatedAt?: string | null
  /** 主账号 Cookie 钱包派生的结算地区（null=无快照） */
  walletRegion?: string | null
  /** 成员手动选择的地区（持久化恢复现场） */
  memberRegions?: Record<string, string>
}

// ─── family/library（家庭共享库聚合：GetSharedLibraryApps + 成员已购/游玩）───

export interface FamilyLibMember {
  steamid: string
  role: string
  personaName: string
  avatarUrl: string
  ownedCount: number
}

export interface FamilyLibGame {
  appid: number
  name: string | null
  headerImage: string | null
  releaseDate: string | null
  genres: string | null
  cnPriceFen: number | null
  originalPriceFen: number | null
  discount: number
  owners: string[]
  ownerCount: number
  presence: number
  excluded: boolean
  inSharedLib: boolean
  /** 入库时间（rt_time_acquired，秒）——热力图/增长趋势/购买动态口径 */
  timeAcquired: number
  /** 最近入库者（owner_steamids.at(-1)，购买动态的购买者） */
  buyer: string | null
  playtimeMinutes: number
  lastPlayed: number
}

export interface FamilyMemberPlayEntry {
  appid: number
  minutes: number
  minutes2w: number
  last: number
}

export interface FamilyLibraryPayload {
  familyGroupid: string
  familyName: string | null
  members: FamilyLibMember[]
  games: FamilyLibGame[]
  memberPlay: Record<string, FamilyMemberPlayEntry[]>
  sharedCount: number
  /** 快照兜底数据（实时聚合失败/冷启动首开时为 true，游玩明细缺失） */
  fromSnapshot?: boolean
}

export interface FamilyWishlistItem {
  appid: number
  name: string | null
  headerImage: string | null
  genres: string | null
  releaseDate: string | null
  cnPriceFen: number | null
  discount: number
  wantCount: number
  members: string[]
  addedAt: string | null
}

export interface FamilyWishlistPayload {
  fallback: boolean
  familyName: string | null
  memberIds: string[]
  items: FamilyWishlistItem[]
}

export const familyApi = {
  resolve: (input: string) =>
    request<FamilyResolveResult>('GET', `/family/resolve${toQuery({ input })}`),
  sync: () =>
    request<{ joined: boolean; steamid?: string; familyName?: string | null; members: FamilyMemberItem[]; message?: string }>('POST', '/family/sync'),
  status: () => request<FamilyStatus>('GET', '/family/status'),
  saveMemberRegions: (regions: Record<string, string>) =>
    request<{ ok: boolean; memberRegions: Record<string, string> }>(
      'PUT',
      '/family/member-regions',
      { regions },
    ),
  library: () => request<FamilyLibraryPayload>('GET', '/family/library'),
  refreshLibrary: () => request<FamilyLibraryPayload>('POST', '/family/library/refresh'),
  wishlist: () => request<FamilyWishlistPayload>('GET', '/family/wishlist'),
}

// ─── games ───────────────────────────────────────────────

export interface GameListItem {
  appid: number
  name: string
  nameEn: string | null
  discount: number
  discountLabel: string
  positiveRate: number | null
  reviewCount: number
  releaseDate: string
  /** 国区折后现价分（历史名沿用：base=当前基础报价，非原价） */
  basePriceFen: number | null
  /** 国区原价（未折价分）；划线原价展示用 */
  cnOriginalFen: number | null
  lowestPriceFen: number | null
  savingsFen: number
  headerImage: string
  /** 区服键控价格矩阵：{"CN": [formatted, cnyFen, cents, discountPct], ...}，只含有价区 */
  priceMatrix: Record<string, [string, number, number, number]>
  /** 爬过但未抓到价格的区（大写码，missing/blocked）——黄框「待更新」依据 */
  unavailableRegions?: string[]
  hlFlag: number
  /** 0=无 1=永降 2=永涨（国区原价最近一次调价方向） */
  ppFlag: number
  /** 最近一次原价跳变时刻（ISO）；永降/永涨徽章 14 天时效判据 */
  ppChangedAt: string | null
  /** 库内最近变动时间（ISO，updated_at）；降价动态 feed 排序键 */
  updatedAt: string | null
  familySharing: boolean
  tradingCards: boolean
  xgpTier: string | null
  isHb: boolean
  isEpic: boolean
  epicDate: string | null
  hbData: string | null
  /** 第三方渠道 bundle 计数（Barter.vg 档案；null = 未拉取，不展示） */
  bundleCount: number | null
  /** 下架监控：非空 = 已判定下架（ISO）；null = 在售 */
  removedAt: string | null
}

export interface GamesListPayload {
  items: GameListItem[]
  total: number
  hasMore: boolean
  nextCursor: string | null
}

export interface GameVersion {
  /** 标准版无后缀（history 的 versions 里 null = 标准版） */
  suffix: string | null
  isGold: boolean
  subId: number | null
}

/** 截至某日的价格上下文条目（单查与批量共用）；无有效快照时两值均 null */
export interface GamePriceContextItem {
  appid: number
  date: string
  at: { cnyFen: number; discount: number; snapshotAt: string | null } | null
  lowest: { cnyFen: number; discount: number; snapshotAt: string | null } | null
}

export interface GamePriceContext extends GamePriceContextItem {
  region: string
}

export interface LinkedBundle {
  bundleId: number
  name: string
  headerImage: string
  url: string
  /** 0=可补齐 1=必须整包 -1=未知（购买语义） */
  mustPurchaseAsSet: number
  /** 链接/CDN 形态：0=bundle 1=sub（与购买语义解耦） */
  itemKind: number
  priceCny: number | null
  lowestRegion: string
  lowestPriceFen: number | null
  diffFen: number
}

/** 捆绑包单区价格行（区键大写，来自 /bundles 列表聚合） */
export interface BundleRegionPrice {
  priceMinor: number | null
  currency: string | null
  discountPercent: number
  /** 整包基础折扣 %（补齐时额外优惠） */
  baseDiscount: number
  cnyFen: number | null
  /** 该区实际包含的 AppID（锁区检测依据） */
  appIds: number[]
  /** 相对基准区缺失的游戏数（部分锁区） */
  lockedCount: number
  /** 服务端按 minor units + 币种格式化的展示字符串 */
  formatted: string
}

export interface BundleSummary {
  bundleId: number
  name: string
  headerImage: string
  url: string
  mustPurchaseAsSet: number
  /** 链接/CDN 形态：0=bundle 1=sub（与购买语义解耦） */
  itemKind: number
  /** 基准区 appids（取 app_ids 最多的区） */
  appIds: number[]
  regionPrices: Record<string, BundleRegionPrice>
  cnCnyFen: number | null
  lowestRegion: string
  lowestCnyFen: number | null
  diffFen: number
}

export interface BundleGamePrice {
  priceMinor: number | null
  currency: string | null
  cnyFen: number | null
}

export interface BundleGame {
  appid: number
  /** games 表无行时为 null（前端回退 AppID 展示 + 计算器按无数据自动排除） */
  name: string | null
  headerImage: string
  prices: Record<string, BundleGamePrice>
}

export interface BundleDetail extends BundleSummary {
  games: BundleGame[]
}

export interface GameDetail extends GameListItem {
  type: string
  storeUrl: string
  chineseSupport: string | null
  genres: string | null
  developers: string[]
  publishers: string[]
  positiveReviews: number
  cnPriceCents: number | null
  cnCnyFen: number | null
  cnDiscount: number
  lowestRegionCode: string
  isAdult: boolean
  isVisualNovel: boolean
  seriesId: string | null
  versions: GameVersion[]
  linkedBundles: LinkedBundle[]
  viewCount: number
}

export interface HistoryPoint {
  timestamp: string | null
  cnyFen: number
  cnyYuan: number | null
  originalCents: number | null
  formattedPrice: string
  discount: number
  currency: string
}

export interface HistoryPayload {
  region: string
  points: HistoryPoint[]
  lowest: { cnyFen: number; cnyYuan: number; formattedPrice?: string; timestamp?: string | null } | null
  highest: { cnyFen: number; cnyYuan: number } | null
  count?: number
  /** 价格追平/跌破史低的事件数（同一促销期内连续快照只计一次） */
  lowestHits?: number
  /** 该 appid+region 库内 distinct 版本（下拉数据源；标准版 = suffix null 且非 gold，排首位） */
  versions?: GameVersion[]
}

/** 版本 × 地区最新价（走势抽屉「全部版本」区块，/games/{appid}/versions） */
export interface VersionRegionPrice {
  cents: number
  formatted: string
  discount: number
  cnyFen: number | null
}

export interface GameVersionPrices {
  subId: number
  /** 标准版 null；bundle 行后端已剔除 */
  suffix: string | null
  isGold: boolean
  regions: Record<string, VersionRegionPrice>
}

export interface GamesListParams {
  sort?: string
  limit?: number
  after?: string | null
  q?: string
  region?: string
  filterMode?: string
  onlyDiscounted?: boolean
  minRating?: number
  maxRating?: number
  minReviews?: number
  maxReviews?: number
  minPrice?: number
  maxPrice?: number
  isLowest?: boolean
  /** 史低/永降标记过滤：hl=新史低+平史低、pp=永降、any=并集（降价动态 feed） */
  flag?: string
  onlyHb?: boolean
  onlyEpic?: boolean
  onlyXgp?: boolean
  /** 隐藏已拥有（主账户已购库，未配置主账户时任一追踪账户） */
  hideOwned?: boolean
  /** 与国区差价区间（分；percent 模式为 0-100 百分值）——已选地区基准，未选回退全区最低 */
  diffMin?: number
  diffMax?: number
  /** 差价区间解释方式：absolute 分 / percent 百分比 */
  diffType?: string
  /** 三模式「近似全区最低」容差（分）；缺省走后端默认 5 元 */
  toleranceFen?: number
  /** 绝对低价：最低区价实质低于国区（差价 > toleranceFen，缺省 0=严格任何分差） */
  strictLowest?: boolean
  /** 游戏商店默认隐藏 DLC（白名单豁免个别常驻 DLC）；false = 含 DLC */
  excludeDlc?: boolean
}

function toQuery(params: Record<string, unknown>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '' || value === false) continue
    search.set(key, String(value))
  }
  const s = search.toString()
  return s ? `?${s}` : ''
}

export const gamesApi = {
  list: (params: GamesListParams = {}) =>
    request<GamesListPayload>(
      'GET',
      `/games${toQuery(params as Record<string, unknown>)}`,
    ),
  detail: (appid: number | string) =>
    request<GameDetail>('GET', `/games/${appid}`),
  // days 默认 0 = 全部时间：服务端按 sub 代际取并集，前端时间窗只在图表端开窗
  // （预设 365 会让调用方静默拿到截断序列——正是「库里更早有数据」那类投诉的来源）
  history: (appid: number | string, region = 'cn', days = 0, subId?: number) =>
    request<HistoryPayload>(
      'GET',
      `/games/${appid}/history${toQuery({ region, days, subId })}`,
    ),
  versions: (appid: number | string) =>
    request<{ versions: GameVersionPrices[] }>('GET', `/games/${appid}/versions`),
  // 截至某日的价格上下文（账单许可证命中条）：当时价 + 历史最低（标准版序列）
  priceContext: (appid: number | string, date: string, region = 'cn') =>
    request<GamePriceContext>('GET', `/games/${appid}/price-context${toQuery({ date, region })}`),
  // 批量（账单消费明细展开行补拉；服务端 ≤200 对截断）
  priceContextBatch: (items: { appid: number; date: string }[]) =>
    request<{ results: GamePriceContextItem[] }>('POST', '/games/price-context-batch', { items }),
  bundles: (appid: number | string) =>
    request<{ bundles: LinkedBundle[] }>('GET', `/games/${appid}/bundles`),
  cdk: (appid: number | string) =>
    request<{
      appid: number
      subId: number | null
      steampy: { listed: boolean; price: string | null; url: string; error?: string }
      steamcici: { listed: boolean; price: string | null; url: string; error?: string }
    }>('GET', `/games/${appid}/cdk`),
  retryRemoved: (appid: number | string) =>
    request<{ ok: boolean; jobId: number | null; requeued: boolean; note?: string }>(
      'POST',
      `/games/${appid}/retry-removed`,
    ),
}

// ─── bundles（捆绑包浏览视图：列表聚合 + 补齐计算详情） ──────────────────

export const bundlesApi = {
  /** 全量捆绑包（差价降序） */
  list: () => request<{ bundles: BundleSummary[] }>('GET', '/bundles'),
  /** 单包详情：列表字段 + 包内游戏各区现价（补齐计算求和用） */
  detail: (bundleId: number | string) =>
    request<BundleDetail>('GET', `/bundles/${bundleId}`),
  /** 导入捆绑包/Sub：Steam 商店或 SteamDB 链接（/bundle/ 或 /sub/）、裸 ID */
  importBundle: (text: string) =>
    request<BundleImportResult>('POST', '/bundles/import', { text }),
}

/** POST /bundles/import 结果 */
export interface BundleImportResult {
  ok: boolean
  bundleId: number
  kind: 'bundle' | 'sub'
  /** 导入前是否已在库（true = 本次为单包刷新） */
  existed: boolean
  regionPrices: number
  name: string
  appsEnqueued?: number
  appsSkippedNonGame?: number
}

// ─── watch pool（监控池：账户绑定 + 池条目。后端仍走 wishlist 域端点）─────

export interface TrackedAccount {
  steamid: string
  label: string | null
  /** Steam 昵称（miniprofile 通道，绑定/同步/家庭组同步时刷新） */
  personaName?: string | null
  /** 头像 URL（已归一 fastly.steamstatic.com） */
  avatarUrl?: string | null
  /** Steam 好友码（steamid - 76561197960265728） */
  friendCode?: string | null
  kinds: { wishlist: boolean; owned: boolean }
  lastSyncAt: string | null
  itemCount: number
  /** 已购条目数（active 且 owned=True 行数，账户设置弹窗展示） */
  ownedCount?: number
}

export interface PoolItemPayload {
  appid: number
  addedAt: string | null
  /** 游戏名（games 主档中文名；新入池未爬时为 null，前端回落显示 appid） */
  name?: string | null
  /** 游戏英文名（games 主档 name_en；监控条目搜索用） */
  nameEn?: string | null
  /** 封面缩略图 URL（games 主档 header_image；新入池未爬时为 null） */
  headerImage?: string | null
  /** 覆盖该条目的追踪账户（多账户同款聚合为一条的来源清单） */
  steamids: string[]
  /** 愿望单成员（Steam 愿望单同步来源；爬取第一优先级） */
  wishlisted: boolean
  /** 星标关注（游戏卡星标；爬取第一优先级） */
  followed: boolean
  /** 已购库来源（普通监控条目） */
  owned: boolean
  /** 手动加入监控池（池页添加 / 导入；普通监控条目） */
  manualPool: boolean
}

/** 监控条目批量操作结果（添加 / 移除共用逐条明细形状） */
export interface PoolMutationResult {
  results: { appid: number | string; status: string; detail: string }[]
  added?: number
  restored?: number
  exists?: number
  removed?: number
  missing?: number
  fail?: number
  /** 添加后自动触发首爬（任务占用 / 无可用代理时为 false） */
  crawlTriggered?: boolean
}

export interface SyncResult {
  steamid: string
  wishlistCount: number
  ownedCount: number
  added: number
  active: number
  newAppids: number[]
  crawlTriggered?: boolean
}

// ─── owned-library（游戏库页：全部追踪账户的已购游戏矩阵）─────────────────

export interface OwnedLibOwner {
  steamid: string
  /** 本系统首次看到该账户拥有此游戏的时间（同步入库时刻，非 Steam 购买时间） */
  addedAt: string | null
}

export interface OwnedLibGame {
  appid: number
  name: string | null
  nameEn: string | null
  headerImage: string | null
  genres: string | null
  releaseDate: string | null
  /** CN 价 CNY 分（未爬到的游戏为 null，不计价值合计） */
  cnPriceFen: number | null
  originalPriceFen: number | null
  discount: number
  owners: OwnedLibOwner[]
}

export interface OwnedLibAccount {
  steamid: string
  label: string | null
  personaName: string | null
  avatarUrl: string | null
  friendCode: string | null
  isPrimary: boolean
  kinds: { wishlist: boolean; owned: boolean }
  lastSyncAt: string | null
  ownedCount: number
  valueFen: number
  freeCount: number
}

export interface OwnedLibraryPayload {
  accounts: OwnedLibAccount[]
  games: OwnedLibGame[]
  generatedAt: string
}

export const watchPoolApi = {
  ownedLibrary: () => request<OwnedLibraryPayload>('GET', '/owned-library'),
  accounts: () => request<TrackedAccount[]>('GET', '/accounts'),
  add: (steamid: string, label = '', kinds?: { wishlist?: boolean; owned?: boolean }) =>
    request<{ steamid: string; friendCode?: string | null }>('POST', '/accounts', { steamid, label, kinds }),
  updateKinds: (steamid: string, kinds: { wishlist?: boolean; owned?: boolean }) =>
    request<{ steamid: string; kinds: { wishlist: boolean; owned: boolean } }>(
      'PUT',
      `/accounts/${steamid}/kinds`,
      kinds,
    ),
  remove: (steamid: string) =>
    request<{ removed: boolean }>('DELETE', `/accounts/${steamid}`),
  sync: (steamid: string, autoCrawl = true) =>
    request<SyncResult>('POST', `/accounts/${steamid}/sync${toQuery({ autoCrawl })}`),
  items: (steamid?: string) =>
    request<PoolItemPayload[]>('GET', `/wishlist${toQuery({ steamid })}`),
  /** 监控池 appid 轻量全集（dashboard 展厅判定用，避免全量条目的大 JSON） */
  appids: () => request<{ appids: number[]; total: number }>('GET', '/wishlist/appids'),
  /** 批量添加监控条目（池页添加 / 导入文件 / 任务页导入共用；单批上限 500，超出分批调）。
      source = 导入文件名：非空时后端把 appid 登记进预设池清单（随资产种子分发） */
  addItems: (appids: number[], source?: string) =>
    request<PoolMutationResult>('POST', '/pool/items', source ? { appids, source } : { appids }),
  /** 批量移除监控条目（脱池 + 同步免疫 + 清星标） */
  removeItems: (appids: number[]) =>
    request<PoolMutationResult>('POST', '/pool/items/remove', { appids }),
}

// ─── follows（关注列表：游戏卡星标 = 追踪池 manual 条目）──────────────────

export const followsApi = {
  /** 当前关注的 appid 全集（升序；星标状态一次性整表拉取） */
  list: () => request<{ appids: number[] }>('GET', '/follows'),
  /** 关注：入追踪池 + manual 标（爬取最优先、同步免疫） */
  add: (appid: number) =>
    request<{ appid: number; followed: boolean }>('PUT', `/follows/${appid}`),
  /** 取消关注：只清 manual 标 */
  remove: (appid: number) =>
    request<{ appid: number; followed: boolean }>('DELETE', `/follows/${appid}`),
}

// ─── ownership（游戏卡左上角归属状态徽章）──────────────────

export type OwnershipType = 'owned' | 'family' | 'wishlist'

export interface OwnershipInfo {
  type: OwnershipType
  /** 归属账户显示名（主账户显示「我」） */
  owners: string[]
  /** 与 owners 按下标对齐的头像 URL；空串 = 无头像，展示层落首字符占位 */
  ownerAvatars?: string[]
}

export const ownershipApi = {
  /** 批量查询（单次上限 200，由服务端截断） */
  batch: (appids: number[]) =>
    request<{ ownerships: Record<string, OwnershipInfo> }>(
      'GET',
      `/ownership${toQuery({ appids: appids.join(',') })}`,
    ),
}

// ─── crawl ───────────────────────────────────────────────

export interface CrawlJob {
  id: number
  kind: string
  status: string
  mode: string | null
  regions: string[] | null
  stats: Record<string, number> | null
  startedAt: string | null
  finishedAt: string | null
  error: string | null
}

export const crawlApi = {
  run: (scope: string, appids?: number[], kind?: string) =>
    request<{ id: number; count: number; regions: string[] | null }>('POST', '/crawl/run', {
      scope,
      appids,
      kind,
    }),
  stop: (jobId?: number) =>
    request<{ stopped: boolean }>('POST', '/crawl/stop', jobId ? { jobId } : {}),
  jobs: (limit = 20) => request<CrawlJob[]>('GET', `/crawl/jobs${toQuery({ limit })}`),
  active: () => request<{ activeJobId: number | null }>('GET', '/crawl/active'),
  /**
   * 批量导入监控池：后端入池（manual_pool 条目）+ 分类（ok 待首爬 / own 已在库 /
   * fail 无效）；首爬由调用方对新导入（status=ok）触发。复用 RedeemBatchResult
   * 的批量结果形状，另带 poolAdded / poolRestored 池写入计数。
   */
  importApps: (appids: number[]) =>
    request<RedeemBatchResult & { poolAdded?: number; poolRestored?: number }>(
      'POST',
      '/crawl/import',
      { appids },
    ),
}

// ─── system（运行日志 / 数据备份 / 应用更新）──────────────────

export interface BackupItem {
  name: string
  sizeBytes: number
  createdAt: string
}

export interface BackupCreateResult {
  path: string
  name: string
  sizeBytes: number
  integrityOk: boolean
  games: number
  createdAt: string
}

/** 更新检查结果（GitHub Releases 对比；网络不可达 available=false 不抛错） */
export interface UpdateCheckResult {
  available: boolean
  reason?: 'no_releases' | 'no_asset' | 'network' | 'bad_manifest'
  error?: string
  current?: string
  latest?: string
  tag?: string
  notes?: string
  sizeBytes?: number
  publishedAt?: string
  /** 期望校验值（清单提供；GitHub API 路径取资产 digest）。缺省表示未提供 */
  sha256?: string | null
  /** 确切资产名（下载时直连，省掉一次 API 反查） */
  asset?: string | null
  /** 检查结果来源：manifest=读更新清单（首选），api=回落 GitHub API */
  source?: 'manifest' | 'api'
}

/** 下载/校验/解包进度（轮询） */
export interface UpdateProgress {
  running: boolean
  phase: 'download' | 'verify' | 'extract' | 'done' | null
  percent: number | null
  received: number
  total: number | null
  error: string | null
  ok: boolean
}

/** 暂存就绪状态（「重启以完成更新」提示依据） */
export interface UpdatePending {
  pending: boolean
  tag?: string
}

export const systemApi = {
  /** 运行信息（关于页）：应用名 / 版本 / Python / 平台 / 数据目录 / 运行时长 / 发布仓库 */
  info: () =>
    request<{
      app: string
      version: string
      python: string
      platform: string
      data_dir: string
      web_dist_ready: boolean
      uptime_seconds: number
      /** 发布仓库 owner/repo：前端拼发布页链接的唯一来源 */
      repo: string
    }>('GET', '/info'),
  /** 内存环形缓冲的最近日志行 */
  logs: (limit = 200) =>
    request<{ lines: string[] }>('GET', `/system/logs${toQuery({ limit })}`),
  // 数据备份（VACUUM INTO 在线快照）
  backupList: () => request<{ items: BackupItem[] }>('GET', '/system/backup'),
  backupCreate: (label?: string) =>
    request<BackupCreateResult>('POST', '/system/backup', { label: label || null }),
  backupVerify: (name: string) =>
    request<{ name: string; integrityOk: boolean; games: number; sizeBytes: number }>(
      'POST',
      `/system/backup/${encodeURIComponent(name)}/verify`,
    ),
  backupRestore: (name: string) =>
    request<{ restored: boolean; name: string; rolledBack: boolean }>(
      'POST',
      `/system/backup/${encodeURIComponent(name)}/restore`,
    ),
  backupRemove: (name: string) =>
    request<{ removed: boolean }>('DELETE', `/system/backup/${encodeURIComponent(name)}`),
  // 应用更新（GitHub Releases）
  updateCheck: () => request<UpdateCheckResult>('GET', '/system/update-check'),
  updateDownload: (tag: string, sha256?: string | null, asset?: string | null) =>
    request<{ started: boolean }>('POST', '/system/update-download', {
      tag,
      sha256: sha256 ?? null,
      asset: asset ?? null,
    }),
  updateProgress: () => request<UpdateProgress>('GET', '/system/update-progress'),
  updatePending: () => request<UpdatePending>('GET', '/system/update-pending'),
  updateCancel: () => request<{ cleared: boolean }>('POST', '/system/update-cancel'),
}

/** 运行日志 SSE 订阅地址（EventSource 直连） */
export const LOGS_STREAM_URL = '/api/v1/system/logs/stream'

// ─── proxies ─────────────────────────────────────────────

export interface ProxyItem {
  id: number
  label: string | null
  scheme: string
  host: string
  port: number
  hasAuth: boolean
  url: string
  enabled: boolean
  status: string
  latencyMs: number | null
  consecutiveFailures: number
  lastCheckedAt: string | null
  testError?: string | null
}

export interface ProxyStrategy {
  strategy: string
  clashPort: number
}

export interface ClashStatus {
  running: boolean
  port: number | null
  configPath: string | null
  kernel: { found: boolean; path: string | null; builtin: boolean }
  version: string | null
  kernelDir: string
}

export interface ProxySubscriptionItem {
  id: number
  kind: 'clash' | 'plain'
  url: string
  label: string | null
  createdAt: string | null
  lastImportedAt: string | null
  lastStats: {
    fetched?: number
    added?: number
    skipped?: number
    total?: number
    alive?: number
    traffic?: string
    nodes?: number
    cached?: boolean
  } | null
  deprecated?: boolean
  deprecatedAt?: string | null
  deprecatedReason?: string | null
}

export interface ClashNodeTestItem {
  name: string
  alive: boolean
  steamOk: boolean
  exitIp: string | null
  ms: number | null
  duplicate: boolean
  probed?: boolean
  cooling?: boolean
}

export interface ClashNodeTestResult {
  total: number
  probed: number
  alive: number
  aliveUnique: number
  selector: string
  subscriptionId: number | null
  deprecated: boolean
  nodes: ClashNodeTestItem[]
}

export interface ProxyEventItem {
  id: number
  ts: string | null
  kind: string
  target: string | null
  proxyLabel: string | null
  statusCode: number | null
  durationMs: number | null
  error: string | null
}

/** 仪表盘口径统计：手动池逐条计，Clash 按出口 IP 去重（一个出口 IP = 一个代理） */
export interface ProxyPoolStats {
  pool: { total: number; ok: number }
  clash: {
    running: boolean
    subscriptionId: number | null
    nodes: number
    okNodes: number
    exitIps: number
    okExitIps: number
  }
  available: number
  total: number
}

export const proxiesApi = {
  list: () =>
    request<{ items: ProxyItem[]; strategy: ProxyStrategy; subscriptions: ProxySubscriptionItem[] }>(
      'GET',
      '/proxies',
    ),
  stats: () => request<ProxyPoolStats>('GET', '/proxies/stats'),
  add: (url: string, label?: string) =>
    request<{ added: number; items: ProxyItem[] }>('POST', '/proxies', { url, label }),
  update: (id: number, payload: { enabled?: boolean; label?: string }) =>
    request<ProxyItem>('PUT', `/proxies/${id}`, payload),
  remove: (id: number) => request<{ removed: boolean }>('DELETE', `/proxies/${id}`),
  test: (id: number) => request<ProxyItem>('POST', `/proxies/${id}/test`),
  testAll: () => request<{ items: ProxyItem[] }>('POST', '/proxies/test_all'),
  setStrategy: (payload: ProxyStrategy) =>
    request<ProxyStrategy>('PUT', '/proxies/strategy', payload),
  events: (limit = 100) => request<ProxyEventItem[]>('GET', `/proxies/events${toQuery({ limit })}`),
  // 订阅（clash / plain 双方式，长期保存）
  subscriptions: (kind?: 'clash' | 'plain') =>
    request<{ items: ProxySubscriptionItem[] }>(
      'GET',
      `/proxies/subscriptions${toQuery({ kind })}`,
    ),
  addSubscription: (kind: 'clash' | 'plain', url: string, label?: string) =>
    request<{
      id: number
      kind: 'clash' | 'plain'
      url: string
      label: string | null
      nodes?: number
      traffic?: string | null
      cached?: boolean
      warning?: string
      kernelInstalled?: boolean
      kernelVersion?: string | null
    }>('POST', '/proxies/subscriptions', { kind, url, label }),
  removeSubscription: (id: number) =>
    request<{ removed: boolean }>('DELETE', `/proxies/subscriptions/${id}`),
  renameSubscription: (id: number, label: string) =>
    request<ProxySubscriptionItem>('PUT', `/proxies/subscriptions/${id}`, { label }),
  refreshSubscriptionTraffic: (id: number) =>
    request<{ id: number; traffic: string }>('POST', `/proxies/subscriptions/${id}/refresh`),
  syncSubscription: (id: number) =>
    request<{
      id: number
      nodes: number | null
      traffic: string | null
      alive: number
      total: number
      usedCache: boolean
      restarted: boolean
      prunedLedger: number
    }>('POST', `/proxies/subscriptions/${id}/sync`),
  importSubscription: (id: number) =>
    request<{
      fetched: number
      added: number
      skipped: number
      checked?: number
      alive?: number
    }>('POST', `/proxies/subscriptions/${id}/import`),
  clashStatus: () => request<ClashStatus>('GET', '/proxies/clash'),
  clashInstall: () =>
    request<{ ok: boolean; path?: string; version?: string; error?: string }>(
      'POST',
      '/proxies/clash/install',
    ),
  clashInstallProgress: () =>
    request<{
      running: boolean
      phase: string | null
      percent: number | null
      received: number
      total: number | null
      source: string | null
      via: string | null
      error: string | null
      ok: boolean
    }>('GET', '/proxies/clash/install/progress'),
  clashStart: (subscriptionId?: number) =>
    request<ClashStatus>('POST', '/proxies/clash/start', { subscriptionId }),
  clashTest: () => request<ClashNodeTestResult>('POST', '/proxies/clash/test'),
  clashHealthCheck: (force = false) =>
    request<{ state: string; intervalHours: number }>(
      'POST',
      `/proxies/clash/health_check${toQuery({ force })}`,
    ),
  clashStop: () => request<ClashStatus>('POST', '/proxies/clash/stop'),
}

// ─── alerts ──────────────────────────────────────────────

export interface PriceAlertItem {
  id: number
  appid: number
  gameName: string
  region: string
  targetType: string
  targetValue: number | null
  active: boolean
  createdAt: string | null
  lastTriggeredAt: string | null
}

export interface AlertEventItem {
  id: number
  alertId: number
  appid: number
  gameName: string
  region: string
  price: number | null
  triggeredAt: string | null
  notified: boolean
}

export const alertsApi = {
  list: () => request<PriceAlertItem[]>('GET', '/alerts'),
  add: (appid: number, region: string, targetType: string, targetValue?: number) =>
    request<PriceAlertItem>('POST', '/alerts', { appid, region, targetType, targetValue }),
  update: (id: number, payload: { active?: boolean; targetValue?: number; targetType?: string; region?: string }) =>
    request<PriceAlertItem>('PUT', `/alerts/${id}`, payload),
  remove: (id: number) => request<{ removed: boolean }>('DELETE', `/alerts/${id}`),
  events: (limit = 50) => request<AlertEventItem[]>('GET', `/alerts/events${toQuery({ limit })}`),
  // SMTP 邮件设置
  getSmtp: () => request<SmtpConfig>('GET', '/alerts/smtp'),
  updateSmtp: (payload: SmtpConfigPayload) => request<SmtpConfig>('PUT', '/alerts/smtp', payload),
  /** 连通性测试：按表单当前值发测试邮件（密码空 = 用已存密码） */
  testSmtp: (payload: SmtpConfigPayload) => request<{ ok: boolean }>('POST', '/alerts/smtp/test', payload),
  // 游戏搜索
  search: (q: string, region?: string) =>
    request<{ items: GameSearchResult[]; error?: string }>('GET', `/alerts/search${toQuery({ q, region })}`),
}

export interface SmtpConfig {
  host: string
  port: number
  user: string
  password: string
  hasPassword: boolean
  toAddr: string
  useSsl: boolean
}

export interface SmtpConfigPayload {
  host: string
  port: number
  user: string
  password: string
  toAddr: string
  useSsl: boolean
}

export interface GameSearchResult {
  appid: number
  name: string
  nameEn: string
  isFree: boolean
}

// ─── rates ───────────────────────────────────────────────

export interface RateItem {
  currency: string
  rateToCny: number
  fetchedAt: string | null
}

export interface RateHistoryItem {
  date: string
  rateToCny: number
  source: string | null
  fetchedAt: string | null
}

/** 历史窗口档位（null = 全量 16 年档案） */
export type RateRange = '1mo' | '6mo' | '1y' | '5y' | '10y' | 'all'

/**
 * 汇率时间窗。
 *
 * **存 `labelKey` 不存 `label`**：模块级常量只在模块加载时求值一次，值里写死
 * 译文会把语言冻在首次加载那一刻（冻结陷阱），写死 `t()` 同理。存 key、渲染期
 * `t(r.labelKey)` 现取，是这套东西的标准解法——`stores/familyLib.ts` 的
 * `statusKey`、各视图的 `*Key` 常量表都是同一形状。
 *
 * 期 7 之前这里存的是中文 `label`，rates/Index.vue 只好自带一份
 * `RANGE_LABEL_KEYS: Record<RateRange, MessageKey>` 把 id 映射回 key——同一份
 * 对应关系写两遍，其中一份还是中文硬编码（正是该视图 `no-hardcoded-cjk` 命中
 * 的来源之一）。期 7 把对应关系收回定义处，那份重复映射随之删除。
 */
export const RATE_RANGES: { id: RateRange; labelKey: MessageKey }[] = [
  { id: '1mo', labelKey: 'rates.range.oneMonth' },
  { id: '6mo', labelKey: 'rates.range.sixMonths' },
  { id: '1y', labelKey: 'rates.range.oneYear' },
  { id: '5y', labelKey: 'rates.range.fiveYears' },
  { id: '10y', labelKey: 'rates.range.tenYears' },
  { id: 'all', labelKey: 'rates.range.all' },
]

export const ratesApi = {
  list: () => request<{ rates: RateItem[] }>('GET', '/rates'),
  refresh: () =>
    request<{ source: string; count: number; fetchedAt: string }>('POST', '/rates/refresh'),
  history: (currency = 'USD', range: RateRange = 'all') =>
    request<RateHistoryItem[]>('GET', `/rates/history${toQuery({ currency, range })}`),
}

// ─── bills（完整账单分析）────────────────────────────────

export interface BillImportItem {
  id: number
  nickname: string
  sourceFile: string
  importedAt: string | null
  gameNetFen: number
  gameSpendFen: number
  gameRefundFen: number
  orders: number
  fxMissing: number
}

export interface BillTxItem {
  id: number
  date: string
  txType: string
  items: string[]
  currency: string
  amount: number
  sign: number
  cnyFen: number | null
  fxRate: number | null
  fxNote: string
  payment: string
  discountPct: string
  origPrice: number | null
  isGift: boolean
  isRefund: boolean
  origIsGift: boolean
}

export interface BillTopupItem {
  id: number
  date: string
  desc: string
  currency: string
  amount: number
  sign: number
  cnyFen: number | null
  fxRate: number | null
  payment: string
  txType: string
  isRefund: boolean
}

export interface BillCdkItem {
  id: number
  name: string
  /** 许可 appid（steam_fetch 从名称列商店链接提取；老快照/外部导出为 null） */
  appid: number | null
  date: string
  /** free = 免费入库（只展示不计价） */
  acq: 'cdk' | 'gift' | 'free'
  acqLabel: string
  manualFen: number | null
}

export interface BillSummary {
  netFen: number
  spendFen: number
  refundFen: number
  orders: number
  txCount: number
  fxMissing: number
  buyTotalFen: number
  buyRefundFen: number
  giftTotalFen: number
  giftRefundFen: number
  selfNetFen: number
  giftNetFen: number
  quotaFen: number
  accountValueFen: number
  cdkPriced: number
  cdkTotalFen: number
  topupNetFen: number
  topupSpendFen: number
  topupRefundFen: number
}

export interface BillYearStat {
  year: string
  netFen: number
  spendFen: number
  refundFen: number
  count: number
  months: Record<string, { netFen: number; count: number }>
}

export interface BillMonthStat {
  month: string
  netFen: number
  spendFen: number
  refundFen: number
  count: number
}

export interface BillOverview {
  id: number
  nickname: string
  avatar: string
  sourceFile: string
  importedAt: string | null
  warnings: string[]
  summary: BillSummary
  years: BillYearStat[]
  monthSeries: BillMonthStat[]
  topupByCurrency: { currency: string; total: number; cnyFen: number; count: number }[]
  counts: { gameTxs: number; topupTxs: number; cdkGames: number; cdk: number; gift: number; free: number }
}

export interface BillSyncResult {
  importId: number
  nickname: string
  gameTxs: number
  topupTxs: number
  cdkGames: number
  fxMissing: number
  warnings: string[]
  replaced?: number
  ok?: boolean
  historyRows?: number
  licenseRows?: number
}

export interface BillSyncSnapshot {
  running: boolean
  ok?: boolean
  status?: string
  error?: string
  syncedAt?: string
  importId?: number
  nickname?: string
  gameTxs?: number
  topupTxs?: number
  cdkGames?: number
  fxMissing?: number
  historyRows?: number
  licenseRows?: number
  replaced?: number
  /* 同步进行中的实时进度（sync_bills 的 _progress 回调写入快照） */
  stage?: 'identity' | 'history' | 'licenses' | 'import'
  pages?: number
  rows?: number
}

export interface BillTxQuery {
  year?: string
  month?: string
  txType?: string
  search?: string
  limit?: number
  offset?: number
}

export const billsApi = {
  listImports: () => request<{ imports: BillImportItem[] }>('GET', '/bills/imports'),
  syncBills: () => request<BillSyncResult>('POST', '/bills/sync'),
  syncStatus: () => request<BillSyncSnapshot>('GET', '/bills/sync'),
  removeImport: (id: number) =>
    request<{ deleted: boolean; importId: number }>('DELETE', `/bills/imports/${id}`),
  overview: (id: number) => request<BillOverview>('GET', `/bills/imports/${id}/overview`),
  gameTxs: (id: number, q: BillTxQuery = {}) =>
    request<{ total: number; rows: BillTxItem[]; limit: number; offset: number }>(
      'GET',
      `/bills/imports/${id}/game-txs${toQuery({ year: q.year, month: q.month, txType: q.txType, search: q.search, limit: q.limit, offset: q.offset })}`,
    ),
  topupTxs: (id: number, limit = 200, offset = 0) =>
    request<{ total: number; rows: BillTopupItem[]; limit: number; offset: number }>(
      'GET',
      `/bills/imports/${id}/topup-txs${toQuery({ limit, offset })}`,
    ),
  cdks: (id: number, q: { acq?: string; search?: string; limit?: number; offset?: number } = {}) =>
    request<{ total: number; rows: BillCdkItem[]; limit: number; offset: number }>(
      'GET',
      `/bills/imports/${id}/cdk${toQuery({ acq: q.acq, search: q.search, limit: q.limit, offset: q.offset })}`,
    ),
  setCdkPrice: (cdkId: number, manualFen: number | null) =>
    request<BillCdkItem>('PUT', `/bills/cdk/${cdkId}/price`, { manualFen }),
}

// ─── redeem（CDK 批量激活 + 免费产品领取）────────────────────────

export interface RedeemResultItem {
  code?: string
  subid?: number
  appid?: number
  status: 'ok' | 'own' | 'fail'
  detail: string
  subId: string
  subName: string
  /** Steam 返回原文（CDK 激活结果专有，前端"展开原文"核对用） */
  raw?: string
}

export interface RedeemBatchResult {
  results: RedeemResultItem[]
  ok: number
  own: number
  fail: number
}

export interface RedeemQuota {
  hasCookie: boolean
  hasSessionId: boolean
  /** 当前绑定账号 SteamID（激活计数按账号独立） */
  steamId: string
  /** 该账号 30 分钟窗口内已用激活次数 */
  used: number
  limit: number
}

export const redeemApi = {
  activateKeys: (keys: string[]) =>
    request<RedeemBatchResult>('POST', '/redeem/keys', { keys }),
  freeClaims: (subids: number[]) =>
    request<RedeemBatchResult>('POST', '/redeem/free', { subids }),
  quota: () => request<RedeemQuota>('GET', '/redeem/quota'),
}

// ─── metadata（Epic 喜加一展示链：仪表盘卡片）───

export interface EpicOffer {
  title: string
  /** Epic 中文标题，可能为空（回落 title） */
  titleCn: string
  /** Steam appid（标记链另行落库；展示卡不依赖） */
  appid: number | null
  /** 白送起止日（YYYY-M-D 无前导零，UTC 日期） */
  start: string
  end: string
  /** true = 下周预告（尚未开始） */
  upcoming: boolean
  /** 横版封面（空串 = 素材缺失，卡片渲染占位底） */
  image: string
  /** Epic 商店页（新开窗口直达领取） */
  url: string
  /** 原价文案（中文区响应，如 "¥68.00"；划线展示） */
  priceOriginal: string
}

export interface EpicMobileOffer {
  /** 游戏名（GamerPower 自动源）；null = breaker 兜底（名称在图里） */
  title: string | null
  /** 立绘（GamerPower 横图或 CMS breaker 图） */
  image: string
  /** 领取入口：GamerPower 落地官方页 / 移动页兜底 */
  url: string
  /** 截止日 YYYY-M-D（零填充；breaker 兜底时 null） */
  end: string | null
  /** 原价文案如 "$4.99"（划线展示） */
  worth: string | null
  /** 数据来源：gamerpower=自动真名真链 / breaker=兜底立绘 */
  source: 'gamerpower' | 'breaker'
}

export interface EpicOffersPayload {
  ok: boolean
  offers: EpicOffer[]
  /** 移动端每周白送（GamerPower 自动源，breaker 立绘兜底）；null = 未取到 */
  mobile: EpicMobileOffer | null
  /** 北京时间 ISO（卡片「更新于」） */
  fetchedAt: string | null
}

export const metadataApi = {
  /** 当期 + 预告白送元素；后端进程内缓存 30 分钟 */
  epicOffers: (opts?: { noCache?: boolean }) =>
    request<EpicOffersPayload>('GET', '/metadata/epic/offers', undefined, opts),
}
