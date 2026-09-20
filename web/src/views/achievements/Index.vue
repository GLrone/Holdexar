<script setup lang="ts">
/* 成就殿堂（框架 achv-*）：奖杯与游玩时长汇总分析。
 *
 * 分节顺序按「信息密度递增」排：上面是概括（你是谁 / 你怎么玩），越往下越细
 * （具体到哪款游戏、哪一枚成就），最底是全量成就列表。因此生涯八块与原成就
 * 四节是**交错**的，每档前有一个档位标签（总览 / 分析 / 明细）：
 *
 *   总览  career 生涯总览 → overview 成就总览 → titles 账户称号
 *   分析  taste 口味画像 → analysis 数据分析 → heatmap 活跃热力图 → series 系列进度
 *   明细  records 纪录殿堂 → vault 珍藏馆 → highlights 成就亮点 → games 游戏成就
 *
 * 生涯数据走页面级 store（stores/career.ts）：交错落位要求数据不挂在某个容器里。
 * 数据源：achievements store（后端公开页采集，免 Web API Key）+ career store。 */
import { computed, onActivated, onDeactivated, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, PieChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import VChart from 'vue-echarts'

import {
  HlButton,
  HlEmpty,
  HlIcon,
  HlImg,
  HlInput,
  HlSegmented,
  HlSelect,
  HlSpinner,
  HlStat,
  type HlSelectOption,
} from '@/components/ui'
import {
  axisPointerStyle,
  revealChart,
  useChartPalette,
  useTipPalette,
} from '@/api/chartTheme'
import type {
  AchievementAccount,
  AchievementGameFilter,
  AchievementGameSort,
  RarityTier,
} from '@/api/client'
import type { MessageKey } from '@/locales'
import { useI18n } from '@/locales'
import { PALETTE } from '@/lib/familyColors'
import TrophyMedal from '@/components/business/TrophyMedal.vue'
import { vStagger } from '@/lib/stagger'
import { useAchievementsStore } from '@/stores/achievements'
import { useCareerStore } from '@/stores/career'
import ActivityHeatmap from './career/ActivityHeatmap.vue'
import CareerBanner from './career/CareerBanner.vue'
import CareerOverview from './career/CareerOverview.vue'
import RecordHall from './career/RecordHall.vue'
import SeriesProgress from './career/SeriesProgress.vue'
import TasteProfile from './career/TasteProfile.vue'
import TitleWall from './career/TitleWall.vue'
import VaultWall from './career/VaultWall.vue'
import DeferPanel from './DeferPanel.vue'
import DetailDrawer from './DetailDrawer.vue'

use([CanvasRenderer, BarChart, PieChart, GridComponent, TooltipComponent])

const router = useRouter()
const { t } = useI18n()
const store = useAchievementsStore()
const careerStore = useCareerStore()

/** 生涯八块共用一份载荷；空库时不渲染（渲染出来全是空态，反而更吵） */
const career = computed(() => (careerStore.hasData() ? careerStore.career : null))

const drawerOpen = ref(false)
const drawerAppid = ref<number | null>(null)

function openDetail(appid: number): void {
  drawerAppid.value = appid
  drawerOpen.value = true
}

/* keep-alive 常驻：onActivated 在首挂与每次切回都触发，数据照常刷新；
   组件实例与 DOM 跨切页存活，重进零重挂载（本页挂载量 5.6k 节点/171 图，
   每次切回都要整页重付）。 */
defineOptions({ name: 'AchievementsHall' })
onActivated(() => {
  void store.attach()
  void store.load()
  // 可切换账号清单（多账号隔离）；失败不阻断页面
  void store.loadAccounts()
  // 生涯：激活拉一次；同步跑完（syncing true→false）由 store 自动重算
  careerStore.attach(() => store.syncing)
  void careerStore.load()
})

/* 切离时收起明细抽屉：keep-alive 会保留弹层状态，回来时被模态层挡住很突兀 */
onDeactivated(() => {
  drawerOpen.value = false
})

/* 稀有度档位 → 颜色 + 词条 key（模块级只存 key，渲染期 t()） */
const RARITY_META: Record<RarityTier, { color: string; key: MessageKey }> = {
  ultra: { color: PALETTE.gold, key: 'achievements.rarity.ultra' },
  very_rare: { color: PALETTE.violet, key: 'achievements.rarity.very_rare' },
  rare: { color: PALETTE.blue, key: 'achievements.rarity.rare' },
  uncommon: { color: PALETTE.teal, key: 'achievements.rarity.uncommon' },
  common: { color: PALETTE.slate, key: 'achievements.rarity.common' },
  unknown: { color: PALETTE.slate, key: 'achievements.rarity.unknown' },
}

/* 单位 h/kh 走共用词条（同 GlPlay 的分档判断：逻辑选档，文案交给词典） */
function fmtHours(minutes: number): string {
  if (!minutes) return t('common.hours', { h: 0 })
  const h = minutes / 60
  if (h < 10) return t('common.hours', { h: h.toFixed(1) })
  if (h < 1000) return t('common.hours', { h: h.toFixed(0) })
  return t('common.hoursK', { h: (h / 1000).toFixed(1) })
}

function fmtDate(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString()
}

function lastPlayedText(ts: number): string {
  return ts > 0
    ? t('achievements.list.lastPlayed', { date: fmtDate(ts) })
    : t('achievements.list.neverPlayed')
}

/* ── 多账号隔离：默认主账号（值 = 空串），其余按 steamid 指定 ──
   切号时成就 store 与生涯 store 一起换（各自按账号分键水合快照），
   数据一律由后端按 steamid 隔离，前端这边不合并、不缓存别人的账。 */
const accountOptions = computed<HlSelectOption[]>(() => {
  const list = store.accounts
  if (list.length <= 1) return []
  const nameOf = (a: AchievementAccount): string => a.personaName || a.steamid
  const avatarOf = (a: AchievementAccount): string | undefined => a.avatarUrl || undefined
  const primary = list.find((a) => a.isPrimary)
  const opts: HlSelectOption[] = [
    {
      label: primary ? nameOf(primary) : t('achievements.account.primary'),
      value: '',
      avatar: primary ? avatarOf(primary) : undefined,
    },
  ]
  for (const a of list) {
    if (a.isPrimary) continue
    opts.push({
      label:
        a.relation === 'family'
          ? `${nameOf(a)} · ${t('achievements.account.family')}`
          : nameOf(a),
      value: a.steamid,
      avatar: avatarOf(a),
    })
  }
  return opts
})

async function switchAccount(steamid: string): Promise<void> {
  if (steamid === store.account) return
  drawerOpen.value = false
  visibleCount.value = PAGE_SIZE
  careerStore.setAccount(steamid)
  await store.setAccount(steamid)
  await careerStore.load(true, steamid)
}

const accountModel = computed<string>({
  get: () => store.account,
  set: (v) => {
    void switchAccount(v)
  },
})

/** 库外行没有时长与最近游玩数据（Steam 不提供未拥有游戏的这些字段），文案区别对待 */
function rowHoursText(g: { playtimeMin: number; owned: boolean }): string {
  return g.owned ? fmtHours(g.playtimeMin) : t('achievements.list.externalHours')
}

function rowLastPlayedText(g: { lastPlayed: number; owned: boolean }): string {
  return g.owned ? lastPlayedText(g.lastPlayed) : t('achievements.list.externalNoRecord')
}

/** 库外（家庭共享等）成就：来源与已获得枚数，供 KPI 下方一句提示 */
const externalHint = computed(() => {
  const s = store.summary
  if (!s || s.externalGames <= 0) return ''
  return t('achievements.kpi.externalHint', {
    n: s.externalGames,
    u: s.externalUnlocked,
    p: s.externalPlatinum,
  })
})

/** 白金卡扫光相位：按 appid 取伪随机时长与负延迟——每卡各走各的
    （负延迟让首帧直接落在周期中段，无起手空等）。 */
function sheenStyle(appid: number): Record<string, string> {
  // 乘法哈希：appid 大量以 0 结尾，取模会退化成同一值，先散列再取模，
  // 时长与相位才真正彼此错开。
  const h = (appid * 2654435761) % 997
  const duration = 4.1 + (h % 5) * 0.47
  const delay = -((h % 17) * 0.37)
  return {
    '--achv-sheen-duration': `${duration.toFixed(2)}s`,
    '--achv-sheen-delay': `${delay.toFixed(2)}s`,
    // 卡角奖杯的浮动相位（同一哈希源，与扫光错开节奏）
    '--tm-float-delay': `${(-(h % 18) * 0.2).toFixed(2)}s`,
  }
}

/** 全服占比 → 档位色（与后端 rarity_tier 同一套阈值） */
function rarityColorOf(percent: number | null): string {
  const tier: RarityTier =
    percent == null
      ? 'unknown'
      : percent < 1
        ? 'ultra'
        : percent < 5
          ? 'very_rare'
          : percent < 20
            ? 'rare'
            : percent < 50
              ? 'uncommon'
              : 'common'
  return RARITY_META[tier].color
}

/* ── KPI 六卡 ── */
const kpi = computed(() => {
  const s = store.summary
  return {
    platinum: s?.platinum ?? 0,
    games: s?.gamesWithAchievements ?? 0,
    total: s?.totalAchievements ?? 0,
    unlocked: s?.unlockedAchievements ?? 0,
    rate: (s?.completionRate ?? 0).toFixed(1),
    playtime: fmtHours(s?.totalPlaytimeMin ?? 0),
    played: s?.playedGames ?? 0,
  }
})

/* ── 图表（option 全色取 chartTheme/色板，动画契约显式声明）── */
const tipPalette = useTipPalette()
const palette = useChartPalette()
const wipe = ref(false)

/** 稀有度分布环图（已获得成就按全服解锁率分档） */
const rarityLegend = computed(() =>
  (store.summary?.rarityBuckets ?? [])
    .filter((b) => b.tier !== 'unknown' && b.count > 0)
    .map((b) => ({ ...b, label: t(RARITY_META[b.tier].key), color: RARITY_META[b.tier].color })),
)

const rarityOption = computed(() => {
  const tip = tipPalette.value
  const data = rarityLegend.value.map((b) => ({
    name: b.label,
    value: b.count,
    itemStyle: { color: b.color },
  }))
  return {
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      backgroundColor: tip.bg,
      borderColor: tip.border,
      borderWidth: 1,
      borderRadius: 6,
      padding: [7, 11],
      textStyle: { color: tip.text, fontSize: 12 },
      extraCssText: `box-shadow: ${tip.shadow};`,
      
      formatter: (p: unknown) => {
        const it = p as { name: string; value: number }
        return t('achievements.chart.rarityTooltip', { tier: it.name, n: it.value })
      },
    },
    series: [
      {
        type: 'pie',
        radius: ['56%', '82%'],
        center: ['50%', '50%'],
        avoidLabelOverlap: false,
        label: { show: false },
        labelLine: { show: false },
        itemStyle: { borderColor: 'transparent', borderWidth: 2 },
        data,
      },
    ],
  }
})

