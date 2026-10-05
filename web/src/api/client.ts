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
  '/notifications/facts', // 内容链事实通知 60s 轮询（增量游标语义）
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
  /** 每日自动备份开关：false = 只留手动备份（启动补备同闸） */
  backup_auto: boolean
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

export interface FetchSettingsPayload {
  /** Epic 免费游戏：每日核对 + 首页卡片 */
  epic_free: boolean
  /** Humble Choice 当月包核对 */
  hb_choice: boolean
  /** Steam 榜单四板（热销/特惠/新品/即将推出） */
  boards: boolean
  /** 第三方档案进包计数 */
  bundle_counts: boolean
  /** 汇率每日自动更新 */
  fx_auto: boolean
  /** 汇率历史缺口修复 */
  fx_history: boolean
  /** 价格刷新网格步长（小时，1..72，默认 6） */
  price_interval_hours: number
  /** 目录层随价格更新：True = 每轮价格更新带上未关注的目录游戏；下一轮生效 */
  catalog_refresh: boolean
}

export const settingsApi = {
  get: () => request<SettingsPayload>('GET', '/settings'),
  update: (payload: {
    account?: { steam_id?: string; steam_api_key?: string }
    onboarding_done?: boolean
    auto_price?: boolean
    /** 每日自动备份开关 */
    backup_auto?: boolean
    /** 记录已主动提示过的版本号（启动告知去重用） */
    update_notified?: string
    /** 新版本提示总开关 */
    update_notify?: boolean
    /** 静默自动更新开关 */
    update_auto?: boolean
  }) => request<SettingsPayload>('PUT', '/settings', payload),
  getFetch: () => request<FetchSettingsPayload>('GET', '/settings/fetch'),
  updateFetch: (payload: Partial<FetchSettingsPayload>) =>
    request<FetchSettingsPayload>('PUT', '/settings/fetch', payload),
}

// ─── pilot（领航员：比价助手问答）────────────────────────

/** 模型供应商（绑定页左导航 / 菜单分组的账本投影；has_key 不含密钥原文） */
export interface PilotProvider {
  id: string
  name: string
  protocol: string
  base_url: string
  models: string[]
  models_disabled: string[]
  has_key: boolean
  /** 模型上下文窗口（token，用户可选配置）；未知 = null（历史预算回落固定值） */
  context_window: number | null
}

export interface PilotConfigPayload {
  protocol: string
  enabled: boolean
  base_url: string
  model: string
  models: string[]
  /** 被禁用的模型（不进切换菜单；未列出的默认启用） */
  models_disabled: string[]
  has_api_key: boolean
  monthly_cap: number
  /** 活跃供应商的模型窗口（token）；未知 = null */
  context_window: number | null
  usage_inp: number
  usage_out: number
  usage_calls: number
  usage_total: number
  /** 活跃供应商 id（无供应商账本时为空） */
  active: string
  providers: PilotProvider[]
}

/** 领航员入口的游戏上下文（游戏详情页进入时携带） */
export interface PilotGameContext {
  appid: number
  name: string
}

/** 事实摘要（cnyFen 单位为分）：kind=price 单游戏价格事实 / kind=games 候选列表 */
export interface PilotPriceFacts {
  kind: 'price'
  appid: number
  name: string | null
  positiveRate: number | null
  reviewCount: number | null
  cn: { cnyFen: number | null; discount: number } | null
  lowest: { cnyFen: number; snapshotAt: string | null } | null
  year: { minFen: number; maxFen: number; medianFen: number; count: number } | null
  alt?: { region: string; cnyFen: number; discount: number } | null
  /** 近一年国区价格变化点 [YYYY-MM-DD, 分]，按时间升序（阶梯图数据源） */
  trend?: [string, number][]
}

/** 地区对比卡：全区现价中最便宜的若干区 + 账号结算区（cnyFen 升序） */
export interface PilotRegionsFacts {
  kind: 'regions'
  appid: number
  name: string | null
  accountRegion: string
  items: { region: string; display: string; cnyFen: number; discount: number }[]
  count: number
}

/** 多游戏对比卡：2~3 款并排（region 非空 = 国区不可买、价格取自该最低可购区） */
export interface PilotCompareFacts {
  kind: 'compare'
  items: {
    appid: number
    name: string | null
    positiveRate: number | null
    reviewCount: number | null
    cnyFen: number | null
    discount: number
    region: string | null
    lowestFen: number | null
    medianFen: number | null
  }[]
}

export interface PilotGameFactsItem {
  appid: number
  name: string | null
  cnyFen: number | null
  discount: number | null
  positiveRate: number | null
  reviewCount: number | null
  chineseSupport?: string | null
  /** 行内注记（词条化）：key 对应 `pilot.row.{key}`，data 其余键直接插值、at 截日期 */
  note?: { key: string; v?: string; at?: string | null } | null
}

export interface PilotGameFacts {
  kind: 'games'
  /** 卡题词条键片段（`pilot.rows.{titleKey}`）；缺省用通用「候选游戏」 */
  titleKey?: string
  items: PilotGameFactsItem[]
  /** 清单全量数（截前 N 条展示时给模型与标题栏用） */
  total?: number
}

/** 写动作回执（Phase 2 白名单：monitor_add / alert_add，均为单对象可逆动作） */
export interface PilotActionFacts {
  kind: 'action'
  action: 'monitor_add' | 'alert_add'
  appid: number
  name: string | null
  state?: string
  targetType?: string
  targetValueFen?: number | null
  alertId?: number | null
}

