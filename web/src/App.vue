<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch, watchEffect } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAccountStore } from '@/stores/account'
import { useCrawlStatusStore } from '@/stores/crawlStatus'
import { useLocaleStore } from '@/stores/locale'
import { useSettingsStore } from '@/stores/settings'
import { useThemeStore } from '@/stores/theme'
import { useTourStore } from '@/stores/tour'
import { useUpdaterStore } from '@/stores/updater'
import { useI18n, type MessageKey } from '@/locales'
import { ratesApi, type WalletSnapshot } from '@/api/client'
import { buildRateMap, formatWalletCny, walletToCny, type RateMap } from '@/lib/walletCny'
import { APP_NAME } from '@/appInfo'
import ProductTour from '@/components/ProductTour.vue'
import PriceStatusPill from '@/components/business/PriceStatusPill.vue'
import UpdateDialog from '@/components/business/UpdateDialog.vue'
import UpdateEntry from '@/components/business/UpdateEntry.vue'
import {
  HlIcon,
  HlImg,
  HlLangToggle,
  HlSectionRail,
  HlSideNav,
  HlThemeToggle,
  HlTopbarAvatar,
  message,
  type HlSideNavGroup,
} from '@/components/ui'

const route = useRoute()
const router = useRouter()

/* 关窗幕布开关：桌面壳通过 window.__hlxCloseCurtain(bool) 驱动（后台线程派发，
   见 desktop/main.py）。挂载点由 App 提供，壳里拿不到页面时静默放弃。 */
const closeCurtain = ref(false)
declare global {
  interface Window {
    __hlxCloseCurtain?: (on: boolean) => void
  }
}

const crawl = useCrawlStatusStore()
const settingsStore = useSettingsStore()
const themeStore = useThemeStore()
const localeStore = useLocaleStore()
const accountStore = useAccountStore()
const updaterStore = useUpdaterStore()
const { t } = useI18n()
onMounted(() => {
  crawl.start()
  settingsStore.load()
  accountStore.load()
  // 闲时预热成就殿堂视图 chunk（内含 echarts 大包）
  const warmAchievements = () => { void import('./views/achievements/Index.vue') }
  if ('requestIdleCallback' in window) requestIdleCallback(warmAchievements, { timeout: 5000 })
  else setTimeout(warmAchievements, 2500)
  // 钱包快照轮询：后端每分钟轮转刷新，前端只读拉取最新快照（不打 Steam）
  setInterval(() => accountStore.load(), 60_000)
  // 与后端对齐更新状态：上次会话下载好却没重启的暂存，这次启动要弹「重启完成更新」
  void updaterStore.sync()
  // 桌面壳的关窗幕布挂载点（页面卸载时撤掉，避免壳里拿到过期引用）
  window.__hlxCloseCurtain = (on: boolean) => {
    closeCurtain.value = on
  }
  onUnmounted(() => {
    delete window.__hlxCloseCurtain
  })
})

/* ── 更新已下载（暂存就绪）：拉起全局更新弹窗 ──
   下载/暂存态都在 updater store（跨页存活、后端是唯一事实来源），所以下载在
   任何页面完成都会在这里浮现；用户关掉 = 稍后重启，更新包保留。
   弹窗（模糊幕布）是更新模块的唯一交互面，重启按钮也在它里面。
   `updateNotify` 关 = 用户要求零打扰（静默更新模式）：不自动弹——
   桌面壳下次启动会自己消费暂存目录完成换装，用户手动打开弹窗照样能看到。 */
watch(
  () => updaterStore.pendingTag,
  (tag) => {
    if (!tag || updaterStore.pendingDialogDismissed) return
    if (settingsStore.updateNotify === false) return
    void updaterStore.openDialog()
  },
)

/* 弹窗被关掉且暂存还在 = 用户选了「稍后重启」：本次会话不再自动重开
   （更新包保留在 data/update-staging，下次启动仍会提示一次） */
watch(
  () => updaterStore.dialogOpen,
  (visible) => {
    if (!visible && updaterStore.pendingTag) updaterStore.dismissPendingDialog()
  },
)

/* ── 首次启动产品导览 ──
   settings KV `ui.onboarding_done` 判定：标志拉取后为 false → 延时 800ms
   启动（等首屏渲染稳定，避免与页面骨架同时闪）。组件内任何关闭路径写标志。
   开关来自 tour store：关于页 / 设置页的重看入口与首屏自动弹共用同一实例。 */