const unlockOption = computed(() => {
  const c = palette.value
  const tip = tipPalette.value
  const timeline = store.summary?.unlockTimeline ?? []
  return {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 8, right: 12, top: 12, bottom: 6, containLabel: true },
    axisPointer: axisPointerStyle(c, tipPalette.value),
    tooltip: {
      trigger: 'axis',
      backgroundColor: tip.bg,
      borderColor: tip.border,
      borderWidth: 1,
      borderRadius: 6,
      padding: [7, 11],
      textStyle: { color: tip.text, fontSize: 12 },
      extraCssText: `box-shadow: ${tip.shadow};`,
      
      formatter: (params: unknown) => {
        const p = (params as { name: string; value: number }[])[0]
        return t('achievements.chart.unlockTooltip', { month: p.name, n: p.value })
      },
    },
    xAxis: {
      type: 'category',
      data: timeline.map((x) => x.month),
      axisLabel: { color: c.label, fontSize: 10 },
      axisLine: { lineStyle: { color: c.axis } },
      axisTick: { show: false },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { color: c.label, fontSize: 10 },
      axisLine: { show: false },
      splitLine: { lineStyle: { color: c.grid } },
    },
    series: [
      {
        type: 'bar',
        data: timeline.map((x) => x.count),
        barMaxWidth: 16,
        itemStyle: { color: c.success, borderRadius: [3, 3, 0, 0] },
      },
    ],
  }
})

