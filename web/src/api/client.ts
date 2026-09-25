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

/** FastAPI 错误 detail 拍平为人可读文本。
 *  422 校验失败的 detail 是对象数组（{loc, msg, type}），直接 String()
 *  会渲染成 "[object Object]"——错误提示要能一眼看懂（如「flag: String
 *  should match pattern ...」，正是新前端 + 旧后端时最需要的线索）。 */
function formatApiDetail(detail: unknown): string {
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        if (typeof d === 'string') return d
        if (d && typeof d === 'object') {
          const item = d as { loc?: unknown[]; msg?: string }
          // loc 首段是来源（query/body/path），去掉只留参数名
          const loc = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : ''
          return [loc, item.msg ?? ''].filter(Boolean).join(': ')
        }
        return String(d)
      })
      .filter(Boolean)
    return parts.join('; ') || `HTTP ${detail.length}`
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail)
  return String(detail)
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
  '/proxies/clash/test', // 检测进度轮询（/proxies/clash/test/progress 由前缀规则覆盖）
  '/achievements/sync', // 成就同步进行中的快照轮询
  // Epic 卡片：新鲜度由后端快照缓存管理（过期即回旧数据 + 后台刷新），
  // 前端再叠 60s 时间窗会把 stale→fresh 的覆盖整个吞掉（轮询永远读旧响应）
  '/metadata/epic/offers',
  '/metadata/steam/offers', // Steam 喜加一：10min 轮询，赠送结束要立即消失
]

function isNoCachePath(path: string): boolean {
  return NO_CACHE_PATHS.some((p) => path === p || path.startsWith(`${p}?`) || path.startsWith(`${p}/`))
}

/** 精确失效：只清匹配 prefix 的时间窗条目（路径段前缀，与 NO_CACHE_PATHS 同规则）。
 * 后台价格周期完成后由 SSE 触发——此时该拉新数据，但没有写操作可用来全量清。 */