/** 导航卡：agent 跳转模块（target 为模块键，path 为站内路由） */
export interface PilotNavigateFacts {
  kind: 'navigate'
  target: string
  path: string
}

/** 行卡：清单与诊断的通用形态。k 为名称/区码等专有名词；vKey 对应词条键
 *  `pilot.row.{vKey}`（data.priceFen 自动格式化为 ¥，data 其余键直接插值）；
 *  v 为原样值（错误摘要/计数串/区码）；at 为 ISO 时刻，前端截日期显示。 */
export interface PilotRowsFacts {
  kind: 'rows'
  titleKey: string
  name?: string | null
  appid?: number | null
  rows: {
    k: string
    /** 左侧词条键（有值时优先于 k，用于后端只能给机器码的行） */
    kindKey?: string
    vKey?: string
    v?: string
    tone?: 'ok' | 'warn' | 'bad'
    at?: string | null
    data?: Record<string, string | number>
  }[]
}

/** 批量提议卡：待用户确认的批量动作清单（提议无副作用，确认后才逐项执行） */
export interface PilotProposalFacts {
  kind: 'proposal'
  pid: string
  action: 'add_follow' | 'create_price_alert' | 'delete'
  items: { appid?: number; name: string; key?: string; reason?: string }[]
  args: { target_type?: string; target_value_yuan?: number }
  state: 'pending' | 'confirmed' | 'rejected' | 'withdrawn' | 'dismissed'
  /** 确认后的结果回填（仅本地展示，后端以业务账本为准） */
  done?: number
  failedCount?: number
}

/** 家庭组卡：成员行复用家庭页的「头像+昵称+角色+地区」形态（family 域同一 payload 投影） */
export interface PilotFamilyFacts {
  kind: 'family'
  bound: boolean
  joined?: boolean
  walletRegion?: string | null
  members: {
    steamid: string
    name?: string | null
    avatar?: string | null
    role?: 'primary' | 'member'
    region?: string | null
  }[]
  total?: number
}

/** 奖杯卡：成就域 summary 的富投影（KPI + 白金陈列 + 最近解锁） */
export interface PilotAchievementFacts {
  kind: 'achievements'
  hasCredential: boolean
  platinum: number
  unlocked: number
  total: number
  completionRate?: number | null
  lastSyncedAt?: string | null
  platinums: { appid: number; name: string | null }[]
  recent: { appid: number; name: string; gameName?: string | null; at?: number | null }[]
}

/** 工作流流水线步骤 */
export interface PilotStepperStep {
  id: number | string
  title: string
  detail?: string
  status: 'wait' | 'running' | 'ok' | 'warn' | 'empty' | 'error'
  current?: number
  total?: number
  badge?: string
}

/** 长任务/工作流分步卡片：阶段式流水线进度展示（如全量扫描与批量提议） */
export interface PilotStepperFacts {
  kind: 'stepper'
  title: string
  description?: string
  currentStepIndex?: number
  steps: PilotStepperStep[]
}

export type PilotFacts =
  | PilotPriceFacts
  | PilotRegionsFacts
  | PilotCompareFacts
  | PilotGameFacts
  | PilotActionFacts
  | PilotNavigateFacts
  | PilotRowsFacts
  | PilotProposalFacts
  | PilotFamilyFacts
  | PilotAchievementFacts
  | PilotStepperFacts

/** agent 时间线步骤：label 对应词条键 `pilot.step.{label}`，data 供词条插值。
 *  status = ok / empty / denied；running 只存在于流式过程中的本地态。 */
export interface PilotStep {
  label: string
  status: 'ok' | 'empty' | 'denied' | 'running'
  data: { count?: number; name?: string | null; target?: string; path?: string }
}

/** agent 循环的一步（阶段）：该步的思考、前言正文与工具步骤同属一条记录 */
export interface PilotPhase {
  step: number
  /** 该步思考原文（单阶段超预算时被截断，见 truncated） */
  thinking: string
  /** 该步正文；terminal 段的正文即终答 */
  text: string
  /** 该步思考工时（毫秒）；无思考为 null */
  think_ms?: number | null
  /** 该步思考超单阶段预算，已停止累加 */
  truncated?: boolean
  /** 该步是终答轮（无工具调用）；步数耗尽的轮次无此标记 */
  terminal?: boolean
  steps: PilotStep[]
}

