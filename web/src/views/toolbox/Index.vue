<script setup lang="ts">
import { ref, computed, watch, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { HlButton, HlChip, HlIcon, HlSelect, HlStat, HlTooltip, message } from '@/components/ui'
import { redeemApi, billsApi, type BillImportItem, type BillOverview } from '@/api/client'
import { useAccountStore } from '@/stores/account'
import { useI18n, useLocaleFormat, type MessageKey } from '@/locales'

const router = useRouter()
const { t } = useI18n()
const fmt = useLocaleFormat()

/* ── 多账号切换（激活跟随当前账号；切号即换 Cookie + 配额重计）── */
const accountStore = useAccountStore()
const accountOptions = computed(() =>
  accountStore.accounts.map((a) => ({
    // computed 在渲染期求值，t() 这里取词条是响应式的（切语言即重算）
    label: t('toolbox.account.optionLabel', {
      name: a.persona_name || t('toolbox.account.noNickname'),
      code: a.friend_code,
    }),
    value: a.steam_id,
    icon: 'user',
  })),
)
const activeSteamId = computed(() => accountStore.active?.steam_id ?? '')

async function switchAccount(steamId: string) {
  if (steamId === activeSteamId.value) return
  try {
    await accountStore.setActive(steamId)
    // 切号即重计：会话增量作废，以后端该账号计数为新基数
    usedSession.value = []
    await loadQuota()
    message.success(t('toolbox.account.switched'))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}
onMounted(() => {
  void accountStore.load()
})

/* ───────── CDK 批量激活（真实 Steam 激活端点，登录态 Cookie 通道）───────── */
type KeyStatus = 'wait' | 'doing' | 'ok' | 'own' | 'fail'
interface KeyRow {
  code: string
  status: KeyStatus
}
interface ResultRow {
  /** 对应的激活码原文（结果行与左栏队列按序号一一对应的锚点） */
  code: string
  status: KeyStatus
  detail: string
  subId: string
  subName: string
  /** Steam 返回原文（点开"原文"核对） */
  raw: string
}

const BATCH_SIZE = 9
const BATCH_WAIT = 20000
const ACT_LIMIT = 10
const ACT_WINDOW = 30 * 60 * 1000

const keyArea = ref('')
const keyQueue = ref<KeyRow[]>([])
const resultRows = ref<ResultRow[]>([])
/** 批次进度：「批次 n / N」。**state 只存数字，句子在模板里 t() 现取**——
 *  把渲染好的句子写进 ref（如原 `ref('批次 1 / 1')`）会把语言冻在赋值那一刻，
 *  而本页所有赋值都发生在异步回调里，切语言后挂着的文字不会跟着变。 */
const batchNo = ref(1)
const batchTotal = ref(1)
/** 进度文案同理：只存词条 key，模板 t(progressKey) 现取 */
const progressKey = ref<MessageKey>('toolbox.cdk.progress.ready')
const progressCount = ref('0 / 0')
const progressPct = ref(0)
const ksOk = ref(0)
const ksFail = ref(0)
const ksOwn = ref(0)
const running = ref(false)
const copied = ref(false)
const quotaReady = ref(true)
/** 未绑 Cookie 提示的**词条 key**（'' = 无提示）；显示文案见下面的 quotaMsg */
const quotaMsgKey = ref<'' | MessageKey>('')
const quotaMsg = computed(() => (quotaMsgKey.value ? t(quotaMsgKey.value) : ''))
/** 当前绑定账号（激活计数按账号独立，切号即重计） */
const quotaSteamId = ref('')
/** 账号维度已用次数：后端为准 + 本会话内增量（页内多批激活即时累计） */
const usedBase = ref(0)
const usedSession = ref<number[]>([])
/** 结果行展开原文的索引（-1 = 全收起） */
const rawOpenIdx = ref(-1)

/** 30 分钟窗口内本账号已用激活次数（后端基数 + 页内会话增量） */
const usedCount = computed(() => {
  const now = Date.now()
  // 回调参数原名 t，与 useI18n 的 t 同名会遮蔽；改名以免误用（纯改名）
  const sess = usedSession.value.filter((ts) => now - ts < ACT_WINDOW).length
  return usedBase.value + sess
})
const nearLimit = computed(() => usedCount.value >= ACT_LIMIT - 2)

/** 智能识别激活码：从任意大段文本中提取 5-5-5（及多段）格式密钥，中间连字符不可省略 */
function parseKeys() {
  const text = keyArea.value.trim().toUpperCase()
  const reg = /([0-9A-Z]{5}-){2,4}[0-9A-Z]{5}/g
  const keys: string[] = []
  let m: RegExpExecArray | null
  while ((m = reg.exec(text)) !== null) {
    keys.push(m[0])
  }
  return keys
}

/** 输入即渲染左栏队列 + 右栏等待行 */
function renderKeyQueue() {
  const keys = parseKeys()
  keyQueue.value = keys.map((k) => ({ code: k, status: 'wait' as KeyStatus }))
  resultRows.value = keys.map((k) => ({ code: k, status: 'wait' as KeyStatus, detail: '', subId: '', subName: '', raw: '' }))
  progressCount.value = `0 / ${keys.length}`
  batchNo.value = 1
  batchTotal.value = Math.max(1, Math.ceil(keys.length / BATCH_SIZE))
}
watch(keyArea, renderKeyQueue)

async function loadQuota() {
  try {
    const q = await redeemApi.quota()
    quotaReady.value = q.hasCookie && q.hasSessionId
    quotaMsgKey.value = quotaReady.value ? '' : 'toolbox.cdk.quotaMissing'
    // 换绑账号 → 会话增量作废、以后端计数为新基数
    if (q.steamId !== quotaSteamId.value) {
      quotaSteamId.value = q.steamId
      usedSession.value = []
    }
    usedBase.value = q.used
  } catch {
    quotaMsgKey.value = ''
  }
}
onMounted(() => {
  renderKeyQueue()
  void loadQuota()
})

/** 队列状态 → 词条 key（常量表存 key 不存译文：模块级常量只求值一次，会把语言冻住） */
const QUEUE_LABEL_KEY: Record<KeyStatus, MessageKey> = {
  wait: 'toolbox.cdk.status.wait',
  doing: 'toolbox.cdk.status.doing',
  ok: 'toolbox.cdk.status.queueOk',
  own: 'toolbox.cdk.status.own',
  fail: 'toolbox.cdk.status.fail',
}

function queueLabel(s: KeyStatus): string {
  return t(QUEUE_LABEL_KEY[s])
}

/** 真实激活：同批单码并发提交（9 个 ajax 齐发，回执即到即渲染），
 *  批间 20s（Steam 30 分钟 10 次限制节奏，账号计数前端驱动） */
async function startRedeem() {
  if (running.value) return
  const keys = parseKeys()
  if (keys.length === 0) return
  if (!quotaReady.value) {
    progressKey.value = quotaMsgKey.value || 'toolbox.cdk.progress.noCookie'
    message.warning(t('toolbox.cdk.bindFirst'))
    return
  }
  const available = ACT_LIMIT - usedCount.value
  if (available <= 0) {
    progressKey.value = 'toolbox.cdk.progress.limitReached'
    return
  }
  const toActivate = Math.min(keys.length, available)
  running.value = true
  progressPct.value = 0
  progressKey.value = 'toolbox.cdk.progress.running'
  let done = 0
  const totalBatches = Math.ceil(toActivate / BATCH_SIZE)

  for (let b = 0; b < totalBatches; b++) {
    const start = b * BATCH_SIZE
    const batch = keys.slice(start, start + BATCH_SIZE)
    batchNo.value = b + 1
    batchTotal.value = totalBatches

    // 每个码一个独立请求并发发出——谁先回来谁先上屏，不等整批
    const tasks = batch.map(async (code, i) => {
      const qi = start + i
      if (keyQueue.value[qi]) keyQueue.value[qi]!.status = 'doing'
      if (resultRows.value[qi]) resultRows.value[qi]!.status = 'doing'
      let r: { status: string; detail: string; subId: string; subName: string; raw?: string }
      try {
        const res = await redeemApi.activateKeys([code])
        r = res.results[0] ?? {
          status: 'fail',
          // 事件回调里取词条：这一行是本次激活的即时回执，不跨语言切换长驻
          detail: t('toolbox.cdk.failNoResult'),
          subId: '',
          subName: '',
          raw: '',
        }
      } catch (e) {
        r = { status: 'fail', detail: e instanceof Error ? e.message : String(e), subId: '', subName: '', raw: '' }
      }
      const st = (r.status === 'ok' || r.status === 'own' ? r.status : 'fail') as KeyStatus
      usedSession.value.push(Date.now())
      if (keyQueue.value[qi]) keyQueue.value[qi]!.status = st
      if (resultRows.value[qi]) {
        resultRows.value[qi] = {
          code,
          status: st,
          detail: r.detail || '——',
          subId: r.subId || '',
          subName: r.subName || '',
          raw: r.raw || '',
        }
      }
      if (st === 'ok') ksOk.value++
      else if (st === 'own') ksOwn.value++
      else ksFail.value++
      done++
      progressPct.value = Math.round((done / toActivate) * 100)
      progressCount.value = `${done} / ${toActivate}`
    })
    await Promise.all(tasks)

    if (b < totalBatches - 1) {
      progressKey.value = 'toolbox.cdk.progress.batchWait'
      await new Promise((r) => setTimeout(r, BATCH_WAIT))
    }
  }
  progressKey.value = 'toolbox.cdk.progress.done'
  running.value = false

  /* 完成汇总气泡：全部成功 / 部分失败 / 全失败 三档。
     原先的「主句 + 拼接后缀」按中英语序拼不出来，故每档各成一条参数化词条
     （带 skipped 的变体单列，见 zh-CN/toolbox.ts 的 done.*）。 */
  const skipped = keys.length - toActivate
  const params = { ok: ksOk.value, own: ksOwn.value, fail: ksFail.value, skipped }
  if (ksFail.value === 0) {
    message.success(t(skipped > 0 ? 'toolbox.cdk.done.okSkipped' : 'toolbox.cdk.done.ok', params))
  } else if (ksOk.value + ksOwn.value > 0) {
    message.warning(
      t(skipped > 0 ? 'toolbox.cdk.done.partialSkipped' : 'toolbox.cdk.done.partial', params),
    )
  } else {
    message.error(
      t(skipped > 0 ? 'toolbox.cdk.done.allFailedSkipped' : 'toolbox.cdk.done.allFailed', params),
    )
  }
}

function resetRedeem() {
  usedSession.value = []
  keyQueue.value = []
  resultRows.value = []
  rawOpenIdx.value = -1
  progressPct.value = 0
  progressKey.value = 'toolbox.cdk.progress.ready'
  ksOk.value = 0
  ksFail.value = 0
  ksOwn.value = 0
  running.value = false
  copied.value = false
  void loadQuota()
  renderKeyQueue()
}

/** 复制所有 SubID + 版本名（一行一个） */
function copyResults() {
  const lines = resultRows.value
    .filter((r) => r.subId && r.subId !== '0')
    .map((r) => `${r.subId} ${r.subName}`)
  if (!lines.length) return
  navigator.clipboard?.writeText(lines.join('\n'))
  copied.value = true
  window.setTimeout(() => (copied.value = false), 1500)
}

/* ───────── 账户消费账单（真实 bills 域数据）───────── */
const billFilter = ref('all')
const billLoading = ref(true)
const billEmpty = ref(false)
const billImports = ref<BillImportItem[]>([])
const activeImportId = ref<number | null>(null)
const billOverview = ref<BillOverview | null>(null)

type BillCategory = 'refund' | 'gift' | 'market' | 'wallet' | 'game'

interface BillRow {
  date: string
  item: string
  /** 类型列的词条 key。**行数据只存 key 不存译文**：loadBillRows 一次性加工整表，
   *  成品译文会把语言冻在加载那一刻（切语言表格不跟着变）。 */
  typeKey: MessageKey
  typeClass: string
  amount: string
  amountClass: string
  category: BillCategory
}

/** 交易类型 → 词条 key（同上：常量表存 key 不存文案） */
const BILL_TYPE_KEY: Record<BillCategory, MessageKey> = {
  game: 'toolbox.bill.type.game',
  wallet: 'toolbox.bill.type.wallet',
  market: 'toolbox.bill.type.market',
  gift: 'toolbox.bill.type.gift',
  refund: 'toolbox.bill.type.refund',
}

/** 汇率快照（bills 域 cnyFen 已按当日汇率折算，直接展示）。
 *  千分位/小数位随语言（zh-CN 与 en-US 输出一致，见 locales/format.ts） */
function fenToYuan(fen: number | null): string {
  if (fen === null) return '—'
  return `¥${fmt.money(fen / 100)}`
}

async function loadBills() {
  billLoading.value = true
  billEmpty.value = false
  try {
    const res = await billsApi.listImports()
    billImports.value = res.imports
    if (!res.imports.length) {
      billEmpty.value = true
      activeImportId.value = null
      billOverview.value = null
      return
    }
    activeImportId.value = res.imports[0]!.id
    billOverview.value = await billsApi.overview(res.imports[0]!.id)
    void loadBillRows()
  } catch (e) {
    billEmpty.value = true
    message.error(
      t('toolbox.bill.loadFailed', { msg: e instanceof Error ? e.message : String(e) }),
    )
  } finally {
    billLoading.value = false
  }
}

/** 分类汇总（mini-stats + 类型占比），从 overview 实时算 */
const billRows = ref<BillRow[]>([])
const billFiltered = ref<BillRow[]>([])

async function loadBillRows() {
  if (!activeImportId.value) return
  /* 摘要模块只取最新 5 条（后端 date.desc 排序），全量在 /bills 看 */
  const res = await billsApi.gameTxs(activeImportId.value, { limit: 5 })
  const rows: BillRow[] = res.rows.map((tx) => {
    const category: BillCategory = tx.isRefund
      ? 'refund'
      : tx.isGift
        ? 'gift'
        : tx.txType === 'market'
          ? 'market'
          : tx.txType === 'wallet'
            ? 'wallet'
            : 'game'
    const typeClass = tx.isRefund
      ? 'bill-type bill-type--refund'
      : tx.isGift
        ? 'bill-type bill-type--gift'
        : tx.txType === 'wallet'
          ? 'bill-type bill-type--wallet'
          : tx.txType === 'market'
            ? 'bill-type bill-type--market'
            : 'bill-type'
    const amount = `${tx.sign < 0 ? '+' : '-'}${fenToYuan(tx.cnyFen)}`
    const amountClass = tx.sign < 0 ? 'bill-amt bill-amt--in' : 'bill-amt bill-amt--out'
    return {
      date: tx.date.slice(0, 10),
      item: tx.items.join(' + ') || '—',
      typeKey: BILL_TYPE_KEY[category],
      typeClass,
      amount,
      amountClass,
      category,
    }
  })
  billRows.value = rows
  filterBill(billFilter.value)
}

function filterBill(cat: string) {
  billFilter.value = cat
  billFiltered.value = cat === 'all' ? billRows.value : billRows.value.filter((r) => r.category === cat)
}

/** mini-stats：从 overview summary 实时算（净消费/游戏数/本月/退款） */
const billStats = computed(() => {
  const ov = billOverview.value
  if (!ov) return null
  const s = ov.summary
  const now = new Date()
  const monthKey = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
  const thisMonth = ov.monthSeries.find((m) => m.month.startsWith(monthKey))
  return {
    net: fenToYuan(s.netFen),
    games: ov.counts.gameTxs,
    month: thisMonth ? fenToYuan(Math.abs(thisMonth.netFen)) : '¥0.00',
    refunds: `${ov.counts.gameTxs > 0 ? Math.round((s.refundFen / Math.max(s.spendFen, 1)) * 1000) / 10 : 0}%`,
  }
})

/** 近 8 月消费趋势柱（monthSeries 尾部 8 个） */
const billTrend = computed(() => {
  const ov = billOverview.value
  if (!ov) return []
  const series = ov.monthSeries.slice(-8)
  const max = Math.max(...series.map((m) => Math.abs(m.netFen)), 1)
  return series.map((m) => ({
    label: t('toolbox.bill.trend.monthLabel', { month: parseInt(m.month.slice(5, 7), 10) }),
    value: fenToYuan(Math.abs(m.netFen)),
    pct: Math.max(6, Math.round((Math.abs(m.netFen) / max) * 100)),
    peak: Math.abs(m.netFen) === max,
  }))
})

/** 消费类型占比（礼物/退款外的正向消费分类） */
const billRatios = computed(() => {
  const ov = billOverview.value
  if (!ov) return []
  const s = ov.summary
  const self = Math.max(s.selfNetFen, 0)
  const gift = Math.max(s.giftNetFen, 0)
  const topup = Math.max(s.topupSpendFen, 0)
  const total = Math.max(self + gift + topup, 1)
  const parts = [
    { name: t('toolbox.bill.type.game'), fen: self, color: 'var(--accent)' },
    { name: t('toolbox.bill.type.wallet'), fen: topup, color: 'var(--warning)' },
    { name: t('toolbox.bill.type.giftShort'), fen: gift, color: '#ec4899' },
  ]
  return parts.map((p) => ({
    ...p,
    pct: Math.round((p.fen / total) * 100),
  }))
})

async function switchImport(id: number) {
  if (activeImportId.value === id || billLoading.value) return
  activeImportId.value = id
  billOverview.value = await billsApi.overview(id)
  await loadBillRows()
}

function goBillsPage() {
  void router.push('/bills')
}
function goSettingsPage() {
  void router.push('/settings')
}

onMounted(() => void loadBills())
</script>

<template>
  <section class="toolbox-page">
    <!-- 账户消费账单 -->
    <div class="fx-module" data-section="toolbox.section.bill">
      <div class="fx-module__head">
        <div class="fx-module__icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 6h18v12H3z"/><path d="M3 10h18M7 15h3"/></svg>
        </div>
        <div>
          <div class="fx-module__title">{{ t('toolbox.section.bill') }}</div>
          <div class="fx-module__sub">{{ t('toolbox.bill.subtitle') }}</div>
        </div>
      </div>

      <!-- 未导入账单档案：诚实空态引导 -->
      <div v-if="billLoading" class="fx-empty-mini" style="margin: 8px 0 4px">{{ t('toolbox.bill.loading') }}</div>
      <div v-else-if="billEmpty || !billOverview" class="bill-empty">
        <div style="font-size: 13px; color: var(--text-secondary)">{{ t('toolbox.bill.empty.title') }}</div>
        <div style="font-size: 11.5px; color: var(--text-dim); margin-top: 4px">
          {{ t('toolbox.bill.empty.hint') }}
        </div>
        <HlButton size="sm" variant="primary" style="margin-top: 10px" @click="goBillsPage">{{ t('toolbox.bill.empty.action') }}</HlButton>
      </div>

      <template v-else>
      <!-- 多档案切换（最近导入在前） -->
      <div v-if="billImports.length > 1" class="bill-chips" style="margin-bottom: 10px">
        <HlChip
          v-for="imp in billImports"
          :key="imp.id"
          :on="activeImportId === imp.id"
          @click="switchImport(imp.id)"
        >{{ t('toolbox.bill.importChip', { nick: imp.nickname, amount: (imp.gameNetFen / 100).toFixed(0) }) }}</HlChip>
      </div>

      <div class="fx-grid--4" style="margin-bottom: 14px">
        <HlStat :label="t('toolbox.bill.stats.net')" :value="billStats?.net ?? '—'" />
        <HlStat :label="t('toolbox.bill.stats.txCount')">
          {{ billOverview.counts.gameTxs }} <span class="unit">{{ t('toolbox.bill.stats.txUnit') }}</span>
        </HlStat>
        <HlStat tone="warn" :label="t('toolbox.bill.stats.month')" :value="billStats?.month ?? '—'" />
        <HlStat tone="good" :label="t('toolbox.bill.stats.refund')" :value="fenToYuan(billOverview.summary.refundFen)" />
      </div>
      <div class="fx-grid">
        <div>
          <div class="bill-chips">
            <HlChip :on="billFilter === 'all'" @click="filterBill('all')">{{ t('common.all') }}</HlChip>
            <HlChip :on="billFilter === 'game'" @click="filterBill('game')">{{ t('toolbox.bill.type.game') }}</HlChip>
            <HlChip :on="billFilter === 'wallet'" @click="filterBill('wallet')">{{ t('toolbox.bill.type.wallet') }}</HlChip>
            <HlChip :on="billFilter === 'gift'" @click="filterBill('gift')">{{ t('toolbox.bill.type.giftShort') }}</HlChip>
            <HlChip :on="billFilter === 'refund'" @click="filterBill('refund')">{{ t('toolbox.bill.type.refund') }}</HlChip>
          </div>
          <table class="bill-tbl">
            <thead><tr><th>{{ t('toolbox.bill.table.date') }}</th><th>{{ t('toolbox.bill.table.item') }}</th><th>{{ t('toolbox.bill.table.type') }}</th><th style="text-align:right">{{ t('toolbox.bill.table.amount') }}</th></tr></thead>
            <tbody>
              <tr v-for="(row, i) in billFiltered" :key="i">
                <td class="mono" style="color: var(--text-dim)">{{ row.date }}</td>
                <!-- 摘要里长名省略，悬停气泡看全文；完整账单页不省略 -->
                <td class="bill-item">
                  <HlTooltip :content="row.item" wide>
                    <span class="bill-item__text">{{ row.item }}</span>
                  </HlTooltip>
                </td>
                <td><span :class="row.typeClass">{{ t(row.typeKey) }}</span></td>
                <td :class="row.amountClass">{{ row.amount }}</td>
              </tr>
            </tbody>
          </table>
          <div v-if="billFiltered.length === 0" class="fx-empty-mini" style="margin-top: 8px">{{ t('toolbox.bill.table.noRows') }}</div>
          <router-link v-if="billOverview.counts.gameTxs > 5" to="/bills" class="bill-more">
            {{ t('toolbox.bill.viewAll') }} <HlIcon name="chevron-right" :size="13" />
          </router-link>
        </div>
        <div>
          <div class="fx-sub">{{ t('toolbox.bill.trend.title') }}</div>
          <div class="mini-bars">
            <div v-for="m in billTrend" :key="m.label" class="mini-bars__col">
              <span class="mini-bars__lbl">{{ m.value }}</span>
              <div class="mini-bars__bar" :class="{ 'mini-bars__bar--peak': m.peak }" :style="{ height: m.pct + '%' }"></div>
              <span class="mini-bars__lbl">{{ m.label }}</span>
            </div>
            <div v-if="billTrend.length === 0" class="fx-empty-mini">{{ t('toolbox.bill.trend.empty') }}</div>
          </div>
          <div class="fx-sub">{{ t('toolbox.bill.ratio.title') }}</div>
          <div v-for="p in billRatios" :key="p.name" class="ratio-row">
            <span class="ratio-row__name">{{ p.name }}</span>
            <span class="ratio-row__track"><span class="ratio-row__fill" :style="{ width: p.pct + '%', background: p.color }"></span></span>
            <span class="ratio-row__val">{{ p.pct }}%</span>
          </div>
        </div>
      </div>
      </template>
    </div>

    <!-- ═════ 模块 B：CDK 批量激活（独立整行 · 内部左右分栏，1:1 框架移植）═════ -->
    <div class="fx-module" style="margin-bottom: 0" data-section="toolbox.section.cdk">
      <div class="fx-module__head">
        <div class="fx-module__icon">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 2l-2 2m-7.6 7.6a5 5 0 0 1-7.07 7.07 5 5 0 0 1 7.07-7.07zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/></svg>
        </div>
        <div>
          <div class="fx-module__title">{{ t('toolbox.section.cdk') }}</div>
          <div class="fx-module__sub">{{ t('toolbox.cdk.subtitle') }}</div>
        </div>
        <!-- 账号切换：激活/免费领取跟随当前账号，切号即换 Cookie + 配额重计 -->
        <HlSelect
          v-if="accountOptions.length"
          v-model="activeSteamId"
          :options="accountOptions"
          style="margin-left: auto; min-width: 210px"
          @update:model-value="switchAccount"
        />
        <span v-else-if="quotaMsg" class="cdk-batch-indicator is-waiting" style="margin-left: auto">{{ quotaMsg }}</span>
      </div>
      <!-- 未绑 Cookie 引导 -->
      <div v-if="!quotaReady" class="bill-empty" style="margin-bottom: 10px">
        <div style="font-size: 12.5px; color: var(--text-secondary)">{{ t('toolbox.cdk.needCookie.title') }}</div>
        <div style="font-size: 11px; color: var(--text-dim); margin-top: 3px">
          {{ t('toolbox.cdk.needCookie.hint') }}
        </div>
        <HlButton size="sm" variant="primary" style="margin-top: 8px" @click="goSettingsPage">{{ t('toolbox.cdk.needCookie.action') }}</HlButton>
      </div>
      <div class="cdk-split">
        <!-- 左栏：激活码 -->
        <div class="cdk-left">
          <div class="cdk-section-title">{{ t('toolbox.cdk.input.title') }}</div>
          <textarea v-model="keyArea" class="key-area" spellcheck="false" style="min-height: 120px" :placeholder="t('toolbox.cdk.input.placeholder')"></textarea>
          <div class="key-toolbar">
            <HlButton art="outline" tone="green" size="sm" :disabled="running" :loading="running" @click="startRedeem">
              <HlIcon v-if="!running" name="play" />
              {{ t('toolbox.cdk.start') }}
            </HlButton>
            <HlButton art="outline" tone="dark" size="sm" @click="resetRedeem">{{ t('toolbox.cdk.reset') }}</HlButton>
            <span class="cdk-batch-indicator" style="margin-left: auto">{{ t('toolbox.cdk.batch.label', { current: batchNo, total: batchTotal }) }}</span>
          </div>
          <div class="key-progress"><i :style="{ width: progressPct + '%' }"></i></div>
          <div style="font-size: 11px; color: var(--text-muted); display: flex; justify-content: space-between">
            <span>{{ t(progressKey) }}</span><span class="mono">{{ progressCount }}</span>
          </div>
          <div class="cdk-limit-bar">
            <svg class="cdk-limit-icon" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
            <span class="cdk-limit-text">{{ t('toolbox.cdk.limit.hint') }}</span>
            <span class="cdk-limit-count" :class="{ 'near-limit': nearLimit }">{{ t('toolbox.cdk.limit.used', { used: usedCount, total: ACT_LIMIT }) }}</span>
          </div>
          <div class="cdk-section-title" style="margin-top: 4px">{{ t('toolbox.cdk.queue.title') }}</div>
          <div class="key-queue">
            <div v-for="(row, i) in keyQueue" :key="i" class="key-row">
              <span class="key-row__idx">{{ i + 1 }}</span>
              <span class="key-row__code">{{ row.code }}</span>
              <span class="key-st" :class="'key-st--' + row.status">{{ queueLabel(row.status) }}</span>
            </div>
            <div v-if="keyQueue.length === 0" class="cdk-sub-empty" style="padding: 14px; text-align: center">
              {{ t('toolbox.cdk.queue.empty') }}
            </div>
          </div>
        </div>
        <!-- 右栏：激活结果 -->
        <div class="cdk-right">
          <div class="cdk-result-head">
            <div class="cdk-section-title" style="margin-bottom: 0">{{ t('toolbox.cdk.result.title') }}</div>
            <button
              type="button"
              class="cdk-copy-btn"
              :class="{ copied }"
              :title="t('toolbox.cdk.result.copyTitle')"
              @click="copyResults"
            >
              <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            </button>
          </div>
          <div class="key-summary" style="margin-bottom: 0">
            <div class="key-cell"><div class="key-cell__n" style="color: var(--success)">{{ ksOk }}</div><div class="key-cell__t">{{ t('toolbox.cdk.status.ok') }}</div></div>
            <div class="key-cell"><div class="key-cell__n" style="color: var(--danger)">{{ ksFail }}</div><div class="key-cell__t">{{ t('toolbox.cdk.summary.fail') }}</div></div>
            <div class="key-cell"><div class="key-cell__n" style="color: var(--warning)">{{ ksOwn }}</div><div class="key-cell__t">{{ t('toolbox.cdk.summary.own') }}</div></div>
          </div>
          <div class="cdk-result-scroll">
            <table class="cdk-result-table">
              <thead>
                <tr><th class="cdk-col-idx">{{ t('toolbox.cdk.result.col.index') }}</th><th>CDK</th><th class="cdk-col-result">{{ t('toolbox.cdk.result.col.result') }}</th><th>{{ t('toolbox.cdk.result.col.detail') }}</th><th class="cdk-col-sub">SubID</th><th>{{ t('toolbox.cdk.result.col.game') }}</th></tr>
              </thead>
              <tbody>
                <template v-for="(r, i) in resultRows" :key="i">
                  <tr
                    :class="r.status === 'wait' ? 'is-wait' : r.status === 'doing' ? 'is-doing' : 'is-done ' + 'is-' + r.status"
                  >
                    <td class="cdk-col-idx"><span class="cdk-idx">{{ i + 1 }}</span></td>
                    <td class="cdk-col-code">{{ r.code }}</td>
                    <td class="cdk-col-result">
                      <span class="cdk-result-cell">
                        <!-- 三态徽章：图标 + 语义色，区别于原脚本的纯文本红绿字 -->
                        <span v-if="r.status === 'ok'" class="cdk-badge cdk-badge--ok">
                          <HlIcon name="check-circle" :size="13" />{{ t('toolbox.cdk.status.ok') }}
                        </span>
                        <span v-else-if="r.status === 'own'" class="cdk-badge cdk-badge--own">
                          <HlIcon name="info" :size="13" />{{ t('toolbox.cdk.status.own') }}
                        </span>
                        <span v-else-if="r.status === 'fail'" class="cdk-badge cdk-badge--fail">
                          <HlIcon name="x-circle" :size="13" />{{ t('toolbox.cdk.status.fail') }}
                        </span>
                        <span v-else-if="r.status === 'doing'" class="cdk-badge cdk-badge--doing">
                          <span class="cdk-badge__pulse" />{{ t('toolbox.cdk.status.doing') }}
                        </span>
                        <span v-else class="cdk-badge cdk-badge--wait">{{ t('toolbox.cdk.status.wait') }}</span>
                        <!-- 原文按钮：仅已完成 + 有回执时出现（图标按钮，title 提示） -->
                        <button
                          v-if="r.raw && (r.status === 'ok' || r.status === 'own' || r.status === 'fail')"
                          type="button"
                          class="cdk-raw-toggle"
                          :class="{ 'is-open': rawOpenIdx === i }"
                          :title="t(rawOpenIdx === i ? 'toolbox.cdk.raw.collapse' : 'toolbox.cdk.raw.expand')"
                          @click="rawOpenIdx = rawOpenIdx === i ? -1 : i"
                        ><HlIcon name="code" :size="12" /></button>
                      </span>
                    </td>
                    <td class="cdk-detail-cell">
                      <!-- 徽章已表达结果语义，详情列只放增量信息：失败原因 / 原文展开 -->
                      <template v-if="r.status === 'doing'">—</template>
                      <span v-else-if="r.status === 'fail' && r.detail" :class="'cdk-detail cdk-detail--fail'">{{ r.detail }}</span>
                      <span v-else class="cdk-sub-empty">—</span>
                      <pre v-if="rawOpenIdx === i && r.raw" class="cdk-raw">{{ r.raw }}</pre>
                    </td>
                    <td class="cdk-col-sub">
                      <a v-if="r.subId && r.subId !== '0'" class="cdk-sub-id" :href="`https://steamdb.info/sub/${r.subId}/`" target="_blank" rel="noopener noreferrer">{{ r.subId }}</a>
                      <span v-else class="cdk-sub-empty">—</span>
                    </td>
                    <td>
                      <span v-if="r.subName">{{ r.subName }}</span>
                      <span v-else class="cdk-sub-empty">—</span>
                    </td>
                  </tr>
                </template>
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>

  </section>
</template>

<style scoped>
.toolbox-page {
  max-width: 1100px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 账单/激活的诚实空态引导 */
.bill-empty {
  text-align: center;
  padding: 22px 16px;
  border: 1px dashed var(--border-soft);
  border-radius: var(--radius);
  background: var(--surface-inset);
}

.unit {
  font-size: 12px;
  color: var(--text-dim);
}

.key-hint {
  font-size: 11px;
  color: var(--text-dim);
  margin-left: auto;
}

.key-progress-meta {
  font-size: 11px;
  color: var(--text-muted);
  display: flex;
  justify-content: space-between;
}
</style>

<style scoped>
/* 未来模块容器 */
.fx-module {
  background: var(--bg-card);
  border: 1px solid var(--line-1);
  border-radius: var(--radius-lg);
  padding: 18px;
  margin-bottom: 18px;
  box-shadow: var(--shadow-sm);
}
.fx-module__head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 4px;
  flex-wrap: wrap;
}
.fx-module__icon {
  width: 34px;
  height: 34px;
  border-radius: var(--radius);
  display: grid;
  place-items: center;
  background: var(--accent-a15);
  color: var(--accent);
  flex-shrink: 0;
}
.fx-module__icon svg { width: 19px; height: 19px; }
.fx-module__title { font-size: 15px; font-weight: 700; color: var(--text-primary); }
.fx-module__sub { font-size: 12px; color: var(--text-muted); margin: 2px 0 14px 44px; }
.fx-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
.fx-grid--4 { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
@media (max-width: 1100px) { .fx-grid, .fx-grid--4 { grid-template-columns: 1fr; } }
.fx-sub { font-size: 12px; font-weight: 600; color: var(--text-secondary); margin: 14px 0 8px; display: flex; align-items: center; gap: 7px; }
.fx-sub::before { content: ''; width: 3px; height: 12px; border-radius: 2px; background: var(--accent-fill); }

/* KPI 卡本体走 components/ui/HlStat.vue（原 .fx-mini-stat 六行定义删除——它是
   同一个类名在全仓库的第三份副本，三份里两份左对齐一份居中）。 */

/* 消费账单 */
/* `.bill-chip` 三行曾在这里（本仓库该类的**第三份**副本；另两份在 tabs-shared.css 与
   FamLicense 侧。三份里这一份还漏了 `font-family: inherit`，于是同一个 chip 在
   toolbox 与 family 下字体不同）。chip 本体已统一到 components/ui/HlChip.vue，
   此处只留布局。 */
.bill-chips { display: flex; gap: 7px; flex-wrap: wrap; margin-bottom: 12px; }
.bill-tbl { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.bill-tbl th { text-align: left; font-weight: 600; color: var(--text-muted); font-size: 11px; padding: 7px 9px; border-bottom: 1px solid var(--line-2); }
.bill-tbl td { padding: 9px; border-bottom: 1px solid var(--row-border); color: var(--text-secondary); }
.bill-tbl tbody tr:hover { background: var(--hover-soft); }
.bill-amt { font-family: var(--font-mono); font-weight: 600; text-align: right; white-space: nowrap; }
.bill-amt--out { color: var(--danger); }
.bill-amt--in { color: var(--rate-good); }
.bill-type { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--accent-a15); color: var(--accent); white-space: nowrap; }
.bill-type--refund { background: var(--success-a15); color: var(--success); }
.bill-type--wallet { background: color-mix(in srgb, var(--warning) 16%, transparent); color: var(--warning); }
.bill-type--market { background: var(--surface-chip-2); color: var(--text-secondary); }
.bill-type--gift { background: color-mix(in srgb, #ec4899 15%, transparent); color: #ec4899; }
/* 「查看全部」尾链：独立页入口，摘要模块不承载全量 */
.bill-more { display: inline-flex; align-items: center; gap: 3px; margin-top: 9px; font-size: 12px; color: var(--accent); }
.bill-more:hover { text-decoration: underline; }
/* 摘要长名省略：单行截断 + 悬停气泡全文（td 作 tooltip 定位锚点） */
.bill-item { max-width: 240px; }
.bill-item .hl-tip-host { display: block; }
.bill-item__text { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.mini-bars { display: flex; align-items: flex-end; gap: 7px; height: 110px; padding-top: 8px; }
.mini-bars__col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 5px; height: 100%; justify-content: flex-end; }
.mini-bars__bar { width: 100%; max-width: 26px; border-radius: 4px 4px 0 0; background: linear-gradient(180deg, var(--accent-fill), var(--accent)); transition: height 0.8s var(--ease-out); position: relative; }
.mini-bars__bar:hover { filter: brightness(1.12); }
.mini-bars__bar--peak { background: linear-gradient(180deg, var(--warning), color-mix(in srgb, var(--warning) 70%, var(--accent))); }
.mini-bars__lbl { font-size: 10px; color: var(--text-dim); font-family: var(--font-mono); }

.ratio-row { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; font-size: 12px; }
.ratio-row__name { width: 64px; color: var(--text-secondary); flex-shrink: 0; }
.ratio-row__track { flex: 1; height: 8px; border-radius: 999px; background: var(--surface-track); overflow: hidden; }
.ratio-row__fill { height: 100%; border-radius: 999px; transition: width 0.9s var(--ease-out); }
.ratio-row__val { width: 52px; text-align: right; font-family: var(--font-mono); color: var(--text-muted); font-size: 11px; flex-shrink: 0; }

/* CDK 批量激活 */
.key-area { width: 100%; min-height: 96px; resize: vertical; background: var(--input-bg); border: 1px solid var(--border-soft); border-radius: var(--radius); padding: 10px 12px; font-family: var(--font-mono); font-size: 12px; color: var(--text-primary); line-height: 1.7; transition: var(--transition); }
.key-area:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-a15); }
.key-toolbar { display: flex; align-items: center; gap: 9px; margin: 11px 0; flex-wrap: wrap; }
.key-queue { max-height: 230px; overflow: auto; border: 1px solid var(--line-1); border-radius: var(--radius); background: var(--surface-inset); }
.key-row { display: flex; align-items: center; gap: 9px; padding: 7px 11px; font-size: 12px; border-bottom: 1px solid var(--row-border); font-family: var(--font-mono); }
.key-row:last-child { border-bottom: none; }
.key-row__idx { color: var(--text-dim); font-size: 10.5px; min-width: 16px; text-align: right; flex-shrink: 0; }
.key-row__code { color: var(--text-secondary); letter-spacing: 0.4px; }
.key-row__game { font-family: var(--font-sans); color: var(--text-muted); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.key-st { font-size: 11px; padding: 2px 9px; border-radius: 999px; white-space: nowrap; display: inline-flex; align-items: center; gap: 4px; font-family: var(--font-sans); }
.key-st::before { content: ''; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.key-st--wait { background: var(--surface-chip-2); color: var(--text-dim); }
.key-st--doing { background: var(--accent-a15); color: var(--accent); }
.key-st--ok { background: var(--success-a15); color: var(--success); }
.key-st--fail { background: var(--danger-a15); color: var(--danger); }
.key-st--own { background: color-mix(in srgb, var(--warning) 18%, transparent); color: var(--warning); }
.key-progress { height: 8px; border-radius: 999px; background: var(--surface-track); overflow: hidden; margin: 10px 0 6px; }
.key-progress i { display: block; height: 100%; border-radius: 999px; background: linear-gradient(90deg, var(--accent), var(--accent-fill)); transition: width 0.4s linear; width: 0; }
.key-summary { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; margin-top: 11px; }
.key-cell { border-radius: var(--radius); padding: 8px 10px; background: var(--surface-inset); border: 1px solid var(--line-1); text-align: center; }
.key-cell__n { font-size: 17px; font-weight: 700; font-family: var(--font-mono); }
.key-cell__t { font-size: 10.5px; color: var(--text-muted); margin-top: 2px; }

/* ── CDK 批量激活（框架 cdk-split 标准布局）── */
.cdk-split { display: grid; grid-template-columns: 38% 1fr; gap: 14px; }
.cdk-left { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.cdk-right { display: flex; flex-direction: column; gap: 8px; min-width: 0; }
.cdk-limit-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--surface-chip-2);
  border: 1px solid var(--line-1);
  border-radius: 6px;
  font-size: 10.5px;
}
.cdk-limit-bar .cdk-limit-icon { color: var(--warning); flex-shrink: 0; }
.cdk-limit-bar .cdk-limit-text { color: var(--text-secondary); flex: 1; }
.cdk-limit-bar .cdk-limit-count { font-family: var(--font-mono); font-weight: 700; color: var(--text-primary); }
.cdk-result-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 4px; }
.cdk-copy-btn {
  width: 30px;
  height: 30px;
  border-radius: 6px;
  border: 1px solid var(--border-soft);
  background: var(--surface-chip);
  color: var(--text-secondary);
  cursor: pointer;
  display: grid;
  place-items: center;
  transition: var(--transition);
  flex-shrink: 0;
  font-size: 13px;
}
.cdk-copy-btn:hover { border-color: var(--accent); color: var(--accent); background: var(--accent-a10); }
.cdk-copy-btn.copied { border-color: var(--success); color: var(--success); background: var(--success-a15); }
.cdk-result-scroll { max-height: 320px; overflow-y: auto; border: 1px solid var(--line-1); border-radius: 6px; }

/* ── 结果三态徽章（个性化设计：胶囊 + 图标 + 左侧色条，非原脚本纯文本红绿字）── */
.cdk-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 3px 10px 3px 8px;
  border-radius: 6px;
  font-size: 11.5px;
  font-weight: 600;
  line-height: 1.4;
  white-space: nowrap;
  border: 1px solid transparent;
}
.cdk-badge--ok {
  color: var(--success);
  background: var(--success-a15);
  border-color: color-mix(in srgb, var(--success) 32%, transparent);
  box-shadow: inset 2px 0 0 var(--success);
}
.cdk-badge--own {
  color: var(--warning);
  background: color-mix(in srgb, var(--warning) 14%, transparent);
  border-color: color-mix(in srgb, var(--warning) 32%, transparent);
  box-shadow: inset 2px 0 0 var(--warning);
}
.cdk-badge--fail {
  color: var(--danger);
  background: var(--danger-a15);
  border-color: color-mix(in srgb, var(--danger) 32%, transparent);
  box-shadow: inset 2px 0 0 var(--danger);
}
.cdk-badge--doing {
  color: var(--accent);
  background: var(--accent-a15);
  border-color: color-mix(in srgb, var(--accent) 30%, transparent);
  box-shadow: inset 2px 0 0 var(--accent);
}
.cdk-badge--wait { color: var(--text-dim); background: var(--surface-chip-2); }
.cdk-badge__pulse {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: currentColor;
  animation: cdk-pulse 1s ease-in-out infinite;
}
@keyframes cdk-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.35; transform: scale(0.7); }
}

