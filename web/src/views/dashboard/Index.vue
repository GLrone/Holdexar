<script setup lang="ts">
/**
 * 仪表盘 —— 库概况统计 + 新史低精选轮播 + 降价动态 + 汇率概览。
 * 聚合多个已有 API 数据，前端组合展示。
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
  Connection,
  DataLine,
  Discount,
  Monitor,
  PriceTag,
  Refresh,
  Select,
} from '@element-plus/icons-vue'

import {
  crawlApi,
  gamesApi,
  type GameListItem,
  proxiesApi,
  type ProxyPoolStats,
  ratesApi,
  type RateItem,
  type SyncResult,
  watchPoolApi,
} from '@/api/client'
import { formatCnyFen } from '@/api/regions'
import { currencyName } from '@/api/currencies'
import { isPermChangeRecent } from '@/lib/priceFlag'
import { useI18n, useLocaleFormat } from '@/locales'
import { message, HlButton, HlCarousel, HlEmpty, HlImg } from '@/components/ui'
import { useRegionsStore } from '@/stores/regions'
import { useRatesStore } from '@/stores/rates'
import RegionFlag from '@/components/RegionFlag.vue'
import CurrencyFlag from '@/components/CurrencyFlag.vue'
import EpicFreeCards from '@/components/steamhl/EpicFreeCards.vue'

const router = useRouter()
const regionsStore = useRegionsStore()
const ratesStore = useRatesStore()
const { t } = useI18n()
/** 千分位走本地化出口（原 `toLocaleString('zh-CN')`）；函数在调用时读语言，
 *  放进模板表达式即可随语言重算。 */
const fmt = useLocaleFormat()

// ─── 数据 ───
const loading = ref(true)
const totalGames = ref(0)
const discountGames = ref(0)
const monitoredGames = ref(0) // 受监控游戏数（愿望单 + 已购，wishlist 域 active 条目）
const proxyStats = ref<ProxyPoolStats | null>(null)
const priceMoves = ref<GameListItem[]>([])
const spotlights = ref<GameListItem[]>([])
/** 用户关注的游戏（愿望单追踪含已购）：展厅入选条件之一 */
const watchedAppids = ref<Set<number>>(new Set())
/** 展厅评价量门槛：超过 10w 评价的游戏无条件入选 */
const SHOWCASE_MIN_REVIEWS = 100_000
const trackedRates = ref<RateItem[]>([])
/** 追踪集之外的填充币种（置灰补位，凑满展示位；追踪满 10 个时不补） */
const fillRates = ref<RateItem[]>([])
const errorMsg = ref('')

// 系统信息（uptime 本地走秒：取数时刻 + 经过时间）
const sysInfo = ref<{ app: string; version: string; uptime_seconds: number } | null>(null)
const sysInfoFetchedAt = ref(0)
const nowTick = ref(Date.now())
let tickTimer: number | undefined

// ─── 计算属性 ───
const uptimeText = computed(() => {
  if (!sysInfo.value) return '—'
  const base = Number(sysInfo.value.uptime_seconds)
  if (!Number.isFinite(base)) return '—'
  const sec = Math.floor(
    base + Math.max(0, (nowTick.value - sysInfoFetchedAt.value) / 1000),
  )
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  // 三种粒度各是一条整句：中英量词/单位位置不同（「3 分 5 秒」/「3m 5s」），
  // 拼 `${m} 分 ${s} 秒` 拼不出英文。t() 在 computed 求值期取，切语言即重算。
  if (h > 0) return t('dashboard.uptime.hms', { h, m, s })
  if (m > 0) return t('dashboard.uptime.ms', { m, s })
  return t('dashboard.uptime.s', { s })
})

// 代理可用副注：Clash 按出口 IP 计（一个出口 IP = 一个代理），手动池逐条计；
// 未运行明示——0/0 多数是后端重启后 Clash 异步启动链没走完，不是代理全挂
const proxyHint = computed(() => {
  const s = proxyStats.value
  if (!s) return ''
  const parts: string[] = []
  // 每段各自是完整短句，用 · 连接（不是同一句被拆开，故可分别取词条后 join）
  parts.push(
    s.clash.running
      ? t('dashboard.proxy.clashRunning', { n: s.clash.okExitIps })
      : t('dashboard.proxy.clashStopped'),
  )
  if (s.pool.total > 0) {
    parts.push(t('dashboard.proxy.pool', { ok: s.pool.ok, total: s.pool.total }))
  }
  return parts.join(' · ')
})