const tour = useTourStore()
watch(
  () => settingsStore.onboardingDone,
  (done) => {
    if (done === false) {
      setTimeout(() => tour.show(), 800)
    }
  },
  { immediate: true },
)

/* ── 启动检查更新（提示 / 静默自动更新）──
   settings 拉到之后（拿两个更新开关 + 提示锚点）查一次更新，按用户设置分路：
   · 静默自动更新开 → 有新版不提示，直接后台下载校验；桌面壳下次启动消费
     暂存目录自动换装（浏览器态等用户手动重启）。「静默」就是全程无感。
     若提示也开着，下载完成时仍会弹一次「已就绪，重启即可」——静默省掉的是
     「发现版本」这一声吆喝，真正需要用户动手的时刻（重启）不能也瞒着。
   · 只有提示开 → 侧栏「我」亮红点（跟随可用状态持续显示）+ 长 toast 一次
     （每版本一次，落 KV ui.update_notified）。
   · 两者都关 → 启动不查（省一次网络请求），用户仍可在设置页手动检查。
   首次启动会先弹导览蒙层，这里等导览关闭后再提示，避免两条提示打架。 */
const updateNoticePending = ref(false)

function flushUpdateNotice() {
  if (!updateNoticePending.value || tour.open) return
  const latest = updaterStore.info?.latest
  if (!latest) return
  updateNoticePending.value = false
  message.info(t('update.toastAvailable', { version: latest }), 6000)
  void settingsStore.markUpdateNotified(latest)
}

/** 静默自动更新：后台起一次下载（已暂存/已在跑/用户跳过过则不起）。
 *  失败不提示——静默模式的本分就是不打扰；失败详情留在 updater store，
 *  用户手动打开更新弹窗时仍能看到原因与重试按钮。 */
function startSilentUpdate(version: string) {
  if (updaterStore.pendingTag || updaterStore.downloading) return
  if (version === updaterStore.skipped) return
  void updaterStore.download()
}

watch(
  () => settingsStore.loaded,
  async (isLoaded) => {
    if (!isLoaded) return
    const notifyOn = settingsStore.updateNotify !== false
    const autoOn = settingsStore.updateAuto === true
    if (!notifyOn && !autoOn) return
    const result = await updaterStore.check()
    const latest = result?.latest
    if (!result?.available || !latest) return
    if (autoOn) {
      startSilentUpdate(latest)
      return
    }
    if (latest === settingsStore.updateNotified) return
    updateNoticePending.value = true
    // 延时让首屏稳定；若此刻导览已开，flush 会自行让位给导览关闭事件
    window.setTimeout(flushUpdateNotice, 1500)
  },
  { immediate: true },
)

watch(tour.open, (open) => {
  if (!open && updateNoticePending.value) window.setTimeout(flushUpdateNotice, 800)
})

const logo = computed(() =>
  themeStore.isDark ? '/assets/logo_dark.ico' : '/assets/logo_light.ico',
)

/* ── 侧边栏：按框架页分类方式分组（总览 / 资产库 / 监控中心 / 系统）──
   computed 包裹：语言切换时导航文案随词典重算 */
const navGroups = computed<HlSideNavGroup[]>(() => [
  {
    label: t('nav.group.overview'),
    items: [{ label: t('nav.dashboard'), to: '/dashboard', icon: 'dashboard' }],
  },
  {
    label: t('nav.group.assets'),
    items: [
      { label: t('nav.library'), to: '/library', icon: 'store' },
      { label: t('nav.bundles'), to: '/bundles', icon: 'package' },
      { label: t('nav.gamelib'), to: '/gamelib', icon: 'gamepad' },
      { label: t('nav.achievements'), to: '/achievements', icon: 'trophy' },
      { label: t('nav.family'), to: '/family', icon: 'home' },
      { label: t('nav.bills'), to: '/bills', icon: 'list' },
    ],
  },
  {
    label: t('nav.group.monitor'),
    items: [
      { label: t('nav.pool'), to: '/pool', icon: 'target' },
      { label: t('nav.crawl'), to: '/crawl', icon: 'refresh' },
      { label: t('nav.proxies'), to: '/proxies', icon: 'monitor' },
      { label: t('nav.alerts'), to: '/alerts', icon: 'bell' },
      { label: t('nav.rates'), to: '/rates', icon: 'chart' },
    ],
  },
  {
    label: t('nav.group.system'),
    items: [
      { label: t('nav.toolbox'), to: '/toolbox', icon: 'setting' },
      { label: t('nav.logs'), to: '/logs', icon: 'terminal' },
      // 「我」= 设置页，也是更新卡片的落点：有新版本且**提示开着**时这里亮红点
      // （提示关了 = 用户要求零打扰，红点也不能留）
      {
        label: t('nav.me'),
        to: '/settings',
        icon: 'user',
        dot: updaterStore.hasUpdate && settingsStore.updateNotify !== false,
        dotTitle: t('update.navDot'),
      },
      { label: t('nav.about'), to: '/about', icon: 'info' },
    ],
  },
])

