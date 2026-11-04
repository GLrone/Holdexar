<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import {
  ApiError,
  askPilotStream,
  pilotApi,
  type PilotAskResponse,
  type PilotAttachmentChunk,
  type PilotAttachmentRef,
  type PilotFacts,
  type PilotFollowup,
  type PilotNavigateFacts,
  type PilotPhase,
  type PilotPriceFacts,
  type PilotRegionsFacts,
  type PilotResumePayload,
  type PilotSessionListItem,
  type PilotSessionStats,
  type PilotStep,
} from '@/api/client'
import { isEmptyLlmFailure, sessionTime } from '@/lib/pilotView'
import HlPilotComposer from '@/components/business/HlPilotComposer.vue'
import HlPilotTurnItem from '@/components/business/HlPilotTurnItem.vue'
import HlPilotChain from '@/components/business/HlPilotChain.vue'
import HlPilotMarkdown from '@/components/business/HlPilotMarkdown.vue'
import HlPilotSidebar from '@/components/business/HlPilotSidebar.vue'
import { useSessionRowActions } from '@/composables/useSessionRowActions'
import HlPilotOutline from '@/components/business/HlPilotOutline.vue'
import HlPilotRuns from '@/components/business/HlPilotRuns.vue'
import HlPilotStats from '@/components/business/HlPilotStats.vue'
import { useRouter, useRoute } from 'vue-router'
import { useI18n, type MessageKey } from '@/locales'
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
    /** 整页模式：左侧常驻会话列，顶部标签栏退役；抽屉保持不变 */
    page?: boolean
  }>(),
  { game: null, page: false },
)
const { t } = useI18n()
const router = useRouter()
const route = useRoute()

/** 采集当前页面上下文（路由 + 板块 + 会话标题 + 游戏上下文），随每次提问注入模型 */
const viewContext = computed(() => {
  const ctx: { route: string; section?: string; sessionTitle?: string; appid?: number } = { route: route.path }
  const s = route.query.s
  if (typeof s === 'string' && s) ctx.section = s
  // 会话标题让模型知道用户在讨论什么话题（整页 /pilot 模式下路由无信息量，
  // 会话标题是唯一线索）
  const cur = (sessions.value ?? []).find((x) => x.sid === sessionId.value)
  if (cur?.title) ctx.sessionTitle = cur.title
  // 游戏详情页：从路由参数提取 appid（顶栏通用入口不传 game prop，
  // 但用户确实在游戏详情页——路由参数是唯一可靠来源）
  if (route.path.startsWith('/game/')) {
    const id = Number(route.params.appid)
    if (id > 0) ctx.appid = id
  }
  return ctx
})

/** 页面元素选择器：用户点击输入框中的按钮进入选择模式，点击页面元素采集引用 */
/* 页面元素选择器与待发引用/附件清单归 HlPilotComposer；发送载荷经组件 expose 取用 */


const question = ref('')
const streaming = ref(false)
const feedbackBusyKey = ref<string | null>(null)

/** 草稿落盘闸门：挂载回填期间关闸，避免把空值/旧值反写覆盖磁盘上的草稿 */
let draftReady = false

/** 草稿落盘：只认「用户输入」一个来源，发送清空与切会话都要撤掉对应键 */
watch(question, (v) => {
  if (!draftReady) return
  writeDraft(sessionId.value, v)
})

/** 本轮流式阶段的活账本：step_start 开新段，思考/正文/工具按段归位 */
const livePhases = ref<PilotPhase[]>([])
// 排队可见性：供应商按 Key 串行 / 工具执行期都可能长时间无增量——ack.active>1
// 直接提示，其余每段静默（模型等待 / 工具执行）重新武装 8s 提示，把「卡住」变成「进行中」
const queuedHint = ref<'parallel' | 'waiting' | 'toolSlow' | null>(null)
let queuedTimer: ReturnType<typeof setTimeout> | null = null

/** 静默计时：每段静默开始时武装一次；期间收到任何内容即由 clearQueuedHint 撤下 */
function armQueuedHint(kind: 'waiting' | 'toolSlow') {
  if (queuedTimer !== null) clearTimeout(queuedTimer)
  queuedHint.value = null
  queuedTimer = setTimeout(() => { if (streaming.value) queuedHint.value = kind }, 8000)
}

function clearQueuedHint() {
  if (queuedTimer !== null) {
    clearTimeout(queuedTimer)
    queuedTimer = null
  }
  queuedHint.value = null
}


/** 本轮编号引用 → 随提问发送的结构（编号 + 正文） */
/** 流式期步骤展平（过程链的旧口径 prop；阶段记录在场时以 phases 为准） */
const liveStepsFlat = computed(() => livePhases.value.flatMap((p) => p.steps))
/* 终答就位渲染：回答层只承接「尚未被工具定性的当前段」正文——tool_start
   一到，该段正文划归过程链（链内 phasePre 就地展示），回答层空窗等待终答。
   模型在调工具前输出的过程叙述不再以「先文字后卡片」的形态占据回答层。 */
const liveAnswerSource = computed(() => {
  const phases = livePhases.value
  if (!phases.length) return ''
  const last = phases[phases.length - 1]!
  return last.steps.length ? '' : last.text
})

/** 流式期即时卡片账：tool_start 按类型预告挂骨架占位，card 事件原位填充——
 *  卡片归属产出它的阶段，在过程链内随思考时序内嵌展示；done 后由回答层
 *  的正式卡接管（终态卡区在上、终答在下）。 */
interface LiveCardEntry {
  key: number
  name: string
  kind: string | null
  phase: number
  card: PilotFacts | null
}
let liveCardSeq = 0
const liveCards = ref<LiveCardEntry[]>([])
const liveRealCards = computed(() =>
  liveCards.value.map((e) => e.card).filter((c): c is PilotFacts => Boolean(c)))
/** 过程链内嵌视图：只带链渲染需要的字段 */
const liveChainCards = computed(() =>
  liveCards.value.map((e) => ({ key: e.key, phase: e.phase, card: e.card })))

function pushLiveSkeleton(name: string, kind: string | null, phase: number) {
  liveCards.value.push({ key: ++liveCardSeq, name, kind, phase, card: null })
}

function dropLiveSkeleton(name: string) {
  const idx = liveCards.value.findIndex((e) => e.name === name && !e.card)
  if (idx >= 0) liveCards.value.splice(idx, 1)
}

function fillLiveCard(name: string, card: PilotFacts, phase: number) {
  const idx = liveCards.value.findIndex((e) => e.name === name && !e.card)
  if (idx >= 0) {
    liveCards.value[idx] = { ...liveCards.value[idx], card }
  } else {
    // 无预告骨架（提示表未覆盖的工具）：直接追加实体卡，先到先渲染不受影响
    liveCards.value.push({ key: ++liveCardSeq, name, kind: card.kind ?? null, phase, card })
  }
}

function dropUnresolvedCards() {
  if (liveCards.value.some((e) => !e.card)) {
    liveCards.value = liveCards.value.filter((e) => e.card)
  }
}

const turns = ref<Turn[]>([])
// 会话 id：跨表面持久（localStorage）——抽屉/整页/重启应用后续接同一会话，
// 追问与指代靠它串起；「新对话」重新生成。
const sessionId = ref('')
const scrollBox = ref<HTMLDivElement | null>(null)
// 输入区状态栏：最近一轮上下文占用（模型菜单与切换在 HlPilotComposer 内自持）
const lastCtx = ref<{
  tokens: number
  budget: number
  breakdown?: { source: string; chars: number }[] | null
  hitRate?: number | null
} | null>(null)
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
// 模型就地切换：chip 弹清单，选中即 PUT 配置（不关抽屉不跳页）
// 会话管理：常驻标签栏 + 跨会话引用（清单是后端账本投影，轮后刷新；null = 投影未到，不猜）
const sessions = ref<PilotSessionListItem[] | null>(null)
// 历史清单读取失败标记：失败保留旧清单（可能陈旧但不静默清空），面板给显式提示
const sessionsLoadFailed = ref(false)
/** 本会话分叉自哪个会话（账本事实；非派生会话为 null） */
const forkedFrom = ref<string | null>(null)
const referenceSession = ref<{ sid: string; title: string } | null>(null)
// 标签栏打开集：× 只关标签不删会话；账本里的会话随时可从历史面板找回
const openSids = ref<string[]>([])
const historyOpen = ref(false)
const historyRoot = ref<HTMLElement | null>(null)
// 会话列检索词（输入与防抖在 HlPilotSidebar，账本重投影留此）
const sessionQuery = ref('')