const playtimeOption = computed(() => {
  const c = palette.value
  const tip = tipPalette.value
  const top = store.summary?.playtimeTop ?? []
  return {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 8, right: 30, top: 6, bottom: 6, containLabel: true },
    axisPointer: axisPointerStyle(c, tipPalette.value),
    tooltip: {
      trigger: 'item',
      backgroundColor: tip.bg,
      borderColor: tip.border,
      borderWidth: 1,
      borderRadius: 6,
      padding: [7, 11],
      textStyle: { color: tip.text, fontSize: 12 },
      extraCssText: `box-shadow: ${tip.shadow};`,
      
      formatter: (params: unknown) => {
        const p = params as { name: string; value: number }
        return t('achievements.chart.playtimeTooltip', { name: p.name, h: String(p.value) })
      },
    },
    xAxis: {
      type: 'value',
      axisLabel: { color: c.label, fontSize: 10 },
      axisLine: { show: false },
      splitLine: { lineStyle: { color: c.grid } },
    },
    yAxis: {
      type: 'category',
      inverse: true,
      data: top.map((x) => x.name),
      axisLabel: {
        color: c.label,
        fontSize: 11,
        formatter: (v: string) => (v.length > 12 ? `${v.slice(0, 12)}...` : v),
      },
      axisLine: { lineStyle: { color: c.axis } },
      axisTick: { show: false },
    },
    series: [
      {
        type: 'bar',
        data: top.map((x) => Math.round((x.playtimeMin / 60) * 10) / 10),
        barMaxWidth: 13,
        itemStyle: { color: c.line, borderRadius: [0, 3, 3, 0] },
      },
    ],
  }
})

const hasRarity = computed(() => rarityLegend.value.length > 0)
const hasUnlock = computed(() => (store.summary?.unlockTimeline ?? []).length > 0)
const hasPlaytime = computed(() => (store.summary?.playtimeTop ?? []).length > 0)
const hasShelf = computed(() => (store.summary?.platinums ?? []).length > 0)

/* ── 工具条（筛选/排序/搜索；搜索 300ms 防抖）── */
const filterOptions = computed<HlSelectOption[]>(() => [
  { label: t('achievements.filter.trophy'), value: 'trophy' },
  { label: t('achievements.filter.platinum'), value: 'platinum' },
  { label: t('achievements.filter.progress'), value: 'progress' },
  { label: t('achievements.filter.external'), value: 'external' },
  { label: t('achievements.filter.all'), value: 'all' },
])
const sortOptions = computed<HlSelectOption[]>(() => [
  { label: t('achievements.sort.playtime'), value: 'playtime' },
  { label: t('achievements.sort.progress'), value: 'progress' },
  { label: t('achievements.sort.recent'), value: 'recent' },
  { label: t('achievements.sort.name'), value: 'name' },
])

const filterModel = computed<AchievementGameFilter>({
  get: () => store.filter,
  set: (v) => store.setFilter(v),
})
const sortModel = computed<AchievementGameSort>({
  get: () => store.sort,
  set: (v) => store.setSort(v),
})

/* 分批渲染：全量 449 行一次进 DOM 会堵主线程（5614 节点 / 380 图，
   连 setViewport 都卡），首屏只画 60 行，余量按需追加。切换筛选/排序/搜索
   时回到首屏数量——数据已在本地，重置是纯计算，无网络往返。 */
const PAGE_SIZE = 60
const visibleCount = ref(PAGE_SIZE)
const visibleGames = computed(() => store.games.slice(0, visibleCount.value))
const remainCount = computed(() => Math.max(0, store.games.length - visibleCount.value))

watch([() => store.filter, () => store.sort, () => store.search], () => {
  visibleCount.value = PAGE_SIZE
})

const searchModel = ref('')
let searchTimer: ReturnType<typeof setTimeout> | undefined
watch(searchModel, (v) => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => store.setSearch(v.trim()), 300)
})

/* ── 同步状态文案与进度 ── */
const syncProgressText = computed(() => {
  const snap = store.activeSync
  if (!snap) return ''
  const stageKey =
    snap.stage === 'library'
      ? 'achievements.sync.stage.library'
      : snap.stage === 'progress'
        ? 'achievements.sync.stage.progress'
        : 'achievements.sync.stage.details'
  return t('achievements.sync.running', {
    stage: t(stageKey),
    done: snap.done ?? 0,
    total: snap.total ?? 0,
  })
})

const syncPercent = computed(() => {
  const snap = store.activeSync
  if (!snap || !snap.total) return 0
  return Math.min(100, Math.round(((snap.done ?? 0) * 100) / snap.total))
})

const lastSyncedText = computed(() => {
  const iso = store.summary?.lastSyncedAt
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime())
    ? ''
    : t('achievements.sync.lastSynced', { date: d.toLocaleString() })
})

const emptyText = computed(() =>
  store.summary?.lastSyncedAt ? t('achievements.empty.noMatch') : t('achievements.empty.noData'),
)

/* 首屏数据到位后触发一次左→右描线入场 */
const stopSummaryWatch = watch(
  () => store.summary,
  async (s) => {
    if (!s) return
    await revealChart(wipe)
    stopSummaryWatch()
  },
  { immediate: true },
)
</script>

