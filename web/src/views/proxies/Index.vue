<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { storeToRefs } from 'pinia'
import { ElMessageBox } from 'element-plus'

import {
  proxiesApi,
  type ClashNodeTestItem,
  type ClashStatus,
  type ProxyItem,
  type ProxyStrategy,
  type ProxySubscriptionItem,
} from '@/api/client'
import { HlButton, HlDialog, HlIcon, HlSwitch, message } from '@/components/ui'
import { useI18n, type MessageKey } from '@/locales'
import { useProxyTasksStore } from '@/stores/proxyTasks'

/**
 * 代理 IP 池管理 —— 页内分四区：策略卡片网格 / 分区区块 / 节点表格 / 走线控制台。
 * 订阅链接（Clash 机场订阅 / 明文商业代理订阅）长期保存在本地库，按方式区分，可存多条。
 */
const { t } = useI18n()
const proxyTasks = useProxyTasksStore()
const {
  clashTest: clashTestSnap,
  kernelDownloading,
  kernelProgress,
  kernelPhase,
  kernelVia,
  kernelSource,
} = storeToRefs(proxyTasks)
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

// 内核下载进度弹窗（保存订阅缺内核自动下载 / 手动安装共用）：
// 进度数据与轮询在 proxyTasks store（跨页存活、灵动岛同步展示），弹窗是本页 UI
const kernelDialog = ref(false)

/** 发起下载监控：进度与轮询在 store，弹窗是本页 UI，一并打开 */
function beginKernelWatchDialog() {
  kernelDialog.value = true
  proxyTasks.beginKernelWatch()
}

// 下载收场（成功/失败）后弹窗稍候自关：给终态帧留一点可读时间
watch(kernelDownloading, (on, was) => {
  if (was && !on) {
    window.setTimeout(() => {
      if (!kernelDownloading.value) kernelDialog.value = false
    }, 600)
  }
})

// 订阅管理（双方式）
const selectedClashSubId = ref<number | null>(null)
const switchingSub = ref(false)
/** 内核当前跑的订阅 id：radio 重复选它时免打扰，切换失败时回弹锚点 */
const runningSubId = computed(() => {
  const url = clash.value?.subscriptionUrl
  if (!url) return null
  return clashSubs.value.find((s) => s.url === url)?.id ?? null
})

/** 选中即切换：内核在跑 → 热重载切到所选订阅（气泡提示，内核进程不动）；
 *  内核没跑 → 只记录选择，「启动」会直接用它。 */