/* 折叠状态持久化 */
const COLLAPSE_KEY = 'holdexar-sidebar-collapsed'
const collapsed = ref(localStorage.getItem(COLLAPSE_KEY) === '1')
watch(collapsed, (v) => {
  localStorage.setItem(COLLAPSE_KEY, v ? '1' : '0')
})

/* 页面标题：路由 meta.titleKey → 词典。`t` 在渲染期读 store.locale，
   故切语言时这个 computed 会重算（不再是启动时定死的中文）。 */
const pageTitle = computed(() => {
  const key = route.meta.titleKey as MessageKey | undefined
  return key ? t(key) : APP_NAME
})

/* 浏览器标签页标题与页内 h1 **同源**——否则切路由/切语言时页内变了、标签页不动，
   两个「当前页面在哪」的指示互相打架。route 与 locale 都响应式，故 watchEffect
   一次覆盖两种变化。（main.ts 那句 document.title = APP_NAME 是挂载前的首帧
   兜底：此刻 router 还没 ready，读不到 meta。） */
watchEffect(() => {
  document.title = pageTitle.value
})

/* ── Steam 账户（顶栏余额胶囊 + 头像）──
   胶囊默认显示**主账号**余额；点击弹层展示全部绑定账号各自的余额，
   再点击关闭。未绑定时胶囊引导去「我」页。 */
const primaryAccount = computed(() => accountStore.primary)
const wallet = computed(() => primaryAccount.value?.wallet ?? null)
const walletLabel = computed(() => wallet.value?.balance_display ?? '')

/* ── 余额 CNY 换算（顶栏胶囊 + 弹层共用）──
   汇率拉一次内存缓存（fx_rates 表快照，本地接口毫秒级；汇率本身低频变动，
   随钱包 60s 轮询顺带足够）。CNY 原币跳过；无档案币种显示 —。 */
const rateMap = ref<RateMap>(new Map())
onMounted(async () => {
  try {
    rateMap.value = buildRateMap((await ratesApi.list()).rates)
  } catch {
    /* 静默：换算缺失只是少一行提示，不致命 */
  }
})

/** 换算展示文本：原币 CNY 返回 ''（调用方跳过），缺汇率 '—'，否则 '¥x.xx' */
function walletCnyLabel(w: WalletSnapshot | null): string {
  const info = walletToCny(w, rateMap.value)
  if (info.isCny) return ''
  return formatWalletCny(info.amount)
}

const walletTitle = computed(() => {
  if (!wallet.value) return t('wallet.unboundTip')
  const at = wallet.value.checked_at
    ? t('wallet.titleSynced', { time: wallet.value.checked_at.slice(11, 16) })
    : ''
  const err = wallet.value.error ? ` · ${wallet.value.error}` : ''
  const more = accountStore.accounts.length > 1 ? t('wallet.titleMore') : ''
  return t('wallet.titleMain', { balance: wallet.value.balance_display }) + more + at + err
})

/* 余额弹层（点击胶囊展开 / 再点关闭；点外部也关闭） */
const walletPopOpen = ref(false)
const walletPopRef = ref<HTMLElement | null>(null)
const walletPopPos = ref({ top: 0, right: 0 })

async function toggleWalletPop() {
  if (!accountStore.status?.has_cookie) {
    router.push('/settings')
    return
  }
  // 再点关闭；打开时锚定胶囊正下方（右对齐）
  if (walletPopOpen.value) {
    walletPopOpen.value = false
    return
  }
  const el = walletPopRef.value?.getBoundingClientRect()
  if (el) {
    walletPopPos.value = { top: el.bottom + 8, right: window.innerWidth - el.right }
  }
  walletPopOpen.value = true
}