export interface PilotAskResponse {
  /** LLM 回答原文；facts / guide / none 形态下为空串，展示层按 source 渲染 */
  answer: string
  source: 'llm' | 'facts' | 'guide' | 'none'
  /** 推理模型思维链原文（reasoning_content）；普通模型或降级路径为 null */
  thinking?: string | null
  /** 思考工时（毫秒，思考通道活跃墙钟累计）；无思考/降级轮为 null */
  think_ms?: number | null
  /** 回退机器码（llm_off / cap_reached / llm_failed / no_data），用户语言由前端翻 */
  reason: string | null
  facts: PilotFacts | null
  /** 本轮工具取到的结构化卡片（games / price / action），组件渲染数据层 */
  cards: PilotFacts[]
  /** 本轮 agent 时间线全量（后端 tools.tool_step 产出）；降级路径为空 */
  steps?: PilotStep[]
  /** 本轮按循环步分段的阶段记录（权威）；thinking 与 steps 均由它派生 */
  phases?: PilotPhase[]
  cached: boolean
  /** 本轮发给模型的上下文估算 token 数（agent 路径才有） */
  ctx_tokens?: number | null
  /** 当前会话生效标题（账本 title 行或首问兜底）；标题栏实时显示 */
  title?: string | null
  /** 上下文历史预算（装配让位基准） */
  ctx_budget?: number | null
  /** 上下文构成估算（五来源字符数，agent 路径才有）；分段条占比口径 */
  ctx_breakdown?: { source: string; chars: number }[] | null
  /** 输入缓存命中率（0-1，provider 报告缓存 token 时才有；本轮最近一次请求口径） */
  cache_hit_rate?: number | null
  /** 本轮输入 token 合计（provider 用量口径，含缓存部分；未回传为 0） */
  usage_in?: number
  /** 本轮输出 token 合计（provider 回传口径；未回传为 0） */
  usage_out?: number
  /** 本轮缓存读 token 合计（跨请求累计；provider 从未报告为 null） */
  cache_read_tokens?: number | null
  /** 本轮缓存写 token 合计（仅 Anthropic 系报告；从未报告为 null） */
  cache_write_tokens?: number | null
  /** 本轮计费输入总量（含缓存部分，缓存命中率分母口径）；无用量回传为 null */
  cache_base_tokens?: number | null
  /** 生成墙钟合计（毫秒，各请求首个内容增量→流结束；仅用量同步回传的请求计入） */
  decode_ms?: number
  /** 计入生成墙钟的请求的输出 token 合计（decode_ms 配对分子） */
  decode_out?: number
  /** 首字延迟合计（毫秒，跨请求累计） */
  ttft_ms?: number
  /** 计入首字延迟的请求数 */
  ttft_n?: number
  /** 本轮总耗时（毫秒，agent 路径墙钟，含思考与工具执行）；降级轮为 null */
  elapsed_ms?: number | null
  /** 本轮完成时已归档进要点存档的轮数边界（过程链记忆条目用）；降级轮为 null */
  archived_through?: number | null
}

export interface PilotToolRunResult {
  step: PilotStep
  cards: PilotFacts[]
}

export const pilotApi = {
  getConfig: () => request<PilotConfigPayload>('GET', '/pilot/config'),
  /** 快捷动作直达：+ 菜单点选 → 确定性工具执行（不经模型，省两轮延迟） */
  runTool: (name: string, label: string, sessionId?: string) =>
    request<PilotToolRunResult>('POST', '/pilot/tool', {
      name,
      label,
      session_id: sessionId || undefined,
    }),
  test: (payload: { protocol?: string; base_url?: string; api_key?: string; model?: string }) =>
    request<{ ok: boolean; latency_ms: number; model: string; reply: string; reason: string | null; detail: string }>(
      'POST',
      '/pilot/test',
      payload,
    ),
  detect: (payload: { base_url?: string; api_key?: string; protocol?: string; provider_id?: string }) =>
    request<{ protocol: string; vendor: string; models: string[]; suggested: string[]; key_valid: boolean | null; reason: string | null }>(
      'POST',
      '/pilot/detect',
      payload,
    ),
  updateConfig: (payload: {
    protocol?: string
    enabled?: boolean
    base_url?: string
    model?: string
    api_key?: string
    monthly_cap?: number
    models_disabled?: string[]
    /** 切换活跃供应商（模型自动重置为新家启用清单） */
    active?: string
  }) => request<PilotConfigPayload>('PUT', '/pilot/config', payload),
  listProviders: () =>
    request<{ items: PilotProvider[] }>('GET', '/pilot/providers', undefined, { noCache: true }),
  createProvider: (payload: { name?: string; protocol?: string; base_url?: string; api_key?: string; models?: string[]; context_window?: number | null }) =>
    request<PilotProvider>('POST', '/pilot/providers', payload),
  updateProvider: (
    id: string,
    payload: { name?: string; protocol?: string; base_url?: string; models?: string[]; models_disabled?: string[]; api_key?: string; context_window?: number | null },
  ) => request<PilotProvider>('PUT', `/pilot/providers/${encodeURIComponent(id)}`, payload),
  deleteProvider: (id: string) =>
    request<{ ok: boolean }>('DELETE', `/pilot/providers/${encodeURIComponent(id)}`),
  ask: (question: string, appid?: number) =>
    request<PilotAskResponse>('POST', '/pilot/ask', { question, appid }),
  /** 会话账本读取（历史轮还原）；无会话返回 404 */
  getSession: (sessionId: string) =>
    request<PilotSessionOut>('GET', `/pilot/sessions/${encodeURIComponent(sessionId)}`, undefined, { noCache: true }),
  /** 手动归档：早期轮次蒸馏进要点存档（与自动压缩同一实现）；机器码见 reason */
  compactSession: (sessionId: string) =>
    request<{ ok: boolean; reason?: string | null; summary_through?: number; turn_total?: number }>(
      'POST',
      `/pilot/sessions/${encodeURIComponent(sessionId)}/compact`,
    ),
  /** 历史会话清单（mtime 倒序投影） */
  listSessions: (limit = 50) =>
    request<{ items: PilotSessionListItem[] }>('GET', `/pilot/sessions?limit=${limit}`, undefined, { noCache: true }),
  /** 会话改名（账本追加 title 记录，后者胜） */
  renameSession: (sessionId: string, text: string) =>
    request<{ ok: boolean }>('PUT', `/pilot/sessions/${encodeURIComponent(sessionId)}/title`, { text }),
  /** 删除会话文件（与 TTL 清扫同机制） */
  deleteSession: (sessionId: string) =>
    request<{ ok: boolean }>('DELETE', `/pilot/sessions/${encodeURIComponent(sessionId)}`),
  /** 批量提议确认：approve 为真逐项执行既有写动作，为假作废；机器码见 reason */
  confirmProposal: (sessionId: string, pid: string, approve: boolean) =>
    request<{
      ok: boolean
      state: string
      action: string
      total: number
      done: number
      failed: { appid: number; name: string | null }[]
      reason: string | null
    }>('POST', '/pilot/proposal/confirm', { session_id: sessionId, pid, approve }),
}

