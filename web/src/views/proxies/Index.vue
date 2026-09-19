<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { ElMessageBox } from 'element-plus'

import {
  proxiesApi,
  proxypoolApi,
  type ClashNodeTestResult,
  type ClashStatus,
  type ProxyItem,
  type ProxyJobRunItem,
  type ProxyJobRunsPayload,
  type ProxyStrategy,
  type ProxySubscriptionItem,
} from '@/api/client'
import {
  HlAlert,
  HlButton,
  HlDialog,
  HlDrawer,
  HlEmpty,
  HlIcon,
  HlPagination,
  HlSkeleton,
  HlStat,
  HlTable,
  HlTag,
  message,
  type HlTableColumn,
} from '@/components/ui'
import { useI18n, type MessageKey } from '@/locales'

/**
 * 代理 IP 池管理 —— 页内分四区：策略卡片网格 / 分区区块 / 节点表格 / 走线控制台。
 * 订阅链接（Clash 机场订阅 / 明文商业代理订阅）长期保存在本地库，按方式区分，可存多条。
 */
const { t } = useI18n()
const items = ref<ProxyItem[]>([])
const subscriptions = ref<ProxySubscriptionItem[]>([])
const strategy = ref<ProxyStrategy>({ strategy: 'proxy_first', clashPort: 7890 })
const events = ref<Awaited<ReturnType<typeof proxiesApi.events>>>([])
const clash = ref<ClashStatus | null>(null)
const loading = ref(true)
const testingAll = ref(false)
const testingId = ref<number | null>(null)

const batchInput = ref('')
const singleInput = ref('')
const installing = ref(false)
const starting = ref(false)

// 内核下载进度弹窗（保存订阅缺内核自动下载 / 手动安装共用）
const kernelDialog = ref(false)
const kernelProgress = ref(0)
const kernelPhase = ref('')
const kernelVia = ref('')
const kernelSource = ref('')
const kernelDownloading = ref(false)
let kernelPollTimer: ReturnType<typeof setInterval> | null = null