<template>
  <div class="achv-page">
    <!-- 加载 / 错误 -->
    <HlSkeleton v-if="store.loading && !store.summary" variant="text" :count="1" :rows="6" />

    <!-- 无凭证引导 -->
    <div v-else-if="store.summary && !store.summary.hasCredential" class="achv-guide card">
      <div class="achv-guide-trophy"><TrophyMedal tier="platinum" :size="52" glow /></div>
      <HlEmpty icon="" :text="t('achievements.empty.noCredential')" />
      <p class="achv-guide-hint">{{ t('achievements.empty.noCredentialHint') }}</p>
      <HlButton art="outline" tone="blue" size="sm" @click="router.push('/settings')">
        {{ t('achievements.empty.goSettings') }}
      </HlButton>
    </div>

    <template v-else-if="store.summary">
      <!-- 同步入口钉在页面最顶（全页常驻可见）；
           同步进行中按钮置灰，进度由下方 syncbar 呈现 -->
      <div class="achv-topbar">
        <span v-if="lastSyncedText" class="achv-sync-last">{{ lastSyncedText }}</span>
        <HlButton
          art="outline"
          tone="blue"
          size="sm"
          :disabled="store.syncing"
          @click="store.startSync()"
        >
          <HlIcon v-if="!store.syncing" name="refresh" />
          {{ t('achievements.action.sync') }}
        </HlButton>
      </div>

      <!-- 同步进行中：阶段 + 进度条 + 当前游戏（全量约十分钟，必须可见） -->
      <div v-if="store.syncing" class="achv-syncbar card">
        <HlSpinner />
        <div class="achv-syncbar-main">
          <div class="achv-syncbar-line">
            <span>{{ syncProgressText }}</span>
            <span v-if="store.activeSync?.current" class="achv-syncbar-current">
              {{ t('achievements.sync.current', { name: store.activeSync.current }) }}
            </span>
          </div>
          <div class="achv-syncbar-track"><i :style="{ width: syncPercent + '%' }" /></div>
        </div>
      </div>

      <!-- ═══ 生涯数据：空库时不渲染八块（全是空态反而更吵）═══ -->
      <template v-if="career">
        <!-- ── 档位一 · 总览（你是谁）── -->
        <p class="achv-tier">{{ t('achievements.tier.summary') }}</p>
        <CareerBanner />
        <CareerOverview :career="career" />
      </template>

      <!-- ① 总览：KPI + 白金殿堂 -->
      <section data-section="achievements.section.overview" class="achv-block">
        <div class="achv-kpi-row">
          <HlStat
            size="sm"
            :color="PALETTE.gold"
            :style="{ borderColor: PALETTE.gold + '30' }"
            :label="t('achievements.kpi.platinum')"
            :value="String(kpi.platinum)"
            :sub="t('achievements.kpi.platinumSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.teal"
            :style="{ borderColor: PALETTE.teal + '30' }"
            :label="t('achievements.kpi.games')"
            :value="String(kpi.games)"
            :sub="t('achievements.kpi.gamesSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.blue"
            :style="{ borderColor: PALETTE.blue + '30' }"
            :label="t('achievements.kpi.total')"
            :value="String(kpi.total)"
            :sub="t('achievements.kpi.totalSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.lime"
            :style="{ borderColor: PALETTE.lime + '30' }"
            :label="t('achievements.kpi.unlocked')"
            :value="String(kpi.unlocked)"
            :sub="t('achievements.kpi.unlockedSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.violet"
            :style="{ borderColor: PALETTE.violet + '30' }"
            :label="t('achievements.kpi.rate')"
            :value="kpi.rate + '%'"
            :sub="t('achievements.kpi.rateSub')"
          />
          <HlStat
            size="sm"
            :color="PALETTE.amber"
            :style="{ borderColor: PALETTE.amber + '30' }"
            :label="t('achievements.kpi.playtime')"
            :value="kpi.playtime"
            :sub="t('achievements.kpi.playtimeSub', { n: kpi.played })"
          />
        </div>

        <!-- 库外（家庭共享等）达成的成就：合计里含它们，这里点明占了多少 -->
        <p v-if="externalHint" class="achv-ext-hint">{{ externalHint }}</p>

        <!-- 白金殿堂：完美游戏陈列 -->
        <div class="card achv-shelf">
          <div class="achv-head">
            <span class="achv-head-title">
              <TrophyMedal tier="platinum" :size="24" />
              {{ t('achievements.shelf.title') }}
            </span>
            <span class="achv-head-hint">{{ t('achievements.shelf.hint') }}</span>
          </div>
          <div v-if="hasShelf" class="achv-shelf-grid hl-stagger" v-stagger>
            <button
              v-for="p in store.summary.platinums"
              :key="p.appid"
              type="button"
              class="achv-plat-card"
              :style="sheenStyle(p.appid)"
              @click="openDetail(p.appid)"
            >
              <div class="achv-plat-cover-box">
                <HlImg :src="p.headerImage" class="achv-plat-cover" alt="" loading="lazy">
                  <template #fallback>
                    <div class="achv-plat-fallback">{{ p.name.slice(0, 1) }}</div>
                  </template>
                </HlImg>
                <span class="achv-plat-medal"><TrophyMedal tier="platinum" :size="40" glow animated /></span>
              </div>
              <div class="achv-plat-name" :title="p.name">{{ p.name }}</div>
              <div class="achv-plat-sub">
                <span v-if="p.date > 0">{{ t('achievements.shelf.date', { date: fmtDate(p.date) }) }}</span>
                <span v-else>{{ t('achievements.list.platinumTag') }}</span>
                <span class="achv-plat-hours hl-num">{{ fmtHours(p.playtimeMin) }}</span>
              </div>
            </button>
          </div>
          <HlEmpty v-else size="sm" icon="" :text="t('achievements.shelf.empty')" />
        </div>
      </section>

      <!-- ── 档位一（续）· 总览：称号墙 ── -->
      <template v-if="career">
        <DeferPanel anchor="achievements.section.titles">
          <TitleWall :career="career" />
        </DeferPanel>
      </template>

      <!-- ── 档位二 · 分析（你怎么玩）── -->
      <template v-if="career">
        <p class="achv-tier">{{ t('achievements.tier.analysis') }}</p>
        <DeferPanel anchor="achievements.section.taste">
          <TasteProfile :career="career" />
        </DeferPanel>
      </template>

      <!-- ② 分析：稀有度分布 / 解锁趋势 / 时长 Top10 -->
      <DeferPanel anchor="achievements.section.analysis" class="achv-block achv-grid3">
        <div class="card achv-card">
          <div class="achv-head">
            <span class="achv-head-title">{{ t('achievements.chart.rarityTitle') }}</span>
            <span class="achv-head-hint">{{ t('achievements.chart.rarityHint') }}</span>
          </div>
          <template v-if="hasRarity">
            <VChart
              class="achv-chart achv-chart--sm"
              :class="{ 'hl-chart-wipe': wipe }"
              :option="rarityOption"
              autoresize
            />
            <div class="achv-legend">
              <span v-for="b in rarityLegend" :key="b.tier" class="achv-legend-item">
                <i :style="{ background: b.color }" />
                {{ b.label }}
                <b class="hl-num">{{ b.count }}</b>
              </span>
            </div>
          </template>
          <HlEmpty v-else size="sm" icon="" :text="t('achievements.empty.noData')" />
        </div>

        <div class="card achv-card">
          <div class="achv-head">
            <span class="achv-head-title">{{ t('achievements.chart.unlockTitle') }}</span>
            <span class="achv-head-hint">{{ t('achievements.chart.unlockHint') }}</span>
          </div>
          <VChart
            v-if="hasUnlock"
            class="achv-chart"
            :class="{ 'hl-chart-wipe': wipe }"
            :option="unlockOption"
            autoresize
          />
          <HlEmpty v-else size="sm" icon="" :text="t('achievements.empty.noData')" />
        </div>

        <div class="card achv-card">
          <div class="achv-head">
            <span class="achv-head-title">{{ t('achievements.chart.playtimeTitle') }}</span>
            <span class="achv-head-hint">{{ t('achievements.chart.playtimeHint') }}</span>
          </div>
          <VChart
            v-if="hasPlaytime"
            class="achv-chart"
            :class="{ 'hl-chart-wipe': wipe }"
            :option="playtimeOption"
            autoresize
          />
          <HlEmpty v-else size="sm" icon="" :text="t('achievements.empty.noData')" />
        </div>
      </DeferPanel>

      <!-- ── 档位二（续）· 分析：活跃热力图 + 系列进度 ── -->
      <template v-if="career">
        <DeferPanel anchor="achievements.section.heatmap">
          <ActivityHeatmap :career="career" />
        </DeferPanel>
        <DeferPanel anchor="achievements.section.series">
          <SeriesProgress :career="career" />
        </DeferPanel>
      </template>

      <!-- ── 档位三 · 明细（具体到哪款游戏）── -->
      <template v-if="career">
        <p class="achv-tier">{{ t('achievements.tier.detail') }}</p>
        <DeferPanel anchor="achievements.section.records">
          <RecordHall :career="career" />
        </DeferPanel>
        <DeferPanel anchor="achievements.section.vault">
          <VaultWall :career="career" />
        </DeferPanel>
      </template>

      <!-- ③ 亮点：最稀有成就 / 最近解锁 / 接近白金 -->
      <DeferPanel anchor="achievements.section.highlights" class="achv-block achv-grid3">
        <!-- 最稀有成就 -->
        <div class="card achv-card">
          <div class="achv-head">
            <span class="achv-head-title">{{ t('achievements.rarest.title') }}</span>
            <span class="achv-head-hint">{{ t('achievements.rarest.hint') }}</span>
          </div>
          <div v-if="store.summary.rarest.length" class="achv-list-scroll">
            <div v-for="a in store.summary.rarest" :key="`r-${a.appid}-${a.imageName}`" class="achv-hl-row">
              <div class="achv-hl-icon-box">
                <HlImg :src="a.icon" class="achv-hl-icon" alt="" loading="lazy">
                  <template #fallback><div class="achv-hl-icon-fb"><TrophyMedal tier="platinum" :size="24" /></div></template>
                </HlImg>
              </div>
              <div class="achv-hl-main">
                <div class="achv-hl-name" :title="a.name">{{ a.name }}</div>
                <div class="achv-hl-sub" :title="a.gameName">{{ a.gameName }}</div>
              </div>
              <span class="achv-hl-pct" :style="{ color: rarityColorOf(a.globalPercent) }">
                {{ t('achievements.highlight.globalPct', { pct: (a.globalPercent ?? 0).toFixed(1) }) }}
              </span>
            </div>
          </div>
          <HlEmpty v-else size="sm" icon="" :text="t('achievements.empty.noData')" />
        </div>

        <!-- 最近解锁 -->
        <div class="card achv-card">
          <div class="achv-head">
            <span class="achv-head-title">{{ t('achievements.recent.title') }}</span>
            <span class="achv-head-hint">{{ t('achievements.recent.hint') }}</span>
          </div>
          <div v-if="store.summary.recentUnlocks.length" class="achv-list-scroll">
            <div v-for="a in store.summary.recentUnlocks" :key="`u-${a.appid}-${a.imageName}`" class="achv-hl-row">
              <div class="achv-hl-icon-box">
                <HlImg :src="a.icon" class="achv-hl-icon" alt="" loading="lazy">
                  <template #fallback><div class="achv-hl-icon-fb"><TrophyMedal tier="platinum" :size="24" /></div></template>
                </HlImg>
              </div>
              <div class="achv-hl-main">
                <div class="achv-hl-name" :title="a.name">{{ a.name }}</div>
                <div class="achv-hl-sub" :title="a.gameName">{{ a.gameName }}</div>
              </div>
              <span class="achv-hl-date hl-num">{{ fmtDate(a.unlockTime) }}</span>
            </div>
          </div>
          <HlEmpty v-else size="sm" icon="" :text="t('achievements.recent.empty')" />
        </div>

        <!-- 接近白金 -->
        <div class="card achv-card">
          <div class="achv-head">
            <span class="achv-head-title">{{ t('achievements.near.title') }}</span>
            <span class="achv-head-hint">{{ t('achievements.near.hint', { pct: 75 }) }}</span>
          </div>
          <div v-if="store.summary.nearCompletion.length" class="achv-list-scroll">
            <button
              v-for="g in store.summary.nearCompletion"
              :key="`n-${g.appid}`"
              type="button"
              class="achv-near-row"
              @click="openDetail(g.appid)"
            >
              <div class="achv-near-cover-box">
                <HlImg :src="g.headerImage" class="achv-near-cover" alt="" loading="lazy">
                  <template #fallback><div class="achv-near-fb">{{ g.name.slice(0, 1) }}</div></template>
                </HlImg>
              </div>
              <div class="achv-near-main">
                <div class="achv-near-name" :title="g.name">{{ g.name }}</div>
                <div class="achv-bar"><i :style="{ width: g.percent + '%' }" /></div>
              </div>
              <div class="achv-near-side">
                <span class="achv-near-pct hl-num">{{ g.percent.toFixed(1) }}%</span>
                <span class="achv-near-left">{{ t('achievements.near.remaining', { n: g.remaining }) }}</span>
              </div>
            </button>
          </div>
          <HlEmpty v-else size="sm" icon="" :text="t('achievements.empty.noData')" />
        </div>
      </DeferPanel>

      <!-- ④ 游戏成就列表 -->
      <DeferPanel anchor="achievements.section.games" class="achv-block">
        <div class="achv-toolbar">
          <HlSegmented v-model="filterModel" :options="filterOptions" />
          <HlSelect v-model="sortModel" :options="sortOptions" class="achv-sort" />
          <HlInput
            v-model="searchModel"
            prefix-icon="search"
            :placeholder="t('achievements.search.placeholder')"
            class="achv-search"
          />
          <div class="achv-toolbar-right">
            <HlSelect
              v-if="accountOptions.length > 1"
              v-model="accountModel"
              :options="accountOptions"
              class="achv-account"
            />
          </div>
        </div>

        <!-- 最近一次同步失败：给出可读原因（不弹窗打扰） -->
        <div
          v-if="store.activeSync?.ok === false && store.activeSync?.error"
          class="achv-sync-error"
        >
          {{ t('achievements.sync.failed', { error: store.activeSync.error }) }}
        </div>
        <!-- 列表拉取失败：错误显式展示，不吞在 store.error 里（否则界面像「点了没反应」） -->
        <div v-if="store.error" class="achv-sync-error">{{ store.error }}</div>

        <div class="achv-count hl-num">
          {{
            remainCount > 0
              ? t('achievements.list.shown', { shown: visibleGames.length, total: store.games.length })
              : t('achievements.list.count', { n: store.games.length })
          }}
        </div>
        <div v-if="store.games.length" class="achv-list hl-stagger" v-stagger>
          <div
            v-for="g in visibleGames"
            :key="g.appid"
            class="achv-row"
            @click="openDetail(g.appid)"
          >
            <div class="achv-cover-box">
              <HlImg :src="g.headerImage" class="achv-cover" alt="" loading="lazy">
                <template #fallback>
                  <div class="achv-cover-fallback">{{ g.name.slice(0, 1) }}</div>
                </template>
              </HlImg>
            </div>
            <div class="achv-main">
              <div class="achv-row-name">
                <span class="achv-name-text">{{ g.name }}</span>
                <span v-if="!g.owned" class="achv-ext">
                  {{ t('achievements.list.externalTag') }}
                </span>
                <span v-if="g.platinum" class="achv-plat">
                  <TrophyMedal tier="platinum" :size="18" />
                  {{ t('achievements.list.platinumTag') }}
                </span>
              </div>
              <div class="achv-bar">
                <i :style="{ width: g.percent + '%', background: g.platinum ? PALETTE.gold : undefined }" />
              </div>
              <div class="achv-row-sub">
                <span class="hl-num">
                  {{ t('achievements.list.unlockedOf', { unlocked: g.unlocked, total: g.total }) }}
                </span>
                <span class="achv-pct hl-num">{{ g.percent.toFixed(1) }}%</span>
              </div>
            </div>
            <div class="achv-side">
              <div class="achv-time hl-num">{{ rowHoursText(g) }}</div>
              <div class="achv-last">{{ rowLastPlayedText(g) }}</div>
            </div>
          </div>
          <div v-if="remainCount > 0" class="achv-more">
            <HlButton
              art="outline"
              tone="blue"
              size="sm"
              @click="visibleCount += PAGE_SIZE"
            >
              {{ t('achievements.action.more', { n: remainCount }) }}
            </HlButton>
          </div>
        </div>
        <div v-else class="achv-empty-wrap">
          <HlEmpty size="sm" icon="" :text="emptyText" />
          <p v-if="!store.summary.lastSyncedAt" class="achv-empty-hint">
            {{ t('achievements.empty.noDataHint') }}
          </p>
        </div>
      </DeferPanel>

      </template>

    <DetailDrawer v-model="drawerOpen" :appid="drawerAppid" :account="store.account" />
  </div>
