<script setup lang="ts">
/* 完整账单 —— Steam 消费档案（卷宗风）。
   数据由后端常驻任务用 Cookie 在线拉取（history 全量 deep-form 翻页 +
   licenses 分页全量），服务端按交易当日汇率（fx_rate_history 档案）折算
   CNY。三个视图：消费明细 / 充值流水 / 许可证（零售 CDK / 礼物 / 免费，
   家庭页「入库许可证 · CDK 花费台账」模块已并入许可证页签）。 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'

import {
  HlButton,
  HlEmpty,
  HlIcon,
  HlPopconfirm,
  HlSegmented,
  HlTabs,
  message,
  type HlTabItem,
} from '@/components/ui'
import {
  billsApi,
  type BillImportItem,
  type BillOverview,
  type BillSyncSnapshot,
} from '@/api/client'
import { normalizeAvatarUrl } from '@/api/avatar'
import { axisPointerStyle, revealChart, useChartPalette, useTipPalette } from '@/api/chartTheme'
import { useI18n, useLocaleFormat } from '@/locales'
import { useAccountStore } from '@/stores/account'
import LedgerTab from './LedgerTab.vue'
import TopupTab from './TopupTab.vue'
import CdkTab from './CdkTab.vue'
import './bills.css'

use([CanvasRenderer, BarChart, LineChart, GridComponent, TooltipComponent, LegendComponent])

const { t } = useI18n()
const fmt = useLocaleFormat()

/* 页签。**不能**写成模块级常量表 + 直接存译文：常量表在模块加载时求值一次，
   语言会被冻在那一刻。放进 computed，t() 在渲染期取，切语言即重算。 */
const TABS = computed<HlTabItem[]>(() => [
  { key: 'ledger', label: t('bills.tab.ledger') },
  { key: 'topup', label: t('bills.tab.topup') },
  { key: 'cdk', label: t('bills.tab.cdk') },
])

/** tooltip 主题化配色（悬停提示窗红线：echarts 默认白底脱离双主题，禁止） */
const tipPalette = useTipPalette()

/** 画布配色（图例 / 轴 / 轴文字 / 涨跌柱色 / 累计线），与另两图同出口 */
const palette = useChartPalette()

/* ── 账单（多账户）选择 ── */
const imports = ref<BillImportItem[]>([])
const activeId = ref<number | null>(null)
const overview = ref<BillOverview | null>(null)
const loading = ref(false)
const accountStore = useAccountStore()

/** hero 头像统一口径：优先绑定账号的归一化 URL（与顶栏/设置页同源，昵称
 *  匹配同一人），匹配不到或未绑定再退回导入时内嵌 base64，最后占位符 */
const heroAvatar = computed(() => {
  const o = overview.value
  if (!o) return ''
  const acc = accountStore.accounts.find((a) => a.persona_name === o.nickname)
  return normalizeAvatarUrl(acc?.avatar_url) || o.avatar || ''
})

async function loadImports(keepActive = true) {
  const res = await billsApi.listImports()
  imports.value = res.imports
  if (keepActive && activeId.value && res.imports.some((i) => i.id === activeId.value)) return
  activeId.value = res.imports[0]?.id ?? null
}

async function loadOverview() {
  if (activeId.value == null) {
    overview.value = null
    return
  }
  loading.value = true
  try {
    overview.value = await billsApi.overview(activeId.value)
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
    overview.value = null
  } finally {
    loading.value = false
  }
}

async function selectImport(id: number) {
  if (activeId.value === id) return
  activeId.value = id
  await loadOverview()
}

/* ── 同步（后端用 Cookie 在线拉全量，不再走文件导入）── */
const syncing = ref(false)
const syncSnapshot = ref<BillSyncSnapshot | null>(null)

async function loadSyncStatus() {
  try {
    syncSnapshot.value = await billsApi.syncStatus()
  } catch {
    syncSnapshot.value = null
  }
}

/* 进度气泡：后端把翻页阶段写进同步快照（GET /bills/sync），这里 1s 轮询喂给
   气泡文本。Steam 游标翻页不预告总页数，条带只表达「活着」，实数走文本。 */