/** 轮询内核下载进度直到完成/失败（进度端点由后端模块级状态提供） */
async function pollKernelProgress() {
  try {
    const p = await proxiesApi.clashInstallProgress()
    kernelProgress.value = p.percent ?? 0
    kernelPhase.value = p.phase ?? ''
    kernelVia.value = p.via ? t('proxies.kernel.viaWrap', { via: p.via }) : ''
    kernelSource.value = (p.source ?? '').replace(/^https?:\/\//, '').replace(/\/$/, '')
    if (p.error) {
      stopKernelPoll()
      message.error(t('proxies.kernel.downloadFailed', { error: p.error }))
      kernelDialog.value = false
    } else if (p.ok && !p.running) {
      stopKernelPoll()
      kernelProgress.value = 100
      message.success(t('proxies.kernel.downloadDone'))
      setTimeout(() => (kernelDialog.value = false), 600)
    }
  } catch {
    /* 轮询瞬断忽略，下轮再试 */
  }
}

function startKernelPoll() {
  kernelDownloading.value = true
  kernelDialog.value = true
  kernelProgress.value = 0
  // 阶段名由后端轮询回填；这里的初始态留空，模板用 t() 兜底显示「准备下载」，
  // 免得把语言冻在「点下按钮那一刻」的 ref 里（见 brief 冻结陷阱）
  kernelPhase.value = ''
  kernelVia.value = ''
  kernelSource.value = ''
  kernelPollTimer = setInterval(pollKernelProgress, 800)
}

function stopKernelPoll() {
  kernelDownloading.value = false
  if (kernelPollTimer) {
    clearInterval(kernelPollTimer)
    kernelPollTimer = null
  }
}

// 订阅管理（双方式）
const selectedClashSubId = ref<number | null>(null)
const newClashSubUrl = ref('')
const newPlainSubUrl = ref('')
const addingClashSub = ref(false)
const addingPlainSub = ref(false)
const importingPlainId = ref<number | null>(null)
const syncingSubId = ref<number | null>(null)

// 保存订阅失败弹窗（错误详情 + 建议开代理 + 一键重试）
const subFailDialog = ref(false)
const subFailError = ref('')
const subFailKind = ref<'clash' | 'plain'>('clash')

// 编辑订阅弹窗（名称 + 链接两个参数一起改；链接变更即自动重拉）
const editDialog = ref(false)
const editSub = ref<ProxySubscriptionItem | null>(null)
const editLabel = ref('')
const editUrl = ref('')
const editingSub = ref(false)

// Clash 节点检测（出口 IP 去重 → 存活 23/45）
const clashTest = ref<ClashNodeTestResult | null>(null)
const testingClash = ref(false)

const clashSubs = computed(() => subscriptions.value.filter((s) => s.kind === 'clash'))
const plainSubs = computed(() => subscriptions.value.filter((s) => s.kind === 'plain'))

/* 卡片文案走词条 key（模块级常量存译文会把语言冻在加载那一刻） */
interface StrategyCard {
  value: string
  labelKey: MessageKey
  descKey: MessageKey
  icon: string
}
const strategyCards: StrategyCard[] = [
  { value: 'proxy_first', labelKey: 'proxies.strategy.proxyFirst.label', descKey: 'proxies.strategy.proxyFirst.desc', icon: 'zap' },
  { value: 'direct_only', labelKey: 'proxies.strategy.directOnly.label', descKey: 'proxies.strategy.directOnly.desc', icon: 'home' },
  { value: 'direct_first', labelKey: 'proxies.strategy.directFirst.label', descKey: 'proxies.strategy.directFirst.desc', icon: 'home' },
  { value: 'proxy_only', labelKey: 'proxies.strategy.proxyOnly.label', descKey: 'proxies.strategy.proxyOnly.desc', icon: 'globe' },
]

/* subscription-userinfo 头 → 人类可读流量摘要（"upload=x; download=y; total=z"）
   调用点在模板渲染与事件回调里，故 t() 取词条不会冻语言 */
function shortTraffic(raw: string): string {
  const get = (k: string) => Number(raw.match(new RegExp(`${k}=(\\d+)`))?.[1] ?? 0)
  const gb = (n: number) => (n / 1024 ** 3).toFixed(n / 1024 ** 3 >= 10 ? 0 : 1)
  const used = get('upload') + get('download')
  const total = get('total')
  if (!total) return t('proxies.sub.trafficUsed', { size: gb(used) })
  return `${gb(used)} / ${total >= 1024 ** 4 ? gb(total / 1024) + ' TB' : gb(total) + ' GB'}`
}

/** 订阅链接只显示前半段：机场链接动辄上百字符，整条铺开会把流量/存活
    挤出可视区（后半段是 token，对人不携带信息），故硬截断到前 26 字符。 */
function shortUrl(url: string): string {
  return url.length > 26 ? `${url.slice(0, 26)}…` : url
}

const enabledCount = computed(() => items.value.filter((p) => p.enabled).length)

async function load() {
  loading.value = true
  try {
    const data = await proxiesApi.list()
    items.value = data.items
    strategy.value = data.strategy
    subscriptions.value = data.subscriptions
    if (selectedClashSubId.value === null && clashSubs.value.length > 0) {
      selectedClashSubId.value = clashSubs.value[clashSubs.value.length - 1]!.id
    }
    events.value = await proxiesApi.events(80)
    clash.value = await proxiesApi.clashStatus()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

// ─── 订阅（双方式）───

async function addSubscription(kind: 'clash' | 'plain') {
  const url = (kind === 'clash' ? newClashSubUrl.value : newPlainSubUrl.value).trim()
  if (!url) return
  const busy = kind === 'clash' ? addingClashSub : addingPlainSub
  busy.value = true
  // Clash 订阅：内核缺失时后端自动下载（进度弹窗轮询 /clash/install/progress）
  const needKernelWatch =
    kind === 'clash' && !clash.value?.kernel.found && !kernelDownloading.value
  if (needKernelWatch) startKernelPoll()
  try {
    const sub = await proxiesApi.addSubscription(kind, url)
    if (kind === 'clash') {
      newClashSubUrl.value = ''
      selectedClashSubId.value = sub.id
      const bits = [
        sub.nodes != null
          ? t('proxies.sub.savedNodes', { nodes: sub.nodes })
          : t('proxies.sub.saved'),
      ]
      if (sub.kernelInstalled) {
        bits.push(
          sub.kernelVersion
            ? t('proxies.sub.kernelInstalledVersion', { version: sub.kernelVersion })
            : t('proxies.sub.kernelInstalled'),
        )
      }
      if (sub.warning) {
        // 保存成功但下载验证失败：同样给弹窗 + 建议开代理（可稍后在订阅卡上「重拉」）
        subFailKind.value = kind
        subFailError.value = sub.warning
        subFailDialog.value = true
      } else message.success(bits.join(' · '))
    } else {
      newPlainSubUrl.value = ''
      message.success(t('proxies.sub.savedPlain'))
    }
    await load()
  } catch (e) {
    // 保存失败：弹窗呈现失败原因 + 建议开启代理重试（订阅面板域名直连常被墙）
    subFailKind.value = kind
    subFailError.value = e instanceof Error ? e.message : String(e)
    subFailDialog.value = true
  } finally {
    if (needKernelWatch) stopKernelPoll()
    kernelDialog.value = false
    busy.value = false
  }
}

/** 失败弹窗「重试」：直接再走一次保存（输入框未清空，开好代理后点重试即可） */
function retryAddSubscription() {
  subFailDialog.value = false
  addSubscription(subFailKind.value)
}

async function removeSubscription(sub: ProxySubscriptionItem) {
  await ElMessageBox.confirm(t('proxies.sub.confirmDelete'), t('proxies.dialog.confirm'), {
    type: 'warning',
  })
  await proxiesApi.removeSubscription(sub.id)
  if (selectedClashSubId.value === sub.id) selectedClashSubId.value = null
  message.success(t('proxies.sub.deleted', { name: sub.label || sub.url }))
  await load()
}

/** 打开编辑弹窗：名称与链接一起呈现，改名换链一次完成 */
function openEditSubscription(sub: ProxySubscriptionItem) {
  editSub.value = sub
  editLabel.value = sub.label ?? ''
  editUrl.value = sub.url
  editDialog.value = true
}

/** 保存编辑：链接变更的 Clash 订阅由后端自动重拉（直连优先，失败借道已保存代理） */
async function saveEditSubscription() {
  const sub = editSub.value
  if (!sub) return
  const url = editUrl.value.trim()
  if (!/^https?:\/\//i.test(url)) {
    message.warning(t('proxies.sub.urlInvalid'))
    return
  }
  editingSub.value = true
  try {
    const res = await proxiesApi.updateSubscription(sub.id, {
      label: editLabel.value,
      url,
    })
    editDialog.value = false
    if (res.synced) {
      const parts = [t('proxies.sub.nodesCount', { n: res.nodes ?? 0 })]
      if (res.traffic) parts.push(shortTraffic(res.traffic))
      message.success(t('proxies.sub.editedSynced', { parts: parts.join(' · ') }))
    } else if (res.warning) {
      message.warning(res.warning)
    } else {
      message.success(t('proxies.sub.edited'))
    }
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    editingSub.value = false
  }
}

/** 流量实时回填：进页静默拉 subscription-userinfo 头（不动内核，失败不弹噪） */
async function refreshTrafficQuiet() {
  await Promise.all(
    clashSubs.value
      .filter((s) => !s.deprecated)
      .map(async (s) => {
        try {
          const res = await proxiesApi.refreshSubscriptionTraffic(s.id)
          s.lastStats = { ...(s.lastStats ?? {}), traffic: res.traffic }
        } catch {
          /* 面板暂不可达：保留上次值，下次进页再试 */
        }
      }),
  )
}

/** 重新拉取订阅：下载新配置 + 账本收敛，配置有变化自动重启内核生效 */
async function syncSubscription(sub: ProxySubscriptionItem) {
  syncingSubId.value = sub.id
  try {
    const res = await proxiesApi.syncSubscription(sub.id)
    const parts = [t('proxies.sub.nodesCount', { n: res.nodes ?? 0 })]
    if (res.traffic) parts.push(shortTraffic(res.traffic))
    const summary = { parts: parts.join(' · ') }
    message.success(
      res.restarted
        ? t('proxies.sub.syncedRestarted', summary)
        : t('proxies.sub.synced', summary),
    )
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    syncingSubId.value = null
  }
}

/** 废弃订阅显性确认：删除前提示其处于废弃状态（不可用 >95%） */
async function removeSubscriptionGuarded(sub: ProxySubscriptionItem) {
  if (sub.deprecated) {
    await ElMessageBox.confirm(
      t('proxies.sub.confirmDeleteDeprecated', {
        reason: sub.deprecatedReason ?? t('proxies.sub.deprecatedReason'),
      }),
      t('proxies.sub.deleteDeprecatedTitle'),
      { type: 'warning' },
    )
  }
  await removeSubscription(sub)
}

async function importPlainSubscription(sub: ProxySubscriptionItem) {
  importingPlainId.value = sub.id
  try {
    message.info(t('proxies.plain.importToast'))
    const stats = await proxiesApi.importSubscription(sub.id)
    const counts = { added: stats.added, skipped: stats.skipped }
    message.success(
      stats.checked !== undefined
        ? t('proxies.plain.importedChecked', {
            ...counts,
            alive: stats.alive ?? 0,
            checked: stats.checked,
          })
        : t('proxies.plain.imported', counts),
    )
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    importingPlainId.value = null
  }
}

// ─── 策略 / 节点池 ───

function pickStrategy(card: StrategyCard) {
  strategy.value.strategy = card.value
  applyStrategy()
}

async function applyStrategy() {
  try {
    await proxiesApi.setStrategy(strategy.value)
    message.success(t('proxies.strategy.updated'))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

/** 本地混合端口独立保存（后端三字段可空独立落库，不随策略切换） */
async function saveClashPort() {
  const port = Number(strategy.value.clashPort)
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    message.warning(t('proxies.port.invalid'))
    await load() // 恢复库里的合法值
    return
  }
  try {
    await proxiesApi.setStrategy({ ...strategy.value, clashPort: port })
    message.success(t('proxies.port.updated'))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function addSingle() {
  const raw = singleInput.value.trim()
  if (!raw) return
  try {
    await proxiesApi.add(raw)
    singleInput.value = ''
    message.success(t('proxies.node.added'))
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function addBatch() {
  const raw = batchInput.value.trim()
  if (!raw) return
  try {
    const res = await proxiesApi.add(raw)
    message.success(t('proxies.node.addedCount', { n: res.added }))
    batchInput.value = ''
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function toggle(item: ProxyItem) {
  await proxiesApi.update(item.id, { enabled: !item.enabled })
  message.success(t(item.enabled ? 'proxies.node.disabled' : 'proxies.node.enabled'))
  await load()
}

async function remove(item: ProxyItem) {
  await ElMessageBox.confirm(t('proxies.node.confirmDelete', { url: item.url }), t('proxies.dialog.confirm'), {
    type: 'warning',
  })
  await proxiesApi.remove(item.id)
  message.success(t('proxies.node.deleted'))
  await load()
}

async function clearAll() {
  await ElMessageBox.confirm(
    t('proxies.node.confirmClear', { n: items.value.length }),
    t('proxies.dialog.confirm'),
    { type: 'warning' },
  )
  for (const p of items.value) await proxiesApi.remove(p.id)
  message.success(t('proxies.node.cleared'))
  await load()
}

async function test(item: ProxyItem) {
  testingId.value = item.id
  try {
    const res = await proxiesApi.test(item.id)
    if (res.status === 'ok') message.success(t('proxies.node.testOk', { ms: res.latencyMs }))
    else {
      message.error(
        t('proxies.node.testFailed', {
          error: res.testError ?? t('proxies.node.unknownError'),
        }),
      )
    }
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    testingId.value = null
  }
}

async function testAll() {
  testingAll.value = true
  message.loading(t('proxies.node.checking'))
  try {
    await proxiesApi.testAll()
    message.success(t('proxies.node.testAllDone'))
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    testingAll.value = false
  }
}

// ─── Clash ───

async function installKernel() {
  installing.value = true
  startKernelPoll()
  try {
    const res = await proxiesApi.clashInstall()
    stopKernelPoll()
    if (res.ok) {
      kernelProgress.value = 100
      message.success(t('proxies.kernel.installOk', { version: res.version ?? '' }))
      setTimeout(() => (kernelDialog.value = false), 600)
    } else {
      message.error(res.error ?? t('proxies.kernel.installFailed'))
      kernelDialog.value = false
    }
    clash.value = await proxiesApi.clashStatus()
  } catch (e) {
    stopKernelPoll()
    kernelDialog.value = false
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    installing.value = false
  }
}

async function startClash() {
  const usableSubs = clashSubs.value.filter((s) => !s.deprecated)
  if (clashSubs.value.length === 0) {
    message.warning(t('proxies.clash.needSub'))
    return
  }
  if (usableSubs.length === 0) {
    message.error(t('proxies.clash.allDeprecated'))
    return
  }
  // 选中的订阅若已废弃，回退到最近一条可用订阅
  const selected = clashSubs.value.find((s) => s.id === selectedClashSubId.value)
  if (selected?.deprecated) {
    selectedClashSubId.value = usableSubs[usableSubs.length - 1]!.id
    message.warning(t('proxies.clash.switchedToUsable'))
  }
  starting.value = true
  try {
    const status = await proxiesApi.clashStart(selectedClashSubId.value ?? undefined)
    message.success(t('proxies.clash.started', { port: status.port }))
    clash.value = await proxiesApi.clashStatus()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    starting.value = false
  }
}

async function stopClash() {
  await proxiesApi.clashStop()
  clashTest.value = null
  message.success(t('proxies.clash.hasStopped'))
  clash.value = await proxiesApi.clashStatus()
}

async function runClashTest() {
  testingClash.value = true
  try {
    message.info(t('proxies.clash.testing'))
    clashTest.value = await proxiesApi.clashTest()
    const msg = t('proxies.clash.testDone', {
      alive: clashTest.value.alive,
      total: clashTest.value.total,
    })
    if (clashTest.value.deprecated) {
      message.error(t('proxies.clash.testDoneDeprecated', { msg }))
    } else {
      message.success(msg)
    }
    await load() // 订阅列表刷新废弃标记
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    testingClash.value = false
  }
}

// ─── 生产作业（真实爬取作业台账：**只记事实，不下健康结论**）───
// 数据来自 /api/v1/proxypool/job-runs：一次真实 run_crawl 一行。这里不做任何
// 健康分/等级/判死的推导——阈值要等真实数据积累之后再谈。

const jobRuns = ref<ProxyJobRunsPayload>({
  summary: {
    day: '',
    runs: 0,
    success: 0,
    partial: 0,
    failed: 0,
    interrupted: 0,
    running: 0,
    avgDurationMs: null,
    poolExitIpCount: null,
  },
  total: 0,
  items: [],
})
const jobsLoading = ref(false)
const jobsError = ref('')
const jobsPage = ref(1)
const JOB_PAGE_SIZE = 20
const jobDetail = ref<ProxyJobRunItem | null>(null)
const jobDetailOpen = ref(false)

/** NULL ≠ 0：后端没这个事实就显示 —，绝不显示 0（与三值证据同一条原则） */
const NONE = '—'

function fmtCount(v: number | null | undefined): string {
  return v === null || v === undefined ? NONE : String(v)
}

function fmtDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return NONE
  if (ms < 1000) return `${ms}ms`
  const seconds = ms / 1000
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes}m${Math.round(seconds - minutes * 60)}s`
}

function fmtTime(iso: string | null): string {
  return iso ? iso.slice(5, 19).replace('T', ' ') : NONE
}

function fmtSha(sha: string | null): string {
  return sha ? `${sha.slice(0, 12)}…` : NONE
}

function fmtList(ids: number[]): string {
  return ids.length ? ids.join(', ') : NONE
}

/** 池内运行名是 `<订阅>|<原名>`：窄列只显示原名，全名挂 title */
function nodeShort(name: string | null): string {
  if (!name) return NONE
  const idx = name.indexOf('|')
  return idx >= 0 ? name.slice(idx + 1) : name
}

/* 状态词与错误分类都**只存 key**，渲染期 t()（模块级常量存译文会把语言冻住） */
const STATUS_KEYS: Record<string, MessageKey> = {
  running: 'proxies.jobs.status.running',
  success: 'proxies.jobs.status.success',
  partial: 'proxies.jobs.status.partial',
  failed: 'proxies.jobs.status.failed',
  interrupted: 'proxies.jobs.status.interrupted',
}
const STATUS_TONES = {
  running: 'info',
  success: 'success',
  partial: 'warning',
  failed: 'danger',
  interrupted: 'default',
} as const
const ERROR_KEYS: Record<string, MessageKey> = {
  timeout: 'proxies.jobs.err.timeout',
  connect: 'proxies.jobs.err.connect',
  reset: 'proxies.jobs.err.reset',
  proxy_error: 'proxies.jobs.err.proxy_error',
  rate_limit: 'proxies.jobs.err.rate_limit',
  http_4xx: 'proxies.jobs.err.http_4xx',
  http_5xx: 'proxies.jobs.err.http_5xx',
  tls: 'proxies.jobs.err.tls',
  dns: 'proxies.jobs.err.dns',
  ledger: 'proxies.jobs.err.ledger',
  internal: 'proxies.jobs.err.internal',
  other: 'proxies.jobs.err.other',
}

function statusLabel(status: string): string {
  const key = STATUS_KEYS[status]
  return key ? t(key) : status
}

function statusTone(status: string): 'default' | 'accent' | 'success' | 'danger' | 'warning' | 'info' {
  return STATUS_TONES[status as keyof typeof STATUS_TONES] ?? 'default'
}

function errLabel(kind: string): string {
  const key = ERROR_KEYS[kind]
  return key ? t(key) : kind
}

/** 详情里的失败分类：只列非零项，顺序按后端枚举（ERROR_KEYS 的键序） */
const jobErrors = computed<{ kind: string; label: string; count: number }[]>(() => {
  const by = jobDetail.value?.byError
  if (!by) return []
  return Object.keys(ERROR_KEYS)
    .filter((kind) => (by[kind] ?? 0) > 0)
    .map((kind) => ({ kind, label: errLabel(kind), count: by[kind]! }))
})

const jobColumns = computed<HlTableColumn[]>(() => [
  { key: 'time', label: t('proxies.jobs.col.time'), width: '112px' },
  { key: 'status', label: t('proxies.jobs.col.status'), width: '88px' },
  { key: 'tasks', label: t('proxies.jobs.col.tasks'), numeric: true, width: '72px' },
  { key: 'success', label: t('proxies.jobs.col.success'), numeric: true, width: '72px' },
  { key: 'failed', label: t('proxies.jobs.col.failed'), numeric: true, width: '72px' },
  { key: 'duration', label: t('proxies.jobs.col.duration'), numeric: true, width: '88px' },
  { key: 'node', label: t('proxies.jobs.col.node') },
  { key: 'exitIp', label: t('proxies.jobs.col.exitIp'), width: '128px' },
])

/** HlTable 的 slot 行是 Record<string, unknown>：收敛成一个取值口 */
function asRun(row: Record<string, unknown>): ProxyJobRunItem {
  return row as unknown as ProxyJobRunItem
}

async function loadJobRuns() {
  jobsLoading.value = true
  jobsError.value = ''
  try {
    const offset = (jobsPage.value - 1) * JOB_PAGE_SIZE
    jobRuns.value = await proxypoolApi.jobRuns(JOB_PAGE_SIZE, offset)
  } catch (e) {
    jobsError.value = e instanceof Error ? e.message : String(e)
  } finally {
    jobsLoading.value = false
  }
}

async function openJobRun(row: Record<string, unknown>) {
  const item = asRun(row)
  jobDetail.value = item
  jobDetailOpen.value = true
  try {
    // 详情端点重取一行（列表里的快照可能已过期）
    jobDetail.value = await proxypoolApi.jobRun(item.id)
  } catch {
    /* 拉不到就先用列表那一行渲染，不打断查看 */
  }
}

watch(jobsPage, () => void loadJobRuns())

onMounted(async () => {
  await load()
  void refreshTrafficQuiet() // 实时流量：进页即静默刷新（不阻塞首屏渲染）
  void loadJobRuns() // 生产作业：与首屏并行拉，不阻塞渲染
})
</script>

<template>
  <section class="proxyx-page">
    <!-- Hero -->
    <div class="proxyx-hero">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="30" height="30">
        <circle cx="12" cy="12" r="10" />
        <line x1="2" y1="12" x2="22" y2="12" />
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
      </svg>
      <div>
        <h2>{{ t('proxies.hero.title') }}</h2>
        <p>{{ t('proxies.hero.subtitle') }}</p>
      </div>
    </div>

    <!-- 路由策略 -->
    <div class="proxyx-section" data-section="proxies.section.routing">
      <div class="proxyx-section-title">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
          <path d="M12 22c4-2 8-4 8-10V5l-8-3-8 3v7c0 6 4 8 8 10z" />
        </svg>
        <span>{{ t('proxies.section.routing') }}</span>
      </div>
      <div class="proxyx-strategy-grid">
        <button
          v-for="card in strategyCards"
          :key="card.value"
          class="proxyx-strategy-card"
          :class="{ active: strategy.strategy === card.value }"
          @click="pickStrategy(card)"
        >
          <span class="proxyx-strategy-state" :class="strategy.strategy === card.value ? 'is-on' : 'is-off'">
            <svg v-if="strategy.strategy === card.value" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">
              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14" />
              <polyline points="22 4 12 14.01 9 11.01" />
            </svg>
            <svg v-else viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" width="14" height="14">
              <circle cx="12" cy="12" r="10" />
              <line x1="4.93" y1="4.93" x2="19.07" y2="19.07" />
            </svg>
            {{ t(strategy.strategy === card.value ? 'proxies.strategy.on' : 'proxies.strategy.off') }}
          </span>
          <span class="proxyx-strategy-label">{{ t(card.labelKey) }}</span>
          <span class="proxyx-strategy-desc" v-html="t(card.descKey)" />
        </button>
      </div>
    </div>

    <!-- Clash 接入 -->
    <div class="proxyx-section" data-section="proxies.section.clash">
      <div class="proxyx-section-header">
        <div class="proxyx-section-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
            <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
          </svg>
          <span>{{ t('proxies.section.clash') }}</span>
        </div>
        <div class="proxyx-section-actions">
          <span v-if="clashTest" class="tag" :class="clashTest.alive > 0 ? 'tag--success' : 'tag--danger'">
            {{ t('proxies.clash.aliveTag', { alive: clashTest.alive, total: clashTest.total }) }}
          </span>
          <span v-if="clashTest && clashTest.alive < clashTest.total" class="tag">
            {{ t('proxies.clash.uniqueExits', { n: clashTest.aliveUnique }) }}
          </span>
          <HlButton
            v-if="clashTest || clash?.running"
            art="outline"
            tone="blue"
            size="sm"
            :disabled="testingClash || !clash?.running"
            :loading="testingClash"
            :title="clash?.running ? '' : t('proxies.clash.notRunningTitle')"
            @click="runClashTest"
          >
            {{ t(testingClash ? 'proxies.node.checking' : 'proxies.clash.testNodes') }}
          </HlButton>
        </div>
      </div>

      <div class="proxyx-clash">
        <div class="proxyx-clash-status">
          <span class="tag" :class="clash?.running ? 'tag--success' : ''">
            {{ clash?.running ? t('proxies.clash.running', { port: clash.port }) : t('proxies.clash.stopped') }}
          </span>
          <span class="tag" :class="clash?.kernel.found ? 'tag--accent' : 'tag--danger'">
            {{ t(clash?.kernel.found ? 'proxies.kernel.ready' : 'proxies.kernel.missing') }}
          </span>
          <span v-if="clash?.version" class="tag">{{ clash.version.slice(0, 36) }}</span>
          <!-- 本地混合端口：代理优先的回落探测指向它（自启 Verge 的接入点） -->
          <span class="proxyx-clash-port">
            <label for="clash-port-input">{{ t('proxies.port.label') }}</label>
            <input
              id="clash-port-input"
              v-model.number="strategy.clashPort"
              class="pxinput pxinput--sm proxyx-clash-port__input"
              type="number"
              min="1024"
              max="65535"
              :title="t('proxies.port.title')"
              @change="saveClashPort"
            />
          </span>
        </div>

        <div class="proxyx-clash-row">
          <template v-if="!clash?.kernel.found">
            <HlButton art="outline" tone="green" size="sm" :disabled="installing" :loading="installing" @click="installKernel">
              <HlIcon v-if="!installing" name="download" />
              {{ t(installing ? 'proxies.kernel.downloading' : 'proxies.kernel.autoDownload') }}
            </HlButton>
            <span class="proxyx-hint">{{ t('proxies.kernel.orPlaceManual', { dir: clash?.kernelDir ?? '' }) }}</span>
          </template>
          <template v-else>
            <span class="proxyx-hint">{{ t('proxies.kernel.pathHint', { path: clash.kernel.path }) }}</span>
          </template>
        </div>

        <!-- Clash 订阅（长期保存，可多条；废弃订阅红标置灰，方便快速定位） -->
        <div class="proxyx-sub-list">
          <label
            v-for="sub in clashSubs"
            :key="sub.id"
            class="proxyx-sub-item"
            :class="{ active: selectedClashSubId === sub.id, deprecated: sub.deprecated }"
          >
            <input
              v-model="selectedClashSubId"
              type="radio"
              :value="sub.id"
              name="clash-sub"
              :disabled="sub.deprecated"
            />
            <span v-if="sub.deprecated" class="tag tag--danger proxyx-sub-deprecated" :title="sub.deprecatedReason ?? ''">
              {{ t('proxies.sub.deprecated') }}
            </span>
            <span v-if="sub.label" class="tag">{{ sub.label }}</span>
            <span class="proxyx-sub-url mono">{{ shortUrl(sub.url) }}</span>
            <span v-if="sub.lastStats?.traffic" class="proxyx-sub-hint">{{ shortTraffic(sub.lastStats.traffic) }}</span>
            <span v-if="sub.lastStats?.alive !== undefined" class="tag proxyx-sub-alive">
              {{ t('proxies.sub.aliveTag', { alive: sub.lastStats.alive, total: sub.lastStats.total }) }}
            </span>
            <div class="proxyx-sub-actions">
              <button class="pxbtn pxbtn--sm" @click.prevent="openEditSubscription(sub)">{{ t('proxies.sub.edit') }}</button>
              <button class="pxbtn pxbtn--sm" :disabled="syncingSubId === sub.id" :aria-busy="syncingSubId === sub.id || undefined" @click.prevent="syncSubscription(sub)">
                <span v-if="syncingSubId === sub.id" class="hl-spinner hl-spinner--inline" aria-hidden="true" />
                {{ t(syncingSubId === sub.id ? 'proxies.sub.syncing' : 'proxies.sub.refetch') }}
              </button>
              <button class="pxbtn pxbtn--sm pxbtn--danger" @click.prevent="removeSubscriptionGuarded(sub)">{{ t('proxies.action.delete') }}</button>
            </div>
          </label>
          <div v-if="clashSubs.length === 0" class="proxyx-sub-empty">
            {{ t('proxies.sub.emptyClash') }}
          </div>
          <div v-else-if="clashSubs.every((s) => s.deprecated)" class="proxyx-sub-empty proxyx-sub-empty--danger">
            {{ t('proxies.sub.allDeprecated') }}
          </div>
        </div>
        <div class="proxyx-add-row">
          <input
            v-model="newClashSubUrl"
            type="password"
            class="pxinput"
            :placeholder="t('proxies.sub.clashPlaceholder')"
            @keyup.enter="addSubscription('clash')"
          />
          <HlButton art="outline" tone="green" size="sm" :disabled="addingClashSub" :loading="addingClashSub" @click="addSubscription('clash')">
            <HlIcon v-if="!addingClashSub" name="plus" />
            {{ t('proxies.sub.save') }}
          </HlButton>
        </div>

        <div class="proxyx-clash-row">
          <HlButton
            art="outline"
            size="sm"
            :disabled="starting || !clash?.kernel.found || clashSubs.length === 0"
            :loading="starting"
            @click="startClash"
          >
            <HlIcon v-if="!starting" name="play" />
            {{ t('proxies.clash.start') }}
          </HlButton>
          <HlButton art="outline" tone="dark" size="sm" :disabled="!clash?.running" @click="stopClash">
            {{ t('proxies.clash.stop') }}
          </HlButton>
          <span class="proxyx-hint">{{ t('proxies.clash.startHint') }}</span>
        </div>

        <!-- Clash 节点检测结果（存活=Steam 端点 200；冷却期节点沿用账本状态） -->
        <div v-if="clashTest" class="proxyx-table-wrap">
          <table class="proxyx-table">
            <thead>
              <tr>
                <th>{{ t('proxies.node.colAddress') }}</th>
                <th style="width: 130px">{{ t('proxies.node.colExitIp') }}</th>
                <th style="width: 90px">Steam</th>
                <th style="width: 110px">{{ t('proxies.node.colStatus') }}</th>
                <th style="width: 90px">{{ t('proxies.node.colDuration') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="n in clashTest.nodes" :key="n.name">
                <td class="mono">{{ n.name }}<span v-if="n.duplicate" class="tag" style="margin-left: 6px">{{ t('proxies.clash.sameExit') }}</span></td>
                <td class="mono">{{ n.exitIp ?? '—' }}</td>
                <td>
                  <span class="tag" :class="n.alive ? 'tag--success' : 'tag--danger'">
                    {{ t(n.alive ? 'proxies.clash.reachable' : 'proxies.clash.unreachable') }}
                  </span>
                </td>
                <td>
                  <span v-if="n.cooling" class="tag" :title="t('proxies.clash.coolingTitle')">
                    {{ t('proxies.clash.cooling') }}
                  </span>
                  <span v-else-if="n.alive" class="tag tag--success">{{ t('proxies.clash.healthy') }}</span>
                  <span v-else class="tag tag--danger">{{ t('proxies.clash.unavailable') }}</span>
                </td>
                <td class="mono">{{ n.ms !== null ? `${n.ms}ms` : '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <!-- 代理节点列表 -->
    <div class="proxyx-section" data-section="proxies.section.nodes">
      <div class="proxyx-section-header">
        <div class="proxyx-section-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
            <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
            <line x1="8" y1="21" x2="16" y2="21" />
            <line x1="12" y1="17" x2="12" y2="21" />
          </svg>
          <span>{{ t('proxies.section.nodes') }}</span>
        </div>
        <div class="proxyx-section-actions">
          <span class="tag">{{ t('proxies.node.summary', { total: items.length, enabled: enabledCount }) }}</span>
          <HlButton art="outline" tone="blue" size="sm" :disabled="testingAll || !items.length" :loading="testingAll" @click="testAll">
            <HlIcon v-if="!testingAll" name="refresh" />
            {{ t(testingAll ? 'proxies.node.testingAll' : 'proxies.node.testAll') }}
          </HlButton>
          <HlButton art="outline" tone="dark" size="sm" :disabled="!items.length" @click="clearAll">
            {{ t('proxies.node.clear') }}
          </HlButton>
        </div>
      </div>

      <!-- 明文代理订阅（长期保存，一键导入） -->
      <div class="proxyx-sub-list">
        <div class="proxyx-sub-item proxyx-sub-item--static" v-for="sub in plainSubs" :key="sub.id">
          <span v-if="sub.label" class="tag">{{ sub.label }}</span>
          <span class="proxyx-sub-url mono">{{ shortUrl(sub.url) }}</span>
          <span v-if="sub.lastStats?.added !== undefined" class="tag">
            {{ t('proxies.plain.lastImport', { n: sub.lastStats.added }) }}
          </span>
          <div class="proxyx-sub-actions">
            <button class="pxbtn pxbtn--sm" @click="openEditSubscription(sub)">{{ t('proxies.sub.edit') }}</button>
            <button class="pxbtn pxbtn--sm" :disabled="importingPlainId === sub.id" :aria-busy="importingPlainId === sub.id || undefined" @click="importPlainSubscription(sub)">
              <span v-if="importingPlainId === sub.id" class="hl-spinner hl-spinner--inline" aria-hidden="true" />
              {{ t(importingPlainId === sub.id ? 'proxies.plain.importing' : 'proxies.plain.import') }}
            </button>
            <button class="pxbtn pxbtn--sm pxbtn--danger" @click="removeSubscription(sub)">{{ t('proxies.action.delete') }}</button>
          </div>
        </div>
        <div v-if="plainSubs.length === 0" class="proxyx-sub-empty">
          {{ t('proxies.plain.empty') }}
        </div>
      </div>
      <div class="proxyx-add-row">
        <input
          v-model="newPlainSubUrl"
          class="pxinput"
          :placeholder="t('proxies.plain.placeholder')"
          @keyup.enter="addSubscription('plain')"
        />
        <HlButton art="outline" tone="green" size="sm" :disabled="addingPlainSub" :loading="addingPlainSub" @click="addSubscription('plain')">
          <HlIcon v-if="!addingPlainSub" name="plus" />
          {{ t('proxies.sub.save') }}
        </HlButton>
      </div>

      <div class="proxyx-add-row proxyx-add-row--compact">
        <input
          v-model="singleInput"
          class="pxinput pxinput--sm"
          :placeholder="t('proxies.node.manualPlaceholder')"
          @keyup.enter="addSingle"
        />
        <HlButton art="outline" tone="green" size="sm" @click="addSingle">
          <HlIcon name="plus" />
          {{ t('proxies.node.add') }}
        </HlButton>
      </div>
      <div class="proxyx-add-row proxyx-add-row--compact">
        <textarea
          v-model="batchInput"
          class="pxinput pxinput--sm"
          rows="2"
          :placeholder="t('proxies.node.batchPlaceholder')"
        />
        <HlButton art="outline" size="sm" @click="addBatch">{{ t('proxies.node.batchImport') }}</HlButton>
      </div>

      <div v-loading="loading" class="proxyx-table-wrap">
        <table v-if="items.length" class="proxyx-table">
          <thead>
            <tr>
              <th style="width: 44px">#</th>
              <th>{{ t('proxies.node.colAddress') }}</th>
              <th style="width: 90px">{{ t('proxies.node.colStatus') }}</th>
              <th style="width: 90px">{{ t('proxies.node.colLatency') }}</th>
              <th style="width: 70px">{{ t('proxies.node.colEnabled') }}</th>
              <th style="width: 150px">{{ t('proxies.node.colActions') }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in items" :key="p.id">
              <td>{{ p.id }}</td>
              <td class="mono">{{ p.label ?? p.url }}</td>
              <td>
                <span class="tag" :class="p.status === 'ok' ? 'tag--success' : p.status === 'failed' ? 'tag--danger' : ''">
                  {{ p.status }}
                </span>
              </td>
              <td class="mono">{{ p.latencyMs !== null ? `${p.latencyMs}ms` : '—' }}</td>
              <td>
                <button class="pxswitch" :class="{ on: p.enabled }" @click="toggle(p)">
                  <span />
                </button>
              </td>
              <td>
                <div class="proxyx-actions">
                  <button class="pxbtn pxbtn--sm" :disabled="testingId === p.id" :aria-busy="testingId === p.id || undefined" @click="test(p)">
                    <span v-if="testingId === p.id" class="hl-spinner hl-spinner--inline" aria-hidden="true" />
                    {{ testingId === p.id ? '' : t('proxies.node.test') }}
                  </button>
                  <button class="pxbtn pxbtn--sm pxbtn--danger" @click="remove(p)">{{ t('proxies.action.delete') }}</button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-else class="proxyx-empty">{{ t('proxies.node.empty') }}</div>
      </div>
    </div>

    <!-- 走线控制台 -->
    <div class="proxyx-section" data-section="proxies.section.console">
      <div class="proxyx-section-header">
        <div class="proxyx-section-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
            <polyline points="4 17 10 11 4 5" />
            <line x1="12" y1="19" x2="20" y2="19" />
          </svg>
          <span>{{ t('proxies.section.console') }}</span>
        </div>
        <span class="tag">{{ t('proxies.console.count', { n: events.length }) }}</span>
      </div>
      <div class="proxyx-console">
        <div v-for="e in events" :key="e.id" class="proxyx-console-line" :class="{ err: !!e.error }">
          <span class="t">{{ e.ts?.slice(11, 19) }}</span>
          <span class="k">{{ e.kind }}</span>
          <span class="p">{{ e.proxyLabel }}</span>
          <span class="m">{{ e.error ?? `${e.durationMs ?? '—'}ms` }}</span>
        </div>
        <div v-if="!events.length" class="proxyx-console-line">{{ t('proxies.console.empty') }}</div>
      </div>
    </div>

    <!-- 生产作业（真实爬取作业台账：只记事实，不下健康结论） -->
    <div class="proxyx-section" data-section="proxies.section.jobs">
      <div class="proxyx-section-header">
        <div class="proxyx-section-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
            <path d="M4 19V5" />
            <path d="M4 19h16" />
            <rect x="7" y="11" width="3" height="5" />
            <rect x="12" y="8" width="3" height="8" />
            <rect x="17" y="13" width="3" height="3" />
          </svg>
          <span>{{ t('proxies.section.jobs') }}</span>
        </div>
        <div class="proxyx-section-actions">
          <span class="tag">{{ t('proxies.jobs.total', { n: jobRuns.total }) }}</span>
          <HlButton
            art="outline"
            tone="blue"
            size="sm"
            :disabled="jobsLoading"
            :loading="jobsLoading"
            @click="loadJobRuns"
          >
            <HlIcon v-if="!jobsLoading" name="refresh" />
            {{ t('proxies.jobs.refresh') }}
          </HlButton>
        </div>
      </div>
      <p class="pjobs-desc">{{ t('proxies.jobs.desc') }}</p>

      <HlAlert v-if="jobsError" type="error" :title="t('proxies.jobs.loadFailed')" :desc="jobsError" />

      <div v-else-if="jobsLoading && !jobRuns.items.length" class="pjobs-stats">
        <HlSkeleton v-for="i in 6" :key="i" variant="card" height="72px" />
      </div>

      <template v-else>
        <div class="pjobs-stats">
          <HlStat size="sm" :label="t('proxies.jobs.stat.runs')" :value="jobRuns.summary.runs" />
          <HlStat
            size="sm"
            tone="good"
            :label="t('proxies.jobs.stat.success')"
            :value="jobRuns.summary.success"
          />
          <HlStat
            size="sm"
            tone="warn"
            :label="t('proxies.jobs.stat.partial')"
            :value="jobRuns.summary.partial"
          />
          <HlStat
            size="sm"
            tone="bad"
            :label="t('proxies.jobs.stat.failed')"
            :value="jobRuns.summary.failed"
          />
          <HlStat
            size="sm"
            :label="t('proxies.jobs.stat.avgDuration')"
            :value="fmtDuration(jobRuns.summary.avgDurationMs)"
          />
          <HlStat
            size="sm"
            :label="t('proxies.jobs.stat.poolExitIp')"
            :value="fmtCount(jobRuns.summary.poolExitIpCount)"
          />
        </div>
        <p v-if="jobRuns.summary.interrupted || jobRuns.summary.running" class="pjobs-extra">
          {{
            t('proxies.jobs.extra', {
              interrupted: jobRuns.summary.interrupted,
              running: jobRuns.summary.running,
            })
          }}
        </p>

        <div class="pjobs-table-wrap">
          <HlTable
            v-if="jobRuns.items.length"
            :columns="jobColumns"
            :rows="jobRuns.items"
            :on-row-click="openJobRun"
          >
            <template #time="{ row }">{{ fmtTime(asRun(row).startedAt) }}</template>
            <template #status="{ row }">
              <HlTag :type="statusTone(asRun(row).status)">
                {{ statusLabel(asRun(row).status) }}
              </HlTag>
            </template>
            <template #tasks="{ row }">{{ fmtCount(asRun(row).taskCount) }}</template>
            <template #success="{ row }">{{ fmtCount(asRun(row).successCount) }}</template>
            <template #failed="{ row }">{{ fmtCount(asRun(row).errorCount) }}</template>
            <template #duration="{ row }">{{ fmtDuration(asRun(row).durationMs) }}</template>
            <template #node="{ row }">
              <span :title="asRun(row).node ?? undefined">{{ nodeShort(asRun(row).node) }}</span>
            </template>
            <template #exitIp="{ row }">{{ asRun(row).nodeExitIp ?? NONE }}</template>
          </HlTable>
          <HlEmpty v-else :text="t('proxies.jobs.empty')" />
        </div>

        <div v-if="jobRuns.total > JOB_PAGE_SIZE" class="pjobs-pager">
          <HlPagination v-model="jobsPage" :total="jobRuns.total" :page-size="JOB_PAGE_SIZE" />
        </div>
      </template>
    </div>

    <!-- 生产作业详情（已聚合数据：一轮只有一个节点/一个出口 IP，不做多行表） -->
    <HlDrawer
      v-model="jobDetailOpen"
      :title="t('proxies.jobs.detail.title', { id: jobDetail?.id ?? '' })"
      width="520px"
      :top="56"
    >
      <div v-if="jobDetail" class="pjob-detail">
        <div v-if="jobDetail.interrupted" class="pjob-note">
          {{ t('proxies.jobs.detail.interrupted') }}
        </div>
        <dl class="pjob-grid">
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.basic') }}</dt>
            <dd>#{{ jobDetail.id }} · {{ statusLabel(jobDetail.status) }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.startedAt') }}</dt>
            <dd class="pjob-num">{{ fmtTime(jobDetail.startedAt) }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.finishedAt') }}</dt>
            <dd class="pjob-num">{{ fmtTime(jobDetail.finishedAt) }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.duration') }}</dt>
            <dd class="pjob-num">{{ fmtDuration(jobDetail.durationMs) }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.node') }}</dt>
            <dd>
              <template v-if="jobDetail.node">
                <span class="pjob-mono" :title="jobDetail.node">{{ jobDetail.node }}</span>
                <span class="pjob-sub">{{ jobDetail.nodeExitIp ?? NONE }}</span>
              </template>
              <span v-else class="pjob-sub">{{ t('proxies.jobs.detail.nodeUnknown') }}</span>
            </dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.proxyUrl') }}</dt>
            <dd class="pjob-mono">{{ jobDetail.proxyUrl ?? NONE }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.poolSha') }}</dt>
            <dd class="pjob-mono">{{ fmtSha(jobDetail.poolSha256) }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.poolNodes') }}</dt>
            <dd class="pjob-num">{{ fmtCount(jobDetail.poolNodeCount) }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.poolExitIps') }}</dt>
            <dd class="pjob-num">{{ fmtCount(jobDetail.poolExitIpCount) }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.subscriptions') }}</dt>
            <dd class="pjob-num">{{ fmtList(jobDetail.subscriptionIds) }}</dd>
          </div>
          <div class="pjob-row">
            <dt>{{ t('proxies.jobs.detail.snapshots') }}</dt>
            <dd class="pjob-num">{{ fmtList(jobDetail.snapshotIds) }}</dd>
          </div>
        </dl>

        <h4 class="pjob-h4">{{ t('proxies.jobs.detail.errors') }}</h4>
        <p v-if="jobDetail.byError === null" class="pjob-none">
          {{ t('proxies.jobs.detail.summaryMissing') }}
        </p>
        <template v-else>
          <p v-if="jobDetail.errorSummaryTruncated" class="pjob-note">
            {{ t('proxies.jobs.detail.truncated') }}
          </p>
          <div v-if="jobErrors.length" class="pjob-errs">
            <div v-for="e in jobErrors" :key="e.kind" class="pjob-err">
              <span>{{ e.label }}</span>
              <span class="pjob-num pjob-num--count">{{ e.count }}</span>
            </div>
          </div>
          <p v-else class="pjob-none">{{ t('proxies.jobs.detail.errorsNone') }}</p>
        </template>
      </div>
    </HlDrawer>

    <!-- 内核下载进度弹窗（手动安装 / 保存订阅缺内核自动下载共用） -->
    <HlDialog v-model="kernelDialog" :title="t('proxies.kernel.dialogTitle')" :width="420" :mask-closable="false">
      <div class="kdl">
        <div class="kdl__phase">
          {{ kernelPhase || t('proxies.kernel.phasePrepare') }}{{ kernelSource ? ` ${kernelSource}` : '' }}{{ kernelVia }} {{ t('proxies.kernel.sizeHint') }}
        </div>
        <HlProgress :value="kernelProgress" :show-text="true" />
        <div v-if="kernelDownloading" class="kdl__hint">
          {{ t('proxies.kernel.downloadHint') }}
        </div>
      </div>
    </HlDialog>

    <!-- 保存订阅失败弹窗：失败原因 + 建议开启代理 + 一键重试 -->
    <HlDialog v-model="subFailDialog" :title="t('proxies.sub.failTitle')" :width="420">
      <div class="sfd">
        <div class="sfd__err">{{ subFailError }}</div>
        <div class="sfd__tip">
          {{ t('proxies.sub.failTip') }}
        </div>
      </div>
      <template #footer>
        <HlButton size="sm" @click="subFailDialog = false">{{ t('common.close') }}</HlButton>
        <HlButton art="outline" tone="green" size="sm" @click="retryAddSubscription">{{ t('common.retry') }}</HlButton>
      </template>
    </HlDialog>

    <!-- 编辑订阅：名称 + 链接（链接变更自动重新拉取） -->
    <HlDialog v-model="editDialog" :title="t('proxies.sub.editTitle')" :width="520">
      <div class="sed">
        <label class="sed__row">
          <span class="sed__label">{{ t('proxies.sub.nameLabel') }}</span>
          <input v-model="editLabel" class="pxinput" :placeholder="t('proxies.sub.namePlaceholder')" />
        </label>
        <label class="sed__row">
          <span class="sed__label">{{ t('proxies.sub.urlLabel') }}</span>
          <input v-model="editUrl" class="pxinput mono" :placeholder="t('proxies.sub.urlPlaceholder')" />
        </label>
        <p class="sed__tip">{{ t('proxies.sub.editTip') }}</p>
      </div>
      <template #footer>
        <HlButton size="sm" @click="editDialog = false">{{ t('common.cancel') }}</HlButton>
        <HlButton art="outline" tone="green" size="sm" :disabled="editingSub" :loading="editingSub" @click="saveEditSubscription">
          {{ t('proxies.sub.editSave') }}
        </HlButton>
      </template>
    </HlDialog>
  </section>
</template>

<style scoped>
.proxyx-page {
  max-width: 980px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* Hero */
.proxyx-hero {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 22px 26px;
  border-radius: var(--radius-lg);
  background: linear-gradient(145deg, rgba(30, 44, 58, 0.9), rgba(20, 30, 40, 0.95));
  border: 1px solid var(--border-soft);
  color: var(--accent);
}

.proxyx-hero h2 {
  font-size: 18px;
  font-weight: 700;
  color: var(--text-primary);
  letter-spacing: -0.3px;
}

.proxyx-hero p {
  font-size: 12.5px;
  color: var(--text-muted);
  margin-top: 2px;
}

/* 分区 */
.proxyx-section {
  background: var(--bg-card);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  padding: 18px 22px;
}

.proxyx-section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13.5px;
  font-weight: 600;
  color: var(--accent);
  margin-bottom: 14px;
}

.proxyx-section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.proxyx-section-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

/* 策略卡片网格 */
.proxyx-strategy-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 10px;
}

.proxyx-strategy-card {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 14px;
  background: var(--bg-soft);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  text-align: left;
  cursor: pointer;
  transition: all var(--transition);
  color: var(--text-secondary);
}

.proxyx-strategy-card:hover {
  border-color: var(--border-strong);
  transform: translateY(-2px);
}

.proxyx-strategy-card.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

/* 状态图标徽标：标题前的启用/关闭状态（✓ 角标的替代，六卡减四卡后状态更直观） */
.proxyx-strategy-state {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10.5px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 999px;
  width: max-content;
}

.proxyx-strategy-state.is-on {
  background: var(--success-a15);
  color: var(--success);
}

.proxyx-strategy-state.is-off {
  background: var(--bg-soft);
  border: 1px solid var(--border-soft);
  color: var(--text-muted);
}

.proxyx-strategy-label {
  font-size: 14px;
  font-weight: 700;
  color: var(--text-primary);
}

.proxyx-strategy-card.active .proxyx-strategy-label {
  color: var(--accent);
}

.proxyx-strategy-desc {
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--text-muted);
}

.proxyx-strategy-desc small {
  opacity: 0.8;
}

/* Clash */
.proxyx-clash {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.proxyx-clash-status {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  align-items: center;
}

/* 本地混合端口（纯配置，不属于任何策略）：状态行内联的窄输入 */
.proxyx-clash-port {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;
  font-size: 12px;
  color: var(--text-muted);
}

/* .pxinput 自带 min-width:220/flex:1（表单全宽输入基准，文件更靠后、同 specificity 层叠必胜）
   —— 内联窄框必须用复合选择器提权压回 */
.proxyx-clash-port .proxyx-clash-port__input {
  width: 90px;
  min-width: 90px;
  flex: none;
}

.proxyx-clash-row {
  display: flex;
  gap: 8px;
  align-items: center;
  flex-wrap: wrap;
}

.proxyx-hint {
  font-size: 11.5px;
  color: var(--text-muted);
  word-break: break-all;
}

/* 订阅列表（双方式共用） */
.proxyx-sub-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.proxyx-sub-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  background: var(--bg-soft);
  cursor: pointer;
  transition: all var(--transition);
}

.proxyx-sub-item:hover {
  border-color: var(--border-strong);
}

.proxyx-sub-item.active {
  border-color: var(--accent);
  background: var(--accent-soft);
}

.proxyx-sub-item--static {
  cursor: default;
}

/* 废弃订阅：红边置灰——不可用节点 >95%，后端不再选用（用户手动删除） */
.proxyx-sub-item.deprecated {
  border-color: var(--danger);
  background: var(--bg-soft);
  opacity: 0.65;
}

.proxyx-sub-item.deprecated .proxyx-sub-url {
  text-decoration: line-through;
}

.proxyx-sub-deprecated {
  flex-shrink: 0;
  font-weight: 700;
}

.proxyx-sub-empty--danger {
  border-color: var(--danger);
  color: var(--danger);
}

.proxyx-sub-url {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
  font-size: 12px;
}

/* 流量 / 存活是这行的判读依据：不许被长链接挤出可视区（flex-shrink 归零，
   让压缩只作用在链接上——链接另有 shortUrl 硬截断兜底） */
.proxyx-sub-hint {
  color: var(--text-muted);
  font-size: 11px;
  white-space: nowrap;
  flex-shrink: 0;
}

.proxyx-sub-alive {
  flex-shrink: 0;
}

.proxyx-sub-actions {
  display: flex;
  gap: 6px;
  flex-shrink: 0;
}

.proxyx-sub-empty {
  padding: 10px 12px;
  border: 1px dashed var(--border-soft);
  border-radius: var(--radius);
  color: var(--text-muted);
  font-size: 12px;
  text-align: center;
}

/* 添加行：输入框布满整宽（与 Clash 订阅输入一致） */
.proxyx-add-row {
  display: flex;
  gap: 8px;
  align-items: stretch;
}

.proxyx-add-row + .proxyx-add-row {
  margin-top: 8px;
}

.proxyx-add-row .pxinput {
  flex: 1;
  width: 100%;
  min-width: 0;
}

.proxyx-add-row .pxbtn {
  flex-shrink: 0;
}

/* 紧凑模式：缩小内边距和字号，与订阅行视觉一致 */
.pxinput--sm {
  padding: 6px 10px;
  font-size: 12px;
}

.proxyx-add-row--compact .pxbtn {
  align-self: center;
}

/* 输入/按钮（本地化组件，绕开 el 默认样式）*/
.pxinput {
  flex: 1;
  min-width: 220px;
  background: var(--input-bg);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  color: var(--text-primary);
  padding: 8px 12px;
  font-size: 13px;
  outline: none;
  transition: all var(--transition);
  font-family: inherit;
  resize: vertical;
}

.pxinput:focus {
  border-color: var(--accent);
  box-shadow: 0 0 15px var(--accent-a20);
}

.pxinput::placeholder {
  color: var(--text-muted);
}

.pxbtn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 7px 14px;
  border: 1px solid var(--border-strong);
  border-radius: var(--radius);
  background: var(--bg-card);
  color: var(--text-primary);
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all var(--transition);
}

.pxbtn:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--accent);
}

.pxbtn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.pxbtn--primary {
  background: var(--accent-fill);
  border-color: var(--accent-fill);
  color: var(--on-accent-fill);
  font-weight: 600;
}

.pxbtn--primary:hover:not(:disabled) {
  background: var(--accent-fill-hover);
  border-color: var(--accent-fill-hover);
  color: var(--on-accent-fill);
}

.pxbtn--danger:hover:not(:disabled) {
  border-color: var(--danger);
  color: var(--danger);
}

.pxbtn--sm {
  padding: 4px 10px;
  font-size: 12px;
}

/* 表格 */
.proxyx-table-wrap {
  margin-top: 12px;
  overflow-x: auto;
}

.proxyx-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12.5px;
}

.proxyx-table th {
  text-align: left;
  padding: 8px 10px;
  color: var(--text-muted);
  font-weight: 600;
  border-bottom: 1px solid var(--border-soft);
  background: var(--surface-panel);
}

.proxyx-table td {
  padding: 8px 10px;
  border-bottom: 1px solid var(--border-soft);
  color: var(--text-secondary);
}

.proxyx-table tr:hover td {
  background: var(--accent-soft);
}

.mono {
  font-family: var(--font-mono);
  font-size: 12px;
}

.proxyx-actions {
  display: flex;
  gap: 6px;
  justify-content: flex-end;
}

.proxyx-empty {
  padding: 24px;
  text-align: center;
  color: var(--text-muted);
  font-size: 12.5px;
}

/* 开关 */
.pxswitch {
  width: 36px;
  height: 20px;
  border-radius: 999px;
  background: rgba(0, 0, 0, 0.4);
  border: 1px solid var(--border-soft);
  position: relative;
  cursor: pointer;
  transition: all var(--transition);
}

.pxswitch span {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: var(--text-muted);
  transition: all var(--transition);
}

.pxswitch.on {
  background: var(--accent);
  border-color: var(--accent);
}

.pxswitch.on span {
  left: 18px;
  background: var(--ink-on-fill);
}

/* 走线控制台 */
.proxyx-console {
  background: rgba(0, 0, 0, 0.35);
  border-radius: var(--radius);
  padding: 10px 12px;
  max-height: 240px;
  overflow-y: auto;
  font-family: var(--font-mono);
  font-size: 11.5px;
}

.proxyx-console-line {
  display: flex;
  gap: 12px;
  padding: 3px 0;
  color: var(--text-secondary);
  white-space: nowrap;
}

.proxyx-console-line.err .m {
  color: var(--danger);
}

.proxyx-console-line .t {
  color: var(--text-faint, #5a7080);
}

.proxyx-console-line .k {
  color: var(--accent);
  min-width: 44px;
}

.proxyx-console-line .p {
  color: var(--text-muted);
  min-width: 110px;
}

/* 内核下载进度弹窗 */
.kdl {
  display: grid;
  gap: 12px;
  padding: 4px 2px 8px;
}

.kdl__phase {
  color: var(--text-muted);
  font-size: 13px;
}

.kdl__hint {
  color: var(--text-muted);
  font-size: 12px;
}

/* 保存订阅失败弹窗 */
.sfd {
  display: grid;
  gap: 10px;
  padding: 4px 2px 8px;
}

.sfd__err {
  color: var(--danger);
  font-size: 13px;
  word-break: break-all;
}

.sfd__tip {
  color: var(--text-muted);
  font-size: 12px;
}

/* 编辑订阅弹窗（名称 + 链接两行表单） */
.sed {
  display: grid;
  gap: 12px;
  padding: 4px 2px 8px;
}

.sed__row {
  display: grid;
  gap: 6px;
}

.sed__label {
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary);
}

.sed__row .pxinput {
  width: 100%;
  min-width: 0;
}

.sed__tip {
  margin: 0;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.5;
}

/* ─── 生产作业（只记事实，不下健康结论）───
   配色一律走 token：这里只表达"事实的分类"，不表达"好坏判定"——失败计数用
   tone 只是让数字可扫读，页面里没有任何"亚健康/危险"之类的结论词。 */
.pjobs-desc {
  margin: 8px 0 14px;
  color: var(--text-muted);
  font-size: 11.5px;
  line-height: 1.5;
}

.pjobs-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}

.pjobs-extra {
  margin: 0 0 var(--space-3);
  color: var(--text-secondary);
  font-size: 11.5px;
}

.pjobs-table-wrap {
  margin-top: var(--space-2);
}

.pjobs-pager {
  display: flex;
  justify-content: center;
  margin-top: var(--space-4);
}

.pjobs-table-wrap :deep(.hl-table tbody tr) {
  cursor: pointer;
}

.pjob-detail {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.pjob-grid {
  display: grid;
  gap: var(--space-2);
  margin: 0;
}

.pjob-row {
  display: grid;
  grid-template-columns: 118px 1fr;
  gap: var(--space-3);
  align-items: baseline;
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--border-soft);
}

.pjob-row dt {
  color: var(--text-muted);
  font-size: 11.5px;
}

.pjob-row dd {
  margin: 0;
  color: var(--text-primary);
  font-size: 12.5px;
  word-break: break-all;
}

.pjob-num {
  font-variant-numeric: tabular-nums;
}

.pjob-num--count {
  color: var(--text-secondary);
}

.pjob-mono {
  font-family: var(--font-mono);
  font-size: 11.5px;
}

.pjob-sub {
  display: block;
  margin-top: 2px;
  color: var(--text-muted);
  font-size: 11px;
}

.pjob-h4 {
  margin: var(--space-2) 0 0;
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 600;
}

.pjob-errs {
  display: grid;
  gap: 6px;
}

.pjob-err {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: 6px 10px;
  border-radius: var(--radius-lg);
  background: var(--surface-inset);
  font-size: 12px;
}

.pjob-note {
  padding: 8px 10px;
  border-radius: var(--radius-lg);
  background: var(--surface-inset);
  color: var(--text-secondary);
  font-size: 11.5px;
  line-height: 1.5;
}

.pjob-none {
  margin: 0;
  color: var(--text-muted);
  font-size: 11.5px;
}
</style>