function onDocClickClose(e: MouseEvent) {
  const target = e.target as HTMLElement
  if (!walletPopOpen.value) return
  // 宿主内点击（胶囊本身走 toggle）与弹层内点击都不关闭
  if (walletPopRef.value?.contains(target)) return
  if (target.closest('.wallet-balances-pop')) return
  walletPopOpen.value = false
}
onMounted(() => document.addEventListener('click', onDocClickClose))

const profile = computed(() => accountStore.status?.profile ?? null)
const avatarSrc = computed(() => profile.value?.avatar_url || '/assets/logo_steam.png')
const avatarName = computed(() => profile.value?.persona_name || 'Holdexar')

/** 弹层内手动刷新当前账号余额（60s 轮询静默，这里给气泡反馈） */
async function manualRefreshWallet() {
  if (accountStore.syncing) return
  await accountStore.sync()
  const w = accountStore.status?.wallet
  if (w?.check_ok) {
    message.success(t('wallet.toastRefreshed', { balance: w.balance_display }))
  } else {
    message.error(accountStore.status?.sync_error || t('wallet.toastFailed'))
  }
}
</script>

<template>
  <div class="app-shell" :class="{ 'is-curtained': closeCurtain }">
    <!-- 侧边栏（HlSideNav：分组导航 + 收拢按键；收起态品牌区悬停换
         「侧边栏」图标，点击展开——新手引导入口在「关于」页 logo） -->
    <HlSideNav
      v-model:collapsed="collapsed"
      :groups="navGroups"
      :brand-name="APP_NAME"
      :logo-src="logo"
    />

    <!-- 主区 -->
    <div class="app-shell__main">
      <header class="app-header">
        <h1 class="app-header__title">{{ pageTitle }}</h1>

        <div class="app-header__pills">
          <!-- 价格更新状态胶囊：只表达产品结论——内部 job / 队列 / 速度不进这里 -->
          <PriceStatusPill />

          <!-- 更新提示胶囊：发现新版 / 下载中 / 待重启时才出现，悬停看更新内容、
               点击打开更新报告窗口（下载中改显百分比，弹窗关掉也看得见进度） -->
          <UpdateEntry />
        </div>

        <div class="app-header__actions">
          <!-- 语言切换（中/EN）：文案显示目标语言，与主题钮同属外观组 -->
          <HlLangToggle :locale="localeStore.locale" @toggle="localeStore.toggle()" />

          <!-- 主题外观切换（纯图标圆钮）—— 与钱包胶囊换位后居前 -->
          <HlThemeToggle :dark="themeStore.isDark" @toggle="themeStore.toggle()" />

          <!-- Steam 钱包余额胶囊（主题钮后、头像前）：默认显示主账号余额，点击弹出
               全部账号余额列表，再点关闭；未绑定时显示引导态 -->
          <div v-if="walletLabel" ref="walletPopRef" class="wallet-pop-host">
            <button
              class="header-pill header-pill--wallet"
              :class="{ 'is-open': walletPopOpen }"
              :title="walletTitle"
              :disabled="accountStore.syncing"
              @click="toggleWalletPop"
            >
              <HlIcon name="wallet" />
              <span :class="{ 'wallet-syncing': accountStore.syncing }">
                {{ walletLabel }}
              </span>
              <!-- CNY 换算（原币 CNY 跳过；缺汇率 —） -->
              <span v-if="walletCnyLabel(wallet)" class="header-pill__fx">
                ≈ {{ walletCnyLabel(wallet) }}
              </span>
              <HlIcon name="chevron-down" style="font-size: 10px" />
            </button>
            <Teleport to="body">
              <div
                v-if="walletPopOpen"
                class="wallet-balances-pop"
                :style="{ top: walletPopPos.top + 'px', right: walletPopPos.right + 'px' }"
              >
                <div class="wallet-balances-pop__head">
                  <span>{{ t('wallet.popTitle') }}</span>
                  <button
                    class="wallet-balances-pop__refresh"
                    :title="t('wallet.popRefresh')"
                    :disabled="accountStore.syncing"
                    @click="manualRefreshWallet"
                  >
                    <HlIcon name="refresh" style="font-size: 13px" />
                  </button>
                </div>
                <div
                  v-for="acc in accountStore.accounts"
                  :key="acc.steam_id"
                  class="wallet-balances-row"
                  :class="{ 'is-primary': acc.is_primary, 'is-active': acc.is_active }"
                >
                  <HlImg class="wallet-balances-row__avatar" :src="acc.avatar_url" alt="">
                    <template #fallback>
                      <span class="wallet-balances-row__avatar wallet-balances-row__avatar--fallback">
                        {{ (acc.persona_name || acc.friend_code || '?').slice(0, 1) }}
                      </span>
                    </template>
                  </HlImg>
                  <span class="wallet-balances-row__name">
                    {{ acc.persona_name || acc.friend_code || t('wallet.noNickname') }}
                    <span v-if="acc.is_primary" class="wallet-balances-row__badge">{{ t('wallet.badgePrimary') }}</span>
                    <span v-if="acc.is_active" class="wallet-balances-row__badge is-cur">{{ t('wallet.badgeCurrent') }}</span>
                  </span>
                  <span class="wallet-balances-row__balance">
                    {{ acc.wallet?.balance_display ?? '—' }}
                    <!-- CNY 换算（原币 CNY 跳过；缺汇率 —） -->
                    <span
                      v-if="acc.wallet && walletCnyLabel(acc.wallet)"
                      class="wallet-balances-row__fx"
                    >
                      ≈ {{ walletCnyLabel(acc.wallet) }}
                    </span>
                  </span>
                </div>
                <div class="wallet-balances-pop__foot">
                  {{ t('wallet.popFoot') }}
                </div>
              </div>
            </Teleport>
          </div>
          <button
            v-else
            class="header-pill header-pill--wallet header-pill--wallet-empty"
            :title="t('wallet.unboundTip')"
            @click="router.push('/settings')"
          >
            <HlIcon name="wallet" />
            <span>{{ t('wallet.bind') }}</span>
          </button>

          <!-- Steam 头像（框架 topbar-avatar 标准，最右上角；绑定后显示真实头像/昵称，
              图片加载失败回退首字符；状态点 = Steam 真实在线状态三态） -->
          <HlTopbarAvatar
            :src="avatarSrc"
            :name="avatarName"
            :online="accountStore.status?.is_online ?? false"
            :in-game="accountStore.status?.in_game || ''"
            :title="t('avatar.title', { name: avatarName })"
            @click="router.push('/settings')"
          />
        </div>
      </header>

      <main class="app-content">
        <!-- 页内分节定位轨：扫描视图内 [data-section]，无分节页自动隐藏 -->
        <HlSectionRail />
        <div class="view-container">
          <router-view v-slot="{ Component }">
            <transition name="route-fade" mode="out-in">
              <keep-alive include="AchievementsHall">
                <component :is="Component" />
              </keep-alive>
            </transition>
          </router-view>
        </div>
      </main>
    </div>

    <!-- 首次启动产品导览（蒙层+聚光+教练标记气泡；settings KV 判定，自动弹出一次）。
         全局唯一实例：导览要跨路由翻页，挂页面里的实例会被 router.push 卸载 -->
    <ProductTour v-model="tour.open" />

    <!-- 全局更新弹窗（检查/下载/校验/重启全在这里闭环；模糊幕布遮住底层页面）。
         更新模块已从设置页搬出——更新是应用级事务，不该塞在某个页签里。 -->
    <UpdateDialog />

    <!-- 关窗幕布：桌面壳弹出「最小化 / 退出程序」原生弹窗前调用 __hlxCloseCurtain(true)
         拉起（见 desktop/main.py 的 _toggle_close_curtain），弹窗落定后撤下。
         幕布放页面里而非原生壳另开遮罩窗：后者会让 DWM 整块重新合成，把弹窗
         上屏拖慢 ~600ms；页面侧零延迟，且只盖内容区不盖标题栏。
         模糊由 .is-curtained 给内容加 filter: blur()（毛玻璃），本遮罩只管压暗：
         WebView2 的合成路径下 backdrop-filter 只压暗不模糊。 -->
    <Transition name="hl-curtain-fade">
      <div v-if="closeCurtain" class="hl-close-curtain" />
    </Transition>
  </div>