/* 结果行左缘按状态着色（视觉分组，一眼扫出成功/失败） */
.cdk-result-table tbody tr.is-ok td:first-child { box-shadow: inset 2px 0 0 var(--success); }
.cdk-result-table tbody tr.is-own td:first-child { box-shadow: inset 2px 0 0 var(--warning); }
.cdk-result-table tbody tr.is-fail td:first-child { box-shadow: inset 2px 0 0 var(--danger); }

/* 详情单元格：失败原因文本 + 原文展开（跨行块，与文字间距收紧） */
.cdk-detail-cell { min-width: 0; }
.cdk-detail { font-size: 11px; color: var(--text-secondary); }
.cdk-detail--fail { color: var(--danger); }
.cdk-raw-toggle {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  border: 1px dashed var(--border-soft);
  background: var(--surface-chip);
  color: var(--text-muted);
  cursor: pointer;
  transition: var(--transition);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  flex-shrink: 0;
}
.cdk-raw-toggle:hover { border-color: var(--accent); color: var(--accent); }
.cdk-raw-toggle.is-open { border-style: solid; border-color: var(--accent); color: var(--accent); background: var(--accent-a10); }
.cdk-raw {
  margin: 7px 0 2px;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--surface-inset);
  border: 1px solid var(--line-1);
  font-family: var(--font-mono);
  font-size: 10.5px;
  line-height: 1.6;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 180px;
  overflow-y: auto;
}