</template>

<style src="./career/career-shared.css"></style>

<style scoped>
.achv-page { display: flex; flex-direction: column; gap: 12px; }
/* 页顶同步条：右侧贴齐，与下方 syncbar 同视觉层；不占卡片避免与生涯抬头抢高度 */
.achv-topbar { display: flex; align-items: center; justify-content: flex-end; gap: 10px; margin-bottom: -4px; }
.card { background: var(--bg-card); border: 1px solid var(--line-1); border-radius: var(--radius); }
.achv-block { display: flex; flex-direction: column; gap: 10px; }

/* 档位标签：三档信息密度的分界（总览 → 分析 → 明细）。
   做成"左侧短竖线 + 小字"的轻量分界，不抢分节标题的层级。 */
.achv-tier {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 6px 0 0;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--text-dim);
}

.achv-tier::before {
  content: '';
  width: 3px;
  height: 12px;
  border-radius: 2px;
  background: var(--accent-fill);
}

.achv-tier::after {
  content: '';
  flex: 1 1 auto;
  height: 1px;
  background: var(--line-1);
}

/* 无凭证引导 */
.achv-guide { text-align: center; padding: 34px 16px; }
.achv-guide-trophy { display: flex; justify-content: center; margin-bottom: 8px; }
.achv-guide-hint { font-size: 12px; color: var(--text-secondary); margin: 6px 0 14px; }