/** 会话账本里的一轮问答：后端落盘的历史投影，前端按其还原历史轮 */
export interface PilotSessionTurn {
  q: string
  resp: PilotAskResponse
  ts: string | null
}

/** 压缩边界：第 after_turn 轮之后发生过一次压缩（对话流分隔线依据） */
export interface PilotCompactMarker {
  after_turn: number
  trigger: string
  ts: string | null
  pre_tokens?: number | null
  post_tokens?: number | null
}

/** 会话级统计投影（后端对账本全部轮次的只读折叠；底栏统计与消耗明细种子） */
export interface PilotSessionStats {
  turns: number
  steps: number
  elapsed_ms: number
  ttft_ms: number
  ttft_n: number
  /** 速度口径 = decode_out / decode_ms（加权聚合，非各轮速率均值） */
  decode_ms: number
  decode_out: number
  usage_in: number
  usage_out: number
  cache_read_tokens: number | null
  cache_write_tokens: number | null
  /** 计费输入合计（缓存命中率分母；null 时无命中率可言） */
  cache_base_tokens: number | null
}

export interface PilotSessionOut {
  session_id: string
  turns: PilotSessionTurn[]
  updated_at: string | null
  /** 账本总轮数 */
  turn_total: number
  /** 已压缩进要点存档的轮数边界 */
  summary_through: number
  /** 压缩边界列表（按发生顺序），前端在对话流对应位置插分隔线 */
  markers?: PilotCompactMarker[]
  /** 会话生效标题（账本 title 行或首问兜底） */
  title: string
  /** 本会话出现过的批量提议终态（按 pid）：历史轮卡片据此校正可点性 */
  proposals?: { pid: string; state: string; done: number | null; failedCount?: number }[]
  /** 全部轮次统计折叠（空会话为 null） */
  stats?: PilotSessionStats | null
}

/** 历史会话清单项（账本只读投影） */
export interface PilotSessionListItem {
  sid: string
  title: string
  turn_total: number
  /** 账本 mtime（ISO）：最近一次落账时刻 */
  updated_at: string | null
  /** ask 流在途（agent 正在生成本会话回复） */
  running?: boolean
}

export interface PilotStreamEvent {
  type: 'ack' | 'busy' | 'step_start' | 'thinking' | 'answer' | 'tool_start' | 'tool' | 'facts' | 'done' | 'error'
  /** ack：全局在飞轮数（含本轮）——供应商按 Key 排队时前端据此显示等待态 */
  active?: number
  /** step_start：进入第几步（阶段边界，先于该步任何增量） */
  step?: number
  delta?: string
  name?: string
  /** tool / tool_start 事件的步骤词条片段与终态（见 PilotStep） */
  label?: string
  status?: PilotStep['status']
  data?: PilotStep['data']
  facts?: PilotFacts | null
  answer?: string
  /** 推理模型思维链原文；普通模型或降级路径为 null */
  thinking?: string | null
  /** 思考工时（毫秒，思考通道活跃墙钟累计）；无思考/降级轮为 null */
  think_ms?: number | null
  /** 本轮装配发生了上下文压缩（对话流插分隔线；ctx 水位已回落） */
  compacted?: boolean
  source?: PilotAskResponse['source']
  reason?: string | null
  steps?: PilotStep[]
  /** 本轮按循环步分段的阶段记录（done 事件携带，权威） */
  phases?: PilotPhase[]
  cached?: boolean
  title?: string | null
  ctx_tokens?: number | null
  ctx_budget?: number | null
}

/** 流式问答：SSE 帧解析（data: JSON / data: [DONE]），事件逐个回调。
 *  thinking 通道 = 推理模型思维链增量；普通模型只有 answer 通道。
 *  sessionId 串起同一次开舱内的追问与指代（候选待定 / 上一款游戏）。 */
export async function askPilotStream(
  question: string,
  appid: number | undefined,
  sessionId: string | undefined,
  onEvent: (e: PilotStreamEvent) => void,
  referenceSid?: string,
  signal?: AbortSignal,
): Promise<void> {
  const resp = await fetch(`${BASE}/pilot/ask/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question, appid, session_id: sessionId, reference_sid: referenceSid || undefined }),
    signal,
  })
  if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`)
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let idx: number
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      for (const line of raw.split('\n')) {
        if (!line.startsWith('data:')) continue
        const data = line.slice(5).trim()
        if (!data || data === '[DONE]') continue
        try {
          onEvent(JSON.parse(data) as PilotStreamEvent)
        } catch {
          /* 坏帧跳过，不影响后续 */
        }
      }
    }
  }
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
  /** 最近一次尝试时刻（成功失败都写） */
  checked_at: string | null
  /** 上次成功获取余额的时刻（失败改写时保留；显示层缓存窗判定用） */
  ok_at?: string | null
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