.cdk-result-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.cdk-result-table th {
  text-align: left;
  font-weight: 600;
  color: var(--text-muted);
  font-size: 10.5px;
  padding: 7px 10px;
  border-bottom: 1px solid var(--line-2);
  background: var(--surface-inset);
  position: sticky;
  top: 0;
}
/* 列宽：序号定宽 / CDK 等宽不换行 / 结果窄（徽章固定宽）/ 详情自适应 / SubID 定宽等宽数字 / 版本名吃余量 */
.cdk-result-table .cdk-col-idx { width: 46px; }
.cdk-result-table .cdk-col-result { width: 118px; }
.cdk-result-table .cdk-col-sub { width: 92px; }
.cdk-idx { font-family: var(--font-mono); font-size: 11px; color: var(--text-dim); }
.cdk-col-code { font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.3px; color: var(--text-secondary); white-space: nowrap; }
.cdk-result-table td { padding: 7px 10px; border-bottom: 1px solid var(--row-border); vertical-align: middle; }
.cdk-result-table td.cdk-col-sub, .cdk-result-table td.cdk-col-result { white-space: nowrap; }
.cdk-result-cell { display: inline-flex; align-items: center; gap: 5px; }
.cdk-result-table tbody tr { transition: background 0.15s; }
.cdk-result-table tbody tr:hover { background: var(--hover-soft); }
.cdk-result-table tbody tr.is-wait td { color: var(--text-dim); opacity: 0.6; }
.cdk-result-table tbody tr.is-doing td { color: var(--text-secondary); }
.cdk-batch-indicator {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 10px;
  padding: 2px 8px;
  border-radius: 999px;
  background: var(--info-a15);
  color: var(--info);
  font-weight: 600;
}
.cdk-batch-indicator.is-waiting { background: var(--warning-a15); color: var(--warning); }
.cdk-sub-id {
  font-family: var(--font-mono);
  font-size: 11.5px;
  font-weight: 600;
  color: var(--accent);
  text-decoration: none;
  border-bottom: 1px dashed color-mix(in srgb, var(--accent) 55%, transparent);
  white-space: nowrap;
  transition: var(--transition);
}
.cdk-sub-id:hover { color: var(--accent-fill); border-bottom-style: solid; }
.cdk-sub-empty { font-size: 11px; color: var(--text-dim); }
.cdk-limit-count.near-limit { color: var(--danger); }
.cdk-limit-bar .cdk-limit-count { font-family: var(--font-mono); font-weight: 700; color: var(--text-primary); }
@media (max-width: 1100px) {
  .cdk-split { grid-template-columns: 1fr; }
}
</style>

<!-- 系统级「减少动态效果」：CDK 角标的脉冲是无限循环动画，显式关掉（理由同上）。 -->
<style scoped>
@media (prefers-reduced-motion: reduce) {
  .cdk-badge__pulse { animation: none; }
}
</style>