async function onClashSubChange(sub: ProxySubscriptionItem) {
  if (!clash.value?.running) return
  if (switchingSub.value || sub.id === runningSubId.value) return
  switchingSub.value = true
  try {
    message.loading(t('proxies.clash.switching', { name: sub.label || sub.url }))
    const res = await proxiesApi.clashSwitch(sub.id)
    if (res.switched) {
      message.success(t('proxies.clash.switched', { name: sub.label || sub.url }))
      clash.value = await proxiesApi.clashStatus()
      // 接上切换首检的进度轮询（结果面板自动亮起）
      const snap = await proxiesApi.clashTestProgress()
      if (snap && (snap.phase === 'queued' || snap.phase === 'running')) {
        proxyTasks.adoptTest(snap)
        clashTestExpanded.value = true
      }
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
    if (runningSubId.value != null) selectedClashSubId.value = runningSubId.value
  } finally {
    switchingSub.value = false
  }
}
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
const editAutoRefresh = ref(true)
const editingSub = ref(false)

// Clash 节点检测（后台会话：出口 IP 去重 → 存活 N/M）。会话快照、进度轮询与
// 结果消息都在 proxyTasks store（跨页存活、灵动岛同步展示）；本组件承载面板
// 渲染与展开态
const clashTest = clashTestSnap
const clashTestExpanded = ref(true)
const testingClash = computed(
  () => clashTest.value?.phase === 'queued' || clashTest.value?.phase === 'running',
)
const clashTestPct = computed(() => {
  const s = clashTest.value
  if (!s || !s.toProbe) return 0
  return Math.min(100, Math.round((s.probed / s.toProbe) * 100))
})
const clashTestSkipped = computed(() => clashTest.value?.cooldownSkipped ?? 0)
/** 本次检测归属的订阅（内核启动时选中的那条）——结果面板明示，避免「测的哪条」歧义 */
const clashTestSubLabel = computed(() => {
  const id = clashTest.value?.subscriptionId
  if (id == null) return ''
  const sub = clashSubs.value.find((s) => s.id === id)
  return sub?.label || sub?.url || ''
})

/** 同出口收敛：该出口 IP 的节点折叠到代表行下（展开才显示）。
 *  代表行择优 = 本次检测存活优先 → 未冷却（非账本沿用）优先 → 本次延迟低者
 *  优先，都不可用保持出现序——出口 IP 与 Steam 存活是两个独立探测，「拿到
 *  IP 但 Steam 判死」的节点不得凭出现序占住展示位。 */
interface GroupedTestRow { key: string; primary: ClashNodeTestItem; children: ClashNodeTestItem[] }
function repRank(n: ClashNodeTestItem): [number, number, number] {
  return [n.alive ? 0 : 1, n.cooling ? 1 : 0, n.ms ?? Number.MAX_SAFE_INTEGER]
}
function repRankLess(a: [number, number, number], b: [number, number, number]): boolean {
  return a[0] !== b[0] ? a[0] < b[0] : a[1] !== b[1] ? a[1] < b[1] : a[2] < b[2]
}
const expandedExits = ref(new Set<string>())
const groupedTestNodes = computed<GroupedTestRow[]>(() => {
  const rows: GroupedTestRow[] = []
  const byIp = new Map<string, GroupedTestRow>()
  for (const n of clashTest.value?.nodes ?? []) {
    if (!n.exitIp) {
      rows.push({ key: `solo-${rows.length}`, primary: n, children: [] })
      continue
    }
    const g = byIp.get(n.exitIp)
    if (!g) {
      const fresh = { key: n.exitIp, primary: n, children: [] }
      byIp.set(n.exitIp, fresh)
      rows.push(fresh)
    } else if (repRankLess(repRank(n), repRank(g.primary))) {
      g.children.unshift(g.primary) // 被换下的原代表行排在子行最前
      g.primary = n
    } else {
      g.children.push(n)
    }
  }
  return rows
})
function toggleExitGroup(ip: string) {
  const next = new Set(expandedExits.value)
  if (next.has(ip)) next.delete(ip)
  else next.add(ip)
  expandedExits.value = next
}

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
  if (needKernelWatch) beginKernelWatchDialog()
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
    if (needKernelWatch) proxyTasks.stopKernelWatch()
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
  editAutoRefresh.value = sub.autoRefresh !== false
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
      autoRefresh: editAutoRefresh.value,
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

/* ── 自动维护开关（内核自启 / 自动节点体检）──
   随 GET /proxies 的 strategy 一并下发；undefined（未拉到）按开处理，
   与后端默认值一致。手动「检测节点」走 force 路径，不受体检开关影响。 */
const switchesSaving = ref(false)

async function toggleProxySwitch(field: 'autostart' | 'healthAuto', on: boolean) {
  if (switchesSaving.value) return
  switchesSaving.value = true
  const prev = strategy.value[field]
  strategy.value[field] = on
  try {
    strategy.value = await proxiesApi.setStrategy({ [field]: on } as Partial<ProxyStrategy>)
  } catch {
    strategy.value[field] = prev
    message.error(t('proxies.auto.failed'))
  } finally {
    switchesSaving.value = false
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
  beginKernelWatchDialog()
  try {
    const res = await proxiesApi.clashInstall()
    proxyTasks.stopKernelWatch()
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
    proxyTasks.stopKernelWatch()
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
    if (status.fallbackFrom) {
      // 请求的订阅取不到节点配置，后端已自动换其他订阅拉起内核
      message.warning(
        t('proxies.clash.startedFallback', {
          from: status.fallbackFrom.label ?? `#${status.fallbackFrom.id}`,
          to: status.subscription?.label ?? status.subscription?.id ?? '',
          port: status.port,
        }),
      )
    } else {
      message.success(t('proxies.clash.started', { port: status.port }))
    }
    clash.value = await proxiesApi.clashStatus()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    starting.value = false
  }
}

async function stopClash() {
  await proxiesApi.clashStop()
  proxyTasks.clearTest()
  message.success(t('proxies.clash.hasStopped'))
  clash.value = await proxiesApi.clashStatus()
}

/** 启动检测：后端立即返回会话快照，探测在后台推进；面板展开看逐节点过程 */
async function runClashTest() {
  clashTestExpanded.value = true
  await proxyTasks.startTest()
}

/** 检测收场后的订阅列表刷新：废弃标记是会话快照的派生量，落在页面数据侧 */
watch(
  () => clashTest.value?.phase,
  async (phase, prev) => {
    if (phase === 'done' && prev && prev !== 'done') await load()
  },
)

onMounted(async () => {
  await load()
  void refreshTrafficQuiet() // 实时流量：进页即静默刷新（不阻塞首屏渲染）
  // 进页恢复检测会话：进行中则续上轮询与灵动岛任务位（离开页面检测照常推进），
  // 最近一次结果直接展示；无会话（idle/请求失败）不显示面板
  void proxyTasks.attach()
  // 内核下载进行中（手动安装 / 保存订阅触发）：续上进度弹窗与任务位
  await proxyTasks.attachKernel()
  if (kernelDownloading.value) kernelDialog.value = true
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
          <HlButton
            v-if="clashTest || clash?.running"
            art="outline"
            tone="blue"
            size="sm"
            :disabled="testingClash || !clash?.running"
            :title="clash?.running ? '' : t('proxies.clash.notRunningTitle')"
            @click="runClashTest"
          >
            <span v-if="clashTest && testingClash" class="pxtest-run">
              <span v-if="clashTest.phase === 'running'" class="pxtest-run__bar">
                <span class="pxtest-run__fill" :style="{ width: clashTestPct + '%' }" />
              </span>
              <span class="pxtest-run__label">{{
                clashTest.phase === 'running'
                  ? t('proxies.clash.testingProgress', { done: clashTest.probed, total: clashTest.toProbe ?? 0 })
                  : t('proxies.clash.queued')
              }}</span>
            </span>
            <template v-else>{{ t('proxies.clash.testNodes') }}</template>
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

        <!-- 自动维护开关：内核随服务自启 / 定期节点体检（手动检测不受闸） -->
        <div class="proxyx-clash-row proxyx-clash-switches">
          <HlSwitch
            :model-value="strategy.autostart !== false"
            accent
            :disabled="switchesSaving"
            :label="t('proxies.auto.autostartLabel')"
            :title="t('proxies.auto.autostartHint')"
            @update:model-value="(on: boolean) => toggleProxySwitch('autostart', on)"
          />
          <HlSwitch
            :model-value="strategy.healthAuto !== false"
            accent
            :disabled="switchesSaving"
            :label="t('proxies.auto.healthLabel')"
            :title="t('proxies.auto.healthHint')"
            @update:model-value="(on: boolean) => toggleProxySwitch('healthAuto', on)"
          />
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
              :disabled="sub.deprecated || switchingSub"
              @change="onClashSubChange(sub)"
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
            :disabled="starting || clash?.running || !clash?.kernel.found || clashSubs.length === 0"
            :loading="starting"
            :title="clash?.running ? t('proxies.clash.startRunningTitle') : ''"
            @click="startClash"
          >
            <HlIcon v-if="!starting" name="play" />
            {{ t('proxies.clash.start') }}
          </HlButton>
          <HlButton art="outline" tone="dark" size="sm" :disabled="!clash?.running" @click="stopClash">
            {{ t('proxies.clash.stop') }}
          </HlButton>
          <span class="proxyx-hint">{{
            t(clash?.running ? 'proxies.clash.startHintRunning' : 'proxies.clash.startHint')
          }}</span>
        </div>

        <!-- Clash 节点检测结果（存活=Steam 端点 200；冷却期节点沿用账本状态）。
             收起只折叠展示，后台检测照常推进，再展开继续看逐节点过程 -->
        <div v-if="clashTest && clashTest.phase !== 'idle'" class="proxyx-test-panel">
          <div class="proxyx-test-panel__head">
            <span v-if="clashTestSubLabel" class="tag">
              {{ t('proxies.clash.testSubTag', { name: clashTestSubLabel }) }}
            </span>
            <span v-if="clashTest.phase === 'failed'" class="tag tag--danger">
              {{ t('proxies.clash.testFailed') }}
            </span>
            <span
              v-else-if="clashTest.phase === 'done'"
              class="tag"
              :class="clashTest.alive > 0 ? 'tag--success' : 'tag--danger'"
            >
              {{ t('proxies.clash.aliveTag', { alive: clashTest.alive, total: clashTest.total ?? 0 }) }}
            </span>
            <span v-else class="tag">
              {{ t('proxies.clash.testingProgress', { done: clashTest.probed, total: clashTest.toProbe ?? 0 }) }}
            </span>
            <span v-if="clashTest.phase === 'done' && clashTest.alive < (clashTest.total ?? 0)" class="tag">
              {{ t('proxies.clash.uniqueExits', { n: clashTest.aliveUnique ?? 0 }) }}
            </span>
            <span v-if="clashTestSkipped > 0" class="tag">
              {{ t('proxies.clash.cooldownSkipped', { n: clashTestSkipped }) }}
            </span>
            <span class="proxyx-test-panel__spring" />
            <button class="pxbtn pxbtn--sm" @click="clashTestExpanded = !clashTestExpanded">
              {{ t(clashTestExpanded ? 'proxies.clash.collapse' : 'proxies.clash.expand') }}
            </button>
          </div>
          <div v-if="clashTest.phase === 'failed' && clashTest.error" class="proxyx-hint">{{ clashTest.error }}</div>
          <div v-show="clashTestExpanded" class="proxyx-table-wrap">
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
              <template v-for="g in groupedTestNodes" :key="g.key">
                <tr>
                  <td class="mono">
                    {{ g.primary.name }}
                    <button
                      v-if="g.children.length"
                      class="pxbtn pxbtn--sm proxyx-exit-toggle"
                      @click="toggleExitGroup(g.key)"
                    >
                      {{ t('proxies.clash.sameExitGroup', { n: g.children.length }) }}
                    </button>
                  </td>
                  <td class="mono">{{ g.primary.exitIp ?? '—' }}</td>
                  <td>
                    <span class="tag" :class="g.primary.alive ? 'tag--success' : 'tag--danger'">
                      {{ t(g.primary.alive ? 'proxies.clash.reachable' : 'proxies.clash.unreachable') }}
                    </span>
                  </td>
                  <td>
                    <span v-if="g.primary.cooling" class="tag" :title="t('proxies.clash.coolingTitle')">
                      {{ t('proxies.clash.cooling') }}
                    </span>
                    <span v-else-if="g.primary.alive" class="tag tag--success">{{ t('proxies.clash.healthy') }}</span>
                    <span v-else class="tag tag--danger">{{ t('proxies.clash.unavailable') }}</span>
                  </td>
                  <td class="mono">{{ g.primary.ms !== null ? `${g.primary.ms}ms` : '—' }}</td>
                </tr>
                <tr
                  v-for="c in g.children"
                  v-show="expandedExits.has(g.key)"
                  :key="`${g.key}-${c.name}`"
                  class="proxyx-exit-child"
                >
                  <td class="mono">{{ c.name }}</td>
                  <td class="mono">{{ c.exitIp ?? '—' }}</td>
                  <td>
                    <span class="tag" :class="c.alive ? 'tag--success' : 'tag--danger'">
                      {{ t(c.alive ? 'proxies.clash.reachable' : 'proxies.clash.unreachable') }}
                    </span>
                  </td>
                  <td>
                    <span v-if="c.cooling" class="tag" :title="t('proxies.clash.coolingTitle')">
                      {{ t('proxies.clash.cooling') }}
                    </span>
                    <span v-else-if="c.alive" class="tag tag--success">{{ t('proxies.clash.healthy') }}</span>
                    <span v-else class="tag tag--danger">{{ t('proxies.clash.unavailable') }}</span>
                  </td>
                  <td class="mono">{{ c.ms !== null ? `${c.ms}ms` : '—' }}</td>
                </tr>
              </template>
            </tbody>
          </table>
          </div>
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
        <!-- 自动更新开关：限时订阅（只在窗口内可下载、下载后长期可用）关掉它，
             定时刷新不再每轮撞一次注定失败的抓取；手动重拉照常可用。 -->
        <div class="sed__row">
          <span class="sed__label">{{ t('proxies.sub.autoLabel') }}</span>
          <HlSwitch v-model="editAutoRefresh" accent :label="t('proxies.sub.autoLabel')" />
        </div>
        <p class="sed__tip">{{ t('proxies.sub.autoHint') }}</p>
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

/* 自动维护开关行（内核自启 / 定期体检）：两枚开关横向排布 */
.proxyx-clash-switches {
  margin-top: 10px;
  gap: 22px;
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

/* 检测会话面板：状态标签行 + 收起/展开（收起只折叠展示，探测照常推进） */
.proxyx-test-panel {
  margin-top: 12px;
}

.proxyx-test-panel__head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.proxyx-test-panel__spring {
  flex: 1;
}

/* 检测按钮内进度：细进度条 + 计数（探测是分钟级，只转圈会被当成卡死） */
.pxtest-run {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-width: 110px;
}

.pxtest-run__bar {
  flex: 1;
  height: 4px;
  border-radius: 999px;
  background: var(--border-soft);
  overflow: hidden;
}

.pxtest-run__fill {
  display: block;
  height: 100%;
  border-radius: 999px;
  background: var(--accent);
  transition: width calc(var(--duration-2) * var(--motion-scale)) ease;
}

.pxtest-run__label {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

/* 同出口收敛：子行缩进降调，展开开关贴在主行节点名后 */
.proxyx-exit-toggle {
  margin-left: 6px;
}

.proxyx-exit-child td:first-child {
  padding-left: 24px;
  color: var(--text-muted);
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
</style>