/** 应用内账号密码登录的会话状态快照（后端状态机，见 /account/login/*） */
export interface LoginSessionState {
  state:
    | 'idle'
    | 'signing'
    | 'awaiting_code'
    | 'awaiting_confirmation'
    | 'finalizing'
    | 'done'
    | 'failed'
  message: string
  error: string
  /** awaiting_code 时的验证码来源提示：email = 邮箱验证码，totp = 手机令牌 */
  code_hint: '' | 'email' | 'totp'
  /** 会话是否具备可输码形态（等待确认态下可切换为输码） */
  code_available?: boolean
  started_at: string
  updated_at: string
}

export const accountLoginApi = {
  /** 发起登录；后端 409 = 已有登录进行中（busy） */
  start: (accountName: string, password: string) =>
    request<{ ok: boolean; busy?: boolean; state: LoginSessionState; error?: string }>(
      'POST',
      '/account/login/start',
      { account_name: accountName, password },
    ),
  status: () => request<LoginSessionState>('GET', '/account/login/status', undefined, { noCache: true }),
  code: (code: string) =>
    request<{ ok: boolean; state: LoginSessionState; error?: string }>(
      'POST',
      '/account/login/code',
      { code },
    ),
  cancel: () => request<{ ok: boolean; state: LoginSessionState }>('POST', '/account/login/cancel'),
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
  /** 服务端判定的地区（手动 > 钱包结算区 > 资料国家）；null = 未设置 */
  region: string | null
  regionSource: 'manual' | 'wallet' | 'profile' | null
}

/** 一个绑定账号的家庭组记录（家庭组跟随该账号的 Cookie 发现） */
export interface FamilyGroupStatus {
  steamid: string
  accountName: string
  /** 是否同步过家庭组（false = 尚无快照行） */
  synced: boolean
  /** true=已加入 / false=确认未加入 / null=未同步 */
  joined: boolean | null
  familyName: string | null
  familyGroupid: string | null
  updatedAt: string | null
  lastError: string | null
  members: FamilyMemberItem[]
}

export interface FamilySyncResultItem {
  steamid: string
  joined: boolean
  familyName?: string
  memberCount?: number
  error?: string
}

export interface FamilyStatus {
  bound: boolean
  primarySteamid: string
  steamid?: string
  /** 各已加入组成员按 steamid 去重的并集（赠礼等跨组消费方直接取用） */
  members: FamilyMemberItem[]
  groups: FamilyGroupStatus[]
  joined?: boolean
  message?: string
  lastError?: string | null
  /** 主账号钱包结算区（兼容字段；成员地区以 members[].region 为准） */
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
  tags: GameTag[]
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
  tags: GameTag[]
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
  sync: (steamId?: string) =>
    request<{ results: FamilySyncResultItem[]; synced: number; joined: number; failed: number }>(
      'POST',
      `/family/sync${toQuery(steamId ? { steam_id: steamId } : {})}`,
    ),
  status: () => request<FamilyStatus>('GET', '/family/status'),
  saveMemberRegions: (regions: Record<string, string>) =>
    request<{ ok: boolean; memberRegions: Record<string, string> }>(
      'PUT',
      '/family/member-regions',
      { regions },
    ),
  library: (steamId?: string) =>
    request<FamilyLibraryPayload>(
      'GET',
      `/family/library${toQuery(steamId ? { steam_id: steamId } : {})}`,
    ),
  refreshLibrary: (steamId?: string) =>
    request<FamilyLibraryPayload>(
      'POST',
      `/family/library/refresh${toQuery(steamId ? { steam_id: steamId } : {})}`,
    ),
  wishlist: (steamId?: string) =>
    request<FamilyWishlistPayload>(
      'GET',
      `/family/wishlist${toQuery(steamId ? { steam_id: steamId } : {})}`,
    ),
}

// ─── games ───────────────────────────────────────────────

/** 覆盖（活表尝试状态口径）：每个游戏×区的最近一次抓取结果，与触发方
    无关（自动轮/手动/补抓写同一处）。成功观察 = 拿到 Steam 明确答复
    （含 locked / 无购买选项）；failed = 传输类失败（旧价保留展示）。 */