export function invalidateGetCache(prefix: string): void {
  for (const key of [...getCache.keys()]) {
    if (key === prefix || key.startsWith(`${prefix}?`) || key.startsWith(`${prefix}/`)) {
      getCache.delete(key)
    }
  }
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
        if (data?.detail) detail = formatApiDetail(data.detail)
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
  /** 新版本提示总开关：false = 有新版也不弹窗/不亮红点（只留手动检查） */
  update_notify: boolean
  /** 静默自动更新：true = 检测到新版本后台自动下载校验，不打扰，下次启动换装 */
  update_auto: boolean
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
    /** 新版本提示总开关 */
    update_notify?: boolean
    /** 静默自动更新开关 */
    update_auto?: boolean
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
  /** 登录态是否已过期（访问令牌到期且未能自动续期；has_cookie 仍为真） */
  session_expired: boolean
  /** 访问令牌到期时刻（北京时间 ISO；null = 无法判定） */
  session_expires_at: string | null
  /** 是否留有自动续期凭据（登录时勾选「记住我」才有） */
  session_has_refresh: boolean
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
  /** 登录态：has_cookie 只表示"绑过"，能否继续用看下面三项 */
  session_expired: boolean
  session_expires_at: string | null
  session_has_refresh: boolean
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

/** 本轮覆盖率（Cycle 冻结期望集口径；只对本轮期望集内的对象给出） */
export interface PriceCoverage {
  cycleId: number
  cycleStatus: string
  /** 本轮期望刷新的地区数（分母） */
  expectedUnits: number
  ok: number
  /** 锁区：Steam 明确不卖，不是抓取失败 */
  locked: number
  /** 欠账待补抓 */
  missing: number
  blocked: number
  /** 本轮窗口内没有结果：不是「价格不可用」 */
  unobserved: number
  coverage: number
  coverageConfirmed: number
}

/** 价格数据状态。观察时间/新鲜度是**价格**维度，与 updatedAt（实体更新时间）不同源 */
export interface PriceData {
  /** 价格观察时刻（ISO，本对象价格行的 MAX(updated_at)）；null = 尚无价格行 */
  observedAt: string | null
  /** 距现在的时长（小时） */
  ageHours: number | null
  /** fresh <6h / lagging <12h / stale ≥12h（对象级，不按地区分档） */
  freshness: 'fresh' | 'lagging' | 'stale' | null
  /** null = 不属本轮期望集（无 Cycle 归属，不冒充 100%） */
  coverage: PriceCoverage | null
}

export interface GameListItem {
  appid: number
  name: string
  nameEn: string | null
  discount: number
  discountLabel: string
  /** 国区折扣截止（Unix 秒；null=无折扣/未带促销元数据） */
  discountEndsAt?: number | null
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
  /** 价格数据状态（列表接口下发；详情接口不带） */
  priceData?: PriceData | null
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
  /** smart 排序评分（0~1 加权和；公式见后端 scoring.py） */
  smartScore?: number
  /** smart 四因子拆解（实验池对照展示用；0~1 归一值） */
  smartFactors?: {
    save: number
    quality: number
    timing: number
    familiarity: number
  }
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

/** 同系列成员（GPW「同系列」区块行，/games/{appid}/series） */
export interface GameSeriesMember {
  appid: number
  name: string
  headerImage: string
  isSelf: boolean
  type: string
  cnPriceFen: number | null
  cnOriginalFen: number | null
  cnDiscount: number
  lowestPriceFen: number | null
  savingsFen: number
}

/** 同系列归组（服务端名称聚类维护，识别不到 = 404） */
export interface GameSeriesInfo {
  seriesId: string
  /** 展示名：成员展示名公共汉字前缀，缺失回落 seriesId */
  seriesName: string
  members: GameSeriesMember[]
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
  /** smart 四因子评分（服务端预计算，0~1）。选区重锚排序用：
      save(该区差价) + (smartScore − save(diffFen)) 即该区视角的评分 */
  smartScore: number
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
  /** 国区折扣截止（Unix 秒；null=无折扣/未带促销元数据） */
  cnDiscountEndsAt?: number | null
  lowestRegionCode: string
  isAdult: boolean
  isVisualNovel: boolean
  seriesId: string | null
  versions: GameVersion[]
  linkedBundles: LinkedBundle[]
  viewCount: number
  /** 免费态：f2p=永久免费 / promo=限时赠送中；null=付费正常 */
  freeKind: 'f2p' | 'promo' | null
  /** 赠送结束 Unix 秒（仅 promo 态有值） */
  promoEndAt: number | null
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
  /** 屏蔽家庭共享（非主账户的追踪账户已拥有 = 家人库可玩；与卡片「家庭共享」徽章同口径） */
  hideFamilySharing?: boolean
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
  // 同系列成员（打开 GPW 时懒加载；404 = 未识别到系列，区块隐藏）。
  // 方法名避开 series——eslint 图表契约规则按「含 series 键的对象」识别
  // option，API 对象里出现这个键会误报
  seriesInfo: (appid: number | string) =>
    request<GameSeriesInfo>('GET', `/games/${appid}/series`),
  cdk: (appid: number | string, subId?: number) =>
    request<{
      appid: number
      subId: number | null
      steampy: { listed: boolean; price: string | null; url: string; error?: string }
      steamcici: { listed: boolean; price: string | null; url: string; error?: string }
    }>('GET', `/games/${appid}/cdk${subId !== undefined ? `?sub_id=${subId}` : ''}`),
  retryRemoved: (appid: number | string) =>
    request<{ ok: boolean; jobId: number | null; requeued: boolean; note?: string }>(
      'POST',
      `/games/${appid}/retry-removed`,
    ),
}

// ─── bundles（捆绑包浏览视图：列表聚合 + 补齐计算详情） ──────────────────

export const bundlesApi = {
  /** 全量捆绑包（sort=diff 差价降序 | smart 智能评分降序，服务端预计算列；
   *  discount 折扣力度由前端排序，服务端按 diff 出底序） */
  list: (sort: 'diff' | 'smart' | 'discount' = 'diff') =>
    request<{ bundles: BundleSummary[] }>('GET', `/bundles${toQuery({ sort })}`),
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
  /** 榜单发现源落池（热销/新品/即将推出轮询并入；普通监控条目） */
  boardPool: boolean
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
  /** 添加后自动触发首爬（任务占用时为 false） */
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

/** 价格事实变化的类型：与后端 `crawl/events.py` 的 EVENT_TYPES 一一对应 */
export const PRICE_EVENT_TYPES = [
  'PRICE_DROP',
  'PRICE_INCREASE',
  'NEW_HISTORICAL_LOW',
  'HISTORICAL_LOW_MATCH',
  'PERMANENT_PRICE_CHANGE',
  'REGION_LOCKED',
  'REGION_UNLOCKED',
  'PRICE_UNAVAILABLE',
  'PRICE_RESTORED',
  'FREE_PROMO',
  'REMOVED',
] as const

export type PriceEventType = (typeof PRICE_EVENT_TYPES)[number]

/**
 * 一条价格事实变化（`price_events` 一行）。
 * 只增不改、没有状态流转；语义（变化判没判出来）全在后端，前端不重判。
 */
export interface PriceEventItem {
  id: number
  cycleId: number
  appid: number
  /** 大写区码；null = 该事件由游戏级对象表达（促销免费 / 下架），不属单一地区 */
  region: string | null
  eventType: PriceEventType
  /** 变化前的有效值；没有前值时为 null */
  previous: Record<string, unknown> | null
  current: Record<string, unknown> | null
  /** 事实发生时刻（ISO）。展示时间只用它，不用实体更新时间 */
  occurredAt: string | null
}

/** 一轮价格刷新（`price_cycles` 一行）；前端只看「最近一轮收敛没有」 */
export interface PriceCycleItem {
  id: number
  kind: string
  status: string
  scope: string
  expectedUnits: number
  enteredRepairing: boolean
  startedAt: string | null
  finishedAt: string | null
  error: string | null
  stats: Record<string, number | null> | null
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
  /** 价格刷新轮次（新→旧）；前端只用来看「最近一轮是否已收敛」 */
  cycles: (limit = 1) => request<PriceCycleItem[]>('GET', `/crawl/cycles${toQuery({ limit })}`),
  /**
   * 价格事件：**唯一**的事件来源，事实记录只读。
   * 事件类型与前后值都由后端判定，前端只做格式化展示，不据价格矩阵自行推断。
   */
  priceEvents: (params: { cycleId?: number; appid?: number; eventType?: string; limit?: number } = {}) =>
    request<PriceEventItem[]>(
      'GET',
      `/crawl/price-events${toQuery({
        cycle_id: params.cycleId,
        appid: params.appid,
        event_type: params.eventType,
        limit: params.limit,
      })}`,
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
  /** probe = 并发探测通道（判资产在不在 + 量延迟），download/verify/extract/done 见名知义 */
  phase: 'probe' | 'download' | 'verify' | 'extract' | 'done' | null
  percent: number | null
  received: number
  total: number | null
  error: string | null
  ok: boolean
  /** 机器可读失败归因：asset_missing=该版本没包 / network=通道全挂 /
   *  verify_failed=校验不过 / cancelled=用户取消 / error=其它 */
  code: 'asset_missing' | 'network' | 'verify_failed' | 'cancelled' | 'error' | null
  /** 瞬时速率（B/s，后端每秒刷新一次；0 = 暂无） */
  speed: number
  /** 当前通道名（"直连" / "直连·镜像" / "本地混合端口 7890"…） */
  channel: string | null
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
  /** 发起下载（后端 fire-and-forget，进度走 updateProgress 轮询）。
      size = 清单体积：镜像分块响应不给 Content-Length 时后端用它算百分比 */
  updateDownload: (
    tag: string,
    sha256?: string | null,
    asset?: string | null,
    size?: number | null,
  ) =>
    request<{ started: boolean }>('POST', '/system/update-download', {
      tag,
      sha256: sha256 ?? null,
      asset: asset ?? null,
      size: size ?? null,
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
  /** 自动更新订阅：false 时定时刷新跳过它，只保留手动重拉 */
  autoRefresh?: boolean
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

/** 节点检测会话快照：后台逐节点探测，phase 驱动按钮进度与结果面板。
 *  idle = 当前进程无会话；queued = 已受理待开测（如前一轮体检占着串行锁）；
 *  running = 探测中（nodes 逐节点追加）；done/failed 终态保留最近一次结果。 */
export interface ClashTestProgress {
  phase: 'idle' | 'queued' | 'running' | 'done' | 'failed'
  total: number | null
  toProbe: number | null
  probed: number
  cooldownSkipped: number
  alive: number
  aliveUnique: number | null
  selector: string | null
  subscriptionId: number | null
  deprecated: boolean | null
  nodes: ClashNodeTestItem[]
  startedAt: string | null
  finishedAt: string | null
  error: string | null
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
  /** 编辑订阅：改名 + 换链接（换链接的 clash 订阅保存即自动重拉，synced=true） */
  updateSubscription: (id: number, payload: { label?: string; url?: string; autoRefresh?: boolean }) =>
    request<{
      id: number
      kind: 'clash' | 'plain'
      url: string
      label: string | null
      synced: boolean
      nodes?: number | null
      traffic?: string | null
      alive?: number
      total?: number
      restarted?: boolean
      warning?: string
    }>('PUT', `/proxies/subscriptions/${id}`, payload),
  refreshSubscriptionTraffic: (id: number) =>
    request<{ id: number; traffic: string }>('POST', `/proxies/subscriptions/${id}/refresh`),
  syncSubscription: (id: number) =>
    request<{
      id: number
      label: string | null
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
  /** 当前代理策略下解析出的代理 URL（null=直连）；商店页空态诊断用 */
  resolveProxy: () => request<{ proxyUrl: string | null }>('GET', '/proxies/resolve'),
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
  clashTestStart: () => request<ClashTestProgress>('POST', '/proxies/clash/test'),
  clashTestProgress: () => request<ClashTestProgress>('GET', '/proxies/clash/test/progress'),
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
  /** 封面（库内 header_image，缺档由服务端回退 Steam CDN 拼图） */
  gameHeader: string
  region: string
  /** price 类 = 人民币分（阈值口径），pct 类 = 百分数 */
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
  /** 封面（同规则列表口径） */
  gameHeader: string
  region: string
  /** 触发时该区货币最小单位（原始快照） */
  price: number | null
  /** 触发时刻人民币分快照；旧事件无快照为 null（前端回退 priceText） */
  priceCny: number | null
  /** 本币价文本（如 "$59.99"），服务端按区币种格式化 */
  priceText: string
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
  /** 删除单条触发历史 */
  removeEvent: (id: number) => request<{ removed: boolean }>('DELETE', `/alerts/events/${id}`),
  /** 清空全部触发历史，返回删除条数 */
  clearEvents: () => request<{ removed: number }>('DELETE', '/alerts/events'),
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

// ─── notifications（价格事件通知）─────────────────────────────

/** 通知类别：用户面分类，一个类别覆盖多个内部事件类型（内部枚举不出界面） */
export interface NotificationCategory {
  key: string
  label: string
  enabled: boolean
  /** 该类别覆盖的内部事件类型数量（只用于展示说明） */
  eventTypes: number
}

export interface NotificationPrefs {
  enabled: boolean
  quietEnabled: boolean
  quietStart: string
  quietEnd: string
  includeDetails: boolean
  /** 单条候选最多投递次数 */
  maxAttempts: number
  categories: NotificationCategory[]
  /** SMTP 的可公开部分：不含密码 / 授权码 */
  smtp: {
    configured: boolean
    host: string
    port: number
    userMasked: string
    hasPassword: boolean
    useSsl: boolean
  }
  lastDelivery: {
    status: string
    at: string | null
    attempts: number
    reason: string | null
    retryable: boolean
  } | null
}

export interface NotificationPrefsUpdate {
  enabled?: boolean
  quietEnabled?: boolean
  quietStart?: string
  quietEnd?: string
  includeDetails?: boolean
  /** 只传要改的类别，后端按已知类别合并 */
  categories?: Record<string, boolean>
}

export interface NotificationStats {
  total: number
  delivered: number
  pending: number
  suppressed: number
  sending: number
  failed: number
  /** 失败里还能再试的（临时故障且未到次数上限） */
  retryable: number
  /** 已经永久放弃的（凭据/配置错误或次数用尽） */
  permanent: number
  attempts: number
  maxAttempts: number
  byStatus: Record<string, { count: number; attempts: number }>
}

export const notificationsApi = {
  prefs: () => request<NotificationPrefs>('GET', '/notifications/prefs'),
  updatePrefs: (payload: NotificationPrefsUpdate) =>
    request<{ ok: boolean; enabled: boolean }>('PUT', '/notifications/prefs', payload),
  /** 连通性测试：与 Price Event / Candidate / Cycle 无关，不写任何业务数据 */
  test: () => request<{ ok: boolean }>('POST', '/notifications/test'),
  stats: () => request<NotificationStats>('GET', '/notifications/stats'),
}

/** 历史窗口档位（null = 全量 16 年档案） */
export type RateRange = '1mo' | '6mo' | '1y' | '5y' | '10y' | 'all'

/**
 * 汇率时间窗。
 *
 * **存 `labelKey` 不存 `label`**：模块级常量只在模块加载时求值一次，值里写死
 * 译文会把语言冻在首次加载那一刻（冻结陷阱），写死 `t()` 同理。存 key、渲染期
 * `t(r.labelKey)` 现取——`stores/familyLib.ts` 的 `statusKey`、各视图的 `*Key`
 * 常量表都是同一形状。对应关系只在此处定义一份，视图不另存映射。
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
  /** 游戏名（sandbox 探测官方直出）；null = breaker 兜底（名称在图里） */
  title: string | null
  /** 立绘（促销元素官方封面或 CMS breaker 图） */
  image: string
  /** 领取入口：sandbox 探测拼结账直链 / 移动页兜底 */
  url: string
  /** 截止日 YYYY-M-D（零填充；breaker 兜底时 null） */
  end: string | null
  /** 原价文案如 "$4.99"（划线展示） */
  worth: string | null
  /** 数据来源：epic=官方数据探测真名真链 / breaker=兜底立绘 */
  source: 'epic' | 'breaker'
}

export interface EpicOffersPayload {
  ok: boolean
  offers: EpicOffer[]
  /** 移动端每周白送（sandbox offers 探测，breaker 立绘兜底）；null = 未取到 */
  mobile: EpicMobileOffer | null
  /** 北京时间 ISO（卡片「更新于」；快照态为上次抓取时刻） */
  fetchedAt: string | null
  /** true = 本次响应来自缓存（含进程重启后的落库快照） */
  cached?: boolean
  /** true = 快照已过期、后台正在刷新（前端短轮询届时自动覆盖） */
  stale?: boolean
}

export const metadataApi = {
  /** 当期 + 预告白送元素；后端快照缓存 30 分钟（冷启动先回快照 + 后台刷新） */
  epicOffers: (opts?: { noCache?: boolean }) =>
    request<EpicOffersPayload>('GET', '/metadata/epic/offers', undefined, opts),
  /** 当月 HB Choice 游戏清单（纯本地库读，零外网；ok=false = 尚未入库） */
  hbChoiceOffers: () => request<HbChoiceOffersPayload>('GET', '/metadata/hb/offers'),
  /** 正在赠送中的 Steam 限时免费（纯本地库读；offers 空 = 无赠送，模块整块隐藏） */
  steamFreeOffers: () => request<SteamFreeOffersPayload>('GET', '/metadata/steam/offers'),
}

export interface SteamFreeOffer {
  appid: number
  name: string
  /** Steam 横版封面 */
  headerImage: string | null
  /** 国区原价（分，赠送期划线展示） */
  originalPriceFen: number | null
  /** 赠送结束 Unix 秒（Steam free_to_keep_ends） */
  endTs: number
}

export interface SteamFreeOffersPayload {
  ok: boolean
  offers: SteamFreeOffer[]
  /** 北京时间 ISO */
  fetchedAt: string | null
}

export interface HbChoiceGame {
  appid: number
  /** games 行名（占位行 = HB 侧标题） */
  name: string
  /** Steam 横版封面；null = 占位行待回补（卡片渲染占位底） */
  headerImage: string | null
  /** 国区现价（分）；null = 无价格行 */
  priceFen: number | null
  /** 国区原价（分）；null = 无价格行 */
  originalPriceFen: number | null
  discount: number
  /** 非 CN 区最低 CNY 分（games.min_cny_fen 预计算列）；null = 无 */
  lowestCnyFen: number | null
}

export interface HbChoiceOffersPayload {
  ok: boolean
  /** 当月标签（与 games.hb_data 同一约定，如 "HB慈善包26年9月包"） */
  label: string
  machineName: string | null
  productName: string | null
  /** 当月包页（machineName 推导 /membership/{Month}-{Year}；无游标回落订阅主页） */
  monthUrl: string
  /** 跳过本月直达页（官方 secureArea，未登录跳登录页） */
  skipUrl: string
  settingsUrl: string
  games: HbChoiceGame[]
  /** ok=false 时的原因文案 */
  error?: string
}

// ─── 成就殿堂（奖杯）────────────────────────────────────

export type RarityTier = 'ultra' | 'very_rare' | 'rare' | 'uncommon' | 'common' | 'unknown'

/** 游戏行来源：owned=本号已购；shared=库外（家庭共享等）；manual=手动补录 */
export type AchievementSource = 'owned' | 'shared' | 'manual'

export interface AchievementAccount {
  steamid: string
  personaName: string
  avatarUrl: string
  isPrimary: boolean
  isActive: boolean
  /** bound=本地已绑账号（有 Cookie）；family=主账号所在家庭组成员 */
  relation: 'bound' | 'family'
}

export interface AchievementSummary {
  hasCredential: boolean
  steamid: string
  /** 白金（全成就）游戏数 */
  platinum: number
  /** 有成就系统的游戏数（含库外） */
  gamesWithAchievements: number
  /** 有游玩时长的游戏数 */
  playedGames: number
  totalAchievements: number
  unlockedAchievements: number
  completionRate: number
  totalPlaytimeMin: number
  /** 其中库外（家庭共享等）：游戏数 / 已获得成就数 / 白金数 */
  externalGames: number
  externalUnlocked: number
  externalPlatinum: number
  lastSyncedAt: string | null
  /** 白金殿堂陈列（按达成日期新→旧） */
  platinums: {
    appid: number
    name: string
    headerImage: string
    playtimeMin: number
    total: number
    /** 白金时刻 epoch 秒；0 = 未知 */
    date: number
    source: AchievementSource
  }[]
  /** 最稀有成就（已获得，按全服占比升序） */
  rarest: AchievementRow[]
  /** 最近解锁（按时间降序） */
  recentUnlocks: AchievementRow[]
  /** 接近白金（完成度 ≥ 门槛，按完成度降序） */
  nearCompletion: {
    appid: number
    name: string
    headerImage: string
    unlocked: number
    total: number
    percent: number
    remaining: number
    playtimeMin: number
    source: AchievementSource
  }[]
  /** 已获得成就的稀有度分布 */
  rarityBuckets: { tier: RarityTier; count: number }[]
  playtimeTop: { appid: number; name: string; playtimeMin: number }[]
  unlockTimeline: { month: string; count: number }[]
}

/** 汇总模块里的成就行（最稀有 / 最近解锁共用） */
export interface AchievementRow {
  appid: number
  name: string
  imageName: string
  icon: string | null
  globalPercent: number | null
  /** 解锁 epoch 秒 */
  unlockTime: number
  gameName: string
}

export type AchievementGameFilter = 'trophy' | 'platinum' | 'progress' | 'external' | 'all'
export type AchievementGameSort = 'playtime' | 'progress' | 'recent' | 'name'

export interface AchievementGameItem {
  appid: number
  name: string
  headerImage: string | null
  playtimeMin: number
  lastPlayed: number
  total: number
  unlocked: number
  /** 还差几枚（未解锁数） */
  remaining: number
  percent: number
  platinum: boolean
  source: AchievementSource
  /** 是否在本人已购库里（false = 库外：家庭共享等，时长未知） */
  owned: boolean
}

export interface AchievementGamesPayload {
  games: AchievementGameItem[]
  count: number
}

export interface AchievementItem {
  imageName: string
  name: string
  description: string
  icon: string | null
  iconGray: string | null
  /** 全服解锁占比 0~100；null = 未取到 */
  globalPercent: number | null
  rarity: RarityTier
  achieved: boolean
  /** 解锁 epoch 秒；0 = 未解锁 */
  unlockTime: number
}

export interface AchievementDetailPayload extends AchievementGameItem {
  /** 白金达成时刻 epoch 秒；0 = 未白金 */
  perfectDate: number
  achievements: AchievementItem[]
}

export interface AchievementSyncSnapshot {
  running: boolean
  ok?: boolean | null
  stage?: string
  done?: number
  total?: number
  current?: string
  error?: string
  startedAt?: string
  syncedAt?: string
  /** 本轮同步的目标账号（多账号下用于判断状态是否属于当前所选账号） */
  steamid?: string
}

export const achievementsApi = {
  summary: (steamid = '') =>
    request<AchievementSummary>('GET', `/achievements/summary${toQuery({ steamid })}`),
  /** 可切换账号清单（已绑账号 + 家庭成员；不含凭证） */
  accounts: () => request<{ accounts: AchievementAccount[] }>('GET', '/achievements/accounts'),
  games: (filter: AchievementGameFilter, sort: AchievementGameSort, q = '', steamid = '') =>
    request<AchievementGamesPayload>(
      'GET',
      `/achievements/games${toQuery({ filter, sort, q, steamid })}`,
    ),
  gameDetail: (appid: number, steamid = '') =>
    request<AchievementDetailPayload>('GET', `/achievements/games/${appid}${toQuery({ steamid })}`),
  sync: (steamid = '') =>
    request<AchievementSyncSnapshot>('POST', `/achievements/sync${toQuery({ steamid })}`),
  syncStatus: () => request<AchievementSyncSnapshot>('GET', '/achievements/sync'),
}

// ─── 游戏生涯（称号 / 热力图 / 偏好画像 / 纪录 / 评语墙）──────────
// 后端只吐原始度量：称号阈值、评分权重与全部文案在前端（lib/careerTitles.ts
// 与词典），因此调阈值不用改接口。

/** 生涯里反复出现的游戏摘要（封面图始终来自 store header_image） */
export interface CareerGame {
  appid: number
  name: string
  headerImage: string | null
  playtimeMin: number
  unlocked: number
  total: number
  platinum: boolean
}

/** 时长/偏好排行行（类型、开发商、发行商、系列） */
export interface CareerTasteRow {
  games: number
  playtimeMin: number
  platinum: number
  genre?: string
  name?: string
}

/** 生涯口味画像（雷达/条形/年代构成的数据源） */
export interface CareerTasteProfile {
  genres: (CareerTasteRow & { genre: string })[]
  developers: (CareerTasteRow & { name: string })[]
  publishers: (CareerTasteRow & { name: string })[]
  series: (CareerTasteRow & { name: string })[]
  decades: { decade: string; games: number; playtimeMin: number }[]
  chineseGames: number
  freshGames: number
  avgReleaseYear: number
  oldestGame: (CareerGame & { releaseDate: string }) | null
  newestGame: (CareerGame & { releaseDate: string }) | null
}

/** 系列进度行（按 games.series_id 聚合，只含拥有 ≥2 款的系列） */
export interface CareerSeriesRow {
  /** 系列标识（games.series_id 原样；展示名见 name） */
  seriesId: string
  /** 展示名：成员展示名的最长公共汉字前缀，否则回落标识 */
  name: string
  owned: number
  played: number
  platinum: number
  /** 系列内已全成就（unlocked ≥ total）的作品数 */
  completed: number
  /** 全拥有作品的成就账 */
  unlocked: number
  total: number
  /** 只算已玩成员的成就账（进度条口径） */
  playedUnlocked: number
  playedTotal: number
  playtimeMin: number
  /** playedUnlocked / playedTotal，0~1；无已玩成员时为 0 */
  progress: number
  /** 系列封面：该系列时长最高的一款 */
  topGame: CareerGame | null
  /** 缺口最小的一款（0 < unlocked < total），最有行动价值的下一步 */
  nextGame: (CareerGame & { unlocked: number; total: number; remaining: number }) | null
}

export interface CareerSeriesPayload {
  rows: CareerSeriesRow[]
  /** 拥有 ≥2 款的系列数 */
  seriesTotal: number
  /** 有系列标记的系列数（含单款） */
  taggedTotal: number
  /** 全白金系列数（每款都白金且至少一款） */
  perfected: number
}

export interface CareerPayload {
  hasCredential: boolean
  steamid: string
  lastSyncedAt: string | null
  playtime: {
    totalMin: number
    playedGames: number
    avgMin: number
    medianMin: number
    maxMin: number
    maxGame: CareerGame | null
    over10h: number
    over20h: number
    over50h: number
    over100h: number
    over200h: number
    idleGames: number
    untouchedGames: number
    /** 六档时长分布（分档文案由前端按序取 key） */
    histogram: number[]
  }
  trophy: {
    total: number
    unlocked: number
    rate: number
    gamesWithAchievements: number
    perfect: number
    platinumRate: number
    rareCount: number
    rareShare: number
    /** 已解锁成就全服占比均值（越低越硬核） */
    avgRarity: number
    /** 每小时解锁数 */
    perHour: number
    rarity: Record<RarityTier, number>
  }
  platinum: {
    count: number
    avgMin: number
    medianMin: number
    fastest: (CareerGame & { date: number }) | null
    slowest: (CareerGame & { date: number }) | null
    genres: { genre: string; count: number }[]
    spanDays: number
    perYear: { year: number; count: number }[]
    firstDate: number
    lastDate: number
  }
  activity: {
    /** 稀疏日表：'YYYY-MM-DD' → 当日解锁数 */
    days: Record<string, number>
    firstDate: string
    lastDate: string
    activeDays: number
    totalUnlocks: number
    currentStreak: number
    longestStreak: number
    longestStreakEnd: number
    busiestDay: { date: string; count: number } | null
    hourHistogram: number[]
    weekdayHistogram: number[]
    /** 7×24 展开的星期×时段矩阵（星期为主序） */
    hourWeekday: number[]
    monthly: { month: string; count: number }[]
    maxGapDays: number
    nightUnlocks: number
    morningUnlocks: number
    dayUnlocks: number
    eveningUnlocks: number
    weekendUnlocks: number
    firstUnlock: (AchievementRow & { headerImage?: string }) | null
    spanDays: number
  }
  yearly: { year: number; unlocks: number; games: number; platinum: number }[]
  taste: CareerTasteProfile
  series: CareerSeriesPayload
  milestones: {
    kind: 'count' | 'rarest'
    index: number
    at: number
    name: string
    icon: string
    gameName: string
    appid: number
    headerImage?: string
    globalPercent?: number
  }[]
  quotes: {
    name: string
    text: string
    icon: string
    globalPercent: number | null
    appid: number
    gameName: string
  }[]
  library: {
    valueFen: number
    pricedGames: number
    costPerHourFen: number
    avgPositiveRate: number
    topValue: (CareerGame & { priceFen: number; positiveRate: number }) | null
  }
  records: {
    fastestComplete: (CareerGame & { spanMin: number; spanSec: number; fromTime: number; toTime: number; unlocks: number }) | null
    slowestComplete: (CareerGame & { spanMin: number; spanSec: number; fromTime: number; toTime: number; unlocks: number }) | null
    marathonDay: { date: string; count: number } | null
    busiestHour: number
    mostUnlocksGame: (CareerGame & { unlocked: number }) | null
    biggestPlatinum: (CareerGame & { date: number }) | null
  }
  spotlight: CareerGame[]
  /** 开了坑没拿满的游戏（feed 未完待续墙） */
  unfinished: (CareerGame & { remaining: number })[]
  unfinishedCount: number
  /** 有实际时长但最后一次启动最早的那批（feed 尘封角落） */
  dormant: (CareerGame & { lastPlayed: number })[]
  completedGames: number
}

export const careerApi = {
  /** 生涯全量度量（一次读取喂满全部子模块）。
   *  默认吃 60s 时间窗缓存（聚合不便宜），只有用户点「重新推导」才绕开。 */
  career: (fresh = false, steamid = '') =>
    request<CareerPayload>('GET', `/achievements/career${toQuery({ steamid })}`, undefined, {
      noCache: fresh,
    }),
}