let progressToast: ReturnType<typeof message.progress> | null = null
let progressTimer: number | null = null

function stopProgressPolling() {
  if (progressTimer !== null) {
    clearInterval(progressTimer)
    progressTimer = null
  }
  progressToast?.close()
  progressToast = null
}
onUnmounted(stopProgressPolling)

function syncStageText(snap: BillSyncSnapshot): string {
  if (snap.stage === 'history') {
    return t('bills.sync.stageHistory', { pages: snap.pages ?? 0, rows: snap.rows ?? 0 })
  }
  if (snap.stage === 'licenses') return t('bills.sync.stageLicenses')
  if (snap.stage === 'import') return t('bills.sync.stageImport')
  return t('bills.sync.stageIdentity')
}

async function syncNow() {
  if (syncing.value) return
  syncing.value = true
  progressToast = message.progress(t('bills.sync.stageIdentity'))
  progressTimer = window.setInterval(async () => {
    try {
      const snap = await billsApi.syncStatus()
      if (snap.running) progressToast?.update(syncStageText(snap))
    } catch {
      /* 单次轮询失败不打断同步，下一秒再取 */
    }
  }, 1000)
  try {
    const res = await billsApi.syncBills()
    message.success(
      t('bills.sync.success', {
        nickname: res.nickname,
        bills: res.gameTxs,
        cdk: res.cdkGames,
        rows: res.historyRows,
      }),
    )
    await loadImports(false)
    activeId.value = res.importId
    await loadOverview()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    stopProgressPolling()
    syncing.value = false
    await loadSyncStatus()
  }
}

