<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import {
  ApiError,
  askPilotStream,
  pilotApi,
  type PilotAskResponse,
  type PilotFacts,
  type PilotGameFactsItem,
  type PilotNavigateFacts,
  type PilotPhase,
  type PilotPriceFacts,
  type PilotProvider,
  type PilotRegionsFacts,
  type PilotSessionListItem,
  type PilotSessionStats,
  type PilotStep,
} from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { mdLiteToHtml } from '@/lib/markdownLite'
import { buildCtxSegments, shouldShowCacheHitRate } from '@/lib/ctxSegments'
import { fmtTokens, pilotCoverUrl, positivePct, ratingClass, sessionTime } from '@/lib/pilotView'
import HlImg from '@/components/ui/HlImg.vue'
import HlPilotCards from '@/components/business/HlPilotCards.vue'
import HlPilotChain from '@/components/business/HlPilotChain.vue'
import HlPilotOutline from '@/components/business/HlPilotOutline.vue'
import HlPilotStats from '@/components/business/HlPilotStats.vue'
import { useRouter } from 'vue-router'
import { useI18n, type MessageKey } from '@/locales'
import HlButton from '@/components/ui/HlButton.vue'
import HlTextarea from '@/components/ui/HlTextarea.vue'
import HlChip from '@/components/ui/HlChip.vue'
import HlTag from '@/components/ui/HlTag.vue'
import HlDropdown from '@/components/ui/HlDropdown.vue'
import HlInput from '@/components/ui/HlInput.vue'
import { message } from '@/components/ui'

/**
 * 领航台对话核心（抽屉与整页 /pilot 共用同一实现——一事实一实现）。
 * 时间线形态：每轮问答 = 问句气泡 + agent 回复层（过程链/回答/结构化卡片）；
 * 工具机器名不出用户面，步骤文案由 `pilot.step.{label}` 词条渲染。
 * 会话账本在服务端，会话 id 落 localStorage——两种表面续接同一对话。
 * 头部 actions 插槽供宿主追加动作（抽屉的「关闭」）。
 */
const props = withDefaults(
  defineProps<{
    game?: { appid: number; name: string } | null
    /** 整页模式：左侧常驻会话列（ZCode 语义），顶部标签栏退役；抽屉保持不变 */
    page?: boolean
  }>(),
  { game: null, page: false },
)
const { t } = useI18n()
const router = useRouter()

interface Turn {
  q: string
  /** pending = 用户发言已上屏、回复未到的占位轮（回复完成后原位替换） */
  kind: 'candidates' | 'action' | 'price' | 'games' | 'answer' | 'guide' | 'reason' | 'pending' | 'compact'
    | 'proposal'
  /** compact 分隔线的触发来源（auto / manual），仅 kind=compact 时有值 */
  compactTrigger?: string
  /** 本轮被用户停止生成（保留已流出内容） */
  stopped?: boolean
  text?: string
  reasonKey?: MessageKey
  items?: PilotGameFactsItem[]
  cards?: PilotFacts[]
  cached?: boolean
  steps: PilotStep[]
  /** 按 agent 循环步分段的阶段记录（权威）；旧账本轮次无此项，过程链回落 think + steps */
  phases?: PilotPhase[]
  think?: string
  /** 思考用时（秒，服务端工时接入后随 done 下发；缺省不显示时长） */
  thinkSec?: number
  /** 整轮工时（秒，服务端墙钟含工具执行；缺省不显示时长） */
  elapsedSec?: number
  /** 过程链记忆快照：本轮上下文占用 + 归档边界 + 缓存命中 */
  mem?: { tokens: number | null; budget: number | null; archived: number | null; cached: boolean } | null
}

const question = ref('')
const streaming = ref(false)
/** 本轮流式阶段的活账本：step_start 开新段，思考/正文/工具按段归位 */
const livePhases = ref<PilotPhase[]>([])
// 排队可见性：供应商按 Key 串行时第二个会话长时间无增量——ack.active>1 或
// 受理后 8s 无模型事件时提示「等待模型」，把「卡住」变成「排队中」
const queuedHint = ref<'parallel' | 'waiting' | null>(null)
let queuedTimer: ReturnType<typeof setTimeout> | null = null

function clearQueuedHint() {
  if (queuedTimer !== null) {
    clearTimeout(queuedTimer)
    queuedTimer = null
  }
  queuedHint.value = null
}
/** 流式期步骤展平（过程链的旧口径 prop；阶段记录在场时以 phases 为准） */
const liveStepsFlat = computed(() => livePhases.value.flatMap((p) => p.steps))
const turns = ref<Turn[]>([])
// 会话 id：跨表面持久（localStorage）——抽屉/整页/重启应用后续接同一会话，
// 追问与指代靠它串起；「新对话」重新生成。
const sessionId = ref('')
const scrollBox = ref<HTMLDivElement | null>(null)
// 输入区状态栏：在用模型 + 最近一轮上下文占用（均来自后端账本/配置，前端不自测）
const pilotModel = ref('')
const pilotReady = ref(false)
const pilotModels = ref<string[]>([])
const pilotDisabled = ref<string[]>([])
// 多供应商：菜单按厂商分组收敛/展开（活跃组默认展开）
const pilotProviders = ref<PilotProvider[]>([])
const pilotActiveId = ref('')
const openGroups = ref<Set<string>>(new Set())
const lastCtx = ref<{
  tokens: number
  budget: number
  breakdown?: { source: string; chars: number }[] | null
  hitRate?: number | null
} | null>(null)
const ctxOpen = ref(false)
const ctxSegments = computed(() => buildCtxSegments(lastCtx.value?.breakdown))
const ctxTitle = computed(() =>
  lastCtx.value
    ? t('pilot.composer.ctx', { n: `${fmtTokens(lastCtx.value.tokens)}/${fmtTokens(lastCtx.value.budget)}` })
    : t('pilot.ctx.empty'),
)
const ctxCacheVisible = computed(() => shouldShowCacheHitRate(lastCtx.value?.hitRate, import.meta.env.DEV))
// 会话统计投影（底栏 HlPilotStats 与 ctx 面板消耗段）：getSession 折叠为种子，
// 每轮 done 原位累加——与后端 session_stats 同口径的增量折叠，不另算一遍
const sessionStats = ref<PilotSessionStats | null>(null)
function foldSessionStats(e: PilotAskResponse) {
  const cur = sessionStats.value
  const hasCache = e.cache_base_tokens != null && e.cache_base_tokens > 0
  if (!cur) {
    if (!e.usage_in && !e.usage_out && !e.steps?.length && !hasCache) return
    sessionStats.value = {
      turns: 1,
      steps: e.steps?.length ?? 0,
      elapsed_ms: e.elapsed_ms ?? 0,
      ttft_ms: e.ttft_ms ?? 0,
      ttft_n: e.ttft_n ?? 0,
      decode_ms: e.decode_ms ?? 0,
      decode_out: e.decode_out ?? 0,
      usage_in: e.usage_in ?? 0,
      usage_out: e.usage_out ?? 0,
      cache_read_tokens: e.cache_read_tokens ?? null,
      cache_write_tokens: e.cache_write_tokens ?? null,
      cache_base_tokens: e.cache_base_tokens ?? null,
    }
    return
  }
  cur.turns += 1
  cur.steps += e.steps?.length ?? 0
  cur.elapsed_ms += e.elapsed_ms ?? 0
  cur.ttft_ms += e.ttft_ms ?? 0
  cur.ttft_n += e.ttft_n ?? 0
  cur.decode_ms += e.decode_ms ?? 0
  cur.decode_out += e.decode_out ?? 0
  cur.usage_in += e.usage_in ?? 0
  cur.usage_out += e.usage_out ?? 0
  if (e.cache_read_tokens != null) cur.cache_read_tokens = (cur.cache_read_tokens ?? 0) + e.cache_read_tokens
  if (e.cache_write_tokens != null) cur.cache_write_tokens = (cur.cache_write_tokens ?? 0) + e.cache_write_tokens
  if (e.cache_base_tokens != null) cur.cache_base_tokens = (cur.cache_base_tokens ?? 0) + e.cache_base_tokens
}
/** 会话级缓存命中率（Σ缓存读 / Σ计费输入；分母未知或为 0 不出） */
const statsCacheHit = computed(() => {
  const s = sessionStats.value
  if (!s || s.cache_read_tokens == null || !s.cache_base_tokens) return null
  return Math.min(1, s.cache_read_tokens / s.cache_base_tokens)
})
/** 会话消耗合计（计费输入 + 输出；无任何用量为 0） */
const statsTotal = computed(() => {
  const s = sessionStats.value
  if (!s) return 0
  return (s.cache_base_tokens ?? s.usage_in) + s.usage_out
})
const statsNumFmt = computed(() => new Intl.NumberFormat(useI18n().locale.value === 'zh-CN' ? 'zh-CN' : 'en'))
function fmtInt(v: number): string {
  return statsNumFmt.value.format(v)
}
/** 命中率百分比：部分命中永不进位到 100（99.9x 诚实显示） */
function fmtHit(hit: number): string {
  const p = hit * 100
  if (p >= 100) return '100'
  if (p > 99.9) return '99.9'
  return String(Math.round(p * 10) / 10)
}
const pctFmt = new Intl.NumberFormat(useI18n().locale.value === 'zh-CN' ? 'zh-CN' : 'en', {
  style: 'percent',
  maximumFractionDigits: 1,
})
function fmtPct(v: number | null | undefined): string {
  return typeof v === 'number' && Number.isFinite(v) ? pctFmt.format(v) : '—'
}
// 模型就地切换：chip 弹清单，选中即 PUT 配置（不关抽屉不跳页）
const switchingModel = ref(false)
// 会话管理：常驻标签栏 + 跨会话引用（清单是后端账本投影，轮后刷新；null = 投影未到，不猜）
const sessions = ref<PilotSessionListItem[] | null>(null)
const renamingSid = ref('')
const renameText = ref('')
const deleteArmSid = ref('')
let deleteArmTimer: ReturnType<typeof setTimeout> | null = null
const referenceSession = ref<{ sid: string; title: string } | null>(null)
// 标签栏打开集：× 只关标签不删会话（ZCode 语义）；账本里的会话随时可从历史面板找回
const openSids = ref<string[]>([])
const historyOpen = ref(false)
const historyRoot = ref<HTMLElement | null>(null)

const ctxPercent = computed(() => {
  if (!lastCtx.value || lastCtx.value.budget <= 0) return 0
  return Math.min(Math.round((lastCtx.value.tokens / lastCtx.value.budget) * 100), 100)
})


const REASON_KEYS: Record<string, MessageKey> = {
  llm_off: 'pilot.reason.llm_off',
  cap_reached: 'pilot.reason.cap_reached',
  llm_failed: 'pilot.reason.llm_failed',
  no_data: 'pilot.reason.no_data',
  need_target: 'pilot.reason.need_target',
  session_busy: 'pilot.reason.busy',
}

const SESSION_KEY = 'holdexar-pilot-session-id'

function readSavedSessionId(): string {
  try {
    return localStorage.getItem(SESSION_KEY) ?? ''
  } catch {
    return ''
  }
}

function saveSessionId(id: string) {
  try {
    if (id) localStorage.setItem(SESSION_KEY, id)
    else localStorage.removeItem(SESSION_KEY)
  } catch {
    /* 存储不可用时静默：会话仅当前生命周期有效 */
  }
}