// ─── 数据加载 ───
async function load() {
  loading.value = true
  errorMsg.value = ''
  try {
    // 并行请求所有数据源
    const [gamesRes, feedRes, spotRes, ratesRes, proxiesRes, accountsRes, allGamesRes] =
      await Promise.allSettled([
        gamesApi.list({ limit: 1, onlyDiscounted: true }),
        // 降价动态：全库史低/永降标记（与提醒规则无关），按最近变动排序；
        // 多拉一批在前端做展厅过滤（10w+ 评价 或 用户关注），只取前 10
        gamesApi.list({ flag: 'any', sort: 'updated', limit: 30 }),
        // 轮播精选：新史低按折扣力度排序（同样先多拉再展厅过滤取前 5）
        gamesApi.list({ flag: 'hl', sort: 'discount', limit: 30 }),
        ratesApi.list(),
        proxiesApi.stats(),
        watchPoolApi.appids(),
        // 游戏总数（不含过滤）——曾挂在并行块外的串行尾巴上，白多一程
        gamesApi.list({ limit: 1 }),
      ])

    // 游戏总数 + 打折数
    if (gamesRes.status === 'fulfilled') {
      discountGames.value = gamesRes.value.total
    }
    if (allGamesRes.status === 'fulfilled') {
      totalGames.value = allGamesRes.value.total
    }

    // 关注集（愿望单追踪，含已购）：展厅入选条件之一，先于 feed/精选处理
    if (accountsRes.status === 'fulfilled') {
      watchedAppids.value = new Set(accountsRes.value.appids)
      monitoredGames.value = accountsRes.value.total
    }

    // 展厅入选标准：评价量超过 10w 或用户关注的游戏，至少满足其一
    const isShowcaseWorthy = (g: GameListItem) =>
      (g.reviewCount ?? 0) > SHOWCASE_MIN_REVIEWS || watchedAppids.value.has(g.appid)

    // 降价动态（过滤后的前 10 条）
    if (feedRes.status === 'fulfilled') {
      priceMoves.value = feedRes.value.items.filter(isShowcaseWorthy).slice(0, 10)
    }

    // 轮播精选（只取真·新史低，hl_flag=1，再过展厅标准取前 5）
    if (spotRes.status === 'fulfilled') {
      spotlights.value = spotRes.value.items
        .filter((i) => i.hlFlag === 1 && isShowcaseWorthy(i))
        .slice(0, 5)
    }

    // 汇率（与汇率页共用「追踪币种」自选集：追踪的按自选顺序优先全量展示，
    // 不足 10 行时用其余币种置灰补位）
    if (ratesRes.status === 'fulfilled') {
      const all = ratesRes.value.rates
      trackedRates.value = ratesStore.tracked
        .map((code) => all.find((r) => r.currency === code))
        .filter((r): r is RateItem => r !== undefined)
      fillRates.value =
        trackedRates.value.length >= 10
          ? []
          : all
              .filter((r) => !ratesStore.isTracked(r.currency))
              .slice(0, 10 - trackedRates.value.length)
    }

    // 代理（出口 IP 口径：Clash 按出口 IP 去重，手动池逐条）
    if (proxiesRes.status === 'fulfilled') {
      proxyStats.value = proxiesRes.value
    }

    // 系统信息
    try {
      const resp = await fetch('/api/v1/info')
      if (resp.ok) {
        sysInfo.value = await resp.json()
        sysInfoFetchedAt.value = Date.now()
      }
    } catch {
      /* 静默 */
    }
  } catch (e) {
    errorMsg.value = e instanceof Error ? e.message : String(e)
    message.error(errorMsg.value)
  } finally {
    loading.value = false
  }
}

// ─── 快捷操作（先启动对应动作，再跳转到页面看进度/结果）───
const starting = ref('')