</template>

<style scoped>
/* 胶囊宿主：仅定位锚点（弹层 Teleport 到 body，按胶囊 getBoundingClientRect 定位） */
.wallet-pop-host {
  display: inline-flex;
  position: relative;
}

/* 钱包余额胶囊：复用 header-pill 视觉，button 语义可点开/收起 */
.header-pill--wallet {
  cursor: pointer;
  border-color: var(--border-soft);
  color: var(--accent);
  font-weight: 600;
  transition: border-color var(--transition), transform var(--transition);
}

.header-pill--wallet:hover:not(:disabled) {
  border-color: var(--accent);
  transform: translateY(-1px);
}

.header-pill--wallet:disabled {
  cursor: default;
  opacity: 0.7;
}

.header-pill--wallet.is-open {
  border-color: var(--accent);
}

/* 未绑定引导态：虚线描边 + 弱化文字，与真实余额胶囊区分 */
.header-pill--wallet-empty {
  border-style: dashed;
  color: var(--text-muted);
  font-weight: 500;
}

/* 顶栏胶囊内 CNY 换算：弱化小字，绿色 = 换算收益/资产语义 */
.header-pill__fx {
  font-size: 11px;
  font-weight: 500;
  color: var(--success);
  white-space: nowrap;
}

.wallet-syncing {
  animation: wallet-pulse 0.8s ease-in-out infinite alternate;
}