async function removeImport(item: BillImportItem) {
  try {
    await billsApi.removeImport(item.id)
    message.success(t('bills.delete.success', { nickname: item.nickname }))
    await loadImports(false)
    await loadOverview()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

/* ── 金额格式化 ──
   原为写死 `toLocaleString('zh-CN')`，改走 useLocaleFormat()。zh-CN 与 en-US 的
   数字输出逐字相同（分组符与小数点同形，见 locales/format.ts 的说明），故显示
   与原实现完全一致，换掉的是「将来加入分组规则不同的语言不必再回头改这里」。
   `¥` 符号留在组件侧（同 yAxis 的 `¥{value}`），不进词条；金额本身也不以已格式
   化的数字串形式塞进词条——词条里只有 {amount} 占位符。 */
const fmtFen = (fen: number | null | undefined) => (fen == null ? '—' : `¥${fmt.money(fen / 100)}`)
const fmtNum = (n: number) => fmt.group(n)

/* ── 统计卡 ── */
const refundRate = computed(() => {
  const s = overview.value?.summary
  if (!s || !s.spendFen) return null
  return `${((s.refundFen / s.spendFen) * 100).toFixed(1)}%`
})

const stats = computed(() => {
  const s = overview.value?.summary
  if (!s) return []
  return [
    { key: 'net', label: t('bills.stats.net.label'), value: fmtFen(s.netFen), sub: t('bills.stats.net.sub'), tone: 'accent', big: true, tip: '' },
    { key: 'spend', label: t('bills.stats.spend.label'), value: fmtFen(s.spendFen), sub: t('bills.stats.spend.sub', { count: fmtNum(s.orders) }), tone: '', big: false, tip: '' },
    { key: 'refund', label: t('bills.stats.refund.label'), value: fmtFen(s.refundFen), sub: refundRate.value ? t('bills.stats.refund.sub', { rate: refundRate.value }) : '', tone: 'danger', big: false, tip: '' },
    { key: 'quota', label: t('bills.stats.quota.label'), value: fmtFen(s.quotaFen), sub: t('bills.stats.quota.sub'), tone: 'success', big: false, tip: t('bills.stats.quota.tip', { self: fmtFen(s.selfNetFen), gift: fmtFen(s.giftNetFen) }) },
    { key: 'topup', label: t('bills.stats.topup.label'), value: fmtFen(s.topupNetFen), sub: t('bills.stats.topup.sub', { count: fmtNum(overview.value?.counts.topupTxs ?? 0) }), tone: 'violet', big: false, tip: '' },
  ]
})

/* ── 月度图（柱=当月净支出，线=累计）── */
const activeTab = ref('ledger')
const chartYear = ref<string>('all')
const yearOptions = computed(() => [
  { label: t('bills.year.all'), value: 'all' },
  ...(overview.value?.years ?? []).map((y) => ({
    label: t('bills.year.value', { year: y.year }),
    value: y.year,
  })),
])

/**
 * 入场不动画、只有「用户主动切换年份」才做数据过渡。
 *
 * 与 rates 同款坑：echarts 的入场动画由 rAF 驱动，后台标签页里 rAF 被限流，
 * 动画会永停第一帧——柱高为 0，看起来就是一张空图。本图此前连 `animation`
 * 都没声明（走 echarts 默认 true），正好踩在坑上。
 * 切年份必须是用户点了 chips 才可能发生，所以那一刻开动画是安全的。
 */
const animateChart = ref(false)
let animResetTimer: number | undefined

watch(chartYear, () => {
  animateChart.value = true
  window.clearTimeout(animResetTimer)
  animResetTimer = window.setTimeout(() => {
    animateChart.value = false
  }, 500)
})
onUnmounted(() => window.clearTimeout(animResetTimer))

const monthRows = computed(() => {
  const all = overview.value?.monthSeries ?? []
  if (chartYear.value === 'all') return all
  return all.filter((m) => m.month.startsWith(chartYear.value))
})

// 左→右描线入场（与走势图 / 汇率图同一实现，见 api/chartTheme 的 revealChart）。
// 挂在 monthRows 上：数据到位即展开，切年份也会重放；空态不播。
const wipe = ref(false)
watch(monthRows, () => {
  if (monthRows.value.length) revealChart(wipe)
})

const chartOption = computed(() => {
  const rows = monthRows.value
  let acc = 0
  const netYuan = rows.map((m) => m.netFen / 100)
  const cum = rows.map((m) => (acc += m.netFen) / 100)
  const c = palette.value
  const tip = tipPalette.value
  return {
    // 入场同步落画（后台标签页 rAF 限流会把入场动画钉在首帧，柱高为 0 = 空图）；
    // 只有用户点年份 chips 触发的数据更新才过渡（见 animateChart）。
    animation: animateChart.value,
    animationDuration: 300,
    animationDurationUpdate: 400,
    animationEasingUpdate: 'cubicOut',
    backgroundColor: 'transparent',
    grid: { left: 56, right: 56, top: 32, bottom: 28 },
    // 悬停时贴在轴上的指示线与轴标签框（走 tooltip 同款底/框）
    axisPointer: axisPointerStyle(c, tip),
    tooltip: {
      trigger: 'axis',
      // 悬停窗走主题 token（echarts 默认白底黑字脱离双主题）
      backgroundColor: tip.bg,
      borderColor: tip.border,
      borderWidth: 1,
      borderRadius: 6,
      padding: [7, 11],
      textStyle: { color: tip.text, fontSize: 12 },
      extraCssText: `box-shadow: ${tip.shadow};`,
      valueFormatter: (v: number) => `¥${Number(v).toFixed(2)}`,
    },
    legend: { textStyle: { color: c.label, fontSize: 11 }, top: 0, right: 0 },
    xAxis: {
      type: 'category',
      data: rows.map((m) => m.month.slice(2)),
      axisLabel: { color: c.label, fontSize: 10 },
      axisLine: { lineStyle: { color: c.axis } },
      axisTick: { show: false },
    },
    yAxis: [
      {
        type: 'value',
        axisLabel: { color: c.label, fontSize: 10, formatter: '¥{value}' },
        splitLine: { lineStyle: { color: c.grid } },
      },
      { type: 'value', show: false },
    ],
    series: [
      {
        // 图例名进词条：本图注册了 LegendComponent，这两个名字真的显示在图例里
        // （PriceTrendChart 不译 series.name 的理由是它没注册 legend，不适用这里）。
        // computed 求值期调 t()，读 locale store 即建立依赖，切语言会重算。
        name: t('bills.chart.legend.net'),
        type: 'bar',
        data: netYuan.map((v) => ({
          value: v,
          // 涨跌取正负语义色。此处曾写死 `#1c6ea4`（浅色 --accent）配 `#e74c3c`
          // （深色 --danger）—— 一根柱子上各取一个主题的常量，两边都不对。
          itemStyle: { color: v >= 0 ? c.positive : c.negative, borderRadius: [3, 3, 0, 0] },
        })),
        barMaxWidth: 14,
      },
      {
        name: t('bills.chart.legend.cum'),
        type: 'line',
        yAxisIndex: 1,
        data: cum,
        symbol: 'none',
        lineStyle: { color: c.success, width: 2 },
        itemStyle: { color: c.success },
        smooth: true,
      },
    ],
  }
})

onMounted(async () => {
  try {
    await loadImports()
    await loadOverview()
    await loadSyncStatus()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
})
</script>

<template>
  <section class="bills-page">
    <!-- ═══ 空态：尚无账单（HlEmpty 居中容器 + 富插槽正文）═══ -->
    <div v-if="!imports.length && !loading" class="card section-card">
      <HlEmpty icon="📜">
        <h3>{{ t('bills.empty.title') }}</h3>
        <!-- 整句一条词条、行内 <b> 与 <br /> 写在词条值里，v-html 渲染
             （同 bundles.calc.excludeHint 先例）。按强调边界切三段再拼，
             英文拼不出完整句子。 -->
        <p class="bills-empty__text" v-html="t('bills.empty.desc')"></p>
        <HlButton variant="primary" :disabled="syncing" :loading="syncing" @click="syncNow">
          <HlIcon v-if="!syncing" name="refresh" /> {{ syncing ? t('bills.sync.inProgress') : t('bills.sync.cta') }}
        </HlButton>
      </HlEmpty>
    </div>

    <template v-else>
      <!-- ═══ 账户卷宗头 ═══ -->
      <div class="card section-card bills-hero" data-section="bills.section.overview">
        <div class="bills-hero__id">
          <img v-if="heroAvatar" :src="heroAvatar" class="bills-hero__avatar" alt="" />
          <span v-else class="bills-hero__avatar bills-hero__avatar--ph">👤</span>
          <div class="bills-hero__who">
            <div class="bills-hero__nick">{{ overview?.nickname ?? '…' }}</div>
            <div class="bills-hero__meta">
              <span v-if="overview?.importedAt">{{ t('bills.hero.syncedAt', { date: overview.importedAt.slice(0, 10) }) }}</span>
              <span class="tag" :title="t('bills.hero.txCountTip')">{{ t('bills.hero.txCount', { count: fmtNum(overview?.summary.txCount ?? 0) }) }}</span>
              <span v-if="(overview?.summary.fxMissing ?? 0) > 0" class="tag tag--warning">
                {{ t('bills.hero.fxMissing', { count: overview?.summary.fxMissing ?? 0 }) }}
              </span>
            </div>
          </div>
        </div>

        <div class="bills-hero__value">
          <div class="bills-hero__value-label">
            {{ t('bills.hero.valueLabel') }} <span class="bills-hero__value-f">{{ t('bills.hero.valueFormula') }}</span>
          </div>
          <div class="bills-hero__value-num">{{ fmtFen(overview?.summary.accountValueFen ?? 0) }}</div>
          <div class="bills-hero__value-c">
            <span>{{ t('bills.hero.selfNet', { amount: fmtFen(overview?.summary.selfNetFen ?? 0) }) }}</span>
            <i>+</i>
            <span>{{ t('bills.hero.cdkTotal', { amount: fmtFen(overview?.summary.cdkTotalFen ?? 0) }) }}</span>
          </div>
        </div>

        <div class="bills-hero__actions">
          <div v-if="syncSnapshot?.running" class="tag tag--warning bills-sync-note">{{ t('bills.sync.autoRunning') }}</div>
          <div
            v-else-if="syncSnapshot?.error"
            class="tag tag--warning bills-sync-note"
            :title="t('bills.sync.errorRetry', { error: syncSnapshot.error })"
          >{{ t('bills.sync.lastFailed') }}</div>
          <HlButton variant="primary" :disabled="syncing" :loading="syncing" @click="syncNow">
            <HlIcon v-if="!syncing" name="refresh" /> {{ syncing ? t('bills.sync.inProgress') : t('bills.sync.ctaShort') }}
          </HlButton>
        </div>
      </div>

      <!-- ═══ 账户切换 chips（每枚 chip 自带删除键：删除动作与账户成组，
           原先所有删除键平铺在 chips 之后，与账户对不上号）═══ -->
      <div v-if="imports.length > 1" class="bills-accounts">
        <div v-for="imp in imports" :key="imp.id" class="bills-accounts__item">
          <button
            type="button"
            class="bills-accounts__chip"
            :class="{ 'is-on': imp.id === activeId }"
            @click="selectImport(imp.id)"
          >
            {{ imp.nickname || t('bills.accounts.fallbackName', { id: imp.id }) }}
            <span class="bills-accounts__net">{{ fmtFen(imp.gameNetFen) }}</span>
          </button>
          <HlPopconfirm
            :text="t('bills.accounts.confirmDelete', { nickname: imp.nickname })"
            :confirm-label="t('bills.action.delete')"
            class="bills-accounts__del"
            @confirm="removeImport(imp)"
          >
            <button type="button" class="bills-accounts__delbtn" :title="t('bills.accounts.deleteTip')">
              <HlIcon name="delete" />
            </button>
          </HlPopconfirm>
        </div>
      </div>

      <template v-if="overview">
        <!-- ═══ 统计条 ═══ -->
        <div class="bills-stats" data-section="bills.section.stats">
          <div
            v-for="st in stats"
            :key="st.key"
            class="bills-stat"
            :class="[`bills-stat--${st.tone}`, { 'bills-stat--big': st.big }]"
            :title="st.tip"
          >
            <div class="bills-stat__label">{{ st.label }}</div>
            <div class="bills-stat__num">{{ st.value }}</div>
            <div v-if="st.sub" class="bills-stat__sub">{{ st.sub }}</div>
          </div>
        </div>

        <!-- ═══ 月度走势 ═══ -->
        <div class="card section-card" data-section="bills.section.chart">
          <div class="bills-chart-head">
            <div>
              <div class="section-title">{{ t('bills.chart.title') }}</div>
              <div class="section-desc">{{ t('bills.chart.desc') }}</div>
            </div>
            <HlSegmented v-model="chartYear" :options="yearOptions" />
          </div>
          <VChart
            v-if="monthRows.length"
            class="bills-chart"
            :class="{ 'hl-chart-wipe': wipe }"
            :option="chartOption"
            autoresize
          />
          <HlEmpty v-else icon="📉" :text="t('bills.chart.empty')" />
        </div>

        <!-- ═══ 三视图 ═══ -->
        <HlTabs v-model="activeTab" :tabs="TABS" class="bills-tabs" data-section="bills.section.ledger">
          <template #default="{ active }">
            <LedgerTab
              v-if="active === 'ledger'"
              :import-id="overview.id"
              :years="overview.years"
              :warnings="overview.warnings"
            />
            <TopupTab
              v-else-if="active === 'topup'"
              :import-id="overview.id"
              :by-currency="overview.topupByCurrency"
              :summary="overview.summary"
            />
            <CdkTab
              v-else-if="active === 'cdk'"
              :import-id="overview.id"
              :counts="overview.counts"
              @changed="loadOverview"
            />
          </template>
        </HlTabs>
      </template>
    </template>
  </section>
</template>

<style scoped>
/* 空态富插槽正文：HlEmpty 已居中，这里只补行高与上边距 */
.bills-empty__text {
  max-width: 460px;
  font-size: 12.5px;
  line-height: 1.8;
  margin-top: 6px;
}
</style>