function failText(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

async function startCrawl() {
  starting.value = 'crawl'
  try {
    const res = await crawlApi.run('wishlist')
    message.success(t('dashboard.toast.crawlStarted', { id: res.id, count: res.count }))
  } catch (e) {
    message.error(failText(e))
  }
  router.push('/crawl')
  starting.value = ''
}

async function syncAllWishlist() {
  starting.value = 'wishlist'
  try {
    const accounts = await watchPoolApi.accounts()
    if (accounts.length === 0) {
      message.warning(t('dashboard.toast.noAccounts'))
    } else {
      // 批量同步不逐账户触发爬取（避免 N 个 job 竞争），汇总新增后统一触发一次
      const results = await Promise.allSettled(
        accounts.map((a) => watchPoolApi.sync(a.steamid, false)),
      )
      const ok = results.filter(
        (r): r is PromiseFulfilledResult<SyncResult> => r.status === 'fulfilled',
      )
      const added = ok.reduce((n, r) => n + r.value.added, 0)
      message.success(
        t('dashboard.toast.synced', { ok: ok.length, total: accounts.length, added }),
      )
      const newAppids = [...new Set(ok.flatMap((r) => r.value.newAppids))]
      if (newAppids.length > 0) {
        try {
          // 与 sync_account 自动爬取同款语义：新增条目直接全量抓取
          await crawlApi.run('appids', newAppids)
        } catch {
          message.warning(t('dashboard.toast.crawlTriggerFailed'))
        }
      }
    }
  } catch (e) {
    message.error(failText(e))
  }
  router.push('/pool')
  starting.value = ''
}

async function refreshRates() {
  starting.value = 'rates'
  try {
    const res = await ratesApi.refresh()
    message.success(t('dashboard.toast.ratesRefreshed', { count: res.count, source: res.source }))
  } catch (e) {
    message.error(failText(e))
  }
  router.push('/rates')
  starting.value = ''
}

/** 迷你汇率行点击 → 汇率页并预选该币种（query 深链，汇率页承接并定位到走势板块） */
function goRate(code: string) {
  router.push({ path: '/rates', query: { currency: code } })
}

async function checkProxies() {
  starting.value = 'proxies'
  try {
    const res = await proxiesApi.clashHealthCheck(false)
    message.success(t('dashboard.toast.proxyCheckStarted', { state: res.state }))
  } catch (e) {
    message.error(failText(e))
  }
  router.push('/proxies')
  starting.value = ''
}

function fmtTime(t: string | null | undefined): string {
  if (!t) return '—'
  return t.slice(5, 16).replace('T', ' ')
}

// ─── 代理统计自愈轮询 ───
// Clash 状态是后端进程内单例：后端重启后走异步启动链（下载订阅→拉内核→
// 体检），期间 stats 真实返回 0/0。仪表盘只在挂载时取一次快照会把这个
// 窗口值钉死——轻量轮询 + 切回窗口即刷，让数字在内核起来后自动恢复。
let statsTimer: number | undefined

async function refreshProxyStats() {
  try {
    proxyStats.value = await proxiesApi.stats()
  } catch {
    /* 静默：下轮再试 */
  }
}

function startStatsPolling() {
  stopStatsPolling()
  statsTimer = window.setInterval(refreshProxyStats, 30_000)
}

function stopStatsPolling() {
  if (statsTimer !== undefined) {
    window.clearInterval(statsTimer)
    statsTimer = undefined
  }
}

function onVisibilityChange() {
  if (document.visibilityState === 'visible') refreshProxyStats()
}

/** 国区价展示文本；无国区价返回 null */
function cnPrice(g: GameListItem): string | null {
  const cn = g.priceMatrix['CN']
  return cn ? cn[0] : null
}

/** 全区最低价（含国区）：低价区 + 原货币价 + 人民币折算；无任何有价区返回 null */
function lowestOf(g: GameListItem): { region: string; text: string; cny: string } | null {
  let best: { code: string; text: string; fen: number } | null = null
  for (const [code, v] of Object.entries(g.priceMatrix)) {
    if (v[1] > 0 && (!best || v[1] < best.fen)) best = { code, text: v[0], fen: v[1] }
  }
  return best
    ? { region: best.code, text: best.text, cny: formatCnyFen(best.fen) }
    : null
}

/** 跑马灯条目：不足 8 条复制一轮保证无缝滚动观感 */
const marqueeItems = computed(() =>
  priceMoves.value.length >= 8 || priceMoves.value.length === 0
    ? priceMoves.value
    : [...priceMoves.value, ...priceMoves.value],
)

onMounted(() => {
  regionsStore.load()
  load()
  tickTimer = window.setInterval(() => {
    nowTick.value = Date.now()
  }, 1000)
  startStatsPolling()
  document.addEventListener('visibilitychange', onVisibilityChange)
})

onBeforeUnmount(() => {
  if (tickTimer !== undefined) window.clearInterval(tickTimer)
  stopStatsPolling()
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <section class="dashboard-page">
    <!-- 统计卡片 -->
    <div class="stat-grid" data-section="dashboard.section.overview">
      <div class="card stat-card" v-loading="loading">
        <div class="stat-card__icon stat-card__icon--blue">
          <el-icon :size="22"><DataLine /></el-icon>
        </div>
        <div class="stat-card__body">
          <div class="stat-card__value">{{ fmt.group(totalGames) }}</div>
          <div class="stat-card__label">{{ t('dashboard.stats.totalGames') }}</div>
        </div>
      </div>

      <div class="card stat-card" v-loading="loading">
        <div class="stat-card__icon stat-card__icon--green">
          <el-icon :size="22"><Discount /></el-icon>
        </div>
        <div class="stat-card__body">
          <div class="stat-card__value">{{ fmt.group(discountGames) }}</div>
          <div class="stat-card__label">{{ t('dashboard.stats.discounts') }}</div>
        </div>
      </div>

      <div class="card stat-card" v-loading="loading">
        <div class="stat-card__icon stat-card__icon--orange">
          <el-icon :size="22"><PriceTag /></el-icon>
        </div>
        <div class="stat-card__body">
          <div class="stat-card__value">{{ monitoredGames }}</div>
          <div class="stat-card__label">{{ t('dashboard.stats.monitored') }}</div>
        </div>
      </div>

      <div class="card stat-card" v-loading="loading">
        <div class="stat-card__icon stat-card__icon--cyan">
          <el-icon :size="22"><Connection /></el-icon>
        </div>
        <div class="stat-card__body">
          <div class="stat-card__value">
            {{ proxyStats ? `${proxyStats.available}/${proxyStats.total}` : '—' }}
          </div>
          <div class="stat-card__label">{{ t('dashboard.stats.proxyAvailable') }}</div>
          <div v-if="proxyHint" class="stat-card__hint">{{ proxyHint }}</div>
        </div>
      </div>
    </div>

    <!-- 降价速报跑马灯：CSS 无缝滚动，hover 暂停，点击跳详情 -->
    <div v-if="priceMoves.length > 0" class="card marquee-card" data-section="dashboard.section.priceDrops">
      <div class="marquee">
        <div class="marquee__track">
          <button
            v-for="(g, i) in marqueeItems"
            :key="`${g.appid}-${i}`"
            type="button"
            class="marquee__item"
            @click="router.push(`/game/${g.appid}`)"
          >
            <span v-if="g.hlFlag === 1" class="move-badge move-badge--new">{{
              t('dashboard.badge.newLow')
            }}</span>
            <span v-else-if="g.hlFlag === 2" class="move-badge move-badge--tie">{{
              t('dashboard.badge.tieLow')
            }}</span>
            <span v-else-if="g.discount > 0" class="move-badge move-badge--off">{{
              t('dashboard.badge.discount')
            }}</span>
            <!-- 永降不在打折且在 14 天时效窗内才展示：打折中的行已由上方折扣
                 徽章说明降价原因，变化超过 14 天的不再常驻 -->
            <span
              v-if="g.ppFlag === 1 && g.discount === 0 && isPermChangeRecent(g.ppChangedAt)"
              class="move-badge move-badge--cut"
            >{{ t('dashboard.badge.permDrop') }}</span>
            <span class="marquee__name">{{ g.name }}</span>
            <span class="marquee__price">
              <img
                src="/assets/trophy_gold.png"
                class="marquee__trophy"
                :alt="t('dashboard.lowestRegion')"
              />
              <RegionFlag :code="lowestOf(g)?.region ?? 'CN'" compact />
              <template v-if="lowestOf(g)">
                <span v-if="lowestOf(g)!.region !== 'CN'" class="marquee__cny">
                  {{ lowestOf(g)!.cny }}
                </span>
                <span class="marquee__orig">{{ lowestOf(g)!.text }}</span>
              </template>
              <span v-else class="marquee__orig">{{ cnPrice(g) ?? '—' }}</span>
            </span>
          </button>
        </div>
      </div>
    </div>

    <!-- Epic 喜加一卡片组（当期在送 + 下周预告，每小时自刷，点击直达 Epic 商店页） -->
    <EpicFreeCards />

    <!-- 主体区域 -->
    <div class="dashboard-main">
      <!-- 左列：新史低精选轮播 + 降价动态 -->
      <div class="dashboard-col">
        <!-- 新史低精选（自动轮播，hover 暂停） -->
        <div
          v-if="spotlights.length > 0"
          class="card section-card"
          data-section="dashboard.section.spotlight"
        >
          <div class="section-header">
            <div class="section-title">
              <el-icon><Discount /></el-icon>
              {{ t('dashboard.section.spotlight') }}
            </div>
          </div>
          <HlCarousel :count="spotlights.length" :interval="4000" class="spot-carousel">
            <template #default="{ index }">
              <div
                v-if="spotlights[index]"
                class="spot-slide"
                @click="router.push(`/game/${spotlights[index]!.appid}`)"
              >
                <HlImg
                  class="spot-slide__img"
                  :src="spotlights[index]!.headerImage"
                  :alt="spotlights[index]!.name"
                />
                <div class="spot-slide__shade" />
                <!-- 折扣 + 史低合一章，右上角——复用游戏库 .discount-badge 全局设计 -->
                <div class="spot-slide__badges">
                  <div class="discount-badge" :class="`hl-type-${spotlights[index]!.hlFlag}`">
                    <span>-{{ spotlights[index]!.discount }}%</span>
                    <span class="db-text">{{ t('dashboard.badge.newLow') }}</span>
                  </div>
                </div>
                <div class="spot-slide__info">
                  <span class="spot-slide__name">{{ spotlights[index]!.name }}</span>
                  <!-- 双区价格（对齐降价动态）：国区价 + 🏆最低区原币价与人民币换算 -->
                  <span class="spot-slide__side">
                    <span class="spot-slide__cn">
                      <RegionFlag code="CN" compact />
                      {{ cnPrice(spotlights[index]!) ?? '—' }}
                    </span>
                    <span v-if="lowestOf(spotlights[index]!)" class="spot-slide__lowest">
                      <img
                        src="/assets/trophy_gold.png"
                        class="spot-slide__trophy"
                        :alt="t('dashboard.lowestRegion')"
                      />
                      <RegionFlag :code="lowestOf(spotlights[index]!)!.region" compact />
                      <span class="spot-slide__orig">{{ lowestOf(spotlights[index]!)!.text }}</span>
                      <span class="spot-slide__cny">{{ lowestOf(spotlights[index]!)!.cny }}</span>
                    </span>
                  </span>
                </div>
              </div>
            </template>
          </HlCarousel>
        </div>

        <!-- 降价动态（全库新史低/永降，按最近变动排序） -->
        <div class="card section-card" data-section="dashboard.section.priceMoves">
          <div class="section-header">
            <div class="section-title">
              <el-icon><Select /></el-icon>
              {{ t('dashboard.section.priceMoves') }}
            </div>
            <el-button size="small" text @click="router.push('/library')">
              {{ t('dashboard.action.viewAll') }} →
            </el-button>
          </div>

          <HlEmpty
            v-if="priceMoves.length === 0 && !loading"
            size="sm"
            icon=""
            :text="t('dashboard.empty.noMoves')"
          />
          <div v-else class="move-list">
            <div
              v-for="g in priceMoves"
              :key="g.appid"
              class="move-item"
              @click="router.push(`/game/${g.appid}`)"
            >
              <HlImg
                class="move-item__cover"
                :src="g.headerImage"
                :alt="g.name"
                loading="lazy"
              />
              <div class="move-item__body">
                <div class="move-item__name">{{ g.name }}</div>
                <div class="move-item__badges">
                  <span v-if="g.hlFlag === 1" class="move-badge move-badge--new">{{
                    t('dashboard.badge.newLow')
                  }}</span>
                  <span v-else-if="g.hlFlag === 2" class="move-badge move-badge--tie">{{
                    t('dashboard.badge.tieLow')
                  }}</span>
                  <!-- 打折但非史低（史低标记之外的折扣行）也要有状态说明 -->
                  <span v-else-if="g.discount > 0" class="move-badge move-badge--off">{{
                    t('dashboard.badge.discount')
                  }}</span>
                  <!-- 永降不在打折且在 14 天时效窗内才展示：打折中的行已由上方
                       折扣徽章说明降价原因，变化超过 14 天的不再常驻 -->
                  <span
                    v-if="g.ppFlag === 1 && g.discount === 0 && isPermChangeRecent(g.ppChangedAt)"
                    class="move-badge move-badge--cut"
                  >{{ t('dashboard.badge.permDrop') }}</span>
                  <span v-if="g.discount > 0" class="move-badge move-badge--pct">{{ g.discountLabel }}</span>
                </div>
              </div>
              <div class="move-item__meta">
                <span class="move-item__price">
                  <RegionFlag code="CN" compact />
                  {{ cnPrice(g) ?? '—' }}
                </span>
                <span v-if="lowestOf(g)" class="move-item__lowest">
                  <img
                    src="/assets/trophy_gold.png"
                    class="move-item__trophy"
                    :alt="t('dashboard.lowestRegion')"
                  />
                  <RegionFlag :code="lowestOf(g)!.region" compact />
                  <span class="move-item__cny">{{ lowestOf(g)!.cny }}</span>
                  <span class="move-item__orig">{{ lowestOf(g)!.text }}</span>
                </span>
                <span class="muted">{{ fmtTime(g.updatedAt) }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 右列：汇率 + 快捷操作 + 系统信息（右下角） -->
      <div class="dashboard-col dashboard-col--side">
        <!-- 汇率概览 -->
        <div class="card section-card" data-section="dashboard.section.rates">
          <div class="section-header">
            <div class="section-title">
              <el-icon><DataLine /></el-icon>
              {{ t('dashboard.section.rates') }}
            </div>
            <el-button size="small" text @click="router.push('/rates')">
              {{ t('dashboard.action.viewAll') }} →
            </el-button>
          </div>

          <HlEmpty
            v-if="trackedRates.length === 0 && fillRates.length === 0 && !loading"
            size="sm"
            icon=""
            :text="t('dashboard.empty.noRates')"
          />
          <div v-else class="rate-mini-list">
            <div
              v-for="r in trackedRates"
              :key="r.currency"
              class="rate-mini"
              :title="currencyName(r.currency)"
              @click="goRate(r.currency)"
            >
              <CurrencyFlag :code="r.currency" class="rate-mini__cur" />
              <span class="rate-mini__value">{{ r.rateToCny.toFixed(4) }}</span>
            </div>
            <div
              v-for="r in fillRates"
              :key="r.currency"
              class="rate-mini rate-mini--dim"
              :title="currencyName(r.currency)"
              @click="goRate(r.currency)"
            >
              <CurrencyFlag :code="r.currency" class="rate-mini__cur" />
              <span class="rate-mini__value">{{ r.rateToCny.toFixed(4) }}</span>
            </div>
          </div>
        </div>

        <!-- 快捷操作：先启动动作再跳转 -->
        <div class="card section-card" data-section="dashboard.section.quickActions">
          <div class="section-title">
            <el-icon><Refresh /></el-icon>
            {{ t('dashboard.section.quickActions') }}
          </div>
          <div class="quick-actions">
            <HlButton size="sm" block :disabled="starting !== ''" :loading="starting === 'crawl'" @click="startCrawl">
              {{ t('dashboard.action.crawl') }}
            </HlButton>
            <HlButton size="sm" block :disabled="starting !== ''" :loading="starting === 'wishlist'" @click="syncAllWishlist">
              {{ t('dashboard.action.syncWishlist') }}
            </HlButton>
            <HlButton size="sm" block :disabled="starting !== ''" :loading="starting === 'rates'" @click="refreshRates">
              {{ t('dashboard.action.refreshRates') }}
            </HlButton>
            <HlButton size="sm" block :disabled="starting !== ''" :loading="starting === 'proxies'" @click="checkProxies">
              {{ t('dashboard.action.checkProxies') }}
            </HlButton>
          </div>
        </div>

        <!-- 系统信息（右列末尾 = 右下角） -->
        <div class="card section-card" data-section="dashboard.section.sysInfo">
          <div class="section-title">
            <el-icon><Monitor /></el-icon>
            {{ t('dashboard.section.sysInfo') }}
          </div>
          <div class="sys-info">
            <div class="sys-info__row">
              <span class="muted">{{ t('dashboard.sys.app') }}</span>
              <span>{{ sysInfo?.app || '—' }}</span>
            </div>
            <div class="sys-info__row">
              <span class="muted">{{ t('dashboard.sys.version') }}</span>
              <span>{{ sysInfo?.version || '—' }}</span>
            </div>
            <div class="sys-info__row">
              <span class="muted">{{ t('dashboard.sys.uptime') }}</span>
              <span>{{ uptimeText }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.dashboard-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* ─── 统计卡片 ─── */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}

.stat-card {
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 18px 20px;
}

.stat-card__icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 44px;
  height: 44px;
  border-radius: 10px;
  flex-shrink: 0;
}

.stat-card__icon--blue {
  background: var(--accent-a15);
  color: var(--accent);
}

.stat-card__icon--green {
  background: var(--success-a15);
  color: var(--success);
}

.stat-card__icon--orange {
  background: rgba(255, 159, 67, 0.15);
  color: #ff9f43;
}

.stat-card__icon--cyan {
  background: rgba(0, 191, 255, 0.15);
  color: #00bfff;
}

.stat-card__value {
  font-size: 24px;
  font-weight: 700;
  color: var(--text-primary);
  line-height: 1.1;
}

.stat-card__label {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
}

.stat-card__hint {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 2px;
  opacity: 0.85;
}

/* ─── 主体布局 ─── */
.dashboard-main {
  display: grid;
  grid-template-columns: 1fr 300px;
  gap: 16px;
}

.dashboard-col {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

@media (max-width: 900px) {
  .dashboard-main {
    grid-template-columns: 1fr;
  }
}

/* ─── 通用区块 ─── */
.section-card {
  padding: 18px 20px;
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 15px;
  font-weight: 600;
  color: var(--text-primary);
}

.section-title .el-icon {
  color: var(--accent, #66c0f4);
}

/* ─── 新史低精选轮播 ─── */
.spot-carousel :deep(.hl-carousel__slide) {
  height: 180px;
}

.spot-slide {
  position: relative;
  width: 100%;
  height: 100%;
  cursor: pointer;
}

.spot-slide__img {
  display: block;
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.spot-slide__shade {
  position: absolute;
  inset: 0;
  /* 三段平滑沉底：文字自然落在最暗处，没有「色块」的边界感 */
  background: linear-gradient(
    180deg,
    rgba(0, 0, 0, 0) 30%,
    rgba(0, 0, 0, 0.38) 58%,
    rgba(0, 0, 0, 0.8) 100%
  );
}

/* 折扣+史低合一章：容器定位仿游戏库 .discount-badges-container（右上角），
   章本体复用全局 .discount-badge.hl-type-* 设计（颜色与游戏库完全一致） */
.spot-slide__badges {
  position: absolute;
  top: 8px;
  right: 8px;
  z-index: 10;
}

.spot-slide__badges .discount-badge {
  font-size: 13px;
  padding: 3px 8px;
}

.spot-slide__badges .discount-badge .db-text {
  font-size: 12px;
}

.spot-slide__info {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px 12px;
  color: var(--text-on-fill);
}

.spot-slide__name {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  font-weight: 600;
  text-shadow: 0 1px 6px rgba(0, 0, 0, 0.55);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 右侧纵排两行（对齐降价动态的价格形态）：国区价在上、🏆最低区在下 */
.spot-slide__side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  flex-shrink: 0;
}

.spot-slide__cn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 700;
}

.spot-slide__lowest {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
}

.spot-slide__trophy {
  display: block;
  width: 14px;
  height: 14px;
}

.spot-slide__orig {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-muted);
}

.spot-slide__cny {
  font-size: 13px;
  font-weight: 700;
  color: var(--success, #a4d007);
}

/* ─── 降价动态列表 ─── */
.move-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.move-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 12px;
  border-radius: var(--radius);
  background: var(--bg-soft);
  transition: background var(--transition);
  cursor: pointer;
}

.move-item:hover {
  background: var(--accent-soft);
}

.move-item__cover {
  display: block;
  width: 92px;
  height: 43px;
  object-fit: cover;
  border-radius: 6px;
  flex-shrink: 0;
  background: var(--bg-soft);
}

.move-item__body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.move-item__name {
  font-size: 13px;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.move-item__badges {
  display: flex;
  align-items: center;
  gap: 6px;
}

.move-badge {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 10px;
  font-weight: 600;
}

.move-badge--new {
  background: var(--danger-a15);
  color: var(--danger);
}

.move-badge--tie {
  background: rgba(255, 159, 67, 0.15);
  color: #ff9f43;
}

.move-badge--cut {
  background: var(--success-a15);
  color: var(--success);
}

.move-badge--off {
  background: var(--accent-a15);
  color: var(--accent);
}

/* 折扣百分比（色相同 --off，独立 class 便于区分语义） */
.move-badge--pct {
  background: var(--accent-a15);
  color: var(--accent);
}

.move-item__meta {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  flex-shrink: 0;
}

.move-item__price {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

/* 全区最低价（🏆 冠军区）：人民币折算为主（绿），原货币弱化（灰小字） */
.move-item__lowest {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--success, #a4d007);
}

.move-item__cny {
  font-size: 13px;
  font-weight: 700;
  color: var(--success, #a4d007);
}

.move-item__orig {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-muted);
}

.move-item__trophy {
  display: block;
  width: 14px;
  height: 14px;
}

/* ─── 降价速报跑马灯 ─── */
.marquee-card {
  padding: 10px 0;
}

.marquee {
  overflow: hidden;
  width: 100%;
}

.marquee__track {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  padding-left: 10px;
  white-space: nowrap;
  /* 内容完整宽度：不被容器钳制，否则 flex 子项被压缩、原货币价被截断，
     translateX(-50%) 的无缝滚动也随之失真 */
  width: max-content;
  animation: marquee-scroll 60s linear infinite;
}

.marquee:hover .marquee__track {
  animation-play-state: paused;
}

@keyframes marquee-scroll {
  from {
    transform: translateX(0);
  }
  to {
    transform: translateX(-50%);
  }
}

.marquee__item {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 4px 10px;
  border: none;
  background: var(--bg-soft);
  border-radius: var(--radius);
  cursor: pointer;
  color: inherit;
  font: inherit;
  flex-shrink: 0;
  transition: background var(--transition);
}

.marquee__item:hover {
  background: var(--accent-soft);
}

.marquee__name {
  font-size: 13px;
  color: var(--text-primary);
}

.marquee__price {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  font-weight: 600;
  color: var(--success, #a4d007);
}

.marquee__trophy {
  display: block;
  width: 14px;
  height: 14px;
}

.marquee__cny {
  font-size: 13px;
  font-weight: 700;
  color: var(--success, #a4d007);
}

.marquee__orig {
  font-size: 11px;
  font-weight: 400;
  color: var(--text-muted);
}

/* ─── 汇率迷你列表 ─── */
.rate-mini-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.rate-mini {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px;
  border-radius: var(--radius);
  background: var(--bg-soft);
  cursor: pointer;
  /* 点击直达汇率页走势板块（goRate）；hover 反馈只动背景，不动布局 */
  transition: background-color calc(var(--duration-1) * var(--motion-scale));
}

.rate-mini:hover {
  background: var(--accent-soft);
}

.rate-mini__cur {
  font-size: 13px;
  color: var(--text-primary);
}

.rate-mini__value {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
}

/* 追踪集之外的补位币种（置灰弱化） */
.rate-mini--dim {
  opacity: 0.55;
}

/* ─── 系统信息 ─── */
.sys-info {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.sys-info__row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 13px;
  color: var(--text-primary);
}

/* ─── 快捷操作（2×2 网格）─── */
.quick-actions {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  margin-top: 4px;
}

.muted {
  font-size: 12px;
  color: var(--text-muted);
}
</style>

<!-- 系统级「减少动态效果」：横幅跑马灯是无限循环动画，显式关掉（理由同上）。 -->
<style scoped>
@media (prefers-reduced-motion: reduce) {
  .marquee__track { animation: none; }
}
</style>