/* 同步进度条 */
.achv-syncbar { display: flex; align-items: center; gap: 12px; padding: 10px 14px; }
.achv-syncbar-main { flex: 1; min-width: 0; }
.achv-syncbar-line { display: flex; align-items: baseline; gap: 10px; font-size: 12px; color: var(--text-secondary); }
.achv-syncbar-current { font-size: 10.5px; color: var(--text-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.achv-syncbar-track { height: 5px; border-radius: 3px; background: var(--surface-track); overflow: hidden; margin-top: 6px; }
.achv-syncbar-track i { display: block; height: 100%; border-radius: 3px; background: var(--accent-fill); transition: width calc(var(--duration-4) * var(--motion-scale)) var(--ease-out); }

/* KPI 六卡（HlStat sm 档；边框取 familyColors 具名色 + 30 透明） */
.achv-kpi-row { display: grid; grid-template-columns: repeat(6, 1fr); gap: 8px; }
.achv-kpi-row :deep(.hl-stat) { border: 1px solid var(--line-1); border-radius: var(--radius-sm); }
@media (max-width: 1100px) { .achv-kpi-row { grid-template-columns: repeat(3, 1fr); } }

/* 模块头 */
.achv-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.achv-head-title { display: inline-flex; align-items: center; gap: 5px; font-size: 13px; font-weight: 700; color: var(--text-primary); }
.achv-head-icon { color: var(--warning); }
.achv-head-hint { font-size: 10.5px; color: var(--text-dim); text-align: right; }

/* 白金殿堂 */
.achv-shelf { padding: 12px 14px; }
.achv-shelf-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(158px, 1fr)); gap: 8px; }
/* 白金卡鎏金（两层，互不干扰）：
   ① ::before = 旋转鎏金边框：conic 全环不透明（每段都是金，只是明暗在转），
      mask 镂空出 2px 环带 —— 四边恒有金边，旋转读作"光沿边流动"。
   ② ::after = 全卡扫光：斜置白光带（前沿淡青、尾迹淡粉）整卡掠过，
      覆盖封面与文字区，不止封面那一条。
   每卡的相位与时长由视图层按 appid 取伪随机（sheenStyle），彼此独立不成波。 */