@keyframes wallet-pulse {
  from {
    opacity: 0.55;
  }
  to {
    opacity: 1;
  }
}
</style>

<!-- 全局（非 scoped）：弹层 Teleport 到 body，样式须全局挂载 -->
<style lang="css">
.wallet-balances-pop {
  position: fixed;
  z-index: 1200;
  min-width: 280px;
  max-width: 360px;
  padding: 8px;
  border-radius: 12px;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  box-shadow: 0 10px 32px rgba(0, 0, 0, 0.28);
}

.wallet-balances-pop__head {
  font-size: 11.5px;
  color: var(--text-muted);
  padding: 2px 8px 8px;
  border-bottom: 1px solid var(--border-soft);
  margin-bottom: 4px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.wallet-balances-pop__refresh {
  width: 22px;
  height: 22px;
  border-radius: 6px;
  border: 1px solid transparent;
  background: transparent;
  color: var(--text-muted);
  cursor: pointer;
  display: grid;
  place-items: center;
  transition: var(--transition);
}

.wallet-balances-pop__refresh:hover:not(:disabled) {
  color: var(--accent);
  border-color: var(--border-soft);
}

.wallet-balances-pop__refresh:disabled {
  cursor: default;
  opacity: 0.5;
  animation: wallet-refresh-pulse 0.8s ease-in-out infinite alternate;
}

@keyframes wallet-refresh-pulse {
  from {
    opacity: 0.55;
  }
  to {
    opacity: 1;
  }
}

.wallet-balances-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 8px;
  border-radius: 8px;
}

.wallet-balances-row.is-active {
  background: var(--surface-inset, transparent);
}

.wallet-balances-row__avatar {
  width: 26px;
  height: 26px;
  border-radius: 6px;
  object-fit: cover;
  flex-shrink: 0;
  border: 1px solid var(--border-soft);
}

.wallet-balances-row__avatar--fallback {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: var(--text-muted);
}

.wallet-balances-row__name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 13px;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 5px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wallet-balances-row__badge {
  font-size: 10px;
  padding: 0 5px;
  border-radius: 999px;
  color: var(--accent);
  border: 1px solid var(--accent);
  flex-shrink: 0;
}

.wallet-balances-row__badge.is-cur {
  color: var(--success, #27ae60);
  border-color: var(--success, #27ae60);
}

.wallet-balances-row__balance {
  flex-shrink: 0;
  font-size: 13.5px;
  font-weight: 700;
  color: var(--accent);
  font-variant-numeric: tabular-nums;
}

/* 弹层行内 CNY 换算：小字绿色，余额右侧换行对齐 */
.wallet-balances-row__balance .wallet-balances-row__fx {
  display: block;
  margin-top: 1px;
  font-size: 10.5px;
  font-weight: 500;
  color: var(--success);
  text-align: right;
}

.wallet-balances-pop__foot {
  font-size: 10.5px;
  color: var(--text-muted);
  padding: 6px 8px 2px;
  border-top: 1px solid var(--border-soft);
  margin-top: 4px;
}

/* 更新已下载：全局重启提示 */
.update-ready__text {
  margin: 0 0 8px;
  font-size: 13.5px;
  line-height: 1.65;
  color: var(--text-primary);
}
.update-ready__hint {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
}
</style>

<!-- 系统级「减少动态效果」：钱包同步中的呼吸脉冲是**无限循环**动画，不能靠
     --motion-scale（0s + infinite 会空转），必须显式关掉。此前它漏在
     hl-framework.css 那份降级清单之外——那份清单只管 .hl-* 前缀。 -->
<style scoped>
@media (prefers-reduced-motion: reduce) {
  .wallet-syncing { animation: none; }
}
</style>