function newSessionId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `s-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

const OPEN_SIDS_KEY = 'holdexar-pilot-open-sids'

function readOpenSids(): string[] {
  try {
    const raw = localStorage.getItem(OPEN_SIDS_KEY)
    const arr = raw ? JSON.parse(raw) : []
    return Array.isArray(arr) ? arr.filter((x) => typeof x === 'string') : []
  } catch {
    return []
  }
}

function saveOpenSids() {
  try {
    localStorage.setItem(OPEN_SIDS_KEY, JSON.stringify(openSids.value.slice(0, 30)))
  } catch {
    /* 存储不可用时静默 */
  }
}

/** 打开标签（历史面板找回 / 当前会话落账本时自动挂上） */
function openSessionSid(sid: string) {
  if (sid && !openSids.value.includes(sid)) {
    openSids.value = [...openSids.value, sid]
    saveOpenSids()
  }
}

/** × = 只关标签，账本不动；关的是当前会话则回到新对话 */
function closeSessionTab(sid: string) {
  openSids.value = openSids.value.filter((s) => s !== sid)
  saveOpenSids()
  if (sid === sessionId.value) startNewChat()
}

/** 从历史面板找回：挂上标签并切换过去 */
function openFromHistory(sid: string) {
  historyOpen.value = false
  if (sid === sessionId.value) return
  openSessionSid(sid)
  void switchSession(sid)
}

onMounted(() => {
  document.addEventListener('click', onDocClickCloseMenu)
  void loadPilotStatus()
  void loadSessions()
  const saved = readSavedSessionId()
  if (saved) {
    sessionId.value = saved
    openSessionSid(saved)
    void restoreSession(saved)
  } else {
    sessionId.value = newSessionId()
    saveSessionId(sessionId.value)
  }
})

/** 领航员在用模型与可用态（设置页保存后重开表面即刷新）。 */
async function loadPilotStatus() {
  try {
    const cfg = await pilotApi.getConfig()
    pilotModel.value = cfg.model || ''
    pilotProviders.value = cfg.providers || []
    pilotActiveId.value = cfg.active || ''
    pilotReady.value = Boolean(cfg.enabled && cfg.base_url && cfg.model && cfg.has_api_key)
  } catch {
    pilotReady.value = false
  }
}

// 模型就地切换（ZCode 对话面板样式）：ghost chip 弹向上清单，点击即切不关抽屉
/** 分组清单：按供应商收敛，每组只出启用模型；当前模型不在启用清单也保底可见 */
const menuGroups = computed(() =>
  pilotProviders.value.map((p) => {
    const disabled = new Set(p.models_disabled)
    const models = p.models.filter((m) => !disabled.has(m))
    if (p.id === pilotActiveId.value && pilotModel.value && !models.includes(pilotModel.value)) {
      models.unshift(pilotModel.value)
    }
    return { id: p.id, name: p.name || t('pilot.title'), models, active: p.id === pilotActiveId.value }
  }),
)

function toggleGroup(id: string) {
  const next = new Set(openGroups.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  openGroups.value = next
}

const modelMenuOpen = ref(false)
const modelPickRoot = ref<HTMLElement | null>(null)

function openModelMenu() {
  const next = new Set(openGroups.value)
  if (pilotActiveId.value) next.add(pilotActiveId.value)
  openGroups.value = next
  modelMenuOpen.value = !modelMenuOpen.value
}

function onDocClickCloseMenu(e: MouseEvent) {
  if (modelPickRoot.value && !modelPickRoot.value.contains(e.target as Node)) modelMenuOpen.value = false
  if (historyRoot.value && !historyRoot.value.contains(e.target as Node)) historyOpen.value = false
}

async function pickModel(providerId: string, m: string) {
  const sameProvider = providerId === pilotActiveId.value
  if (switchingModel.value || (sameProvider && m === pilotModel.value)) {
    modelMenuOpen.value = false
    return
  }
  switchingModel.value = true
  try {
    await pilotApi.updateConfig(sameProvider ? { model: m } : { active: providerId, model: m })
    pilotActiveId.value = providerId
    pilotModel.value = m
    modelMenuOpen.value = false
  } catch {
    message.error(t('pilot.modelMenu.switchFailed'))
  }
  switchingModel.value = false
}

/* 等待话术：每轮轮换一句（有活力、说人话，不重复出现感） */
const LOADING_KEYS = ['pilot.loading.scan', 'pilot.loading.compare', 'pilot.loading.brief', 'pilot.loading.low'] as const
const loadingSeq = ref(0)
const loadingText = computed(() => t(LOADING_KEYS[loadingSeq.value % LOADING_KEYS.length] as MessageKey))

function goSettings() {
  void router.push('/settings')
}

/** 重开表面 / 重启应用后续接上次对话（后端会话账本还原历史轮）。 */
async function restoreSession(sid: string) {
  try {
    const data = await pilotApi.getSession(sid)
    // 会话统计投影（底栏与消耗明细种子）：空会话为 null，不在旧会话上残留
    sessionStats.value = data.stats ?? null
    if (!data.turns.length) return
    proposalStates.value = new Map(
      (data.proposals ?? []).map((p) => [p.pid, { ...p, failedCount: p.failedCount ?? 0 }]),
    )
    const items: Turn[] = []
    data.turns.forEach((t, idx) => {
      const turn = buildTurn(t.q, { ...t.resp, cached: false }, [])
      syncProposalCards(turn.cards ?? [])
      items.push(turn)
      for (const m of data.markers ?? []) {
        if (m.after_turn === idx + 1) {
          items.push({ q: '', kind: 'compact', steps: [], compactTrigger: m.trigger || 'auto' })
        }
      }
    })
    turns.value = items
    const lastResp = data.turns[data.turns.length - 1]?.resp
    lastCtx.value =
      lastResp?.ctx_tokens != null && lastResp?.ctx_budget != null
        ? {
            tokens: lastResp.ctx_tokens,
            budget: lastResp.ctx_budget,
            breakdown: lastResp.ctx_breakdown ?? null,
            hitRate: lastResp.cache_hit_rate ?? null,
          }
        : null
    void scrollBottom()
  } catch {
    /* 会话不存在（过期清理）或读取失败：按空白新会话继续 */
  }
}

function startNewChat() {
  if (streaming.value) return
  // 已在未落账本的空新对话上：+ 即停留原地，不再换 id
  if (onFreshChat.value && !turns.value.length) return
  turns.value = []
  livePhases.value = []
  lastCtx.value = null
  sessionStats.value = null
  sessionId.value = newSessionId()
  saveSessionId(sessionId.value)
  referenceSession.value = null
  void loadSessions()
}

/* ── 会话标签栏与跨会话引用（账本只读投影；切换/改名/删除/引用同源） ── */

async function loadSessions() {
  try {
    const data = await pilotApi.listSessions(50)
    sessions.value = data.items
  } catch {
    sessions.value = []
  }
}

/** 本地即时标记运行态（后端 ask 流登记为准，done 后 loadSessions 归位；
    空会话未落账本时清单里本就没有该行，无需标记） */
function markRunningLocal(sid: string, running: boolean) {
  const item = (sessions.value ?? []).find((s) => s.sid === sid)
  if (item) item.running = running
}

/** 标签栏 = 打开集 ∩ 账本投影：× 关过的会话不再上栏，但仍在账本里（历史面板可找回）；
    账本未到（null）时渲染空栏，不猜 */
const tabItems = computed<PilotSessionListItem[]>(() => {
  const ledger = sessions.value
  if (ledger == null) return []
  const open = new Set(openSids.value)
  return ledger.filter((s) => open.has(s.sid))
})

/** 当前会话尚未落账本（真新对话）：+ 呈高亮当前态，再点不换 id */
const onFreshChat = computed(
  () => sessions.value != null && !sessions.value.some((s) => s.sid === sessionId.value),
)

async function switchSession(sid: string) {
  if (streaming.value || sid === sessionId.value) return
  sessionId.value = sid
  saveSessionId(sid)
  turns.value = []
  livePhases.value = []
  lastCtx.value = null
  sessionStats.value = null
  referenceSession.value = null
  await restoreSession(sid)
}

function startRename(it: PilotSessionListItem) {
  renamingSid.value = it.sid
  renameText.value = it.title
}

async function submitRename() {
  const sid = renamingSid.value
  const text = renameText.value.trim()
  renamingSid.value = ''
  if (!sid || !text) return
  try {
    await pilotApi.renameSession(sid, text)
    const item = (sessions.value ?? []).find((x) => x.sid === sid)
    if (item) item.title = text
  } catch {
    message.error(t('pilot.history.renameFailed'))
  }
}

/** 两段式删除：首点武装（4s 内再点执行），防误删 */
function armDelete(sid: string) {
  if (deleteArmSid.value === sid) {
    void deleteSession(sid)
    return
  }
  deleteArmSid.value = sid
  if (deleteArmTimer) clearTimeout(deleteArmTimer)
  deleteArmTimer = setTimeout(() => {
    deleteArmSid.value = ''
  }, 4000)
}

async function deleteSession(sid: string) {
  deleteArmSid.value = ''
  if (deleteArmTimer) clearTimeout(deleteArmTimer)
  try {
    await pilotApi.deleteSession(sid)
  } catch (e) {
    // 404 = 账本本无此会话（竞态/TTL 清扫）：视为已删；其余失败如实报错
    if (!(e instanceof ApiError && e.status === 404)) {
      message.error(t('pilot.history.deleteFailed'))
      return
    }
  }
  sessions.value = (sessions.value ?? []).filter((x) => x.sid !== sid)
  openSids.value = openSids.value.filter((s) => s !== sid)
  saveOpenSids()
  if (sid === sessionId.value) startNewChat()
}

// + 菜单的快捷动作（本项目专属直达工具）：点选即执行，不经模型
const QUICK_ACTIONS: { tool: string; key: string; icon: string; divided?: boolean }[] = [
  { tool: 'list_follows', key: 'pilot.quick.follows', icon: 'list' },
  { tool: 'list_alerts', key: 'pilot.quick.alerts', icon: 'bell' },
  { tool: 'hb_monthly', key: 'pilot.quick.hb', icon: 'gift' },
  { tool: 'epic_free', key: 'pilot.quick.epic', icon: 'gift' },
  { tool: 'price_drops', key: 'pilot.quick.drops', icon: 'chart' },
  { tool: 'rates_overview', key: 'pilot.quick.rates', icon: 'globe' },
  { tool: 'sync_wallet', key: 'pilot.quick.wallet', icon: 'wallet', divided: true },
  { tool: 'sync_bills', key: 'pilot.quick.bills', icon: 'list' },
  { tool: 'sync_achievements', key: 'pilot.quick.achievements', icon: 'star' },
  { tool: 'sync_family', key: 'pilot.quick.family', icon: 'user' },
  { tool: 'notify_test', key: 'pilot.quick.notify', icon: 'zap' },
  { tool: 'find_deletables', key: 'pilot.quick.deletables', icon: 'delete', divided: true },
]

const quickBusy = ref(false)

async function runQuick(tool: string) {
  if (quickBusy.value) return
  const item = QUICK_ACTIONS.find((a) => a.tool === tool)
  if (!item) return
  quickBusy.value = true
  const label = t(item.key as MessageKey)
  turns.value.push({ q: label, kind: 'pending', steps: [] })
  void scrollBottom()
  try {
    const r = await pilotApi.runTool(tool, label, sessionId.value || undefined)
    turns.value.splice(
      turns.value.length - 1,
      1,
      { q: label, kind: 'answer', text: '', cards: r.cards, steps: [r.step] } as Turn,
    )
  } catch {
    turns.value.splice(
      turns.value.length - 1,
      1,
      { q: label, kind: 'reason', reasonKey: 'pilot.error', steps: [] } as Turn,
    )
  }
  quickBusy.value = false
  void scrollBottom()
}

const plusMenuItems = computed(() => {
  const actions = QUICK_ACTIONS.map((a) => ({
    label: t(a.key as MessageKey),
    value: `tool:${a.tool}`,
    icon: a.icon,
    divided: a.divided,
  }))
  const others = (sessions.value ?? []).filter((s) => s.sid !== sessionId.value)
  const refs = others.length
    ? others.map((it) => ({
        label: `${t('pilot.ref.prefix')}${it.title || t('pilot.session.untitled')}`,
        value: it.sid,
        divided: it === others[0],
      }))
    : [{ label: t('pilot.ref.none'), value: '', divided: true }]
  return [...actions, ...refs]
})

function onMenuSelect(item: { value?: string }) {
  if (item.value?.startsWith('tool:')) {
    void runQuick(item.value.slice(5))
    return
  }
  onReferenceSelect(item)
}

function onReferenceSelect(item: { value?: string }) {
  if (!item.value) return
  const it = (sessions.value ?? []).find((x) => x.sid === item.value)
  if (it) referenceSession.value = { sid: it.sid, title: it.title || t('pilot.session.untitled') }
}

function fen(v: number | null | undefined): string {
  return typeof v === 'number' && v > 0 ? formatCnyFen(v) : '—'
}

/** 聚焦框：主内容区外圈亮环，2.2s 后淡出移除 */
function focusMainRegion() {
  const el = document.querySelector('.app-shell__main')
  if (!el) return
  const r = el.getBoundingClientRect()
  const ring = document.createElement('div')
  ring.style.cssText = [
    'position:fixed', `top:${r.top - 6}px`, `left:${r.left - 6}px`,
    `width:${r.width + 12}px`, `height:${r.height + 12}px`,
    'border:2px solid var(--accent)', 'border-radius:14px',
    'box-shadow:0 0 0 4px var(--accent-a15), 0 0 24px var(--accent-a15)',
    'pointer-events:none', 'z-index:60',
    'transition:opacity 0.5s', 'opacity:1',
  ].join(';')
  document.body.appendChild(ring)
  setTimeout(() => {
    ring.style.opacity = '0'
    setTimeout(() => ring.remove(), 600)
  }, 1800)
}

/** 导航卡生效：跳转模块 + 聚焦框（对话表面保持打开，模块在底层切换） */
function applyNavigate(f: PilotNavigateFacts) {
  if (!f.path) return
  void router.push(f.path)
  setTimeout(focusMainRegion, 700)
}

/* ── 批量提议：待确认清单的确认/取消与账本校正 ───────────────────── */

const busyPid = ref<string | null>(null)
/** pid → 提议终态（会话账本投影）：历史轮据此把已处理的卡片改成只读 */
const proposalStates = ref<Map<string, { pid: string; state: string; done: number | null; failedCount: number }>>(new Map())

/** 按账本终态校正卡片：非 pending 一律不可点，避免出现点了没反应的按钮。 */
function syncProposalCards(cards: PilotFacts[]) {
  for (const c of cards) {
    if (c.kind !== 'proposal') continue
    const known = proposalStates.value.get(c.pid)
    if (!known) continue
    if (known.state === 'confirmed') {
      c.state = 'confirmed'
      c.done = known.done ?? c.items.length
      c.failedCount = known.failedCount
    } else if (known.state === 'rejected' || known.state === 'dismissed') {
      c.state = 'rejected'
    } else if (known.state === 'withdrawn') {
      c.state = 'withdrawn'
    }
  }
}

async function onProposal(payload: { pid: string; approve: boolean }) {
  const sid = sessionId.value
  if (!sid || busyPid.value) return
  busyPid.value = payload.pid
  try {
    const res = await pilotApi.confirmProposal(sid, payload.pid, payload.approve)
    if (!res.ok) {
      message.error(t('pilot.proposal.gone'))
      return
    }
    proposalStates.value.set(payload.pid, {
      pid: payload.pid,
      state: res.state,
      done: res.done,
      failedCount: res.failed?.length ?? 0,
    })
    for (const turn of turns.value) syncProposalCards(turn.cards ?? [])
  } catch {
    message.error(t('pilot.error'))
  } finally {
    busyPid.value = null
  }
}

/** 终答取带 terminal 标记的那一段正文；步数耗尽 / 用户中断的轮次回落最后一段有正文的 */
function answerOf(phases: PilotPhase[], fallback: string): string {
  if (!phases.length) return fallback
  const terminal = phases.find((p) => p.terminal)
  if (terminal?.text) return terminal.text
  for (let i = phases.length - 1; i >= 0; i--) {
    if (phases[i].text) return phases[i].text
  }
  return fallback
}

function buildTurn(q: string, e: PilotAskResponse, livePhaseSnapshot: PilotPhase[]): Turn {
  const cards = (e.cards ?? []).filter(Boolean)
  // 阶段记录优先取服务端终态；未下发阶段（降级 / 快捷路径）时用流式期快照
  const phases = e.phases?.length ? e.phases : livePhaseSnapshot
  const answerText = answerOf(phases, e.answer || '')
  const base = {
    q,
    steps: e.steps?.length ? e.steps : phases.flatMap((p) => p.steps),
    phases: phases.length ? phases : undefined,
    // 有阶段记录时思考由 phases 承载，不再重复带整段原文
    think: phases.length ? undefined : (e.thinking || undefined),
    thinkSec: e.think_ms ? Math.max(1, Math.round(e.think_ms / 1000)) : undefined,
    elapsedSec: e.elapsed_ms ? Math.max(1, Math.round(e.elapsed_ms / 1000)) : undefined,
    mem: {
      tokens: e.ctx_tokens ?? null,
      budget: e.ctx_budget ?? null,
      archived: e.archived_through ?? null,
      cached: Boolean(e.cached),
    },
  }
  // 批量提议：卡片带确认/取消，故独立成轮（模型说明文字随卡片一并展示）
  if (cards.some((c) => c.kind === 'proposal')) {
    syncProposalCards(cards)
    return { q, kind: 'proposal', text: answerText, cards, ...base }
  }
  if (e.source === 'llm') return { q, kind: 'answer', text: answerText, cards, ...base }
  if (e.source === 'facts') {
    if (!cards.length && e.facts?.kind === 'navigate') cards.push(e.facts)
    if (cards.length) {
      const first = cards[0]
      if (first.kind === 'proposal') {
        // 提议卡走 proposal 轮形态（确认监听只挂在这一分支上）
        return { q, kind: 'proposal', cards, ...base }
      }
      if (first.kind === 'action' || first.kind === 'navigate') {
        return { q, kind: 'action', cards, ...base }
      }
      if (first.kind === 'price') return { q, kind: 'price', cards, ...base }
      return { q, kind: 'games', items: first.kind === 'games' ? first.items : [], cards, ...base }
    }
    if (e.facts?.kind === 'action') {
      return { q, kind: 'action', cards: [e.facts], ...base }
    }
    if (e.facts?.kind === 'price') return { q, kind: 'price', cards: [e.facts], ...base }
    if (e.facts?.kind === 'games') {
      return {
        q,
        kind: e.reason === 'need_target' ? 'candidates' : 'games',
        items: e.facts.items,
        reasonKey: e.reason === 'need_target' ? 'pilot.reason.need_target' : undefined,
        ...base,
      }
    }
  }
  if (e.source === 'guide') return { q, kind: 'guide', ...base }
  return { q, kind: 'reason', reasonKey: (e.reason && REASON_KEYS[e.reason]) || 'pilot.error', ...base }
}

async function scrollBottom() {
  await nextTick()
  pinIfFollow()
}

/* 智能跟随：用户在底部附近（48px 内）时钉住最新进度，上滚即让位、回底恢复。
   文本增长由流式写入点主动触发（flushDeltas）；结构变化（新阶段 / 步骤 / 卡片）
   由 MutationObserver 捕获；封面图异步撑高不走 DOM 变更，另听 img load（捕获态）
   与窗口 resize。 */
const followBottom = ref(true)

/* 有效滚动容器二态：抽屉里 turns 自滚；整页 turns 不自滚（不出内层滚动条，
   滚动交给页面滚动条），钉底锚会话区底缘——新内容长在上方，输入区与最新进度同屏。 */
const pageScroller = ref<HTMLElement | null>(null)
let savedScrollBehavior = ''

/* 整页钉位：把会话区（.pilot-console）底缘——有会话统计时即统计栏、无则输入区——
   停在滚动容器底上方 14px 处所需的 scrollTop。rect 是视口坐标、容器会被顶栏压下，
   必须减去容器 top，否则整块钉高一截。该值只随内容布局变化、与当前 scrollTop 无关，
   故可同时用作跟随判据。 */
const PAGE_PIN_GAP = 14

function pagePinTarget(): number {
  const vc = pageScroller.value
  const inner = scrollBox.value
  if (!vc || !inner) return 0
  const foot = inner.closest('.pilot-console') as HTMLElement | null
  if (!foot) return 0
  const vcRect = vc.getBoundingClientRect()
  return Math.max(
    foot.getBoundingClientRect().bottom - vcRect.top + vc.scrollTop - vc.clientHeight + PAGE_PIN_GAP,
    0,
  )
}

function onContainerScroll(e: Event) {
  const el = e.target as HTMLElement
  if (el === pageScroller.value) {
    /* 整页（turns 不自滚）：跟随判据必须与钉位同源。composer 下方还有会话统计栏
       与组件总览，「到页面真底的距离」恒 > 48px，拿它判会把刚钉好的位置误判成
       「用户已上滚」→ 跟随当场关闭 → composer 被后续增长推出视口。 */
    followBottom.value = el.scrollTop >= pagePinTarget() - 48
  } else {
    followBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  }
  scheduleOutlineProbe()
}

function pinIfFollow() {
  const inner = scrollBox.value
  if (!inner) return
  if (inner.scrollHeight > inner.clientHeight + 1) {
    if (followBottom.value) inner.scrollTop = inner.scrollHeight
    return
  }
  if (!followBottom.value || !pageScroller.value) return
  pageScroller.value.scrollTop = pagePinTarget()
}

/* 钉底与渲染同帧：nextTick 回调排在 DOM 补丁之后、本帧绘制之前，读一次布局即钉到底。
   改用定时器节流会让「增长→补偿」跨帧可见：正文每长出一行，整块内容即下坠一行高再拽回。 */
let pinQueued = false

function schedulePinIfFollow() {
  if (pinQueued) return
  pinQueued = true
  void nextTick(() => {
    pinQueued = false
    if (followBottom.value) pinIfFollow()
  })
}

let growthObserver: MutationObserver | null = null

/* ── 对话目录：turns 的只读投影，跳转走语义定位，高亮按滚动采样跟随 ── */

const outlineActive = ref<number | null>(null)
const outlineOpen = ref(false)
let outlineProbeTimer: ReturnType<typeof setTimeout> | null = null

/** 阅读线（有效滚动容器顶下 48px）之上最后一轮即当前轮；仅目录打开时计算。
    整页模式盒子会整体滚出视口，线必须锚滚动容器而非 turns 盒 */
function probeOutlineActive() {
  const inner = scrollBox.value
  if (!inner || !outlineOpen.value) return
  const scroller = inner.scrollHeight > inner.clientHeight + 1 ? inner : pageScroller.value
  const line = (scroller ?? inner).getBoundingClientRect().top + 48
  const rows = inner.querySelectorAll<HTMLElement>('[data-turn-index]')
  let current: number | null = rows.length ? Number(rows[0].dataset.turnIndex) : null
  for (const row of rows) {
    if (row.getBoundingClientRect().top > line) break
    current = Number(row.dataset.turnIndex)
  }
  outlineActive.value = current
}

/* 60ms 尾沿节流：滚动停止后必有一次收尾探测，且不依赖绘制帧 */
function scheduleOutlineProbe() {
  if (!outlineOpen.value || outlineProbeTimer !== null) return
  outlineProbeTimer = setTimeout(() => {
    outlineProbeTimer = null
    probeOutlineActive()
  }, 60)
}

function onOutlineToggle(open: boolean) {
  outlineOpen.value = open
  if (open) probeOutlineActive()
}

function jumpToTurn(index: number) {
  const inner = scrollBox.value
  if (!inner) return
  const row = inner.querySelector<HTMLElement>(`[data-turn-index="${index}"]`)
  if (!row) return
  // 有效滚动容器二态与钉底同源：内滚优先，内滚放不下交给页面滚动条
  const scroller = inner.scrollHeight > inner.clientHeight + 1 ? inner : pageScroller.value
  if (!scroller) return
  const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
  const top = Math.max(0, row.getBoundingClientRect().top - scroller.getBoundingClientRect().top + scroller.scrollTop - 12)
  followBottom.value = false
  scroller.scrollTo({ top, behavior: reduced ? 'auto' : 'smooth' })
  scheduleOutlineProbe()
}

/* 会话内容整体更换（还原/新对话/切换）后旧下标全部失效 */
watch(turns, () => {
  outlineActive.value = null
  if (outlineOpen.value) scheduleOutlineProbe()
})

function onVisibilityChange() {
  // 后台期间 rAF 停摆、增量积压在缓冲——切回前台立即补放
  if (document.visibilityState === 'visible') flushDeltas()
}

onMounted(() => {
  document.addEventListener('visibilitychange', onVisibilityChange)
  const el = scrollBox.value
  if (!el) return
  growthObserver = new MutationObserver(() => schedulePinIfFollow())
  growthObserver.observe(el, { childList: true, subtree: true })
  el.addEventListener('load', pinIfFollow, true)
  window.addEventListener('resize', pinIfFollow)
  const vc = el.closest<HTMLElement>('.view-container')
  if (vc) {
    pageScroller.value = vc
    savedScrollBehavior = vc.style.scrollBehavior
    // 钉底是高频程序化滚动，smooth 会拖尾——console 存续期内改即时
    vc.style.scrollBehavior = 'auto'
    vc.addEventListener('scroll', onContainerScroll, { passive: true })
  }
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClickCloseMenu)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  if (deltaTimer !== null) clearTimeout(deltaTimer)
  growthObserver?.disconnect()
  if (outlineProbeTimer !== null) clearTimeout(outlineProbeTimer)
  scrollBox.value?.removeEventListener('load', pinIfFollow, true)
  window.removeEventListener('resize', pinIfFollow)
  if (pageScroller.value) {
    pageScroller.value.removeEventListener('scroll', onContainerScroll)
    pageScroller.value.style.scrollBehavior = savedScrollBehavior
  }
  if (deleteArmTimer) clearTimeout(deleteArmTimer)
})

/* 流式 delta 合帧：token 级 setState 是逐字全量重渲染（v-html 整段重跑
   mdLite），响应卡顿的主因——rAF 每帧最多刷一次，done 时强制清空缓冲。
   增量落到当前阶段；进入下一段前先冲刷，避免增量跨段错位。 */
let pendingThink = ''
let pendingAnswer = ''
let deltaRaf: number | null = null
let deltaTimer: ReturnType<typeof setTimeout> | null = null

/** 当前阶段（列表末位）；无阶段时为 null（定界事件到达前的孤儿增量丢弃） */
function currentPhase(): PilotPhase | null {
  return livePhases.value.length ? livePhases.value[livePhases.value.length - 1] : null
}

function flushDeltas() {
  if (deltaRaf !== null) {
    cancelAnimationFrame(deltaRaf)
    deltaRaf = null
  }
  if (deltaTimer !== null) {
    clearTimeout(deltaTimer)
    deltaTimer = null
  }
  const phase = currentPhase()
  let grew = false
  if (pendingThink) {
    if (phase) {
      phase.thinking += pendingThink
      grew = true
    }
    pendingThink = ''
  }
  if (pendingAnswer) {
    if (phase) {
      phase.text += pendingAnswer
      grew = true
    }
    pendingAnswer = ''
  }
  // 文本增长不产生结构变更，钉底由写入点主动触发（不再逐帧监听 characterData 回头读整条会话高度）
  if (grew) schedulePinIfFollow()
}

function queueDelta(kind: 'thinking' | 'answer', delta: string) {
  if (kind === 'thinking') pendingThink += delta
  else pendingAnswer += delta
  if (deltaRaf === null && deltaTimer === null) {
    deltaRaf = requestAnimationFrame(flushDeltas)
    // 后台窗口 rAF 永停（遮挡/切走时思考假死）：定时兜底续流，切回即补放
    deltaTimer = setTimeout(flushDeltas, 250)
  }
}

function onComposerKeydown(e: KeyboardEvent) {
  // Enter 发送；输入法组词确认的 Enter（isComposing）不是发送意图，放行给编辑器
  if (e.key !== 'Enter' || e.isComposing || e.keyCode === 229) return
  if (e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) return
  e.preventDefault()
  ask()
}

let abortCtrl: AbortController | null = null

/** 停止生成本轮：中断 SSE，已流出的内容保留在对话里。 */
function stopPilot() {
  abortCtrl?.abort()
}

async function ask(text?: string) {
  const q = (text ?? question.value).trim()
  if (!q || streaming.value) return
  if (!text) question.value = ''
  streaming.value = true
  abortCtrl = new AbortController()
  livePhases.value = []
  clearQueuedHint()
  loadingSeq.value += 1
  // 用户发言立即上屏（pending 占位轮，回复完成后原位替换）；跟随复位——刚发的消息必须看着它长出来
  followBottom.value = true
  turns.value.push({ q, kind: 'pending', steps: [] })
  // 本地即时点亮「工作中」（后端登记为准，done 后 loadSessions 归位）
  markRunningLocal(sessionId.value, true)
  void scrollBottom()
  let done: PilotAskResponse | null = null
  let failed = false
  let stopped = false
  // 本轮已即时跳转：tool 事件到达即切页（不等后续模型叙述），done 后不再重复跳
  let navApplied = false
  try {
    await askPilotStream(q, props.game?.appid, sessionId.value || undefined, (e) => {
      if (e.type === 'ack') {
        // 受理回执：另一会话在处理 → 并行提示；否则 8s 无增量再提示等待模型
        if ((e.active ?? 1) > 1) queuedHint.value = 'parallel'
        else queuedTimer = setTimeout(() => { if (streaming.value) queuedHint.value = 'waiting' }, 8000)
      } else if (e.type === 'busy') {
        done = { answer: '', thinking: null, source: 'none', reason: 'session_busy', facts: null, cards: [], cached: false }
        clearQueuedHint()
      } else if (e.type === 'step_start') {
        clearQueuedHint()
        // 阶段边界：先冲刷上一段残留增量，再开新段
        flushDeltas()
        livePhases.value.push({ step: e.step ?? livePhases.value.length + 1, thinking: '', text: '', steps: [] })
      } else if (e.type === 'thinking' && e.delta) {
        clearQueuedHint()
        queueDelta('thinking', e.delta)
      } else if (e.type === 'answer' && e.delta) {
        clearQueuedHint()
        queueDelta('answer', e.delta)
      } else if (e.type === 'tool_start') {
        clearQueuedHint()
        flushDeltas()
        // 导航直通等路径不发阶段定界，按首段兜底
        if (!livePhases.value.length) {
          livePhases.value.push({ step: 1, thinking: '', text: '', steps: [] })
        }
        currentPhase()?.steps.push({ label: e.label ?? '', status: 'running', data: {} })
      } else if (e.type === 'tool') {
        // 回填本阶段最后一个运行态步骤；无运行态（如重放）直接追加
        const phase = currentPhase()
        const pending = phase ? [...phase.steps].reverse().find((s) => s.status === 'running') : undefined
        if (pending) {
          pending.label = e.label ?? pending.label
          pending.status = e.status ?? 'ok'
          pending.data = e.data ?? {}
        } else if (phase) {
          phase.steps.push({ label: e.label ?? '', status: e.status ?? 'ok', data: e.data ?? {} })
        }
        if (e.name === 'navigate' && e.status === 'ok' && e.data?.path && !navApplied) {
          navApplied = true
          void router.push(e.data.path)
          setTimeout(focusMainRegion, 700)
        }
      } else if (e.type === 'done') {
        done = e as PilotAskResponse
        clearQueuedHint()
        void loadSessions()
        openSessionSid(sessionId.value)
        foldSessionStats(e as PilotAskResponse)
        if (typeof e.ctx_tokens === 'number' && typeof e.ctx_budget === 'number') {
          lastCtx.value = {
            tokens: e.ctx_tokens,
            budget: e.ctx_budget,
            breakdown: e.ctx_breakdown ?? null,
            hitRate: e.cache_hit_rate ?? null,
          }
        }
        if ((e as PilotStreamEvent).compacted) {
          // 本轮装配发生了压缩：分隔线插在占位轮之前（压缩边界在对话流中的位置）
          turns.value.splice(Math.max(0, turns.value.length - 1), 0, { q: '', kind: 'compact', steps: [], compactTrigger: 'auto' })
        }
      } else if (e.type === 'error') {
        clearQueuedHint()
        done = { answer: '', thinking: null, source: 'none', reason: e.reason ?? 'llm_failed', facts: null, cards: [], cached: false }
      }
    }, referenceSession.value?.sid, abortCtrl.signal)
  } catch (err) {
    if (abortCtrl.signal.aborted) {
      stopped = true
    } else {
      failed = true
    }
  }
  referenceSession.value = null
  flushDeltas()
  const phaseSnapshot = livePhases.value.map((p) => ({
    ...p,
    steps: p.steps.map((s) => ({ ...s, data: { ...s.data } })),
  }))
  const snapshotSteps = phaseSnapshot.flatMap((p) => p.steps)
  let turn: Turn
  if (failed) {
    turn = { q, kind: 'reason', reasonKey: 'pilot.error', steps: snapshotSteps } as Turn
    // 失败还原输入（用户没在新输入时才还原，可一键重试）
    if (!question.value.trim()) question.value = q
  } else if (stopped) {
    // 中断保留已流出的阶段与终答候选，历史轮仍可展开过程
    turn = {
      q,
      kind: 'answer',
      text: answerOf(phaseSnapshot, ''),
      phases: phaseSnapshot.length ? phaseSnapshot : undefined,
      steps: snapshotSteps,
      stopped: true,
    } as Turn
  } else {
    turn = buildTurn(q, done ?? { answer: '', thinking: null, source: 'none', reason: 'llm_failed', facts: null, cards: [], cached: false }, phaseSnapshot)
  }
  turns.value.splice(turns.value.length - 1, 1, turn)
  // busy = 本轮未受理：原样还原输入，等会话空闲后一键重发
  if (done?.reason === 'session_busy' && !question.value.trim()) question.value = q
  // 导航卡生效：取本轮最后一张，跳模块并打聚焦框（tool 事件已即时跳过则不再重复）
  const navCards = (turn.cards ?? []).filter((c): c is PilotNavigateFacts => c.kind === 'navigate' && Boolean(c.path))
  const lastNav = navCards[navCards.length - 1]
  if (lastNav && !navApplied) applyNavigate(lastNav)
  streaming.value = false
  markRunningLocal(sessionId.value, false)
  livePhases.value = []
  clearQueuedHint()
  void scrollBottom()
}

function pickCandidate(turn: Turn, index: number) {
  if (turn !== turns.value[turns.value.length - 1] || streaming.value) return
  const name = turn.items?.[index]?.name
  // 回发候选名：后端名称检索唯中即执行；无名时按展示序号回发（词条化）
  if (name) {
    void ask(name)
    return
  }
  void ask(t('pilot.candidate.pick', { n: index + 1 }))
}

interface FollowUpItem {
  key: MessageKey
  prompt: string
}

const lastDoneTurnIndex = computed(() => {
  for (let i = turns.value.length - 1; i >= 0; i--) {
    const t = turns.value[i]
    if (t.kind !== 'pending' && t.kind !== 'compact') return i
  }
  return -1
})

/** 追问 dock 内容：空会话给开场推荐，有轮给最后一轮的追问；流式中不出 */
const dockFollowUps = computed<FollowUpItem[]>(() => {
  if (streaming.value) return []
  const i = lastDoneTurnIndex.value
  if (i < 0) {
    return [
      { key: 'pilot.followup.hotDeals', prompt: t('pilot.followup.hotDealsPrompt') },
      { key: 'pilot.followup.freebies', prompt: t('pilot.followup.freebiesPrompt') },
      { key: 'pilot.followup.auditWorkflow', prompt: t('pilot.followup.auditWorkflowPrompt') },
      { key: 'pilot.followup.myFollows', prompt: t('pilot.followup.myFollowsPrompt') },
    ]
  }
  return getTurnFollowUps(turns.value[i])
})

function getTurnFollowUps(turn: Turn): FollowUpItem[] {
  // 1. 查完价格/单游戏卡
  const priceCard = turn.cards?.find((c) => c.kind === 'price') as PilotPriceFacts | undefined
  if (priceCard && priceCard.name) {
    const name = priceCard.name
    return [
      { key: 'pilot.followup.compareRegions', prompt: t('pilot.followup.compareRegionsPrompt', { name }) },
      { key: 'pilot.followup.similarGames', prompt: t('pilot.followup.similarGamesPrompt', { name }) },
      { key: 'pilot.followup.addFollow', prompt: t('pilot.followup.addFollowPrompt', { name }) },
      { key: 'pilot.followup.setAlert', prompt: t('pilot.followup.setAlertPrompt', { name }) },
      { key: 'pilot.followup.priceHistory', prompt: t('pilot.followup.priceHistoryPrompt', { name }) },
    ]
  }

  // 2. 查完多区价格
  const regionsCard = turn.cards?.find((c) => c.kind === 'regions') as PilotRegionsFacts | undefined
  if (regionsCard && regionsCard.name) {
    const name = regionsCard.name
    return [
      { key: 'pilot.followup.priceHistory', prompt: t('pilot.followup.priceHistoryPrompt', { name }) },
      { key: 'pilot.followup.similarGames', prompt: t('pilot.followup.similarGamesPrompt', { name }) },
      { key: 'pilot.followup.addFollow', prompt: t('pilot.followup.addFollowPrompt', { name }) },
    ]
  }

  // 3. 查完网络 / 代理 / 出口 / 任务
  const hasNetCard = turn.cards?.some((c) => c.kind === 'rows' && ['proxy', 'gate', 'jobs', 'proxyStatus', 'jobFailures'].includes(c.titleKey))
  if (hasNetCard) {
    return [
      { key: 'pilot.followup.testLatency', prompt: t('pilot.followup.testLatencyPrompt') },
      { key: 'pilot.followup.switchNode', prompt: t('pilot.followup.switchNodePrompt') },
      { key: 'pilot.followup.checkFailures', prompt: t('pilot.followup.checkFailuresPrompt') },
    ]
  }

  // 4. 查完家庭组
  const hasFamily = turn.cards?.some((c) => c.kind === 'family')
  if (hasFamily) {
    return [
      { key: 'pilot.followup.familyOverlap', prompt: t('pilot.followup.familyOverlapPrompt') },
      { key: 'pilot.followup.familyPlayable', prompt: t('pilot.followup.familyPlayablePrompt') },
      { key: 'pilot.followup.familyUnbound', prompt: t('pilot.followup.familyUnboundPrompt') },
    ]
  }

  // 5. 查完提醒 / 批量提议 / 工作流
  const hasProposal = turn.cards?.some((c) => c.kind === 'proposal' || c.kind === 'stepper')
  const hasAlerts = turn.cards?.some((c) => c.kind === 'rows' && c.titleKey === 'alerts')
  if (hasProposal || hasAlerts) {
    return [
      { key: 'pilot.followup.listAlerts', prompt: t('pilot.followup.listAlertsPrompt') },
      { key: 'pilot.followup.auditWorkflow', prompt: t('pilot.followup.auditWorkflowPrompt') },
      { key: 'pilot.followup.myFollows', prompt: t('pilot.followup.myFollowsPrompt') },
    ]
  }

  // 6. 游戏清单与候选推荐
  const gamesCard = turn.cards?.find((c) => c.kind === 'games')
  if (gamesCard || turn.kind === 'candidates' || (turn.items && turn.items.length > 0)) {
    return [
      { key: 'pilot.followup.highRated', prompt: t('pilot.followup.highRatedPrompt') },
      { key: 'pilot.followup.budgetFriendly', prompt: t('pilot.followup.budgetFriendlyPrompt') },
      { key: 'pilot.followup.auditWorkflow', prompt: t('pilot.followup.auditWorkflowPrompt') },
      { key: 'pilot.followup.deckPlayableGeneral', prompt: t('pilot.followup.deckPlayableGeneralPrompt') },
    ]
  }

  // 7. 通用兜底追问
  return [
    { key: 'pilot.followup.hotDeals', prompt: t('pilot.followup.hotDealsPrompt') },
    { key: 'pilot.followup.freebies', prompt: t('pilot.followup.freebiesPrompt') },
    { key: 'pilot.followup.auditWorkflow', prompt: t('pilot.followup.auditWorkflowPrompt') },
    { key: 'pilot.followup.myFollows', prompt: t('pilot.followup.myFollowsPrompt') },
  ]
}

function triggerFollowUp(prompt: string) {
  if (streaming.value) return
  void ask(prompt)
}
</script>

<template>
  <div class="pilot-console" :class="{ 'is-page': page }">
    <!-- 整页会话列：账本全量清单常驻左侧（sticky 跟随视口），新对话/对话目录在列头；
         行内双击重命名、hover × 两段式删除——与标签栏同一套动作语义 -->
    <!-- 会话列 Teleport 到页面级挂点（Index 提供 #pilot-side-target，位于居中对话列
         之外的左侧空白区）——列与思考链/输入框的布局彻底解耦，尺寸由挂点约束；
         defer 必需：子组件挂载早于 Index 子树插入 document，目标须延后解析 -->
    <Teleport v-if="page" defer to="#pilot-side-target">
    <aside class="pilot-side">
      <div class="pilot-side__tools">
        <button
          type="button"
          class="pilot-side__new"
          :class="{ 'is-current': onFreshChat }"
          :disabled="streaming"
          @click="startNewChat"
        >
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
          </svg>
          {{ t('pilot.newChat') }}
        </button>
        <HlPilotOutline
          v-if="page"
          side="right"
          :turns="turns"
          :active-index="outlineActive"
          @jump="jumpToTurn"
          @toggle="onOutlineToggle"
        />
      </div>
      <div class="pilot-side__scroll">
        <div v-if="sessions == null" class="pilot-side__empty">{{ t('pilot.history.empty') }}</div>
        <div v-else-if="!sessions.length" class="pilot-side__empty">{{ t('pilot.history.empty') }}</div>
        <template v-else>
          <div
            v-for="it in sessions"
            :key="it.sid"
            class="pilot-side__row"
            :class="{ 'is-current': it.sid === sessionId }"
            :title="`${it.title || t('pilot.session.untitled')} · ${t('pilot.history.turns', { n: it.turn_total })}${it.running ? ' · ' + t('pilot.session.running') : ''}`"
            @click="openFromHistory(it.sid)"
            @dblclick="startRename(it)"
          >
            <template v-if="renamingSid === it.sid">
              <HlInput
                v-model="renameText"
                class="pilot-side__rename"
                @keydown.enter="submitRename"
                @keydown.esc="renamingSid = ''"
              />
              <button type="button" class="pilot-side__x is-ok" :title="t('pilot.history.rename')" @click.stop="submitRename">
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </button>
            </template>
            <template v-else>
              <span class="pilot-side__main">
                <span class="pilot-side__name">
                  <span
                    v-if="it.running"
                    class="pilot-side__running"
                    :title="t('pilot.session.running')"
                    aria-hidden="true"
                  ></span>
                  {{ it.title || t('pilot.session.untitled') }}
                </span>
                <span class="pilot-side__meta">
                  {{ t('pilot.history.turns', { n: it.turn_total }) }} · {{ sessionTime(it.updated_at) }}
                </span>
              </span>
              <button
                type="button"
                class="pilot-side__x"
                :class="{ 'is-armed': deleteArmSid === it.sid }"
                :title="deleteArmSid === it.sid ? t('pilot.history.deleteConfirm') : t('pilot.history.delete')"
                @click.stop="armDelete(it.sid)"
              >
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
                </svg>
              </button>
            </template>
          </div>
        </template>
      </div>
    </aside>
    </Teleport>

    <header class="pilot-head">
      <span class="pilot-head__title">{{ t('pilot.title') }} · {{ t('pilot.console') }}</span>
      <div class="pilot-head__actions">
        <slot name="actions" />
      </div>
    </header>

    <!-- 会话标签栏：× 只关标签（会话留存账本），历史 ⟲ 面板找回/删除，双击重命名。
         滚动区只包标签——+ / ⟲ 与下拉面板必须留在 overflow 之外，否则面板展开
         会把标签栏撑出原生滚动条并被裁剪 -->
    <div class="pilot-tabbar">
      <div class="pilot-tabbar__scroll">
        <div
          v-for="it in tabItems"
          :key="it.sid"
          class="pilot-tab"
          :class="{ 'is-current': it.sid === sessionId }"
          :title="`${it.title || t('pilot.session.untitled')} · ${t('pilot.history.turns', { n: it.turn_total })} · ${t('pilot.history.dblRename')}`"
          @dblclick="startRename(it)"
        >
        <template v-if="renamingSid === it.sid">
          <HlInput
            v-model="renameText"
            class="pilot-tab__rename"
            @keydown.enter="submitRename"
            @keydown.esc="renamingSid = ''"
          />
          <button type="button" class="pilot-tab__x is-ok" :title="t('pilot.history.rename')" @click="submitRename">
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
          </button>
        </template>
        <template v-else>
          <svg v-if="it.sid === sessionId" class="pilot-tab__cur" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
          <button type="button" class="pilot-tab__main" @click="switchSession(it.sid)">
            <span class="pilot-tab__name">{{ it.title || t('pilot.session.untitled') }}</span>
          </button>
          <button
            type="button"
            class="pilot-tab__x"
            :class="{ 'is-disabled': streaming && it.sid === sessionId }"
            :title="t('pilot.history.close')"
            @click="!(streaming && it.sid === sessionId) && closeSessionTab(it.sid)"
          >
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
            </svg>
          </button>
        </template>
        </div>
      </div>
      <button
        type="button"
        class="pilot-tabbar__add"
        :class="{ 'is-current': onFreshChat }"
        :title="t('pilot.newChat')"
        :disabled="streaming"
        @click="startNewChat"
      >
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
        </svg>
      </button>
      <div ref="historyRoot" class="pilot-history">
        <button
          type="button"
          class="pilot-tabbar__add pilot-tabbar__hist"
          :class="{ 'is-open': historyOpen }"
          :title="t('pilot.history.title')"
          @click="historyOpen = !historyOpen"
        >
          <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M12 8v4l2.5 2.5M21 12a9 9 0 1 1-9-9" stroke="currentColor" stroke-width="2" stroke-linecap="round" />
            <path d="M21 3v5h-5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </button>
        <Transition name="hl-pop">
          <div v-if="historyOpen" class="pilot-history__menu">
            <div v-if="!(sessions ?? []).length" class="pilot-history__empty">{{ t('pilot.history.empty') }}</div>
            <div v-for="it in sessions ?? []" :key="it.sid" class="pilot-history__row">
              <button type="button" class="pilot-history__item" @click="openFromHistory(it.sid)">
                <span class="pilot-history__name">
                  <span
                    v-if="it.running"
                    class="pilot-side__running"
                    :title="t('pilot.session.running')"
                    aria-hidden="true"
                  ></span>
                  {{ it.title || t('pilot.session.untitled') }}
                </span>
                <span class="pilot-history__meta">
                  {{ it.running ? t('pilot.session.running') : `${t('pilot.history.turns', { n: it.turn_total })} · ${sessionTime(it.updated_at)}` }}
                </span>
              </button>
              <button
                type="button"
                class="pilot-tab__x"
                :class="{ 'is-armed': deleteArmSid === it.sid }"
                :title="deleteArmSid === it.sid ? t('pilot.history.deleteConfirm') : t('pilot.history.delete')"
                @click="armDelete(it.sid)"
              >
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
                </svg>
              </button>
            </div>
          </div>
        </Transition>
      </div>
      <HlPilotOutline
        v-if="!page"
        :turns="turns"
        :active-index="outlineActive"
        @jump="jumpToTurn"
        @toggle="onOutlineToggle"
      />
    </div>

    <div ref="scrollBox" class="pilot-turns" @scroll.passive="onContainerScroll">
      <div v-for="(turn, i) in turns" :key="i" class="pilot-turn" :data-turn-index="turn.kind === 'compact' ? undefined : i">
        <!-- 压缩分隔线：账本边界事件，历史消息一条不动（ZCode 压缩语义） -->
        <div v-if="turn.kind === 'compact'" class="pilot-compact">
          <span class="pilot-compact__line" aria-hidden="true"></span>
          <span class="pilot-compact__pill">
            <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
              <path d="M7 4h8l4 4v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" />
              <path d="M9.5 12.5l2 2 3.5-4" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" />
            </svg>
            {{ t(turn.compactTrigger === 'manual' ? 'pilot.compact.manual' : 'pilot.compact.auto') }}
          </span>
          <span class="pilot-compact__line" aria-hidden="true"></span>
        </div>
        <template v-else>
        <p class="pilot-turn__q">{{ turn.q }}</p>

        <!-- agent 回复层：与用户气泡两种图层的统一左对齐面板；pending 轮只上屏发言，回复侧由下方 live 区承担 -->
        <div v-if="turn.kind !== 'pending'" class="pilot-reply">
          <!-- agent 过程链：链头一行摘要（工时+步数），展开看思考/步骤/记忆；done.steps 全量回填 -->
          <HlPilotChain
            v-if="turn.phases?.length || turn.think || turn.steps.length"
            :steps="turn.steps"
            :phases="turn.phases ?? []"
            :think="turn.think"
            :think-sec="turn.thinkSec ?? null"
            :elapsed-sec="turn.elapsedSec ?? null"
            :memory="turn.mem ?? null"
            :failed="turn.kind === 'reason'"
          />

          <div v-if="turn.kind === 'reason'" class="pilot-reason">
            {{ turn.reasonKey ? t(turn.reasonKey) : '' }}
            <span v-if="turn.cached">({{ t('pilot.cached') }})</span>
          </div>

          <div v-else-if="turn.kind === 'candidates'" class="pilot-briefing">
            <div class="pilot-briefing__title">{{ turn.reasonKey ? t(turn.reasonKey) : '' }}</div>
            <button
              v-for="(g, gi) in turn.items"
              :key="gi"
              type="button"
              class="pilot-cand"
              :class="{ 'is-locked': i !== turns.length - 1 }"
              @click="pickCandidate(turn, gi)"
            >
              <HlImg
                :src="pilotCoverUrl(g.appid)"
                :alt="g.name || `AppID ${g.appid}`"
                loading="lazy"
                class="pilot-cand__cover"
              >
                <template #fallback>
                  <span class="pilot-cand__cover-fallback">{{ (g.name || `AppID ${g.appid}`).slice(0, 2) }}</span>
                </template>
              </HlImg>
              <span class="pilot-cand__name">{{ gi + 1 }}. {{ g.name }}</span>
              <span
                v-if="typeof g.positiveRate === 'number' && g.positiveRate > 0"
                class="pilot-cand__rating"
                :class="ratingClass(g.positiveRate)"
              >{{ positivePct(g.positiveRate) }}%</span>
              <span class="pilot-cand__meta">
                {{ fen(g.cnyFen) }}<template v-if="g.discount"> · -{{ g.discount }}%</template>
              </span>
            </button>
          </div>

          <HlPilotCards v-else-if="turn.kind === 'action'" :cards="turn.cards ?? []" />

          <HlPilotCards v-else-if="turn.kind === 'price'" :cards="turn.cards ?? []" />

          <HlPilotCards v-else-if="turn.kind === 'games'" :cards="turn.cards ?? []" />

          <!-- 批量提议：清单 + 确认/取消，确认后才真正执行 -->
          <div v-else-if="turn.kind === 'proposal'" class="pilot-proposal">
            <HlPilotCards
              :cards="turn.cards ?? []"
              :busy-pid="busyPid"
              @proposal="onProposal"
            />
            <div
              v-if="turn.text"
              class="pilot-answer"
              v-html="mdLiteToHtml(turn.text)"
            ></div>
          </div>

          <!-- 回答：极简 Markdown 渲染（分点/粗体/行内码/代码块；渲染器内先转义后拼标签） -->
          <template v-else-if="turn.kind === 'answer'">
            <div
              v-if="turn.text"
              class="pilot-answer"
              v-html="mdLiteToHtml(turn.text)"
            ></div>
            <div v-if="turn.stopped" class="pilot-stopped">{{ t('pilot.stopped') }}</div>
          </template>

          <div v-else-if="turn.kind === 'guide'" class="pilot-guide">
            <p class="pilot-guide__title">{{ t('pilot.guide.title') }}</p>
            <p>{{ t('pilot.guide.monitor') }}</p>
            <p>{{ t('pilot.guide.alert') }}</p>
            <router-link class="pilot-guide__link" to="/library">{{ t('pilot.guide.link') }}</router-link>
          </div>

          <!-- agent 轮的结构化卡片：统一富卡片渲染（价格/列表/动作/导航） -->
          <HlPilotCards
            v-if="turn.cards?.length && turn.kind === 'answer'"
            :cards="turn.cards"
            :busy-pid="busyPid"
            @proposal="onProposal"
          />
        </div>
        </template>
      </div>

      <div v-if="streaming" class="pilot-turn">
        <div class="pilot-reply">
          <!-- 运行中过程链：阶段块随定界逐个长出，每段正文随所在阶段就地展示；
               完成时原位替换为历史链，终答正文改由回答层承接 -->
          <HlPilotChain
            live
            :steps="liveStepsFlat"
            :phases="livePhases"
            :waiting-text="loadingText"
          />
          <div v-if="queuedHint" class="pilot-queued">
            {{ t(queuedHint === 'parallel' ? 'pilot.queued.parallel' : 'pilot.queued.waiting') }}
          </div>
        </div>
      </div>
    </div>

    <!-- 追问 dock：固定钉在输入框正上方（位置恒定只换内容）——空会话给开场推荐，
         有轮给最后一轮的上下文追问；流式中隐藏避免与进行中的生成抢注意力 -->
    <div v-if="dockFollowUps.length" class="pilot-followups pilot-followups--dock">
      <span class="pilot-followups__label">{{ t('pilot.followup.title') }}</span>
      <div class="pilot-followups__list">
        <button
          v-for="fu in dockFollowUps"
          :key="fu.key"
          type="button"
          class="pilot-followup-chip"
          @click="triggerFollowUp(fu.prompt)"
        >
          <svg class="pilot-followup-chip__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M5 12h14M12 5l7 7-7 7" />
          </svg>
          <span>{{ t(fu.key) }}</span>
        </button>
      </div>
    </div>

    <div class="pilot-composer">
      <HlTextarea
        v-model="question"
        :rows="2"
        class="pilot-composer__input"
        :placeholder="t('pilot.ask.placeholder')"
        @keydown="onComposerKeydown"
      />
      <div class="pilot-composer__bar">
        <div class="pilot-composer__chips">
          <!-- 跨会话引用：+ 弹最近会话清单（排除当前），选中后下一轮提问携带其要点（一次性） -->
          <HlDropdown :items="plusMenuItems" trigger="click" direction="up" @select="onMenuSelect">
            <HlChip tone="neutral" class="pilot-ref-add" :title="t('pilot.ref.add')">
              <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                <path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" />
              </svg>
            </HlChip>
          </HlDropdown>
          <HlTag v-if="referenceSession" type="accent" closable @close="referenceSession = null">
            {{ t('pilot.ref.chip', { title: referenceSession.title }) }}
          </HlTag>
          <HlTag v-if="game" type="accent">{{ t('pilot.context.of') }} · {{ game.name }}</HlTag>
          <div
            class="pilot-ctxpick"
            @click.stop
            @mouseenter="ctxOpen = true"
            @mouseleave="ctxOpen = false"
          >
            <button
              type="button"
              class="pilot-ctxpick__btn"
              :class="{ 'is-warn': ctxPercent > 85 }"
              :title="ctxTitle"
              :aria-label="ctxTitle"
              @click="ctxOpen = !ctxOpen"
            >
              <svg class="pilot-ctxpick__ring" viewBox="0 0 20 20" aria-hidden="true">
                <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor"
                        stroke-width="2.5" opacity="0.25" />
                <circle cx="10" cy="10" r="8" fill="none" stroke="currentColor"
                        stroke-width="2.5" stroke-linecap="round"
                        stroke-dasharray="50.265 50.265"
                        :stroke-dashoffset="50.265 * (1 - ctxPercent / 100)"
                        transform="rotate(-90 10 10)" />
              </svg>
              <span v-if="lastCtx" class="pilot-ctxpick__num">{{ `${fmtTokens(lastCtx.tokens)}/${fmtTokens(lastCtx.budget)}` }}</span>
            </button>
            <Transition name="hl-pop">
              <div v-if="ctxOpen" class="pilot-ctxpick__panel">
                <div class="pilot-ctxpick__head">
                  <span>{{ t('pilot.ctx.title') }}</span>
                  <span class="pilot-ctxpick__num">
                    {{ lastCtx ? `${fmtTokens(lastCtx.tokens)} / ${fmtTokens(lastCtx.budget)}` : t('pilot.ctx.empty') }}
                  </span>
                </div>
                <div class="pilot-ctxpick__track">
                  <div
                    class="pilot-ctxpick__fill"
                    :class="{ 'is-warn': ctxPercent > 85 }"
                    :style="{ width: ctxPercent + '%' }"
                  >
                    <i
                      v-for="seg in ctxSegments"
                      :key="seg.source"
                      :class="`is-${seg.source}`"
                      :style="{ flexGrow: seg.share }"
                      aria-hidden="true"
                    ></i>
                  </div>
                </div>
                <div v-if="ctxSegments.length" class="pilot-ctxpick__legend">
                  <div v-for="seg in ctxSegments" :key="seg.source" class="pilot-ctxpick__row">
                    <i :class="`is-${seg.source}`" aria-hidden="true"></i>
                    <span class="pilot-ctxpick__src">{{ t(`pilot.ctx.src.${seg.source}` as MessageKey) }}</span>
                    <span class="pilot-ctxpick__pct">{{ fmtPct(seg.share) }}</span>
                  </div>
                </div>
                <div
                  v-if="typeof lastCtx?.hitRate === 'number'"
                  class="pilot-ctxpick__cache"
                  :class="{ 'is-low': !ctxCacheVisible }"
                >
                  <span>{{ t('pilot.ctx.cacheHit') }}</span>
                  <span class="pilot-ctxpick__pct">{{ fmtPct(lastCtx.hitRate) }}</span>
                </div>
                <!-- 本会话消耗：账本全轮折叠（DSH UsagePill 口径——分桶精确数，
                     部分命中永不显示 100%；provider 未报告的桶整行不出） -->
                <div v-if="statsTotal > 0 && sessionStats" class="pilot-ctxpick__usage">
                  <div class="pilot-ctxpick__usage-head">
                    <span>{{ t('pilot.usage.title') }}</span>
                    <span class="pilot-ctxpick__pct">{{ fmtInt(statsTotal) }}</span>
                  </div>
                  <div v-if="statsCacheHit != null" class="pilot-ctxpick__urow">
                    <span>{{ t('pilot.usage.cacheHit') }}</span>
                    <span>{{ fmtHit(statsCacheHit) }}</span>
                  </div>
                  <div class="pilot-ctxpick__urow">
                    <span>{{ t('pilot.usage.uncached') }}</span>
                    <span>{{ fmtInt((sessionStats.cache_base_tokens ?? sessionStats.usage_in) - (sessionStats.cache_read_tokens ?? 0)) }}</span>
                  </div>
                  <div v-if="sessionStats.cache_read_tokens != null" class="pilot-ctxpick__urow">
                    <span>{{ t('pilot.usage.cacheRead') }}</span>
                    <span>{{ fmtInt(sessionStats.cache_read_tokens) }}</span>
                  </div>
                  <div v-if="sessionStats.cache_write_tokens != null" class="pilot-ctxpick__urow">
                    <span>{{ t('pilot.usage.cacheWrite') }}</span>
                    <span>{{ fmtInt(sessionStats.cache_write_tokens) }}</span>
                  </div>
                  <div class="pilot-ctxpick__urow">
                    <span>{{ t('pilot.usage.output') }}</span>
                    <span>{{ fmtInt(sessionStats.usage_out) }}</span>
                  </div>
                </div>
              </div>
            </Transition>
          </div>
        </div>

        <div class="pilot-composer__right">
          <!-- 模型就地切换：ZCode 对话面板样式（ghost chip + 向上清单 + 底部管理入口） -->
          <div v-if="pilotReady" ref="modelPickRoot" class="pilot-modelpick" @click.stop>
            <button
              type="button"
              class="pilot-modelpick__chip"
              :class="{ 'is-open': modelMenuOpen, 'is-busy': switchingModel }"
              @click="openModelMenu()"
            >
              <span class="pilot-modelpick__name">{{ pilotModel || t('pilot.composer.modelOff') }}</span>
              <span class="pilot-modelpick__chevron" aria-hidden="true"></span>
            </button>
            <Transition name="hl-pop">
              <div v-if="modelMenuOpen" class="pilot-modelpick__menu">
                <div class="pilot-modelpick__list">
                  <div v-for="g in menuGroups" :key="g.id" class="pilot-modelpick__group">
                    <button
                      type="button"
                      class="pilot-modelpick__group-head"
                      :class="{ 'is-open': openGroups.has(g.id) }"
                      @click="toggleGroup(g.id)"
                    >
                      <span class="pilot-modelpick__group-name" :title="g.name">{{ g.name }}</span>
                      <span
                        v-if="g.active"
                        class="pilot-modelpick__group-badge"
                      >{{ t('pilot.modelMenu.inUse') }}</span>
                      <span class="pilot-modelpick__group-chev" aria-hidden="true"></span>
                    </button>
                    <template v-if="openGroups.has(g.id)">
                      <button
                        v-for="m in g.models"
                        :key="m"
                        type="button"
                        class="pilot-modelpick__item"
                        :class="{ 'is-on': m === pilotModel && g.active }"
                        @click="pickModel(g.id, m)"
                      >
                        <span class="pilot-modelpick__item-name" :title="m">{{ m }}</span>
                        <span
                          v-if="m === pilotModel && g.active"
                          class="pilot-modelpick__check"
                          aria-hidden="true"
                        ></span>
                      </button>
                    </template>
                  </div>
                </div>
                <div class="pilot-modelpick__foot">
                  <button type="button" class="pilot-modelpick__manage" @click="goSettings">
                    {{ t(pilotModels.length ? 'pilot.modelMenu.manage' : 'pilot.modelMenu.empty') }}
                  </button>
                </div>
              </div>
            </Transition>
          </div>
          <HlChip v-else tone="accent" @click="goSettings">{{ t('pilot.composer.modelOff') }}</HlChip>
          <button
            v-if="streaming"
            type="button"
            class="pilot-send pilot-send--stop"
            :title="t('pilot.stop')"
            :aria-label="t('pilot.stop')"
            @click="stopPilot"
          >
            <span class="pilot-send__square" aria-hidden="true"></span>
          </button>
          <HlButton
            v-else
            variant="primary"
            size="sm"
            class="pilot-send"
            :disabled="!question.trim()"
            :title="t('pilot.ask.button')"
            :aria-label="t('pilot.ask.button')"
            @click="ask()"
          >
            <span class="pilot-send__arrow" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none">
                <path d="M12 5v14M5 12l7-7 7 7" stroke="currentColor" stroke-width="2.2"
                      stroke-linecap="round" stroke-linejoin="round" />
              </svg>
            </span>
          </HlButton>
        </div>
      </div>
    </div>

    <!-- 底栏会话统计：轮/步/速度读数 + 点击弹总耗时/首字面板（账本投影，无轮不渲染） -->
    <HlPilotStats v-if="sessionStats && sessionStats.turns > 0" :stats="sessionStats" />
  </div>
</template>

<style scoped>
.pilot-console {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 4px 2px;
  /* 撑满宿主（抽屉 body）：对话区吃掉剩余空间，输入框恒在底部 */
  height: 100%;
}

/* ── 整页：会话列 Teleport 到页面级左侧挂点（与对话列布局解耦）；
   head 标题与页面标题重复故整页隐藏 ── */
.pilot-console.is-page {
  height: auto;
}

.pilot-console.is-page .pilot-head,
.pilot-console.is-page .pilot-tabbar {
  display: none;
}

/* 列体尺寸由宿主挂点（Index 的 .pilot-page__side）约束，这里只管纵向分布 */
.pilot-side {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.pilot-side__tools {
  display: flex;
  align-items: center;
  gap: 4px;
}

.pilot-side__new {
  flex: 1;
  min-width: 0;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border: 1px dashed var(--border-soft);
  border-radius: 8px;
  background: none;
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  white-space: nowrap;
}

.pilot-side__new:hover:not(:disabled),
.pilot-side__new.is-current {
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}

.pilot-side__new:disabled {
  opacity: 0.4;
}

.pilot-side__new svg {
  width: 13px;
  height: 13px;
  flex: none;
}

.pilot-side__scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px;
  scrollbar-width: thin;
  scrollbar-color: var(--scroll-thumb) transparent;
}

.pilot-side__row {
  position: relative;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 8px;
  cursor: pointer;
}

.pilot-side__row:hover {
  background: var(--accent-a10);
}

.pilot-side__row.is-current {
  background: var(--accent-a10);
}

.pilot-side__row.is-current::before {
  content: '';
  position: absolute;
  left: 0;
  top: 6px;
  bottom: 6px;
  width: 2px;
  border-radius: 1px;
  background: var(--accent);
}

.pilot-side__main {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.pilot-side__name {
  display: flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-primary);
}

.pilot-side__row.is-current .pilot-side__name {
  color: var(--accent);
}

.pilot-side__meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--text-secondary);
  font-variant-numeric: tabular-nums;
}

/* 生成中脉动点：accent 圆点呼吸（reduced-motion 降级为常亮） */
.pilot-side__running {
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  animation: pilot-running-pulse 1.2s ease-in-out infinite;
}

@keyframes pilot-running-pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.3;
  }
}

@media (prefers-reduced-motion: reduce) {
  .pilot-side__running {
    animation: none;
  }
}

.pilot-side__x {
  flex: none;
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  border-radius: 6px;
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s;
}

.pilot-side__row:hover .pilot-side__x,
.pilot-side__x.is-armed,
.pilot-side__x.is-ok {
  opacity: 1;
}

.pilot-side__x:hover,
.pilot-side__x.is-armed {
  background: var(--accent-a10);
  color: var(--warning);
}

.pilot-side__x.is-ok {
  color: var(--success);
}

.pilot-side__x svg {
  width: 12px;
  height: 12px;
}

.pilot-side__rename {
  flex: 1;
  min-width: 0;
}

.pilot-side__empty {
  padding: 12px 8px;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: center;
}

/* 窄屏整页回落标签栏：左列由 Index 隐藏，tabbar 恢复（会话入口不丢） */
@media (max-width: 1080px) {
  .pilot-console.is-page .pilot-head {
    display: none;
  }

  .pilot-console.is-page .pilot-tabbar {
    display: flex;
  }
}

.pilot-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.pilot-head__title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

/* 会话标签栏：账本投影小标签（横向滚动，双击重命名，× 两段式删除，+ 即新对话） */
.pilot-tabbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px;
}

/* 滚动区只包标签（overflow 会裁剪历史下拉面板，按钮必须在 overflow 之外）；
   scrollbar-width/color 配对取 token——单设 scrollbar-width 会让 Chromium
   绕开全局 ::-webkit-scrollbar 主题出原生细滚条 */
.pilot-tabbar__scroll {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 4px;
  overflow-x: auto;
  padding: 2px;
  scrollbar-width: thin;
  scrollbar-color: var(--scroll-thumb) transparent;
}

.pilot-tabbar__add {
  flex: none;
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border: 1px dashed var(--border-soft);
  border-radius: 8px;
  color: var(--text-secondary);
}

/* 当前停留在未落账本的新对话上：+ 即「所在位置」 */
.pilot-tabbar__add.is-current {
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}

/* 历史入口：与 + 同语言的方钮，面板向下弹（账本只读投影，找回/删除都在这） */
.pilot-history {
  position: relative;
  flex: none;
}

.pilot-tabbar__hist.is-open {
  border-style: solid;
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}

.pilot-history__menu {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  z-index: 6;
  width: 264px;
  max-height: 320px;
  overflow-y: auto;
  padding: 4px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
  /* 与全局 ::-webkit-scrollbar 主题对齐（webkit 伪元素在设 scrollbar-width 的
     元素上不生效，需标准属性配对） */
  scrollbar-width: thin;
  scrollbar-color: var(--scroll-thumb) transparent;
}

.pilot-history__row {
  display: flex;
  align-items: center;
  gap: 2px;
  border-radius: 6px;
}

.pilot-history__row:hover {
  background: var(--accent-a10);
}

.pilot-history__item {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 4px 6px 10px;
  border: none;
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.pilot-history__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--text-primary);
}

.pilot-history__meta {
  flex: none;
  font-size: 11px;
  color: var(--text-secondary);
}

.pilot-history__row .pilot-tab__x {
  opacity: 0;
  transition: opacity 0.15s;
}

.pilot-history__row:hover .pilot-tab__x,
.pilot-history__row .pilot-tab__x.is-armed {
  opacity: 1;
}

.pilot-history__empty {
  padding: 12px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: center;
}

.pilot-tabbar__add:hover:not(:disabled) {
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}

.pilot-tabbar__add:disabled {
  opacity: 0.4;
}

.pilot-tabbar__add svg {
  width: 14px;
  height: 14px;
}

.pilot-tab {
  flex: none;
  display: flex;
  align-items: center;
  gap: 2px;
  max-width: 172px;
  padding: 0 2px 0 8px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-card);
}

.pilot-tab.is-current {
  border-color: var(--accent-a30);
  background: var(--accent-a10);
}

.pilot-tab.is-armed {
  border-color: var(--danger);
}

.pilot-tab__cur {
  flex: none;
  width: 12px;
  height: 12px;
  color: var(--accent);
}

.pilot-tab__main {
  flex: 1;
  min-width: 0;
  padding: 6px 0;
  text-align: left;
}

.pilot-tab__name {
  display: block;
  max-width: 118px;
  font-size: 12px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pilot-tab__rename {
  /* 击穿框架 .hl-input-wrap 的 min-width:180px——否则改名框撑破 172px 标签盒漫过 + 号 */
  width: 118px;
  min-width: 0;
}

.pilot-tab__x {
  flex: none;
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  color: var(--text-secondary);
}

.pilot-tab__x svg {
  width: 12px;
  height: 12px;
}

.pilot-tab__x:hover {
  background: var(--accent-a15);
  color: var(--accent);
}

.pilot-tab__x.is-ok {
  color: var(--success);
}

.pilot-tab__x.is-armed {
  color: var(--ink-on-fill);
  background: var(--danger);
}

.pilot-tab__x.is-disabled {
  opacity: 0.4;
  pointer-events: none;
}

/* 加号引用入口：与右侧 chip 同语言的小方钮 */
.pilot-ref-add {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  padding: 0;
}

.pilot-ref-add svg {
  width: 14px;
  height: 14px;
}

.pilot-head__actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

/* 输入区 = 唯一 accent 描边面（对话区无 accent 边框），聚焦时亮起光晕——
   与用户气泡/agent 面板三层互异，抽屉里一眼可辨「这是输入的地方」 */
.pilot-composer {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--accent-a30);
  border-radius: 12px;
  background: var(--bg-base);
  transition: border-color var(--duration-4) var(--motion-scale),
              box-shadow var(--duration-4) var(--motion-scale);
}

.pilot-composer:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent-a15) inset, 0 0 18px var(--accent-a15);
}

.pilot-composer .pilot-composer__input {
  min-height: 0;
  padding: 2px 4px;
  background: transparent;
  border-radius: 0;
  box-shadow: none;
  resize: none;
}

/* 聚焦光晕由容器承载，textarea 自身的内发光关掉 */
.pilot-composer .pilot-composer__input:focus {
  box-shadow: none;
}

/* 圆形发送钮：复用 HlButton（primary 配色 + 内置 loading），这里只改尺寸 */
.pilot-send {
  flex: none;
  width: 34px;
  height: 34px;
  padding: 0;
  border-radius: 50%;
}

.pilot-send__arrow {
  display: grid;
  place-items: center;
}

.pilot-send__arrow svg {
  width: 16px;
  height: 16px;
}

.pilot-send svg {
  width: 16px;
  height: 16px;
}

.pilot-send--stop {
  display: grid;
  place-items: center;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  color: var(--danger);
  cursor: pointer;
}

.pilot-send--stop:hover {
  border-color: var(--danger);
}

.pilot-send__square {
  width: 10px;
  height: 10px;
  border-radius: 2px;
  background: currentColor;
}

.pilot-stopped {
  margin-top: 2px;
  font-size: 12px;
  color: var(--text-secondary);
}

.pilot-ctxpick {
  position: relative;
  min-width: 0;
}

/* + 菜单向上弹出且不遮按键：bottom 锚在按键上方（同模型菜单），覆盖框架的 top+translate 技巧 */
.pilot-composer :deep(.hl-dropdown-pop.is-open-up) {
  top: auto;
  bottom: calc(100% + 6px);
  --pop-y: 0px;
}

.pilot-ctxpick__btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 8px;
  font: inherit;
  font-size: 12px;
  color: var(--accent);
  border: none;
  border-radius: 999px;
  background: var(--accent-a10);
  cursor: pointer;
}

.pilot-ctxpick__btn:hover {
  color: var(--text-primary);
}

.pilot-ctxpick__btn.is-warn {
  color: var(--warning);
}

.pilot-ctxpick__ring {
  width: 14px;
  height: 14px;
}

.pilot-ctxpick__num {
  font-variant-numeric: tabular-nums;
}

.pilot-ctxpick__panel {
  position: absolute;
  bottom: calc(100% + 8px);
  left: 0;
  z-index: 7;
  display: flex;
  flex-direction: column;
  gap: 8px;
  width: 260px;
  padding: 12px;
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
}

.pilot-ctxpick__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 12px;
  color: var(--text-secondary);
}

.pilot-ctxpick__num,
.pilot-ctxpick__pct {
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  color: var(--text-primary);
}

.pilot-ctxpick__track {
  height: 6px;
  border-radius: 3px;
  background: var(--accent-a10);
  overflow: hidden;
}

.pilot-ctxpick__fill {
  display: flex;
  height: 100%;
  border-radius: 3px;
  overflow: hidden;
  min-width: 2px;
  transition: width calc(var(--duration-4, 0.2s) * var(--motion-scale, 1)) ease;
}

.pilot-ctxpick__fill.is-warn i {
  filter: saturate(0.6) brightness(0.9);
}

.pilot-ctxpick__fill i {
  display: block;
  min-width: 2px;
}

.pilot-ctxpick__fill i.is-system {
  background: var(--accent);
}

.pilot-ctxpick__fill i.is-tools {
  background: color-mix(in oklab, var(--accent) 78%, var(--bg-card));
}

.pilot-ctxpick__fill i.is-summary {
  background: color-mix(in oklab, var(--accent) 58%, var(--bg-card));
}

.pilot-ctxpick__fill i.is-history {
  background: color-mix(in oklab, var(--accent) 42%, var(--bg-card));
}

.pilot-ctxpick__fill i.is-current {
  background: color-mix(in oklab, var(--accent) 28%, var(--bg-card));
}

.pilot-ctxpick__legend {
  display: grid;
  gap: 4px;
}

.pilot-ctxpick__row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.pilot-ctxpick__row i {
  flex: none;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  border: 1px solid var(--border-soft);
}

.pilot-ctxpick__row i.is-system {
  background: var(--accent);
}

.pilot-ctxpick__row i.is-tools {
  background: color-mix(in oklab, var(--accent) 78%, var(--bg-card));
}

.pilot-ctxpick__row i.is-summary {
  background: color-mix(in oklab, var(--accent) 58%, var(--bg-card));
}

.pilot-ctxpick__row i.is-history {
  background: color-mix(in oklab, var(--accent) 42%, var(--bg-card));
}

.pilot-ctxpick__row i.is-current {
  background: color-mix(in oklab, var(--accent) 28%, var(--bg-card));
}

.pilot-ctxpick__src {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pilot-ctxpick__pct {
  margin-left: auto;
}

.pilot-ctxpick__cache {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
  font-size: 12px;
  color: var(--text-secondary);
}

.pilot-ctxpick__cache.is-low .pilot-ctxpick__pct {
  color: var(--text-secondary);
}

/* 本会话消耗段：段头（合计精确数）+ 分桶行，与图例行同字层 */
.pilot-ctxpick__usage {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--border-soft);
}

.pilot-ctxpick__usage-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding-bottom: 2px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-primary);
}

.pilot-ctxpick__urow {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  padding: 2px 0;
  font-size: 11px;
  color: var(--text-secondary);
}

.pilot-ctxpick__urow span:last-child {
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
}

.pilot-composer__bar {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.pilot-composer__right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
}

/* 压缩分隔线：两侧细线 + 居中状态 pill（账本边界事件，历史不动） */
.pilot-compact {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
}

.pilot-compact__line {
  flex: 1;
  height: 1px;
  background: var(--border-soft);
}

.pilot-compact__pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 3px 10px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  font-size: 11px;
  color: var(--text-secondary);
  background: var(--bg-card);
}

.pilot-queued {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}

.pilot-queued::before {
  content: '';
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--warning);
  animation: pilot-queued-pulse 1.2s ease-in-out infinite;
}

@keyframes pilot-queued-pulse {
  0%, 100% { opacity: 0.35; }
  50% { opacity: 1; }
}

.pilot-compact__pill svg {
  width: 12px;
  height: 12px;
}

.pilot-composer__chips .hl-chip.is-warn {
  color: var(--warning);
}

.pilot-composer__chips {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  /* 不能 overflow:hidden：+ 菜单与上下文面板都从这里向上弹出，剪掉=「点了没反应」 */
}

.pilot-turns {
  display: flex;
  flex-direction: column;
  gap: 14px;
  /* 吃掉标签栏与输入框之间的全部剩余空间（输入框恒在底部），内容超出内滚 */
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding-right: 4px;
}

.pilot-turn {
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
  max-width: 100%;
}

/* agent 回复统一图层：恒占满会话列宽（不随内容伸缩），与用户气泡（右对齐
   accent 气泡）形成两种可辨识的层 */
.pilot-reply {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 10px 12px;
  background: var(--bg-base);
  border: 1px solid var(--border-soft);
  border-radius: 4px 14px 14px 14px;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  box-sizing: border-box;
}

.pilot-turn__q {
  align-self: flex-end;
  max-width: 88%;
  margin: 0;
  padding: 7px 12px;
  background: var(--accent-a10);
  border-radius: 14px 14px 4px 14px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-primary);
  white-space: pre-wrap;
  word-break: break-word;
}

.pilot-composer .hl-button {
  flex: none;
}

.pilot-reason {
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
  background: var(--bg-card);
  border: 1px dashed var(--border-soft);
}

/* 回答走对话流排版（不装箱）：markdown 标签由渲染器产出，scoped 下用 :deep() 着样式 */
.pilot-answer {
  margin: 0;
  font-size: 13px;
  line-height: 1.75;
  color: var(--text-primary);
  word-break: break-word;
}

.pilot-answer :deep(p) {
  margin: 0 0 6px;
}

.pilot-answer :deep(p:last-child) {
  margin-bottom: 0;
}

.pilot-answer :deep(strong) {
  font-weight: 600;
}

.pilot-answer :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--accent-a10);
  font-size: 12px;
}

.pilot-answer :deep(ul),
.pilot-answer :deep(ol) {
  margin: 2px 0 6px;
  padding-left: 18px;
  display: grid;
  gap: 2px;
}

.pilot-answer :deep(li)::marker {
  color: var(--text-secondary);
}

.pilot-answer :deep(pre) {
  margin: 4px 0 8px;
  padding: 10px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-card);
  overflow-x: auto;
}

.pilot-answer :deep(pre code) {
  padding: 0;
  background: none;
  border-radius: 0;
  font-size: 12px;
  line-height: 1.6;
}

.pilot-answer :deep(h3) {
  margin: 10px 0 4px;
  padding-left: 8px;
  border-left: 3px solid var(--accent);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.5;
  color: var(--text-primary);
}

.pilot-answer :deep(h3:first-child) {
  margin-top: 0;
}

.pilot-answer :deep(mark) {
  padding: 0 3px;
  border-radius: 4px;
  background: var(--accent-a15);
  color: var(--text-primary);
  font-weight: 600;
}

.pilot-answer :deep(blockquote) {
  margin: 4px 0 8px;
  padding: 8px 10px;
  border-left: 3px solid var(--accent);
  border-radius: 0 8px 8px 0;
  background: var(--accent-a10);
}

.pilot-answer :deep(blockquote p) {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-secondary);
}

.pilot-answer :deep(hr) {
  margin: 8px 0;
  border: none;
  border-top: 1px solid var(--border-soft);
}

.pilot-answer :deep(.pilot-table-wrap) {
  margin: 8px 0 10px;
  max-width: 100%;
  overflow-x: auto;
  border-radius: 8px;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
}

.pilot-answer :deep(.pilot-table) {
  width: 100%;
  min-width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  line-height: 1.5;
  text-align: left;
}

.pilot-answer :deep(.pilot-table th) {
  padding: 8px 10px;
  background: var(--surface-inset);
  color: var(--text-secondary);
  font-weight: 600;
  border-bottom: 1px solid var(--border-soft);
  white-space: nowrap;
}

.pilot-answer :deep(.pilot-table td) {
  padding: 7px 10px;
  border-bottom: 1px solid var(--border-soft);
  color: var(--text-primary);
}

.pilot-answer :deep(.pilot-table tr:last-child td) {
  border-bottom: none;
}

.pilot-answer :deep(.pilot-table tbody tr:hover) {
  background: var(--accent-a10);
}

.pilot-briefing__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 6px;
}

.pilot-briefing__text {
  margin: 0;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-primary);
}

.pilot-cand {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  background: var(--bg-card);
  cursor: pointer;
  text-align: left;
}

.pilot-cand + .pilot-cand {
  margin-top: 6px;
}

.pilot-cand.is-locked {
  cursor: default;
  opacity: 0.6;
}

.pilot-cand__cover {
  flex: none;
  width: 72px;
  aspect-ratio: 460 / 215;
  border-radius: 5px;
  overflow: hidden;
  background: var(--surface-inset);
  display: grid;
  place-items: center;
}

.pilot-cand__cover :deep(img) {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.pilot-cand__cover-fallback {
  font-size: 11px;
  color: var(--text-secondary);
}

.pilot-cand__name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pilot-cand__meta {
  font-size: 12px;
  color: var(--text-secondary);
  white-space: nowrap;
}

.pilot-cand__rating {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--success);
}

.pilot-cand__rating.medium {
  color: var(--warning);
}

.pilot-cand__rating.low {
  color: var(--text-secondary);
}

.pilot-guide p {
  margin: 0 0 6px;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-primary);
}

.pilot-guide__title {
  font-weight: 600;
}

.pilot-guide__link {
  font-size: 13px;
}

/* 模型就地切换：ghost chip + 向上弹清单（ZCode 对话面板样式） */
.pilot-modelpick {
  position: relative;
}

.pilot-modelpick__chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 220px;
  padding: 4px 8px;
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
}

.pilot-modelpick__chip:hover,
.pilot-modelpick__chip.is-open {
  background: var(--accent-a10);
}

.pilot-modelpick__chip.is-busy {
  opacity: 0.6;
  cursor: wait;
}

.pilot-modelpick__name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-weight: 500;
  color: var(--text-secondary);
}

.pilot-modelpick__chip.is-open .pilot-modelpick__name {
  color: var(--text-primary);
}

.pilot-modelpick__chevron {
  flex: none;
  width: 7px;
  height: 7px;
  border-right: 1.5px solid var(--text-faint);
  border-bottom: 1.5px solid var(--text-faint);
  transform: rotate(45deg) translateY(-1px);
  transition: transform 0.2s;
}

.pilot-modelpick__chip.is-open .pilot-modelpick__chevron {
  transform: rotate(-135deg) translateY(-2px);
}

.pilot-modelpick__menu {
  position: absolute;
  bottom: calc(100% + 8px);
  right: 0;
  z-index: 6;
  display: flex;
  flex-direction: column;
  min-width: 208px;
  max-width: min(300px, calc(100vw - 3rem));
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: 10px;
  box-shadow: var(--shadow-lg);
  overflow: hidden;
}

.pilot-modelpick__list {
  max-height: 288px;
  overflow-y: auto;
  padding: 4px;
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.pilot-modelpick__item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 32px;
  padding: 6px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
  text-align: left;
}

.pilot-modelpick__item:hover {
  background: var(--accent-a10);
}

.pilot-modelpick__item-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  font-family: var(--font-mono, monospace);
  color: var(--text-primary);
}

.pilot-modelpick__item.is-on .pilot-modelpick__item-name {
  color: var(--accent);
  font-weight: 600;
}

.pilot-modelpick__check {
  flex: none;
  width: 10px;
  height: 5px;
  border-left: 1.8px solid var(--accent);
  border-bottom: 1.8px solid var(--accent);
  transform: rotate(-45deg) translateY(-1px);
}

.pilot-modelpick__foot {
  border-top: 1px solid var(--border-soft);
  padding: 4px;
}

.pilot-modelpick__manage {
  width: 100%;
  padding: 6px 10px;
  border: none;
  border-radius: 6px;
  background: transparent;
  cursor: pointer;
  font-size: 12px;
  color: var(--text-secondary);
  text-align: left;
}

.pilot-modelpick__manage:hover {
  background: var(--accent-a10);
  color: var(--text-primary);
}


/* 菜单分组：厂商收敛头 + 组内模型行 */
.pilot-modelpick__group + .pilot-modelpick__group {
  border-top: 1px solid var(--border-soft);
}

.pilot-modelpick__group-head {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  padding: 7px 10px 5px;
  border: none;
  background: none;
  cursor: pointer;
  text-align: left;
}

.pilot-modelpick__group-head:hover {
  background: var(--accent-a10);
}

.pilot-modelpick__group-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  font-weight: 600;
  color: var(--text-secondary);
}

.pilot-modelpick__group-badge {
  flex: none;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--accent-a10);
  font-size: 10px;
  color: var(--accent);
}

.pilot-modelpick__group-chev {
  flex: none;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--text-faint);
  border-bottom: 1.5px solid var(--text-faint);
  transform: rotate(45deg);
}

.pilot-modelpick__group-head.is-open .pilot-modelpick__group-chev {
  transform: rotate(-135deg);
}

.pilot-followups {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

/* dock 变体：钉在输入框正上方，位置恒定（间距由 console 的 flex gap 供给） */
.pilot-followups--dock {
  padding-inline: 2px;
}

.pilot-followups__label {
  font-size: 11px;
  font-weight: 500;
  color: var(--text-secondary);
}

.pilot-followups__list {
  display: flex;
  flex-wrap: nowrap;
  overflow-x: auto;
  scrollbar-width: none;
  gap: 8px;
  padding-bottom: 2px;
}

.pilot-followups__list::-webkit-scrollbar {
  display: none;
}

.pilot-followup-chip {
  appearance: none;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  color: var(--text-primary);
  padding: 4px 11px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 1.3;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 5px;
  white-space: nowrap;
  flex-shrink: 0;
  transition: all 0.15s ease;
}

.pilot-followup-chip__icon {
  width: 10px;
  height: 10px;
  color: var(--text-muted);
  transition: transform 0.15s ease, color 0.15s ease;
}

.pilot-followup-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-a10);
}

.pilot-followup-chip:hover .pilot-followup-chip__icon {
  color: var(--accent);
  transform: translateX(2px);
}
</style>