export interface PriceCoverage {
  /** 分母：用户启用区服数 */
  expectedUnits: number
  /** 成功观察区数（含 locked / no_options） */
  success: number
  /** 本次失败区数（传输类） */
  failed: number
  /** 应抓但尚无尝试记录的区数 */
  notAttempted: number
  coverage: number | null
  /** 区级问题明细（大写区码 → 尝试态），只含非成功区：
      failed 带 lastSuccessAt（「展示的是 X 时刻的数据」） */
  regions?: Record<
    string,
    { outcome: 'failed' | 'notAttempted'; answer: string | null; lastSuccessAt: string | null }
  >
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

/** 捆绑包价格数据状态：与 PriceData 同口径（观察时刻/新鲜度分档），无
    coverage——捆绑包不属价格刷新 Cycle 的期望集 */
export type BundlePriceData = Omit<PriceData, 'coverage'>

/** 游戏热门用户标签（Steam 玩家自定义标签；数组顺序即票重降序 = 热门程度，
    至少一个语言对照到名字的冷门标签不会下发；name=中文名 nameEn=英文名） */
export interface GameTag {
  tagid: number
  name: string
  nameEn?: string | null
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
  priceMatrix: Record<string, [string, number, number, number, boolean?]>
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
  /** 促销截止（Unix 秒；null=无促销/未下发促销元数据） */
  discountEndsAt?: number | null
  cnyFen: number | null
  /** 该区实际包含的 AppID（锁区检测依据）。**仅详情下发** */
  appIds?: number[]
  /** 相对基准区缺失的游戏数（部分锁区）。**仅详情下发** */
  lockedCount?: number
  /** 服务端按 minor units + 币种格式化的展示字符串。**仅详情下发** */
  formatted?: string
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
  /** 价格数据状态（观察时刻/新鲜度，与游戏卡同口径）；列表聚合是缓存载荷，
      ageHours/freshness 冻结在构建时刻——展示期以 observedAt 现算为准 */
  priceData?: BundlePriceData | null
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
  tags: GameTag[]
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
  /** 现价矩阵完全为空（无任何可用观察行）——「重新探测」横幅显隐依据 */
  storeDataMissing: boolean
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
  /** 史低/永降标记过滤：hl=新史低+平史低、pp=永降、any=并集（降价动态 feed）；
   *  new/flat/nonhl=史低三态，可逗号组合按 OR 叠加（游戏库史低多选） */
  flag?: string
  /** 送礼分析模式：out=我可以送给谁（giftSender 固定送礼方）；
   *  in=哪些游戏可以低价送给我（giftReceivers 单收礼方，送礼方全区自动遍历）。
   *  判据=收礼侧区价 ≤ 送礼侧区价×1.15（付款双轨的送礼方价轨） */
  giftMode?: string
  /** out 模式送礼方区码（小写）；in 模式不使用 */
  giftSender?: string
  /** out 模式=目标地区 csv 多选；in 模式=收礼方单值 */
  giftReceivers?: string
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
  /** 关注和愿望单优先：关注恒置顶，开启后愿望单成员（含家庭愿望单）叠加置顶前缀 */
  wishlistPriority?: boolean
  /** 游戏商店默认隐藏 DLC（白名单豁免个别常驻 DLC）；false = 含 DLC */
  excludeDlc?: boolean
  /** 目录移除作用域：false/缺省 = 常规列表（隐藏已移除款）；true = 只出已移除款（恢复视图） */
  removed?: boolean
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
  /** 批量移出游戏商店（假删除：列表隐藏 + 停止取价；单批上限 500，超出分批调） */
  remove: (appids: number[]) =>
    request<{ removed: number; missing: number }>('POST', '/games/remove', { appids }),
  /** 批量恢复被移除的游戏（回到商店并补一次取价；单批上限 500，超出分批调） */
  restore: (appids: number[]) =>
    request<{ restored: number; missing: number }>('POST', '/games/restore', { appids }),
}

// ─── bundles（捆绑包浏览视图：列表聚合 + 补齐计算详情） ──────────────────

export const bundlesApi = {
  /** 全量捆绑包（sort=diff 差价降序 | smart 智能评分降序，服务端预计算列；
   *  discount 折扣力度由前端排序，服务端按 diff 出底序）。
   *  removed=true 只出已移除/已排除的包（恢复视图） */
  list: (sort: 'diff' | 'smart' | 'discount' = 'diff', removed = false) =>
    request<{ bundles: BundleSummary[] }>(
      'GET',
      `/bundles${toQuery({ sort, removed: removed || undefined })}`,
    ),
  /** 单包详情：列表字段 + 包内游戏各区现价（补齐计算求和用） */
  detail: (bundleId: number | string) =>
    request<BundleDetail>('GET', `/bundles/${bundleId}`),
  /** 导入捆绑包/Sub：Steam 商店或 SteamDB 链接（/bundle/ 或 /sub/）、裸 ID */
  importBundle: (text: string) =>
    request<BundleImportResult>('POST', '/bundles/import', { text }),
  /** 当前关注的包 id 全集（升序；卡片星标状态一次性整表拉取） */
  follows: () => request<{ bundleIds: number[] }>('GET', '/bundles/follows'),
  /** 关注一个包（挂 favorite 来源，列表置顶） */
  follow: (bundleId: number) =>
    request<{ bundleId: number; followed: boolean }>('PUT', `/bundles/${bundleId}/follow`),
  /** 取消关注：只摘 favorite 来源 */
  unfollow: (bundleId: number) =>
    request<{ bundleId: number; followed: boolean }>('DELETE', `/bundles/${bundleId}/follow`),
  /** 移除一个包：列表隐藏 + 退出刷新（可在已移除视图恢复） */
  remove: (bundleId: number) =>
    request<{ removed: boolean; bundleId: number }>('POST', `/bundles/${bundleId}/remove`),
  /** 恢复被移除的包（只解除移除链路所挂的排除） */
  restore: (bundleId: number) =>
    request<{ restored: boolean; bundleId: number }>('POST', `/bundles/${bundleId}/restore`),
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
  tags: GameTag[]
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
  cycleId: number | null
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
  /** 轮批次总账：分母 = 建轮冻结的预估（欠账段不在内），done = 已收尾段已处理量 */
  batchesExpected: number | null
  batchesDone: number | null
  enteredRepairing: boolean
  startedAt: string | null
  finishedAt: string | null
  error: string | null
  stats: Record<string, number | null> | null
}

export const crawlApi = {
  stop: (jobId?: number) =>
    request<{ stopped: boolean }>('POST', '/crawl/stop', jobId ? { jobId } : {}),
  jobs: (limit = 20) => request<CrawlJob[]>('GET', `/crawl/jobs${toQuery({ limit })}`),
  active: () =>
    request<{
      activeJobId: number | null
      busy?: boolean
      /** 池体检（整池串行探测）进行中——不是任务，任务列表看不到 */
      maintenance?: boolean
      /** 正在限流窗口外排队的请求数（>0 = 任务启动后请求还没放行） */
      throttled?: number
    }>('GET', '/crawl/active'),
  /**
   * 启动爬取。cooldown = missing/repair 补抓冷却覆盖（分钟），0 = 立即补，
   * 不传 = 通道默认（补抓失败地区入口传 0，其余调用方不传）。
   */
  run: (scope: string, appids?: number[], kind?: string, cooldown?: number) =>
    request<{ id: number; count: number; regions: string[] | null }>('POST', '/crawl/run', {
      scope,
      appids,
      kind,
      cooldown,
    }),
  /** 启动默认全队列（scope=all）：与自动价格轮同组成，后台串行链立即受理。
      受理前后端预解析各段款数，全空直接 400（用户语言）；
      total = pool+catalog+specials 款数和（missing 段是欠账存在性 0/1） */
  runAll: () =>
    request<{
      queued: boolean
      scope: string
      total: number
      segments: { name: string; count: number }[]
    }>('POST', '/crawl/run', {
      scope: 'all',
    }),
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
  /** 全库最近一次成功价格观察（灵动岛时钟事实源，活表 last_success_at 的 MAX；
   *  自动轮/手动/补抓/回填的成功写入同样推进；null=从未成功观察） */
  latestObservation: () => request<{ lastSuccessAt: string | null }>(
    'GET',
    '/crawl/freshness/latest',
  ),
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

/** 密钥保护状态：凭据静态加密的密钥托管方式与解锁态 */
export interface SecurityStatus {
  /** legacy=机器绑定（默认）/ dpapi=系统凭据保护 / passphrase=口令保护 / unknown=密钥文件损坏 */
  mode: 'legacy' | 'dpapi' | 'passphrase' | 'unknown'
  /** 口令模式下未解锁时为 true：此间凭据不可读写 */
  locked: boolean
  /** 系统凭据保护（Windows DPAPI）在当前平台是否可用 */
  dpapi_available: boolean
  error: string
}

/** 加密导出文件（.hxexport） */
export interface ExportItem {
  name: string
  sizeBytes: number
  createdAt: string
}

export interface ExportResult {
  path: string
  name: string
  sizeBytes: number
  createdAt: string
  /** 各用户数据表导出行数 */
  counts: Record<string, number>
  /** 明确排除的公共数据说明（随包种子/目录/价格） */
  excludes: string[]
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
  // 密钥保护（凭据静态加密的密钥托管：机器绑定 / 系统凭据 / 口令）
  securityStatus: () => request<SecurityStatus>('GET', '/system/security'),
  /** 切换保护方式：后端全库换钥重加密（失败不改动库与密钥文件） */
  securitySetMode: (mode: string, passphrase?: string, currentPassphrase?: string) =>
    request<{ ok: boolean; counts: Record<string, number>; security: SecurityStatus }>(
      'POST',
      '/system/security/mode',
      { mode, passphrase: passphrase || null, current_passphrase: currentPassphrase || null },
    ),
  securityUnlock: (passphrase: string) =>
    request<{ ok: boolean; security: SecurityStatus }>('POST', '/system/security/unlock', {
      passphrase,
    }),
  securityLock: () =>
    request<{ ok: boolean; security: SecurityStatus }>('POST', '/system/security/lock'),
  // 敏感数据加密导出（只含用户侧数据；不含随包种子与公共目录/价格数据）
  exportCreate: (password: string) => request<ExportResult>('POST', '/export', { password }),
  exportList: () => request<{ items: ExportItem[] }>('GET', '/export/list'),
  exportRemove: (name: string) =>
    request<{ removed: boolean }>('DELETE', `/export/${encodeURIComponent(name)}`),
  /** 导出文件下载地址（浏览器直下，不经 request 封装） */
  exportDownloadUrl: (name: string) => `${BASE}/export/download/${encodeURIComponent(name)}`,
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
  /** 内核随服务自启开关（undefined = 未拉到，UI 按开处理） */
  autostart?: boolean
  /** 自动节点体检开关（undefined = 未拉到，UI 按开处理） */
  healthAuto?: boolean
}

export interface ClashStatus {
  running: boolean
  port: number | null
  configPath: string | null
  /** 内核当前跑的订阅 URL（启动/切换时登记）——检测结果归属的事实源 */
  subscriptionUrl: string | null
  kernel: { found: boolean; path: string | null; builtin: boolean }
  version: string | null
  kernelDir: string
  /** 用户最近一次显式选中的订阅（点选/切换/启动成功都落库）——回显与启动缺省同源 */
  selectedSubscriptionId: number | null
  /** 启动响应扩展：实际拉起内核用的订阅（候选遍历后胜出的那条） */
  subscription?: { id: number; label: string | null }
  /** 启动响应扩展：非空 = 请求的订阅取不到配置，已降级到其他订阅启动 */
  fallbackFrom?: { id: number; label: string | null } | null
}

/** 选中订阅即切换（内核热重载，进程不动）；switched=false = 本就在跑这条 */
export interface ClashSwitchResult {
  switched: boolean
  subscriptionUrl: string | null
  port: number | null
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
  setStrategy: (payload: Partial<ProxyStrategy>) =>
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
  clashSwitch: (subscriptionId: number) =>
    request<ClashSwitchResult>('POST', '/proxies/clash/switch', { subscriptionId }),
  /** 内核未运行时记录订阅行点选（跨页面与重启保留，启动缺省用它） */
  clashSelect: (subscriptionId: number) =>
    request<{ selected: number }>('POST', '/proxies/clash/select', { subscriptionId }),
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
  /** 内容链事实通知（灵动岛轮询）：afterId 缺省 = 只对齐游标不回历史 */
  facts: (afterId?: number) =>
    request<FactNoticeBatch>(
      'GET',
      afterId != null ? `/notifications/facts?afterId=${afterId}` : '/notifications/facts',
      undefined,
      { noCache: true },
    ),
}

/** 一条内容链事实变化（HB 当月包换新 / Epic 喜加一轮换）。文案由前端按
 *  source+kind 映射词条现译，后端只发结构化事实。 */
export interface FactNotice {
  id: number
  source: 'hb_choice' | 'epic_free' | string
  kind: 'bundle_changed' | 'free_rotation' | string
  /** 消费参数：HB = {label, productName, count}；Epic = {count, titles[]} */
  data: { label?: string; productName?: string; count?: number; titles?: string[] } | null
  occurredAt: string | null
}

export interface FactNoticeBatch {
  latestId: number
  items: FactNotice[]
}

/** 历史窗口档位（null = 档案全量） */
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
  /** busy = 已有同步在跑（含定时任务），本次未重复发起 */
  status?: string
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

/** 时长/偏好排行行（标签、开发商、发行商、系列） */
export interface CareerTasteRow {
  games: number
  playtimeMin: number
  platinum: number
  tag?: string
  name?: string
}

/** 生涯口味画像（雷达/条形/年代构成的数据源） */
export interface CareerTasteProfile {
  tags: (CareerTasteRow & { tag: string })[]
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
    tags: { tag: string; count: number }[]
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

// ─── proxypool 生产作业台账（/proxies 页「生产作业」分节）──────────────
// 只读：一次真实爬取作业一行，只记事实，不带健康分/等级/判死结论。

export type ProxyJobRunStatus = 'running' | 'success' | 'partial' | 'failed' | 'interrupted'

export interface ProxyJobRunSummary {
  /** 本地日历日（与 crawl_jobs.startedAt 同源） */
  day: string
  runs: number
  success: number
  partial: number
  failed: number
  interrupted: number
  running: number
  /** null = 当日还没有带耗时的作业（显示 —，**不是 0**） */
  avgDurationMs: number | null
  /** null = 池内出口 IP 尚未探测（显示 —，**不是 0**） */
  poolExitIpCount: number | null
}

export interface ProxyJobRunItem {
  id: number
  status: ProxyJobRunStatus
  kind: string
  startedAt: string | null
  finishedAt: string | null
  durationMs: number | null
  taskCount: number | null
  successCount: number | null
  errorCount: number | null
  /** GLOBAL 选中节点（`<订阅>|<原名>`）；读不到为 null */
  node: string | null
  nodeExitIp: string | null
  poolNodeCount: number | null
  poolExitIpCount: number | null
  poolSha256: string | null
  proxyUrl: string | null
  regions: string[]
  workers: number | null
  subscriptionIds: number[]
  snapshotIds: number[]
  /** Active/Candidate 尚未实现：现在恒为 null */
  activeSubscriptionId: number | null
  /** null = 此行没有错误汇总（与「零失败」不是一回事） */
  byError: Record<string, number> | null
  errorSummaryTruncated: boolean
  interrupted: boolean
}

export interface ProxyJobRunsPayload {
  summary: ProxyJobRunSummary
  total: number
  items: ProxyJobRunItem[]
}

/** 单次作业的出口聚合行（按出口 × 端点记账） */
export interface ProxyRunExitRow {
  exitIp: string
  endpoint: string
  node: string | null
  requests: number
  success: number
  e429: number
  e4xx: number
  e5xx: number
  timeout: number
  connectError: number
  other: number
  durationMs: number
}

export interface ProxyJobRunDetail extends ProxyJobRunItem {
  exits: ProxyRunExitRow[]
}

export const proxypoolApi = {
  jobRuns: (limit = 20, offset = 0) =>
    request<ProxyJobRunsPayload>(
      'GET',
      `/proxypool/job-runs${toQuery({ limit, offset })}`,
      undefined,
      { noCache: true },
    ),
  jobRun: (id: number) =>
    request<ProxyJobRunDetail>(
      'GET',
      `/proxypool/job-runs/${id}`,
      undefined,
      { noCache: true },
    ),
}

// ─── Steam 官方活动日历（季节大促 / Next Fest / 主题 Fest）───

export type SteamEventCategory = 'seasonal_sale' | 'next_fest' | 'themed_fest'

export interface SteamEventItem {
  /** 稳定标识（不随界面语言漂移；价格历史周期标签同源） */
  key: string
  category: SteamEventCategory
  nameEn: string
  nameZh: string | null
  /** 活动日（PT 口径，YYYY-MM-DD） */
  start: string
  end: string
  /** 精确 Unix 起止（活动页上线后由商店页元数据补齐；null = 暂无） */
  preciseStartTs: number | null
  preciseEndTs: number | null
  storeUrl: string | null
}

export interface SteamEventsPayload {
  events: SteamEventItem[]
  /** 正在进行中（今天落在窗口内） */
  live: SteamEventItem[]
  /** 最近一个尚未开始的活动；null = 无未来活动 */
  next: SteamEventItem | null
  /** 上次成功同步时刻（北京时间 ISO）；null = 尚未同步过 */
  fetchedAt: string | null
  /** true = 数据已超 7 天未同步（同步源不可达或开关关闭） */
  stale: boolean
}

export const steamEventsApi = {
  list: () => request<SteamEventsPayload>('GET', '/steam-events'),
  sync: () => request<{ count: number; backfilled: number; fetchedAt: string }>(
    'POST',
    '/steam-events/sync',
    undefined,
    { noCache: true },
  ),
}