@property --achv-gild-angle { syntax: '<angle>'; initial-value: 0deg; inherits: false; }
.achv-plat-card {
  position: relative; display: block; text-align: left; padding: 0;
  /* 兜底：不支持 @property 时该变量仍有值（静态金环，动画退化为不转） */
  --achv-gild-angle: 0deg;
  border: 1px solid transparent; border-radius: var(--radius-sm);
  background: var(--surface-inset); cursor: pointer; overflow: hidden;
  transition: transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    box-shadow calc(var(--duration-3) * var(--motion-scale)) var(--ease-out);
}
.achv-plat-card::before {
  content: ''; position: absolute; inset: 0; border-radius: inherit; padding: 2px;
  /* 置于内容与封面图之上：封面是整宽出血图，不抬层会把上边/左右上半段金边压掉 */
  z-index: 2;
  background: conic-gradient(from var(--achv-gild-angle),
    var(--gild-3) 0deg, var(--gild-1) 14deg, var(--gild-2) 30deg, var(--gild-3) 52deg,
    var(--gild-2) 74deg, var(--gild-3) 96deg, var(--gild-1) 120deg, var(--gild-3) 148deg,
    var(--gild-2) 176deg, var(--gild-3) 204deg, var(--gild-1) 232deg, var(--gild-2) 260deg,
    var(--gild-3) 288deg, var(--gild-2) 316deg, var(--gild-1) 340deg, var(--gild-3) 360deg);
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  -webkit-mask-composite: xor; mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  mask-composite: exclude;
  animation: achvGildSpin 9s linear infinite;
  pointer-events: none;
}
.achv-plat-card:hover {
  transform: translateY(-2px);
  box-shadow: 0 6px 22px -6px var(--gild-2);
}
@keyframes achvGildSpin { to { --achv-gild-angle: 360deg; } }

/* 全卡扫光：斜 28° 的白光带（冷前沿 / 暖尾迹），translate 沿斜向推进。
   用旋转元素 + translate3d（而非 background-position 百分比）——
   后者在 background-size≠100% 时位置算法易被引擎算歪，扫带会停在卡中间。 */
.achv-plat-card::after {
  content: ''; position: absolute; inset: -45%; pointer-events: none; z-index: 4;
  background: linear-gradient(90deg,
    transparent 41%,
    var(--sheen-cool) 47%,
    var(--sheen-core) 50%,
    var(--sheen-warm) 53.5%,
    transparent 60%);
  transform: rotate(-28deg) translate3d(-150%, 0, 0);
  animation: achvCardSweep var(--achv-sheen-duration, 4.4s) var(--achv-sheen-delay, 0s) linear infinite;
  will-change: transform;
}
@keyframes achvCardSweep {
  0% { transform: rotate(-28deg) translate3d(-150%, 0, 0); }
  100% { transform: rotate(-28deg) translate3d(150%, 0, 0); }
}

.achv-plat-cover-box { position: relative; height: 74px; background: var(--surface-track); }
.achv-plat-cover { width: 100%; height: 100%; object-fit: cover; display: block; }
.achv-plat-fallback { width: 100%; height: 100%; display: grid; place-items: center; font-size: 22px; font-weight: 700; color: var(--text-dim); }
/* 角标勋章：压在封面右下，半嵌入卡体 */
.achv-plat-medal {
  position: absolute; right: 8px; bottom: -10px; line-height: 0; z-index: 2;
}
/* 循环动画（鎏金）在 reduced-motion 下显式关停（0s+infinite 会空转） */
@media (prefers-reduced-motion: reduce) {
  .achv-plat-card::before,
  .achv-plat-card::after { animation: none; }
}
.achv-plat-name { font-size: 11.5px; font-weight: 700; color: var(--text-primary); padding: 10px 8px 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.achv-plat-sub { display: flex; align-items: center; justify-content: space-between; gap: 6px; font-size: 9.5px; color: var(--text-dim); padding: 2px 8px 8px; }
.achv-plat-hours { color: var(--text-secondary); }

/* 三列模块网格 */
.achv-grid3 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; align-items: start; }
@media (max-width: 1200px) { .achv-grid3 { grid-template-columns: 1fr; } }
.achv-card { padding: 12px 14px; min-width: 0; }
.achv-chart { width: 100%; height: 208px; }
.achv-chart--sm { height: 150px; }

/* 稀有度图例 */
.achv-legend { display: flex; flex-wrap: wrap; gap: 4px 10px; margin-top: 6px; }
.achv-legend-item { display: inline-flex; align-items: center; gap: 4px; font-size: 10.5px; color: var(--text-secondary); }
.achv-legend-item i { width: 8px; height: 8px; border-radius: 2px; display: inline-block; }
.achv-legend-item b { color: var(--text-primary); font-weight: 700; }