const REASON_KEYS: Record<string, MessageKey> = {
  llm_off: 'pilot.reason.llm_off',
  cap_reached: 'pilot.reason.cap_reached',
  llm_failed: 'pilot.reason.llm_failed',
  llm_failed_empty: 'pilot.reason.llm_failed_empty',
  no_data: 'pilot.reason.no_data',
  need_target: 'pilot.reason.need_target',
  session_busy: 'pilot.reason.busy',
  max_steps: 'pilot.reason.max_steps',
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

/** 未发送的提问草稿按会话分键落盘，刷新/重开表面后原地恢复 */
const DRAFT_KEY = 'holdexar-pilot-draft'

function readDraft(sid: string): string {
  if (!sid) return ''
  try {
    return localStorage.getItem(`${DRAFT_KEY}:${sid}`) ?? ''
  } catch {
    return ''
  }
}

function writeDraft(sid: string, text: string) {
  if (!sid) return
  try {
    if (text) localStorage.setItem(`${DRAFT_KEY}:${sid}`, text)
    else localStorage.removeItem(`${DRAFT_KEY}:${sid}`)
  } catch {
    /* 存储不可用时静默：草稿仅当前生命周期有效 */
  }
}

function dropDraft(sid: string) {
  if (!sid) return
  try {
    localStorage.removeItem(`${DRAFT_KEY}:${sid}`)
  } catch {
    /* 同上 */
  }
}

const OPEN_SIDS_KEY = 'holdexar-pilot-open-sids'

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
  // 草稿回填：无条件按当前会话取盘上值——编辑器此刻必为空（刚挂载），
  // 回填后开闸，之后每次输入才允许反写
  question.value = readDraft(sessionId.value)
  draftReady = true
  loadQueue()
})

/** 领航员在用模型与可用态（设置页保存后重开表面即刷新）。 */
function onDocClickCloseMenu(e: MouseEvent) {
  if (historyRoot.value && !historyRoot.value.contains(e.target as Node)) historyOpen.value = false
}

/* 等待话术：每轮轮换一句（有活力、说人话，不重复出现感） */
const LOADING_KEYS = ['pilot.loading.scan', 'pilot.loading.compare', 'pilot.loading.brief', 'pilot.loading.low'] as const
const loadingSeq = ref(0)
const loadingText = computed(() => t(LOADING_KEYS[loadingSeq.value % LOADING_KEYS.length] as MessageKey))