/* 亮点行（最稀有 / 最近解锁） */
.achv-list-scroll { display: flex; flex-direction: column; gap: 5px; max-height: 240px; overflow-y: auto; }
.achv-hl-row { display: flex; align-items: center; gap: 8px; padding: 5px 7px; border: 1px solid var(--line-1); border-radius: var(--radius-sm); background: var(--surface-inset); min-width: 0; }
.achv-hl-icon-box { width: 30px; height: 30px; border-radius: 4px; overflow: hidden; flex-shrink: 0; background: var(--surface-track); }
.achv-hl-icon { width: 100%; height: 100%; object-fit: cover; display: block; }
.achv-hl-icon-fb { width: 100%; height: 100%; display: grid; place-items: center; font-size: 13px; }
.achv-hl-main { flex: 1; min-width: 0; }
.achv-hl-name { font-size: 11.5px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.achv-hl-sub { font-size: 9.5px; color: var(--text-dim); margin-top: 1px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.achv-hl-pct { font-size: 10px; font-weight: 700; flex-shrink: 0; font-family: var(--font-mono); }
.achv-hl-date { font-size: 10px; color: var(--text-dim); flex-shrink: 0; }

/* 接近白金行 */
.achv-near-row { display: flex; align-items: center; gap: 8px; padding: 5px 7px; border: 1px solid var(--line-1); border-radius: var(--radius-sm); background: var(--surface-inset); cursor: pointer; min-width: 0; text-align: left; transition: border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out); }
.achv-near-row:hover { border-color: var(--accent-a45); }
.achv-near-cover-box { width: 56px; height: 26px; border-radius: 3px; overflow: hidden; flex-shrink: 0; background: var(--surface-track); }
.achv-near-cover { width: 100%; height: 100%; object-fit: cover; display: block; }
.achv-near-fb { width: 100%; height: 100%; display: grid; place-items: center; font-size: 11px; color: var(--text-dim); }
.achv-near-main { flex: 1; min-width: 0; }
.achv-near-name { font-size: 11px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 3px; }
.achv-near-side { flex-shrink: 0; text-align: right; }
.achv-near-pct { display: block; font-size: 10.5px; font-weight: 700; color: var(--accent); }
.achv-near-left { font-size: 9px; color: var(--text-dim); }

/* 工具条 */
.achv-toolbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.achv-sort { width: 148px; flex-shrink: 0; }
/* HlSelect 内部 .hl-select-wrap 自带 min-width:170px，会把 132px 的根撑溢出
   压到搜索框上（wrap 170 vs 根 132）——收口到根宽，随根自适应 */
.achv-sort :deep(.hl-select-wrap) { width: 100%; min-width: 0; }
.achv-search { width: 200px; }
.achv-toolbar-right { margin-left: auto; display: flex; align-items: center; gap: 10px; }
/* 账号切换：HlSelect 内部 wrap 自带 min-width:170px，收口到根宽随根自适应 */
.achv-account { width: 178px; flex-shrink: 0; }
.achv-account :deep(.hl-select-wrap) { width: 100%; min-width: 0; }
.achv-sync-last { font-size: 10.5px; color: var(--text-dim); }
.achv-sync-error {
  font-size: 11.5px; color: var(--danger); background: color-mix(in srgb, var(--danger) 8%, transparent);
  border: 1px solid color-mix(in srgb, var(--danger) 30%, transparent);
  border-radius: var(--radius-sm); padding: 7px 11px;
}

/* 列表 */
.achv-count { font-size: 10.5px; color: var(--text-dim); text-align: right; }
.achv-list { display: flex; flex-direction: column; gap: 6px; }
.achv-row {
  display: flex; align-items: center; gap: 10px;
  background: var(--surface-inset); border: 1px solid var(--line-1);
  border-radius: var(--radius-sm); padding: 8px 12px 8px 8px;
  cursor: pointer; min-width: 0;
  transition: border-color calc(var(--duration-2) * var(--motion-scale)) var(--ease-out),
    transform calc(var(--duration-2) * var(--motion-scale)) var(--ease-out);
}
.achv-row:hover { border-color: var(--accent-a45); transform: translateY(-1px); }
.achv-cover-box { width: 92px; height: 43px; border-radius: 4px; overflow: hidden; flex-shrink: 0; background: var(--surface-track); }
.achv-cover { width: 100%; height: 100%; object-fit: cover; display: block; }
.achv-cover-fallback { width: 100%; height: 100%; display: grid; place-items: center; font-size: 16px; font-weight: 700; color: var(--text-dim); }
.achv-main { flex: 1; min-width: 0; }
.achv-row-name { display: flex; align-items: center; gap: 6px; min-width: 0; }
.achv-name-text { font-size: 12.5px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.achv-plat { display: inline-flex; align-items: center; gap: 4px; flex-shrink: 0; font-size: 9.5px; font-weight: 700; color: var(--warning); background: color-mix(in srgb, var(--warning) 14%, transparent); border-radius: 4px; padding: 1px 6px; }
/* 库外（家庭共享等）徽章：与白金徽章同形，用信息色区分来源 */
.achv-ext { display: inline-flex; align-items: center; flex-shrink: 0; font-size: 9.5px; font-weight: 700; color: var(--accent); background: color-mix(in srgb, var(--accent) 14%, transparent); border-radius: 4px; padding: 1px 6px; }
/* KPI 下方的库外提示：一行小字，不抢 KPI 层级 */
.achv-ext-hint { margin: 2px 0 0; font-size: 10.5px; line-height: 1.7; color: var(--text-dim); }
.achv-bar { height: 5px; border-radius: 3px; background: var(--surface-track); overflow: hidden; margin: 5px 0 4px; }
.achv-bar i { display: block; height: 100%; border-radius: 3px; background: var(--accent-fill); transition: width calc(var(--duration-5) * var(--motion-scale)) var(--ease-out); }
.achv-row-sub { display: flex; align-items: center; justify-content: space-between; font-size: 10px; color: var(--text-dim); }
.achv-pct { color: var(--text-secondary); font-weight: 600; }
.achv-side { flex-shrink: 0; text-align: right; }
.achv-time { font-size: 12px; font-weight: 700; color: var(--text-primary); }
.achv-last { font-size: 9.5px; color: var(--text-dim); margin-top: 2px; max-width: 150px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.achv-more { display: flex; justify-content: center; padding: 6px 0 2px; }
.achv-empty-wrap { padding: 14px 0; text-align: center; }
.achv-empty-hint { font-size: 11.5px; color: var(--text-dim); margin-top: 4px; }
</style>