/** 重开表面 / 重启应用后续接上次对话（后端会话账本还原历史轮）。 */
async function restoreSession(sid: string) {
  try {
    const data = await pilotApi.getSession(sid)
    // 会话统计投影（底栏与消耗明细种子）：空会话为 null，不在旧会话上残留
    sessionStats.value = data.stats ?? null
    forkedFrom.value = data.forked_from ?? null
    const inflightQ = String(data.inflight_q ?? '')
    // 空会话（编辑重问首条后常见）：清空旧渲染——服务端投影才是事实源，
    // 早退不清会让已作废的轮残留界面
    if (!data.turns.length && !inflightQ) {
      turns.value = []
      lastCtx.value = null
      livePhases.value = []
      liveCards.value = []
      proposalStates.value = new Map()
      return
    }
    proposalStates.value = new Map(
      (data.proposals ?? []).map((p) => [p.pid, { ...p, failedCount: p.failedCount ?? 0 }]),
    )
    const items: Turn[] = []
    data.turns.forEach((t, idx) => {
      const turn = buildTurn(t.q, { ...t.resp, cached: false }, [])
      syncProposalCards(turn.cards ?? [])
      items.push({ ...turn, tid: t.tid ?? undefined, feedback: t.feedback ?? null,
        turnIndex: idx, expanded: false })
      for (const m of data.markers ?? []) {
        if (m.after_turn === idx + 1) {
          items.push({ q: '', kind: 'compact', steps: [], compactTrigger: m.trigger || 'auto' })
        }
      }
    })
    // 在飞轮补一屏：刷新/重开表面后，进行中那一轮不能凭空消失
    // （该轮终态落账本、或在飞登记撤下后，下次 restore 自然不再出现）
    if (inflightQ) {
      items.push({ q: inflightQ, kind: 'pending', steps: [], turnIndex: items.length })
      markRunningLocal(sid, true)
    }
    for (let i = items.length - 1; i >= 0; i--) {
      if (items[i].kind === 'answer' || items[i].text) {
        items[i].expanded = true
        break
      }
    }
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
  } catch (e) {
    // 会话不存在（过期清理）＝预期归宿，按空白新会话继续；
    // 其他读取失败不能冒充空会话——显式提示，账本原样保留
    if (e instanceof ApiError && e.status === 404) return
    message.error(t('pilot.history.loadFailed'))
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
  draftReady = false
  sessionId.value = newSessionId()
  saveSessionId(sessionId.value)
  question.value = ''
  composerRef.value?.clearQuotes()
  draftReady = true
  loadQueue()
  referenceSession.value = null
  forkedFrom.value = null
  void loadSessions()
}

/* ── 会话标签栏与跨会话引用（账本只读投影；切换/改名/删除/引用同源） ── */

async function loadSessions() {
  try {
    const data = await pilotApi.listSessions(50, sessionQuery.value.trim())
    sessions.value = data.items
    sessionsLoadFailed.value = false
  } catch {
    // 读取失败保留既有清单（可能陈旧但不静默清空），面板给显式提示
    sessionsLoadFailed.value = true
  }
}

/** 会话列分区/检索防抖/行内动作归 HlPilotSidebar；账本清单（sessions）与切换语义留此 */
const { renamingSid, renameText, deleteArmSid, flagBusy, startRename, submitRename, armDelete } =
  useSessionRowActions({ sessions: () => sessions.value, refresh: loadSessions, deleted: onSessionDeleted })

/** 置顶/归档/删除后交账本重投影；删除当前会话则回新对话 */
function onSessionDeleted(sid: string) {
  sessions.value = (sessions.value ?? []).filter((x) => x.sid !== sid)
  openSids.value = openSids.value.filter((s) => s !== sid)
  saveOpenSids()
  if (sid === sessionId.value) startNewChat()
  void loadSessions()
}

/** 分叉失败机器码 → 人话（复用编辑重问的失败码口径） */
const FORK_FAIL_KEYS: Record<string, MessageKey> = {
  no_session: 'pilot.edit.fail.gone',
  busy: 'pilot.edit.fail.busy',
  write_failed: 'pilot.edit.fail.write',
}

/** 话题分叉：从本会话第 idx 轮（含）派生新会话并切过去（源账本零改动）。 */
async function forkFromTurn(idx: number) {
  if (streaming.value || flagBusy.value) return
  const turn = turns.value[idx]
  if (!turn || typeof turn.turnIndex !== 'number') return
  flagBusy.value = 'fork'
  try {
    const out = await pilotApi.forkSession(sessionId.value, turn.turnIndex + 1)
    if (!out.ok || !out.sid) {
      message.error(t(FORK_FAIL_KEYS[out.reason ?? ''] ?? 'pilot.session.forkFailed'))
      return
    }
    openSessionSid(out.sid)
    await loadSessions()
    message.success(t('pilot.session.forked'))
    await switchSession(out.sid)
  } catch {
    message.error(t('pilot.session.forkFailed'))
  } finally {
    flagBusy.value = ''
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
  // 草稿按会话分账：切走前先关闸，防止旧会话文本被写进新会话的键
  draftReady = false
  sessionId.value = sid
  saveSessionId(sid)
  loadQueue()
  turns.value = []
  livePhases.value = []
  lastCtx.value = null
  sessionStats.value = null
  referenceSession.value = null
  forkedFrom.value = null
  question.value = readDraft(sid)
  composerRef.value?.clearQuotes()
  draftReady = true
  await restoreSession(sid)
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
  for (const t of turns.value) {
    t.expanded = false
  }
  turns.value.push({ q: label, kind: 'pending', steps: [] })
  void scrollBottom()
  try {
    const r = await pilotApi.runTool(tool, label, sessionId.value || undefined)
    turns.value.splice(
      turns.value.length - 1,
      1,
      { q: label, kind: 'answer', text: '', cards: r.cards, steps: [r.step], expanded: true } as Turn,
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
  // section = 页内板块键（gamelib/bills/family 页签），经 query.s 交给目标页定位
  void router.push(f.section ? { path: f.path, query: { s: f.section } } : f.path)
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
    // 流式期即时卡同步提议终态（live 区与终态轮同一账）
    syncProposalCards(liveRealCards.value)
  } catch {
    message.error(t('pilot.error'))
  } finally {
    busyPid.value = null
  }
}

/** 终答取带 terminal 标记的那一段正文；步数耗尽 / 用户中断的轮次回落最后一段有正文的。
 * 纯自述段（"我去查一下"）不是结论，服务端已标注 narration，两端口径一致地跳过 */
function answerOf(phases: PilotPhase[], fallback: string): string {
  if (!phases.length) return fallback
  const usable = phases.filter((p) => p.text && !p.narration)
  const terminal = usable.find((p) => p.terminal)
  if (terminal?.text) return terminal.text
  for (let i = usable.length - 1; i >= 0; i--) {
    if (usable[i].text) return usable[i].text
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
    tid: e.turn_id ?? undefined,
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
    attachments: e.attachments?.length ? e.attachments : undefined,
    quotes: e.quotes?.length ? e.quotes : undefined,
    followups: e.followups?.length ? e.followups : undefined,
    diagnostic: e.diagnostic ?? undefined,
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
  const reasonKey = isEmptyLlmFailure(e.reason, e.source) ? 'pilot.reason.llm_failed_empty' : (e.reason && REASON_KEYS[e.reason]) || 'pilot.error'
  return { q, kind: 'reason', reasonKey, ...base }
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

/* 滚动容器唯一：对话记录盒自滚（抽屉与整页同一实现——整页高度由宿主给定），
   输入区在盒外，位置恒定——思考展开/卡片落地都推不动它 */
function onContainerScroll(e: Event) {
  const el = e.target as HTMLElement
  hideSelQuote()
  followBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 48
  scheduleOutlineProbe()
}

function pinIfFollow() {
  const inner = scrollBox.value
  if (!inner || !followBottom.value) return
  if (inner.scrollHeight > inner.clientHeight + 1) inner.scrollTop = inner.scrollHeight
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
/* 徽标轮数：压缩分隔线不入目录，与 Outline 内部编号口径一致 */
const outlineCount = computed(() => turns.value.filter((t) => t.kind !== 'compact').length)
let outlineProbeTimer: ReturnType<typeof setTimeout> | null = null

/** 阅读线（滚动容器顶下 48px）之上最后一轮即当前轮；仅目录打开时计算 */
function probeOutlineActive() {
  const inner = scrollBox.value
  if (!inner || !outlineOpen.value) return
  const line = inner.getBoundingClientRect().top + 48
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
  const reduced = typeof matchMedia === 'function' && matchMedia('(prefers-reduced-motion: reduce)').matches
  const top = Math.max(0, row.getBoundingClientRect().top - inner.getBoundingClientRect().top + inner.scrollTop - 12)
  followBottom.value = false
  inner.scrollTo({ top, behavior: reduced ? 'auto' : 'smooth' })
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
  document.addEventListener('selectionchange', onSelectionChange)
  document.addEventListener('mouseup', revealSelQuote)
  const el = scrollBox.value
  if (!el) return
  growthObserver = new MutationObserver(() => schedulePinIfFollow())
  growthObserver.observe(el, { childList: true, subtree: true })
  el.addEventListener('load', pinIfFollow, true)
  window.addEventListener('resize', pinIfFollow)
  window.addEventListener('resize', hideSelQuote)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onDocClickCloseMenu)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  document.removeEventListener('selectionchange', onSelectionChange)
  document.removeEventListener('mouseup', revealSelQuote)
  if (deltaTimer !== null) clearTimeout(deltaTimer)
  growthObserver?.disconnect()
  window.removeEventListener('resize', hideSelQuote)
  if (outlineProbeTimer !== null) clearTimeout(outlineProbeTimer)
  scrollBox.value?.removeEventListener('load', pinIfFollow, true)
  window.removeEventListener('resize', pinIfFollow)
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

/* ── 对话取用：整条复制 + 拖选引用（答复出来之后的两个常规去处）── */

/** 写剪贴板：异步接口优先，桌面壳未授权时回落到临时编辑区 */
async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text)
      return true
    }
  } catch {
    /* 未授权 / 窗口无焦点 → 回落 */
  }
  try {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch {
    return false
  }
}

async function onCopyText(text: string) {
  const body = text.trim()
  if (!body) return
  if (await writeClipboard(body)) message.success(t('pilot.copy.done'))
  else message.error(t('pilot.copy.failed'))
}

async function rateTurn(turn: Turn, feedback: 'accepted' | 'rejected') {
  const sid = sessionId.value
  const tid = turn.tid
  if (!sid || !tid || turn.feedback === feedback || feedbackBusyKey.value) return
  const key = `${sid}:${tid}`
  feedbackBusyKey.value = key
  try {
    const result = await pilotApi.rateTurn(sid, tid, feedback)
    if (!result.ok) {
      message.error(t('pilot.feedback.failed'))
      return
    }
    turn.feedback = feedback
    message.success(t('pilot.feedback.saved'))
  } catch {
    message.error(t('pilot.feedback.failed'))
  } finally {
    if (feedbackBusyKey.value === key) feedbackBusyKey.value = null
  }
}

/** 答复头的复制钮：取当面那一轮的可见正文（只有数据卡、没有正文的轮不挂钮） */
/** 会话记录点击委托：处理内嵌 SVG 图卡的预览/源码切换、复制操作与代码块复制 */
function onTurnsClick(e: MouseEvent) {
  const codeCopy = (e.target as HTMLElement).closest<HTMLElement>('.pilot-code-copy')
  if (codeCopy) {
    const text = codeCopy.closest<HTMLElement>('.pilot-code')?.querySelector('code')?.textContent ?? ''
    if (text) void onCopyText(text)
    return
  }
  const target = (e.target as HTMLElement).closest<HTMLElement>('.pilot-svg-tab, .pilot-svg-copy, .pilot-svg-quote')
  if (!target) return
  const card = target.closest<HTMLElement>('.pilot-svg')
  if (!card) return
  const action = target.dataset.action
  if (action === 'preview') {
    card.classList.remove('show-code')
    const sourceEl = card.querySelector<HTMLElement>('.pilot-svg-source')
    const stageEl = card.querySelector<HTMLElement>('.pilot-svg-stage')
    if (sourceEl) sourceEl.style.display = 'none'
    if (stageEl) stageEl.style.display = ''
    card.querySelectorAll<HTMLButtonElement>('.pilot-svg-tab').forEach((t) => t.classList.remove('is-active'))
    target.classList.add('is-active')
    return
  }
  if (action === 'code') {
    card.classList.add('show-code')
    const sourceEl = card.querySelector<HTMLElement>('.pilot-svg-source')
    const stageEl = card.querySelector<HTMLElement>('.pilot-svg-stage')
    if (sourceEl) sourceEl.style.display = 'block'
    if (stageEl) stageEl.style.display = 'none'
    card.querySelectorAll<HTMLButtonElement>('.pilot-svg-tab').forEach((t) => t.classList.remove('is-active'))
    target.classList.add('is-active')
    return
  }
  if (action === 'copy') {
    const codeEl = card.querySelector<HTMLElement>('.pilot-svg-source code')
    const text = codeEl?.textContent ?? ''
    if (text) void onCopyText(text)
    return
  }
  if (action === 'quote') {
    // 图卡引用取源码：图本身贴不进提问，代码才是可被追问与改写的对象
    const codeEl = card.querySelector<HTMLElement>('.pilot-svg-source code')
    const text = (codeEl?.textContent ?? '').trim()
    if (text) quoteToComposer(text)
    return
  }
}

/** 拖选片段：选区落在某一轮答复里才算数，输入框与标签栏的选字不算 */
interface SelPiece { text: string; top: number; left: number }

function readReplySelection(): SelPiece | null {
  const sel = window.getSelection()
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null
  const text = sel.toString().trim()
  if (!text) return null
  const range = sel.getRangeAt(0)
  const node = range.commonAncestorContainer
  const host = node.nodeType === Node.ELEMENT_NODE ? (node as Element) : node.parentElement
  if (!host?.closest('.pilot-reply')) return null
  const box = range.getBoundingClientRect()
  return { text, top: box.top, left: box.left + box.width / 2 }
}

const selQuote = ref<SelPiece | null>(null)
// 拖选过程不出条（每动一下 selectionchange 都来），松手才抬出来
let pendingSel: SelPiece | null = null

function onSelectionChange() {
  pendingSel = readReplySelection()
  if (!pendingSel) selQuote.value = null
}

function revealSelQuote() {
  selQuote.value = pendingSel ?? readReplySelection()
}

function hideSelQuote() {
  pendingSel = null
  selQuote.value = null
}

/** 锚在选区正上方居中，左右留出自身半宽不被视口切掉 */
const selBarStyle = computed(() => {
  const s = selQuote.value
  if (!s) return {}
  const half = 76
  const limit = Math.max(half, window.innerWidth - half)
  return { top: `${Math.max(s.top - 44, 8)}px`, left: `${Math.min(Math.max(s.left, half), limit)}px` }
})

const composerRef = ref<InstanceType<typeof HlPilotComposer> | null>(null)

/** 引用回填：消息面引用钮 / 拖选浮条 → 组件插入光标处；浮选与选区清理留在消息面 */
function quoteToComposer(text: string) {
  composerRef.value?.insertQuoteAtCursor(text)
  hideSelQuote()
  window.getSelection()?.removeAllRanges()
}

let abortCtrl: AbortController | null = null

/** 展开中的附件（同一时刻只展开一枚）；chunk 为空 = 正在取块 */
const openAtt = ref<{ ref: PilotAttachmentRef; chunk: PilotAttachmentChunk | null; busy: boolean } | null>(null)

/** 附件上传在途（Console 持有会话权威并驱动 API；清单展示在 HlPilotComposer） */
const attachBusy = ref(false)

async function attachText(text: string, name?: string) {
  if (!sessionId.value) {
    sessionId.value = newSessionId()
    saveSessionId(sessionId.value)
  }
  attachBusy.value = true
  try {
    const ref_ = await pilotApi.createAttachment(sessionId.value, text, name)
    composerRef.value?.addAttachment(ref_)
    message.success(t('pilot.attach.added', { name: ref_.name, n: ref_.lines }))
  } catch {
    message.error(t('pilot.attach.failed'))
  } finally {
    attachBusy.value = false
  }
}

/** 展开/收起附件内容：块按需取（truncated 时可按提示续读下一块） */
async function toggleAttachment(ref_: PilotAttachmentRef) {
  if (openAtt.value?.ref.id === ref_.id) {
    openAtt.value = null
    return
  }
  await loadAttachment(ref_, 1, false)
}

async function loadAttachment(ref_: PilotAttachmentRef, startLine: number, append: boolean) {
  if (!sessionId.value) return
  const keep = append ? (openAtt.value?.chunk?.content ?? '') : ''
  openAtt.value = { ref: ref_, chunk: append ? (openAtt.value?.chunk ?? null) : null, busy: true }
  try {
    const chunk = await pilotApi.readAttachment(sessionId.value, ref_.id, startLine)
    openAtt.value = {
      ref: ref_,
      chunk: keep ? { ...chunk, content: `${keep}\n${chunk.content}` } : chunk,
      busy: false,
    }
  } catch {
    openAtt.value = null
    message.error(t('pilot.attach.readFailed'))
  }
}

/* ── 消息队列：生成中提交的问题排队等本轮结束自动发出 ──
   队列归属当前会话（可增删改序），落 localStorage 防刷新丢失；
   用户主动停止生成时队列暂停（内容不丢），确认后再继续。 */
interface QueuedAsk { id: number; text: string }

const queue = ref<QueuedAsk[]>([])
const queuePaused = ref(false)
let queueSeq = 0

const queueKey = () => `pilot.queue.${sessionId.value || 'local'}`

function saveQueue() {
  try {
    localStorage.setItem(queueKey(), JSON.stringify(queue.value))
  } catch {
    /* 存储不可用（隐私模式）时队列只在内存里，不影响发送 */
  }
}

function loadQueue() {
  queuePaused.value = false
  try {
    const raw = localStorage.getItem(queueKey())
    const items = raw ? (JSON.parse(raw) as QueuedAsk[]) : []
    queue.value = Array.isArray(items)
      ? items.filter((i) => i && typeof i.text === 'string' && i.text.trim())
      : []
    queueSeq = queue.value.reduce((m, i) => Math.max(m, Number(i.id) || 0), 0)
  } catch {
    queue.value = []
  }
}

/** 生成中提交：入队而不是丢弃输入 */
function pushQueue(text: string) {
  const body = text.trim()
  if (!body) return
  queue.value = [...queue.value, { id: ++queueSeq, text: body }]
  saveQueue()
  message.info(t('pilot.queue.enqueued', { n: queue.value.length }))
}

function removeQueued(id: number) {
  queue.value = queue.value.filter((i) => i.id !== id)
  if (!queue.value.length) queuePaused.value = false
  saveQueue()
}

function moveQueued(id: number, delta: number) {
  const i = queue.value.findIndex((x) => x.id === id)
  const j = i + delta
  if (i < 0 || j < 0 || j >= queue.value.length) return
  const next = [...queue.value]
  ;[next[i], next[j]] = [next[j], next[i]]
  queue.value = next
  saveQueue()
}

/** 取回输入框改一改再发：出队并把文本交还 composer */
function takeQueued(id: number) {
  const item = queue.value.find((i) => i.id === id)
  if (!item) return
  question.value = item.text
  writeDraft(sessionId.value, item.text)
  removeQueued(id)
  void nextTick(() => (composer.value?.$el as HTMLTextAreaElement | undefined)?.focus())
}

/** 本轮结束后自动发出队首；暂停中或仍在生成则不动 */
function flushQueue() {
  if (streaming.value || queuePaused.value || !queue.value.length) return
  const head = queue.value[0]
  queue.value = queue.value.slice(1)
  if (!queue.value.length) queuePaused.value = false
  saveQueue()
  void ask(head.text)
}

function resumeQueue() {
  queuePaused.value = false
  flushQueue()
}

/** 停止生成本轮：中断 SSE，已流出的内容保留在对话里。 */
function stopPilot() {
  abortCtrl?.abort()
  // 主动停止 = 队列暂停：后续问题不再自动发出，等用户确认继续
  if (queue.value.length) queuePaused.value = true
}

/* ── 中断续跑：把上一轮已完成的事实回灌，接着把这一轮做完 ── */

/** 步骤结果摘要：数据卡类工具取结构化结果，纯叙述步骤跳过 */
function stepSummary(step: PilotStep): string {
  const data = step.data ?? {}
  if (!Object.keys(data).length) return ''
  try {
    return JSON.stringify(data).slice(0, 400)
  } catch {
    return ''
  }
}

function continueStopped(index: number) {
  if (streaming.value) return
  const turn = turns.value[index]
  if (!turn) return
  const resume: PilotResumePayload = {
    text: turn.text ?? '',
    tools: (turn.steps ?? [])
      .filter((s) => s.status === 'ok')
      .map((s) => ({ label: s.label, status: s.status, summary: stepSummary(s) }))
      .filter((s) => s.summary),
  }
  const q = turn.q
  const resumeQuotes = turn.quotes ? [...turn.quotes] : []
  // 中断轮未落账（账本只在终态写），本地移出后按同一问题重跑——视觉上是接着长出来
  turns.value.splice(index, 1)
  void ask(q, resume, resumeQuotes.length ? resumeQuotes : undefined)
}

/* ── 改一句重问：作废该轮及其后各轮，用新提问重跑 ── */

/** 新轮的账本序号：已落账轮的最大序号 + 1（中断轮不占号，它不入账本） */
function nextTurnIndex(): number {
  let max = -1
  for (const t of turns.value) {
    if (typeof t.turnIndex === 'number' && t.turnIndex > max) max = t.turnIndex
  }
  return max + 1
}

const editingIndex = ref<number | null>(null)
const editBusy = ref(false)

function startEdit(index: number) {
  const turn = turns.value[index]
  if (streaming.value || !turn || editingIndex.value !== null) return
  if (!(typeof turn.turnIndex === 'number' || turn.kind === 'reason' || Boolean(turn.stopped))) return
  editingIndex.value = index
}

/** 该轮可否分叉：只有已落账本（有轮号）且不在生成中的轮才有确定的分叉点 */
const EDIT_FAIL_KEYS: Record<string, MessageKey> = {
  no_session: 'pilot.edit.fail.gone',
  busy: 'pilot.edit.fail.busy',
  write_failed: 'pilot.edit.fail.write',
}

async function submitEdit(i: number, text: string) {
  const turn = turns.value[i]
  if (!turn || !(typeof turn.turnIndex === 'number' || turn.kind === 'reason' || Boolean(turn.stopped)) || !text || editBusy.value) return
  editBusy.value = true
  try {
    if (typeof turn.turnIndex === 'number') {
      const out = await pilotApi.rewindSession(sessionId.value, turn.turnIndex)
      if (!out.ok) {
        message.error(t(EDIT_FAIL_KEYS[out.reason ?? ''] ?? 'pilot.edit.fail.generic'))
        return
      }
      editingIndex.value = null
      // 账本同源：截断后按后端投影重建对话流，再提交新提问
      if (sessionId.value) await restoreSession(sessionId.value)
      void ask(text)
    } else {
      // 未落账轮次（失败轮 / 中断轮）：无需服务端软截断，直接丢弃该轮及其后未入账轮次并重新提问
      editingIndex.value = null
      turns.value.splice(i)
      void ask(text)
    }
  } catch {
    message.error(t('pilot.edit.fail.generic'))
  } finally {
    editBusy.value = false
  }
}

async function retryTurn(index: number) {
  if (streaming.value) return
  const turn = turns.value[index]
  if (!turn || !turn.q) return
  const retryQ = turn.q
  const retryAtts = turn.attachments ? [...turn.attachments] : []
  const retryQuotes = turn.quotes ? [...turn.quotes] : []
  if (typeof turn.turnIndex === 'number') {
    const out = await pilotApi.rewindSession(sessionId.value, turn.turnIndex)
    if (!out.ok) {
      message.error(t(EDIT_FAIL_KEYS[out.reason ?? ''] ?? 'pilot.edit.fail.generic'))
      return
    }
    if (sessionId.value) await restoreSession(sessionId.value)
  } else {
    // 未落账失败轮：直接从对话记录中移除该轮，原位重发
    turns.value.splice(index)
  }
  if (retryAtts.length) composerRef.value?.restoreAttachments(retryAtts)
  void ask(retryQ, undefined, retryQuotes.length ? retryQuotes : undefined)
}

/** 分叉溯源文案：源会话标题按账本投影解析（找不到回落「新对话」） */
const forkedFromLabel = computed(() => {
  const sid = forkedFrom.value
  if (!sid) return ''
  const src = (sessions.value ?? []).find((s) => s.sid === sid)
  return t('pilot.session.forkedFrom', { title: src?.title || t('pilot.session.untitled') })
})

/** 派生标记：点分隔线跳回源会话（源已删则不动，标签栏也不会挂上不存在的会话） */
function openForkSource() {
  const sid = forkedFrom.value
  if (!sid) return
  if (!(sessions.value ?? []).some((s) => s.sid === sid)) {
    message.error(t('pilot.session.forkedFromGone'))
    return
  }
  openFromHistory(sid)
}

/** 清单/标签栏标题：派生会话标出源头，父子在列表里可分辨（账本 forked_from 为准） */
function sessionLabel(item: { title?: string | null; forked_from?: string | null }): string {
  const base = item.title || t('pilot.session.untitled')
  return item.forked_from ? t('pilot.session.forkedTitle', { title: base }) : base
}

async function ask(text?: string, resume?: PilotResumePayload, quotes?: { n: number; text: string }[]) {
  const typedQ = (text ?? question.value).trim()
  // 待发载荷（引用/附件/元素引用）自组件取用；重试/续跑由调用方把该轮的引用原样带回来
  const payload = composerRef.value?.getComposerPayload()
    ?? { quotes: [] as { n: number; text: string }[], attachments: [], elements: [] }
  const sentAtts = payload.attachments
  const sentQuotes = quotes ?? payload.quotes
  // 允许「只挂附件、不写问题」的提交（附件本体已是内容）；提问留空时补一句，
  // 与后端回落用同一句，气泡与账本才不会一边空一边有字
  if (!typedQ && !sentAtts.length) return
  const q = typedQ || t('pilot.attach.only')
  if (streaming.value) {
    // 生成中提交 = 排队，不丢弃输入（队列表只存文本，附件不随队：
    // 只挂附件没写字就不入队，否则会排一句空指向的占位话）
    if (!text) {
      question.value = ''
      dropDraft(sessionId.value)
    }
    if (typedQ) pushQueue(typedQ)
    return
  }
  if (!text) {
    question.value = ''
    dropDraft(sessionId.value)
  }
  // 元素引用随本轮发送后即清空（一次性，与附件同语义）
  const sentElRefs = payload.elements.length ? [...payload.elements] : null
  composerRef.value?.clearPending()
  streaming.value = true
  abortCtrl = new AbortController()
  livePhases.value = []
  liveCards.value = []
  clearQueuedHint()
  loadingSeq.value += 1
  // 用户发言立即上屏（pending 占位轮，回复完成后原位替换）；跟随复位——刚发的消息必须看着它长出来
  followBottom.value = true
  // 发了新对话：收敛历史所有轮次的输出结果（长回答收起，让出视口给新问答）
  for (const t of turns.value) {
    t.expanded = false
  }
  // 本轮预占的账本序号；只有确实落账本的终态才保留（见收尾处的 recorded 判定）
  const pendingIndex = nextTurnIndex()
  turns.value.push({
    q,
    kind: 'pending',
    steps: [],
    turnIndex: pendingIndex,
    attachments: sentAtts.length ? sentAtts : undefined,
    quotes: sentQuotes.length ? sentQuotes : undefined,
  })
  // 本地即时点亮「工作中」（后端登记为准，done 后 loadSessions 归位）
  markRunningLocal(sessionId.value, true)
  void scrollBottom()
  let done: PilotAskResponse | null = null
  let failed = false
  let failedMsg = ''
  let stopped = false
  // 本轮已即时跳转：tool 事件到达即切页（不等后续模型叙述），done 后不再重复跳
  let navApplied = false
  try {
    await askPilotStream(q, props.game?.appid, sessionId.value || undefined, (e) => {
      if (e.type === 'ack') {
        // 受理回执：另一会话在处理 → 并行提示；否则 8s 无增量再提示等待模型
        if ((e.active ?? 1) > 1) queuedHint.value = 'parallel'
        else armQueuedHint('waiting')
      } else if (e.type === 'busy') {
        done = { answer: '', thinking: null, source: 'none', reason: 'session_busy', facts: null, cards: [], cached: false }
        clearQueuedHint()
      } else if (e.type === 'step_start') {
        // 阶段边界：先冲刷上一段残留增量，再开新段；本段模型静默重新计时
        armQueuedHint('waiting')
        flushDeltas()
        dropUnresolvedCards()
        livePhases.value.push({ step: e.step ?? livePhases.value.length + 1, thinking: '', text: '', steps: [] })
      } else if (e.type === 'thinking' && e.delta) {
        clearQueuedHint()
        queueDelta('thinking', e.delta)
      } else if (e.type === 'answer' && e.delta) {
        clearQueuedHint()
        queueDelta('answer', e.delta)
      } else if (e.type === 'tool_start') {
        // 执行期由 tool_progress 续命；两者都断炊时按 8s 提示"仍在执行"
        armQueuedHint('toolSlow')
        flushDeltas()
        // 导航直通等路径不发阶段定界，按首段兜底
        if (!livePhases.value.length) {
          livePhases.value.push({ step: 1, thinking: '', text: '', steps: [] })
        }
        currentPhase()?.steps.push({ label: e.label ?? '', status: 'running', data: {}, startedAt: Date.now() })
        // 卡片类型预告：执行期先挂骨架占位（结果到达前高度先就位），归属当前阶段
        if (e.card_kind) pushLiveSkeleton(e.name ?? '', e.card_kind, livePhases.value.length - 1)
      } else if (e.type === 'tool_progress') {
        // 执行期心跳：已用时长以服务端读数为准（前端本地表只作帧间插值）
        const step = currentPhase()?.steps.find((s) => s.status === 'running')
        if (step) step.elapsedMs = e.elapsed_ms ?? step.elapsedMs
        armQueuedHint('toolSlow')
      } else if (e.type === 'tool') {
        // 回填本阶段最后一个运行态步骤；无运行态（如重放）直接追加
        const phase = currentPhase()
        const pending = phase ? [...phase.steps].reverse().find((s) => s.status === 'running') : undefined
        if (pending) {
          pending.label = e.label ?? pending.label
          pending.status = e.status ?? 'ok'
          pending.data = e.data ?? {}
          pending.elapsedMs = e.duration_ms ?? pending.elapsedMs
          pending.startedAt = undefined
        } else if (phase) {
          phase.steps.push({ label: e.label ?? '', status: e.status ?? 'ok', data: e.data ?? {}, elapsedMs: e.duration_ms })
        }
        clearQueuedHint()
        if (e.name === 'navigate' && e.status === 'ok' && e.data?.path && !navApplied) {
          navApplied = true
          void router.push(e.data.section ? { path: e.data.path, query: { s: e.data.section } } : e.data.path)
          setTimeout(focusMainRegion, 700)
        }
      } else if (e.type === 'card') {
        // 结算帧：每个完成工具一帧，card=null = 本工具无卡（撤骨架）；
        // 无预告骨架的兜底卡归属当前阶段
        if (e.card) fillLiveCard(e.name ?? '', e.card, Math.max(0, livePhases.value.length - 1))
        else dropLiveSkeleton(e.name ?? '')
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
        done = { answer: '', thinking: null, source: 'none', reason: e.reason ?? 'llm_failed', diagnostic: e.diagnostic, facts: null, cards: [], cached: false }
      }
    }, referenceSession.value?.sid, abortCtrl.signal, resume, sentAtts.map((a) => a.id), viewContext.value, sentElRefs || undefined,
       sentQuotes.length ? sentQuotes : undefined)
  } catch (err) {
    if (abortCtrl.signal.aborted) {
      stopped = true
    } else {
      failed = true
      failedMsg = String((err as Error)?.message ?? '')
      // 附件已落盘仍有效：放回待发区，用户可一键重试（不重传内容）
      if (sentAtts.length) composerRef.value?.restoreAttachments(sentAtts)
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
  // 提问展示以后端终态为准：超长正文被转成附件时，账本与气泡同为收窄形态
  const shownQ = done?.q || q
  // 附件芯片：后端终态是权威口径（回传本轮引用），失败/中断轮回落本地已发清单
  const turnAtts = done?.attachments?.length ? done.attachments : sentAtts
  // 编号引用同理：后端终态是权威口径，失败/中断轮回落本地已发清单
  const turnQuotes = done?.quotes?.length ? done.quotes : sentQuotes
  if (failed) {
    turn = {
      q: shownQ,
      kind: 'reason',
      reasonKey: failedMsg.includes('HTTP 422') ? 'pilot.reason.too_long' : 'pilot.error',
      steps: snapshotSteps,
      attachments: turnAtts.length ? turnAtts : undefined,
      quotes: turnQuotes.length ? turnQuotes : undefined,
    } as Turn
  } else if (stopped) {
    // 中断保留已流出的阶段与终答候选，历史轮仍可展开过程；已即时下发的卡片一并保留
    // 后端不为中断补发工具终态帧：快照残留 running 的步骤就地收口为 stopped
    for (const s of snapshotSteps) {
      if (s.status === 'running') s.status = 'stopped'
    }
    turn = {
      q: shownQ,
      kind: 'answer',
      text: answerOf(phaseSnapshot, ''),
      phases: phaseSnapshot.length ? phaseSnapshot : undefined,
      steps: snapshotSteps,
      cards: liveRealCards.value.length ? [...liveRealCards.value] : undefined,
      stopped: true,
      attachments: turnAtts.length ? turnAtts : undefined,
      quotes: turnQuotes.length ? turnQuotes : undefined,
      expanded: true,
    } as Turn
  } else {
    turn = buildTurn(shownQ, done ?? { answer: '', thinking: null, source: 'none', reason: 'llm_failed', facts: null, cards: [], cached: false }, phaseSnapshot)
    turn.expanded = true
    if (!done?.attachments?.length && turnAtts.length) turn.attachments = turnAtts
    if (!done?.quotes?.length && turnQuotes.length) turn.quotes = turnQuotes
  }
  // 账本序号只在「本轮确实落了账本」时保留（buildTurn 不带序号，需在此补）：
  // 中断 / 失败 / 未受理的轮不占号——它们没进账本，占号会让下一轮序号与账本错位，
  // 改一句重问按序号截断时就会截错位置。
  const recorded = !failed && !stopped && done?.source !== 'none'
  if (recorded) turn.turnIndex = pendingIndex
  turns.value.splice(turns.value.length - 1, 1, turn)
  // 导航卡生效：取本轮最后一张，跳模块并打聚焦框（tool 事件已即时跳过则不再重复）
  const navCards = (turn.cards ?? []).filter((c): c is PilotNavigateFacts => c.kind === 'navigate' && Boolean(c.path))
  const lastNav = navCards[navCards.length - 1]
  if (lastNav && !navApplied) applyNavigate(lastNav)
  streaming.value = false
  markRunningLocal(sessionId.value, false)
  livePhases.value = []
  clearQueuedHint()
  void scrollBottom()
  // 本轮结束：队列不再被占用，自动接着发下一条（暂停中则等用户确认）
  flushQueue()
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

/** 后端下发的追问条目 → dock 条目；无对应词条的 key 直接丢弃（不渲染裸 key），最多 4 条 */
function mapServerFollowUps(list: PilotFollowup[] | undefined): FollowUpItem[] {
  const out: FollowUpItem[] = []
  for (const f of list ?? []) {
    if (!f?.key || out.length >= 4) continue
    const key = `pilot.followup.${f.key}` as MessageKey
    const promptKey = `pilot.followup.${f.key}Prompt` as MessageKey
    const label = t(key, f.args)
    const prompt = t(promptKey, f.args)
    // 缺词条时 t 回退成 key 原文
    if (label === key || prompt === promptKey) continue
    out.push({ key, prompt })
  }
  return out
}

/** 追问 dock 内容：空会话给开场推荐，有轮优先用后端给的本轮追问；流式中不出 */
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
  const turn = turns.value[i]
  const served = mapServerFollowUps(turn.followups)
  return served.length ? served : getTurnFollowUps(turn)
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
    <!-- 整页会话列：账本全量清单常驻左侧（Teleport 到页面级挂点，Index 提供 #pilot-side-target） -->
    <HlPilotSidebar
      v-if="page"
      v-model="sessionQuery"
      :sessions="sessions"
      :session-id="sessionId"
      :streaming="streaming"
      :fresh-chat="onFreshChat"
      :label-of="sessionLabel"
      :turns="turns"
      :outline-count="outlineCount"
      :outline-active="outlineActive"
      @new="startNewChat"
      @open="openFromHistory"
      @search="loadSessions"
      @refresh="loadSessions"
      @deleted="onSessionDeleted"
      @jump="jumpToTurn"
      @outline-toggle="onOutlineToggle"
    />

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
          :title="`${sessionLabel(it)} · ${t('pilot.history.turns', { n: it.turn_total })} · ${t('pilot.history.dblRename')}`"
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
            <span class="pilot-tab__name">
              <span
                v-if="it.forked_from"
                class="pilot-forkmark"
                :title="t('pilot.session.forked')"
                aria-hidden="true"
              >
                <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                  <circle cx="6.5" cy="6" r="2.4" />
                  <circle cx="6.5" cy="18" r="2.4" />
                  <circle cx="17.5" cy="9.5" r="2.4" />
                  <path d="M6.5 8.4v7.2M8.9 6h4.1a4.5 4.5 0 0 1 4.5 1.4" />
                </svg>
              </span>
              {{ it.title || t('pilot.session.untitled') }}
            </span>
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
            <div v-if="sessionsLoadFailed" class="pilot-history__empty" role="status">{{ t('pilot.history.loadFailed') }}</div>
            <div v-else-if="!(sessions ?? []).length" class="pilot-history__empty">{{ t('pilot.history.empty') }}</div>
            <div v-for="it in sessions ?? []" :key="it.sid" class="pilot-history__row">
              <button type="button" class="pilot-history__item" @click="openFromHistory(it.sid)">
                <span class="pilot-history__name">
                  <span
                    v-if="it.running"
                    class="pilot-side__running"
                    :title="t('pilot.session.running')"
                    aria-hidden="true"
                  ></span>
                  <span
                    v-if="it.forked_from"
                    class="pilot-forkmark"
                    :title="t('pilot.session.forked')"
                    aria-hidden="true"
                  >
                    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                      <circle cx="6.5" cy="6" r="2.4" />
                      <circle cx="6.5" cy="18" r="2.4" />
                      <circle cx="17.5" cy="9.5" r="2.4" />
                      <path d="M6.5 8.4v7.2M8.9 6h4.1a4.5 4.5 0 0 1 4.5 1.4" />
                    </svg>
                  </span>
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
      <HlPilotRuns variant="tab" />
      <!-- page 模式宽屏时会话列里已有 outline；窄屏时会话列隐藏、tabbar 恢复，
           此处 outline 跟随 tabbar 补位——去掉 v-if=!page 让两种屏宽都有入口 -->
      <HlPilotOutline
        :turns="turns"
        :count="outlineCount"
        :active-index="outlineActive"
        @jump="jumpToTurn"
        @toggle="onOutlineToggle"
      />
    </div>

    <div ref="scrollBox" class="pilot-turns" @scroll.passive="onContainerScroll" @click="onTurnsClick">
      <!-- 分叉溯源：派生标记是对话流首行的一条分隔线（与压缩分隔线同一套线+胶囊语言），
           点它跳回源会话——账本事实可追溯，不是装饰徽章 -->
      <button
        v-if="forkedFromLabel"
        type="button"
        class="pilot-forkline"
        :title="t('pilot.session.forkedFromOpen')"
        @click="openForkSource"
      >
        <span class="pilot-forkline__line" aria-hidden="true"></span>
        <span class="pilot-forkline__pill">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <circle cx="6.5" cy="6" r="2.4" />
            <circle cx="6.5" cy="18" r="2.4" />
            <circle cx="17.5" cy="9.5" r="2.4" />
            <path d="M6.5 8.4v7.2M8.9 6h4.1a4.5 4.5 0 0 1 4.5 1.4" />
          </svg>
          {{ forkedFromLabel }}
        </span>
        <span class="pilot-forkline__line" aria-hidden="true"></span>
      </button>
      <div v-for="(turn, i) in turns" :key="i" class="pilot-turn" :data-turn-index="turn.kind === 'compact' ? undefined : i">
        <!-- 压缩分隔线：账本边界事件，历史消息一条不动 -->
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
        <HlPilotTurnItem
          :turn="turn"
          :index="i"
          :is-last="i === turns.length - 1"
          :streaming="streaming"
          :editing="editingIndex === i"
          :edit-busy="editBusy"
          :feedback-busy="Boolean(feedbackBusyKey)"
          :open-att="openAtt"
          @copy="onCopyText"
          @quote="quoteToComposer"
          @edit-start="startEdit(i)"
          @edit-submit="(idx, text) => submitEdit(idx, text)"
          @edit-cancel="cancelEdit"
          @retry="retryTurn"
          @rate="rateTurn"
          @fork="forkFromTurn"
          @candidate-pick="pickCandidate"
          @proposal="onProposal"
          @continue-stopped="continueStopped"
          @toggle-attachment="toggleAttachment"
          @more-attachment="loadAttachment"
          @toggle-answer="toggleAnswer"
        />
      </div>

      <div v-if="streaming" class="pilot-turn">
        <div class="pilot-reply">
          <div class="pilot-reply__header">
            <span class="pilot-reply__avatar is-live">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <circle cx="12" cy="12" r="10" />
                <polygon points="16.24 7.76 14.12 14.12 7.76 16.24 9.88 9.88 16.24 7.76" />
              </svg>
            </span>
            <span class="pilot-reply__role">Pilot</span>
          </div>

          <!-- 运行中过程链：阶段块随定界逐个长出，每段正文随所在阶段就地展示；
               过程卡按产出阶段内嵌链中（随思考时序原位，随后续思考上移）。
               完成时原位替换为历史链，正式卡与终答由回答层承接 -->
          <HlPilotChain
            live
            hide-phase-text
            :steps="liveStepsFlat"
            :phases="livePhases"
            :waiting-text="loadingText"
            :live-cards="liveChainCards"
            @proposal="onProposal"
            @quote="quoteToComposer"
            @copy="onCopyText"
          />
          <!-- 终答就位：文字流式期在链下原位生长，done 时正式卡接手卡区 -->
          <HlPilotMarkdown v-if="liveAnswerSource" class="pilot-answer" :source="liveAnswerSource" />
          <div v-if="queuedHint" class="pilot-queued">
            {{ t(queuedHint === 'parallel' ? 'pilot.queued.parallel'
              : queuedHint === 'toolSlow' ? 'pilot.queued.toolSlow' : 'pilot.queued.waiting') }}
          </div>
          <!-- 运行中动作行：与终态回复同一位置同一套钮，只在已流出正文时给复制 -->
          <div v-if="liveAnswerSource" class="pilot-acts">
            <button
              type="button"
              class="pilot-act"
              :title="t('pilot.copy')"
              :aria-label="t('pilot.copy')"
              @click="onCopyText(liveAnswerSource)"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="9" y="9" width="11" height="11" rx="2" />
                <path d="M15 5H6a2 2 0 0 0-2 2v9" />
              </svg>
            </button>
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

    <!-- 消息队列：生成中提交的问题排在这里，本轮结束后自动发出；
         可改序、取回重编辑、移除；主动停止生成时暂停但内容不丢 -->
    <div v-if="queue.length || queuePaused" class="pilot-queue">
      <div class="pilot-queue__head">
        <span class="pilot-queue__title">{{ t('pilot.queue.title', { n: queue.length }) }}</span>
        <span v-if="queuePaused" class="pilot-queue__paused">{{ t('pilot.queue.paused') }}</span>
        <button v-if="queuePaused" type="button" class="pilot-queue__resume" @click="resumeQueue">
          {{ t('pilot.queue.resume') }}
        </button>
      </div>
      <div v-for="(item, qi) in queue" :key="item.id" class="pilot-queue__row">
        <span class="pilot-queue__idx">{{ qi + 1 }}</span>
        <span class="pilot-queue__text" :title="item.text">{{ item.text }}</span>
        <button
          type="button"
          class="pilot-queue__btn"
          :disabled="qi === 0"
          :title="t('pilot.queue.up')"
          :aria-label="t('pilot.queue.up')"
          @click="moveQueued(item.id, -1)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M12 19V5M5 12l7-7 7 7" />
          </svg>
        </button>
        <button
          type="button"
          class="pilot-queue__btn"
          :disabled="qi === queue.length - 1"
          :title="t('pilot.queue.down')"
          :aria-label="t('pilot.queue.down')"
          @click="moveQueued(item.id, 1)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M12 5v14M19 12l-7 7-7-7" />
          </svg>
        </button>
        <button
          type="button"
          class="pilot-queue__btn"
          :title="t('pilot.queue.edit')"
          :aria-label="t('pilot.queue.edit')"
          @click="takeQueued(item.id)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M4 20h4L19 9a2.1 2.1 0 0 0-3-3L5 17v3Z" />
            <path d="M14.5 6.5 17.5 9.5" />
          </svg>
        </button>
        <button
          type="button"
          class="pilot-queue__btn"
          :title="t('pilot.queue.remove')"
          :aria-label="t('pilot.queue.remove')"
          @click="removeQueued(item.id)"
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </div>
    </div>

    <HlPilotComposer
      ref="composerRef"
      v-model="question"
      :game="game"
      :reference-session="referenceSession"
      :streaming="streaming"
      :attach-busy="attachBusy"
      :last-ctx="lastCtx"
      :session-stats="sessionStats"
      :stats-total="statsTotal"
      :stats-cache-hit="statsCacheHit"
      :menu-items="plusMenuItems"
      @send="ask()"
      @stop="stopPilot"
      @menu-select="onMenuSelect"
      @attach="attachText"
      @remove-reference="referenceSession = null"
    />

    <!-- 拖选浮条：答复里划一段松手即浮出，两个去处——整段拿走 / 带回输入框。
         Teleport 到 body：会话区是滚动容器，浮条在里面会被裁掉 -->
    <Teleport to="body">
      <Transition name="hl-pop">
        <div
          v-if="selQuote"
          class="pilot-selbar"
          :style="selBarStyle"
          @mousedown.prevent
        >
          <button type="button" class="pilot-selbar__btn" @click="onCopyText(selQuote.text)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <rect x="9" y="9" width="11" height="11" rx="2" />
              <path d="M15 5H6a2 2 0 0 0-2 2v9" />
            </svg>
            <span>{{ t('pilot.copy') }}</span>
          </button>
          <button type="button" class="pilot-selbar__btn" @click="quoteToComposer(selQuote.text)">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="M10 7H6a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2v-4" />
              <path d="M14 4h6v6" />
              <path d="M20 4l-8 8" />
            </svg>
            <span>{{ t('pilot.quote') }}</span>
          </button>
        </div>
      </Transition>
    </Teleport>

    <!-- 底栏会话统计：轮/步/速度读数 + 点击弹总耗时/首字面板（账本投影，无轮不渲染） -->
    <HlPilotStats v-if="sessionStats && sessionStats.turns > 0" :stats="sessionStats" />
  </div>
</template>

<style scoped>
<style scoped>
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
/* 高度由宿主（Index 的定高容器）给出：对话记录吃掉剩余空间内滚，
   输入框恒在底部——长内容（思考展开/卡片）不再把它顶下去 */
.pilot-console.is-page {
  height: 100%;
}
.pilot-console.is-page .pilot-head,
.pilot-console.is-page .pilot-tabbar {
  display: none;
}
/* 列体尺寸由宿主挂点（Index 的 .pilot-page__side）约束，这里只管纵向分布 */
/* 生成中脉动点（标签栏）：accent 圆点呼吸；会话列同类规则在 HlPilotSidebar 内 */
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
/* 派生标记：标题前缀的常驻小图标（会话列 / 标签栏 / 历史面板共用）——
   放前缀是因为窄列会省略标题尾部，后缀标记会被截掉 */
.pilot-forkmark {
  flex: none;
  display: inline-grid;
  place-items: center;
  width: 11px;
  height: 11px;
  margin-right: 4px;
  vertical-align: -1px;
  color: var(--text-muted);
}
.pilot-forkmark svg {
  width: 11px;
  height: 11px;
  stroke: currentColor;
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
.pilot-head__actions {
  display: flex;
  align-items: center;
  gap: 4px;
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
@keyframes pavatar-pulse {
  from { box-shadow: 0 0 0 1px var(--accent-a20); }
  to { box-shadow: 0 0 0 4px var(--accent-a20); }
}
/* 动作行：挂在它作用的那条消息下方，与消息同侧对齐。
   整行常驻占位（高度恒定）只切透明度——显隐不推动上方内容，
   气泡/正文不因出现动作而位移。 */
.pilot-acts {
  display: flex;
  align-items: center;
  gap: 2px;
  height: 22px;
  opacity: 0;
  transition: opacity 0.15s ease;
}
/* 消息队列：生成中提交的问题排队等本轮结束（可改序/取回/移除） */
.pilot-queue {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border: 1px solid var(--border-soft);
  border-radius: 12px;
  background: var(--bg-soft);
}
.pilot-queue__head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.pilot-queue__title {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}
.pilot-queue__paused {
  font-size: 11px;
  color: var(--danger-deep);
}
.pilot-queue__resume {
  margin-left: auto;
  padding: 2px 9px;
  border: 1px solid var(--accent-a30);
  border-radius: 999px;
  background: var(--accent-a10);
  color: var(--accent-deep);
  font-size: 11px;
  cursor: pointer;
}
.pilot-queue__resume:hover {
  background: var(--accent-a20);
}
.pilot-queue__row {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.pilot-queue__idx {
  flex: none;
  width: 16px;
  text-align: center;
  font-size: 11px;
  color: var(--text-muted);
}
.pilot-queue__text {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--text-primary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.pilot-queue__btn {
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: none;
  border-radius: 5px;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
}
.pilot-queue__btn svg {
  width: 12px;
  height: 12px;
  display: block;
}
.pilot-queue__btn:hover:not(:disabled) {
  background: var(--surface-chip);
  color: var(--text-primary);
}
.pilot-queue__btn:disabled {
  opacity: 0.35;
  cursor: default;
}
/* 拖选浮条：定在选区正上方居中，@mousedown.prevent 保住选区不散 */
.pilot-selbar {
  position: fixed;
  z-index: 90;
  transform: translateX(-50%);
  display: inline-flex;
  align-items: center;
  gap: 2px;
  padding: 3px;
  border-radius: 10px;
  background: var(--surface-float);
  border: 1px solid var(--border-soft);
  box-shadow: 0 6px 20px var(--surface-inset-sm);
}
.pilot-selbar__btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 9px;
  border: none;
  border-radius: 7px;
  background: transparent;
  color: var(--text-secondary);
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
}
.pilot-selbar__btn svg {
  width: 13px;
  height: 13px;
}
.pilot-selbar__btn:hover {
  background: var(--surface-chip);
  color: var(--text-primary);
}
/* 回答排版由 HlPilotMarkdown 组件承载；此处只留折叠裁切等容器行为 */
.pilot-answer.is-clamped {
  position: relative;
  max-height: 320px;
  overflow: hidden;
}
.pilot-answer.is-clamped::after {
  content: '';
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  height: 56px;
  background: linear-gradient(to bottom, transparent, var(--bg-base));
  pointer-events: none;
}
.pilot-answer__more {
  align-self: flex-start;
  margin-top: 4px;
  padding: 2px 9px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: none;
  color: var(--text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.pilot-answer__more:hover {
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}
/* 分叉溯源分隔线：与压缩分隔线同一套「两侧细线 + 居中胶囊」语言——
   派生是本会话的起点事实，占对话流首行；可点跳回源会话，故用按钮而非徽章 */
.pilot-forkline {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 4px 0;
  border: none;
  background: none;
  cursor: pointer;
}
.pilot-forkline__line {
  flex: 1;
  height: 1px;
  background: var(--border-soft);
}
.pilot-forkline__pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 70%;
  padding: 3px 10px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  transition: color 0.15s ease, border-color 0.15s ease;
}
.pilot-forkline:hover .pilot-forkline__pill,
.pilot-forkline:focus-visible .pilot-forkline__pill {
  border-color: var(--accent-a30);
  background: var(--accent-a10);
  color: var(--accent);
}
.pilot-forkline:focus-visible {
  outline: none;
}
.pilot-forkline__pill svg {
  width: 12px;
  height: 12px;
  flex: none;
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
