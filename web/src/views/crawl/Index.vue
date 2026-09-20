<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  crawlApi,
  watchPoolApi,
  type CrawlJob,
  type RedeemResultItem,
  type TrackedAccount,
} from '@/api/client'
import { bundlesApi } from '@/api/client'
import { useCrawlStatusStore } from '@/stores/crawlStatus'
import { useSettingsStore } from '@/stores/settings'
import { flagUrl } from '@/api/regions'
import { useRegionsStore } from '@/stores/regions'
import { useI18n } from '@/locales'
import { parseAppRefs, parseFavoritesRefs } from '@/lib/appidRefs'
import { HlAvatar, HlButton, HlDialog, HlIcon, HlInput, HlSwitch, message } from '@/components/ui'

const { t } = useI18n()
const crawl = useCrawlStatusStore()
const regionsStore = useRegionsStore()
const settingsStore = useSettingsStore()

const jobs = ref<CrawlJob[]>([])
const loading = ref(false)
const manualAppids = ref('')
const scope = ref<'appids' | 'wishlist'>('wishlist')

/** 区服列表（服务端下发，含 enabled 状态）；null = 全部启用。
    作用：已购游戏抓取地区的全选/清空备选集与「跟随监控地区」计数
    （监控地区的圈定在监控池页，本页不写 enabled 集）。 */
const availableRegions = computed(() => regionsStore.list)
/** 启用集现值（只读）：跟随态的已购抓取区数展示用 */
const selectedCount = computed(() => regionsStore.enabledCodes.length)

// ── 已购游戏抓取地区（独立于监控地区的子集配置）──────────────
// null = 跟随监控地区（默认，行为与历史一致）；列表 = 已购专属区
const ownedFollow = ref(true) // true = 跟随；false = 自定义
const ownedSelected = ref<string[]>([])
const savingOwned = ref(false)

const ownedCount = computed(() =>
  ownedFollow.value ? selectedCount.value : ownedSelected.value.length,
)

function toggleOwnedRegion(code: string) {
  const set = new Set(ownedSelected.value)
  if (set.has(code)) {
    set.delete(code)
  } else {
    set.add(code)
  }
  ownedSelected.value = [...set]
}

function selectAllOwned() {
  ownedSelected.value = availableRegions.value.map((r) => r.code)
}

function clearAllOwned() {
  ownedSelected.value = []
}

async function saveOwnedRegions() {
  if (ownedFollow.value) {
    try {
      savingOwned.value = true
      await regionsStore.setOwned(null)
      message.success(t('crawl.owned.savedFollow'))
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e))
    } finally {
      savingOwned.value = false
    }
    return
  }
  if (!ownedSelected.value.length) {
    message.warning(t('crawl.owned.emptyWarn'))
    return
  }
  try {
    savingOwned.value = true
    await regionsStore.setOwned(ownedSelected.value)
    message.success(t('crawl.owned.savedCustom', { n: ownedSelected.value.length }))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    savingOwned.value = false
  }
}

// ── 捆绑包导入（Steam 链接识别入库，包内 appid 自动进爬取队列）──
const importText = ref('')
const importing = ref(false)
const importMsg = ref('')

async function doImportBundle() {
  const text = importText.value.trim()
  if (!text || importing.value) return
  importing.value = true
  importMsg.value = ''
  try {
    const r = await bundlesApi.importBundle(text)
    const name = r.name || `#${r.bundleId}`
    importMsg.value = t(r.existed ? 'crawl.bundle.refreshed' : 'crawl.bundle.imported', { name })
    importText.value = ''
    await loadJobs()
  } catch (e) {
    importMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    importing.value = false
  }
}

// ── 批量导入（智能识别链接 appid → 爬取入库作监控数据）──────────────
// 与榜单反哺同语义：导入只爬取入库（games 库），**不写追踪池**——愿望单
// 只保留真实 Steam 账户同步条目，星标关注走游戏卡。后端 /crawl/import
// 单批上限 100，超出分批调；后端只分类（ok 新导入 / own 已在库 / fail 无效），
// 首爬由前端对「新导入」触发（kind=import），任务占用时 409 静默。
const importingApps = ref(false)
const importAppsMsg = ref('')
const importAppsResults = ref<RedeemResultItem[]>([])
const importAppsDone = ref(false)
const importAppsText = ref('')

// ── 收藏列表导入（FAVORITES 粘贴口）────────────────────────
// 源头形态：控制台 copy(JSON.stringify(e.detail.favorites)) 产物——
// 纯数字数组 / 对象数组（appid|appId|id 键）/ 裸 JSON 里夹杂链接文本，
// 统一提取 AppID；与批量导入同走「只入库不进池」语义（导入不产生关注）。
const favText = ref('')
const importingFav = ref(false)
const importFavMsg = ref('')

/**
 * 两个导入口的公共段：分批调 /crawl/import 分类（ok 新导入 / own 已在库 /
 * fail 无效）并汇总结果词条参数；仅对新导入触发首爬——任务占用时 409
 * 静默（重导一次或手动「启动任务」补爬）。绝不写愿望单/关注列表。
 */
async function importBatched(
  appids: number[],
  kind: string,
  unrecognized = 0,
): Promise<{ ok: number; parts: string[]; items: RedeemResultItem[] }> {
  let ok = 0
  let own = 0
  let fail = 0
  let poolIn = 0
  const items: RedeemResultItem[] = []
  for (let i = 0; i < appids.length; i += 100) {
    const r = await crawlApi.importApps(appids.slice(i, i + 100))
    ok += r.ok
    own += r.own
    fail += r.fail
    poolIn += (r.poolAdded ?? 0) + (r.poolRestored ?? 0)
    items.push(...r.results)
  }
  const parts = [t('crawl.import.added', { n: ok }), t('crawl.import.alreadyTracked', { n: own })]
  if (poolIn) parts.push(t('crawl.import.poolAdded', { n: poolIn }))
  if (unrecognized) parts.push(t('crawl.import.unrecognized', { n: unrecognized }))
  if (fail) parts.push(t('crawl.import.invalid', { n: fail }))
  const newIds = items
    .filter((it) => it.status === 'ok' && it.appid)
    .map((it) => it.appid as number)
  if (newIds.length) {
    try {
      await crawlApi.run('appids', newIds, kind)
      crawl.running = true
      parts.push(t('crawl.import.firstCrawlStarted'))
    } catch {
      /* 任务占用：留给手动补爬（重导一次或「启动任务」） */
    }
  }
  return { ok, parts, items }
}

const favPreview = computed(() => parseFavoritesRefs(favText.value))

async function doImportFavorites() {
  const text = favText.value.trim()
  if (!text || importingFav.value) return
  const { appids, invalid } = parseFavoritesRefs(text)
  if (!appids.length) {
    const list = invalid.slice(0, 5).join(', ')
    importFavMsg.value = t(
      invalid.length > 5 ? 'crawl.import.noValidAppidMore' : 'crawl.import.noValidAppid',
      { list },
    )
    return
  }
  importingFav.value = true
  importFavMsg.value = ''
  try {
    const { ok, parts } = await importBatched(appids, 'fav_import', invalid.length)
    importFavMsg.value = parts.join(' · ')
    if (ok > 0) favText.value = ''
    await loadJobs()
  } catch (e) {
    importFavMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    importingFav.value = false
  }
}

const parsedPreview = computed(() => parseAppRefs(importAppsText.value))

async function doImportApps() {
  const text = importAppsText.value.trim()
  if (!text || importingApps.value) return
  const { appids, invalid } = parseAppRefs(text)
  if (invalid.length && !appids.length) {
    const list = invalid.slice(0, 5).join(', ')
    importAppsMsg.value = t(
      invalid.length > 5 ? 'crawl.import.noValidItemsMore' : 'crawl.import.noValidItems',
      { list },
    )
    return
  }
  importingApps.value = true
  importAppsMsg.value = ''
  importAppsResults.value = []
  importAppsDone.value = false
  try {
    const { ok, parts, items } = await importBatched(appids, 'import', invalid.length)
    importAppsResults.value = items
    importAppsDone.value = true
    importAppsMsg.value = parts.join(' · ')
    if (ok > 0) importAppsText.value = ''
    await loadJobs()
  } catch (e) {
    importAppsMsg.value = e instanceof Error ? e.message : String(e)
  } finally {
    importingApps.value = false
  }
}

const progressPercent = computed(() => {
  const total = crawl.total || crawl.done + crawl.qsize
  if (!crawl.running || total === 0) return 0
  return Math.round((crawl.done / total) * 100)
})

// ── 自动价格链启停（只想手动抓的用户关这里：定时爬价 + 失败修复停转）──
const autoPriceToggling = ref(false)

const autoPriceOn = computed(() => settingsStore.autoPrice !== false) // null（未拉到）按开处理

async function toggleAutoPrice(on: boolean) {
  if (autoPriceToggling.value) return
  autoPriceToggling.value = true
  try {
    const ok = await settingsStore.setAutoPrice(on)
    message[ok ? 'success' : 'error'](
      ok
        ? on
          ? t('crawl.autoPrice.on')
          : t('crawl.autoPrice.off')
        : t('crawl.autoPrice.failed'),
    )
  } finally {
    autoPriceToggling.value = false
  }
}

async function loadJobs() {
  loading.value = true
  try {
    await regionsStore.load(true)
    ownedFollow.value = regionsStore.ownedRegions === null
    ownedSelected.value = ownedFollow.value
      ? []
      : [...(regionsStore.ownedRegions ?? [])]
    jobs.value = await crawlApi.jobs(30)
    const active = await crawlApi.active()
    if (active.activeJobId && !crawl.running) {
      // 页面刷新后 SSE 尚无事件：以服务端为准
      crawl.running = true
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

async function start() {
  try {
    let appids: number[] | undefined
    if (scope.value === 'appids') {
      appids = manualAppids.value
        .split(/[,，\s]+/)
        .map((s) => s.trim())
        .filter((s) => /^\d+$/.test(s))
        .map(Number)
      if (!appids.length) {
        message.warning(t('crawl.start.invalidAppids'))
        return
      }
    }
    const res = await crawlApi.run(scope.value, appids)
    message.success(t('crawl.start.started', { id: res.id, count: res.count }))
    crawl.running = true
    await loadJobs()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function stop() {
  try {
    const res = await crawlApi.stop()
    message[res.stopped ? 'success' : 'warning'](
      res.stopped ? t('crawl.stop.requested') : t('crawl.stop.none'),
    )
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

const statusLabel = (status: string) =>
  (
    {
      done: t('crawl.status.done'),
      running: t('crawl.status.running'),
      failed: t('crawl.status.failed'),
      stopped: t('crawl.status.stopped'),
    }
  )[status] ?? status

const statusTag = (status: string) =>
  ({
    done: 'tag--success',
    running: 'tag--accent',
    failed: 'tag--danger',
    stopped: '',
  })[status] ?? ''

const kindLabel = (kind: string) =>
  ({
    manual: t('crawl.kind.manual'),
    scheduled: t('crawl.kind.scheduled'),
    wishlist_sync: t('crawl.kind.wishlistSync'),
    wishlist_sync_owned: t('crawl.kind.ownedSync'),
    missing: t('crawl.kind.missing'),
    repair: t('crawl.kind.repair'),
    backfill: t('crawl.kind.backfill'),
    import: t('crawl.kind.import'),
    fav_import: t('crawl.kind.favImport'),
    pool_add: t('crawl.kind.poolAdd'),
    family_wishlist: t('crawl.kind.familyWishlist'),
    removed_retry: t('crawl.kind.removedRetry'),
    comingsoon_retry: t('crawl.kind.comingsoonRetry'),
    free_promo_retry: t('crawl.kind.freePromoRetry'),
    top100_backfill: t('crawl.kind.top100Backfill'),
    popularnew_backfill: t('crawl.kind.popularnewBackfill'),
    specials_backfill: t('crawl.kind.specialsBackfill'),
    comingsoon_backfill: t('crawl.kind.comingsoonBackfill'),
  })[kind] ?? kind

// ── 账户设置弹窗：按账户开关已购同步（库太大的账户可关，只盯愿望单）──
const showAcctSet = ref(false)
const acctRows = ref<TrackedAccount[]>([])
const acctLoading = ref(false)
const acctToggling = ref('')

/** 好友码 = SteamID64 - 76561197960265728（BigInt；旧后端无字段时前端换算兜底） */
function toFriendCode(steamid: string): string {
  try {
    return String(BigInt(steamid) - 76561197960265728n)
  } catch {
    return steamid
  }
}

function acctName(a: TrackedAccount): string {
  return a.personaName || a.label || a.friendCode || toFriendCode(a.steamid)
}

function acctCode(a: TrackedAccount): string {
  return a.friendCode || toFriendCode(a.steamid)
}

async function openAcctSet() {
  showAcctSet.value = true
  acctLoading.value = true
  try {
    const rows = await watchPoolApi.accounts()
    rows.sort((a, b) => acctName(a).localeCompare(acctName(b), 'zh'))
    acctRows.value = rows
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    acctLoading.value = false
  }
}

async function toggleAcctOwned(a: TrackedAccount, owned: boolean) {
  acctToggling.value = a.steamid
  try {
    await watchPoolApi.updateKinds(a.steamid, { owned })
    a.kinds.owned = owned
    message.success(
      owned
        ? t('crawl.acct.ownedOn', { name: acctName(a) })
        : t('crawl.acct.ownedOff', { name: acctName(a) }),
    )
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    acctToggling.value = ''
  }
}

// ── 收藏列表导入教程（浏览器脚本 + 控制台导出代码）────────────────
//
// 这条通道的数据来自油猴脚本：脚本把收藏存进本地，控制台**主动**查询
// FAVORITES_RESPONSE 才能拿到 JSON（页面加载事件 STEAM_DATA_INIT 只发一次，
// 事后再挂监听收不到）。教程窗把脚本原文与查询代码一并给出，用户不必
// 再到别处翻——脚本随前端产物分发（public/userscript/），打包态由 SPA
// 回退路由按文件路径直出。

/** 油猴脚本静态资源（dev 与打包态同路径；下载链接与文本预览共用） */
const SCRIPT_URL = '/userscript/steamhl-helper.user.js'
/** 下载文件名（ASCII：中文文件名会撞双语红线的「用户可见文案」门禁） */
const SCRIPT_FILENAME = 'SteamHL-Helper.user.js'
/** 控制台导出命令（脚本代码，非用户文案——不进词典，中英同文） */
const FAV_CONSOLE_CODE = `window.addEventListener('FAVORITES_RESPONSE', (e) => copy(JSON.stringify(e.detail.favorites)), { once: true })
window.dispatchEvent(new CustomEvent('FAVORITES_QUERY'))`

const favTutOpen = ref(false)
const favScript = ref('')
const favScriptLoading = ref(false)
const favTutSteps = computed(() => [
  t('crawl.fav.tut.step1'),
  t('crawl.fav.tut.step2'),
  t('crawl.fav.tut.step3'),
  t('crawl.fav.tut.step4'),
])

async function openFavTutorial() {
  favTutOpen.value = true
  if (favScript.value || favScriptLoading.value) return
  favScriptLoading.value = true
  try {
    const resp = await fetch(SCRIPT_URL)
    favScript.value = resp.ok ? await resp.text() : ''
  } catch {
    favScript.value = ''
  } finally {
    favScriptLoading.value = false
  }
}

async function copyTutText(text: string) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    message.success(t('crawl.fav.tut.copied'))
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

onMounted(() => {
  void loadJobs()
  void settingsStore.load()
})
</script>

<template>
  <section class="crawl-page">
    <!-- 启动控制 -->
    <div class="card section-card" data-section="crawl.section.start">
      <div class="section-card__header">
        <div>
          <div class="section-title">{{ t('crawl.section.start') }}</div>
          <div class="section-desc">
            {{ t('crawl.start.desc') }}
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 8px">
          <span class="tag" :class="autoPriceOn ? 'tag--success' : ''">
            {{ autoPriceOn ? t('crawl.autoPrice.badgeOn') : t('crawl.autoPrice.badgeOff') }}
          </span>
          <HlSwitch
            :model-value="autoPriceOn"
            :label="t('crawl.autoPrice.label')"
            accent
            :disabled="autoPriceToggling"
            :title="t('crawl.autoPrice.tip')"
            @update:model-value="toggleAutoPrice"
          />
        </div>
      </div>

      <div class="start-row">
        <el-radio-group v-model="scope">
          <el-radio-button value="wishlist">{{ t('crawl.start.scopeWishlist') }}</el-radio-button>
          <el-radio-button value="appids">{{ t('crawl.start.scopeAppids') }}</el-radio-button>
        </el-radio-group>
        <el-input
          v-if="scope === 'appids'"
          v-model="manualAppids"
          :placeholder="t('crawl.start.appidsPlaceholder')"
          class="start-row__appids"
        />
      </div>

      <div class="start-row">
        <!-- 艺术按键方案二（outline）：启动 = 暖橙 / 停止 = 深色，见 component-framework.html -->
        <HlButton art="outline" size="sm" :disabled="crawl.running" :loading="crawl.running" @click="start">
          <HlIcon v-if="!crawl.running" name="play" />
          {{ crawl.running ? t('crawl.start.running') : t('crawl.start.button') }}
        </HlButton>
        <HlButton art="outline" tone="dark" size="sm" :disabled="!crawl.running" @click="stop">
          <HlIcon name="pause" />
          {{ t('crawl.stop.button') }}
        </HlButton>
        <HlButton variant="default" :title="t('crawl.jobs.refresh')" @click="loadJobs">
          <HlIcon name="refresh" />
        </HlButton>
      </div>

      <!-- 实时进度（SSE）-->
      <div v-if="crawl.running" class="progress-box">
        <el-progress
          :percentage="progressPercent"
          :stroke-width="10"
          :show-text="false"
        />
        <div class="progress-box__meta">
          {{
            t('crawl.progress.meta', {
              done: crawl.done,
              ok: crawl.ok,
              fail: crawl.fail,
              qsize: crawl.qsize,
              speed: crawl.speed,
            })
          }}
        </div>
      </div>
    </div>

    <!-- 捆绑包导入 -->
    <div class="card section-card" data-section="crawl.section.bundleImport">
      <div class="section-title">{{ t('crawl.section.bundleImport') }}</div>
      <div class="section-desc">
        {{ t('crawl.bundle.desc') }}
      </div>
      <div class="start-row">
        <HlInput
          v-model="importText"
          class="bundle-import__input"
          placeholder="https://store.steampowered.com/bundle/…"
          @keydown.enter="doImportBundle"
        />
        <HlButton art="outline" size="sm" :disabled="importing" :loading="importing" @click="doImportBundle">
          <HlIcon v-if="!importing" name="download" />
          {{ importing ? t('crawl.import.importing') : t('crawl.bundle.import') }}
        </HlButton>
      </div>
      <div v-if="importMsg" class="bundle-import__msg">{{ importMsg }}</div>
    </div>

    <!-- 批量导入监控池 -->
    <div class="card section-card" data-section="crawl.section.bulkImport">
      <div class="section-title">{{ t('crawl.section.bulkImport') }}</div>
      <div class="section-desc">
        {{ t('crawl.bulk.desc') }}
      </div>
      <div class="start-row" style="align-items: stretch">
        <textarea
          v-model="importAppsText"
          class="hl-textarea batch-import__input"
          rows="4"
          placeholder="https://store.steampowered.com/app/620&#10;https://steamdb.info/app/570/&#10;105600"
          :disabled="importingApps"
        />
        <div class="batch-import__side">
          <HlButton art="outline" size="sm" :disabled="importingApps" :loading="importingApps" @click="doImportApps">
            <HlIcon v-if="!importingApps" name="download" />
            {{ importingApps ? t('crawl.import.importing') : t('crawl.bulk.import') }}
          </HlButton>
          <HlButton
            v-if="importAppsText"
            variant="text"
            size="sm"
            :disabled="importingApps"
            @click="importAppsText = ''"
          >
            {{ t('crawl.action.clear') }}
          </HlButton>
        </div>
      </div>
      <div v-if="parsedPreview.appids.length || parsedPreview.invalid.length" class="batch-import__preview">
        {{ t('crawl.import.detected', { n: parsedPreview.appids.length }) }}<span v-if="parsedPreview.invalid.length">
          · {{ t('crawl.import.detectedInvalid', { n: parsedPreview.invalid.length }) }}</span>
      </div>
      <div v-if="importAppsMsg" class="bundle-import__msg">{{ importAppsMsg }}</div>
      <!-- 结果明细（摘要封顶 10 行约定：超出折叠为计数提示） -->
      <div v-if="importAppsDone && importAppsResults.length" class="batch-import__results">
        <div
          v-for="it in importAppsResults.slice(0, 10)"
          :key="`${it.appid}-${it.status}-${it.detail}`"
          class="batch-import__result"
          :class="`is-${it.status}`"
        >
          <span class="batch-import__appid">{{ it.appid }}</span>
          <span>{{ it.detail }}</span>
        </div>
        <div v-if="importAppsResults.length > 10" class="batch-import__more">
          {{ t('crawl.bulk.moreHidden', { n: importAppsResults.length - 10 }) }}
        </div>
      </div>
    </div>

    <!-- 收藏列表导入（FAVORITES 通道；导入即入池 + 首爬） -->
    <div class="card section-card" data-section="crawl.section.favImport">
      <div class="section-card__header">
        <div>
          <div class="section-title">{{ t('crawl.section.favImport') }}</div>
          <!-- 整句一条词条，两枚 <code> 由词条自带、v-html 渲染（见 zh-CN/crawl.ts 的说明：
           切成三段会把两个代码标识符钉死在固定位置，英文只能拼出 "A → B" 这种
           读成转换关系的写法，而原意是嵌套）。词条是应用自有静态文案（非用户输入），
           v-html 无注入面；本处无 code 样式，<code> 走浏览器默认。
           vue/no-v-html 未启用（flat/essential 不含），与 HlBanner / bundles 同款写法。 -->
          <div class="section-desc" v-html="t('crawl.fav.desc')"></div>
        </div>
        <HlButton variant="text" size="sm" @click="openFavTutorial">
          <HlIcon name="info" :size="14" />
          {{ t('crawl.fav.tutorial') }}
        </HlButton>
      </div>
      <div class="start-row" style="align-items: stretch">
        <textarea
          v-model="favText"
          class="hl-textarea batch-import__input"
          rows="4"
          :placeholder="t('crawl.fav.placeholder')"
          :disabled="importingFav"
        />
        <div class="batch-import__side">
          <HlButton art="outline" size="sm" :disabled="importingFav" :loading="importingFav" @click="doImportFavorites">
            <HlIcon v-if="!importingFav" name="download" />
            {{ importingFav ? t('crawl.import.importing') : t('crawl.fav.import') }}
          </HlButton>
          <HlButton
            v-if="favText"
            variant="text"
            size="sm"
            :disabled="importingFav"
            @click="favText = ''"
          >
            {{ t('crawl.action.clear') }}
          </HlButton>
        </div>
      </div>
      <div v-if="favPreview.appids.length || favPreview.invalid.length" class="batch-import__preview">
        {{ t('crawl.import.detected', { n: favPreview.appids.length }) }}<span v-if="favPreview.invalid.length">
          · {{ t('crawl.import.detectedInvalid', { n: favPreview.invalid.length }) }}</span>
      </div>
      <div v-if="importFavMsg" class="bundle-import__msg">{{ importFavMsg }}</div>
    </div>

    <!-- 收藏导入教程：数据来源（浏览器脚本）+ 导出代码，脚本随产物分发 -->
    <HlDialog v-model="favTutOpen" :title="t('crawl.fav.tutTitle')" :width="640">
      <ol class="fav-tut__steps">
        <li v-for="step in favTutSteps" :key="step">{{ step }}</li>
      </ol>

      <div class="fav-tut__label">
        {{ t('crawl.fav.tut.console') }}
        <HlButton variant="text" size="sm" @click="copyTutText(FAV_CONSOLE_CODE)">
          {{ t('crawl.fav.tut.copy') }}
        </HlButton>
      </div>
      <pre class="fav-tut__code">{{ FAV_CONSOLE_CODE }}</pre>

      <div class="fav-tut__label">
        {{ t('crawl.fav.tut.script') }}
        <HlButton variant="text" size="sm" :loading="favScriptLoading" @click="copyTutText(favScript)">
          {{ t('crawl.fav.tut.copy') }}
        </HlButton>
        <a class="fav-tut__link" :href="SCRIPT_URL" :download="SCRIPT_FILENAME">
          {{ t('crawl.fav.tut.download') }}
        </a>
      </div>
      <pre class="fav-tut__code fav-tut__code--long">{{ favScript || t('crawl.fav.tut.scriptEmpty') }}</pre>
    </HlDialog>

    <!-- 已购游戏抓取地区 -->
    <div class="card section-card" data-section="crawl.section.owned">
      <div class="section-card__header">
        <div>
          <div class="section-title">{{ t('crawl.section.owned') }}</div>
          <div class="section-desc">
            {{ t('crawl.owned.desc') }}
          </div>
        </div>
        <div style="display: flex; align-items: center; gap: 8px">
          <span v-if="!ownedFollow" class="tag tag--accent">
            {{ t('crawl.regions.selected', { n: ownedCount, total: availableRegions.length }) }}
          </span>
          <HlButton art="outline" size="sm" :title="t('crawl.owned.acctSetTip')" @click="openAcctSet">
            <HlIcon name="setting" />
            {{ t('crawl.owned.acctSet') }}
          </HlButton>
          <el-button
            type="primary"
            size="small"
            :loading="savingOwned"
            @click="saveOwnedRegions"
          >
            <HlIcon v-if="!savingOwned" name="check" />
            {{ t('crawl.action.save') }}
          </el-button>
        </div>
      </div>

      <div class="owned-follow-row">
        <HlSwitch v-model="ownedFollow" :label="t('crawl.owned.follow')" accent />
        <span class="owned-follow-row__hint">
          {{ ownedFollow ? t('crawl.owned.hintFollow') : t('crawl.owned.hintCustom') }}
        </span>
      </div>

      <div v-if="!ownedFollow" class="region-grid">
        <button
          v-for="region in availableRegions"
          :key="region.code"
          class="region-item"
          :class="{ 'is-active': ownedSelected.includes(region.code) }"
          @click="toggleOwnedRegion(region.code)"
        >
          <span class="region-item__text" style="flex-direction: row; align-items: center; gap: 8px; display: flex">
            <img
              :src="flagUrl(region.code)"
              style="width: 18px; height: 13px; border-radius: 2px; flex-shrink: 0"
              :alt="region.code"
              loading="lazy"
              decoding="async"
            >
            <span style="display: flex; flex-direction: column; line-height: 1.35; min-width: 0">
              <span class="region-item__name">{{ region.name }}</span>
              <span class="region-item__meta">{{ region.code.toUpperCase() }} · {{ region.currency }}</span>
            </span>
          </span>
          <span class="region-item__check">
            <HlIcon v-if="ownedSelected.includes(region.code)" name="check" :size="12" />
          </span>
        </button>
      </div>
    </div>

    <!-- 账户设置弹窗：按账户开关已购同步 -->
    <HlDialog v-model="showAcctSet" :title="t('crawl.acct.title')" :width="520">
      <div class="acct-set__hint">
        {{ t('crawl.acct.hint') }}
      </div>
      <div v-loading="acctLoading" class="acct-set__list">
        <div v-for="a in acctRows" :key="a.steamid" class="acct-set__row">
          <HlAvatar :src="a.avatarUrl || ''" :name="acctName(a)" size="sm" />
          <div class="acct-set__main">
            <div class="acct-set__name">{{ acctName(a) }}</div>
            <div class="acct-set__meta">
              {{ t('crawl.acct.meta', { code: acctCode(a), n: a.ownedCount ?? 0 }) }}
            </div>
          </div>
          <HlSwitch
            :model-value="a.kinds?.owned"
            accent
            :disabled="acctToggling === a.steamid"
            @update:model-value="(v: boolean) => toggleAcctOwned(a, v)"
          />
        </div>
        <div v-if="!acctLoading && !acctRows.length" class="acct-set__empty">
          {{ t('crawl.acct.empty') }}
        </div>
      </div>
    </HlDialog>

    <!-- 任务记录 -->
    <div class="card section-card" data-section="crawl.section.jobs">
      <div class="section-title">{{ t('crawl.section.jobs') }}</div>
      <el-table :data="jobs" style="width: 100%" size="small" v-loading="loading">
        <el-table-column prop="id" label="#" width="56" />
        <el-table-column :label="t('crawl.jobs.kind')" width="110">
          <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
        </el-table-column>
        <el-table-column :label="t('crawl.jobs.status')" width="90">
          <template #default="{ row }">
            <span class="tag" :class="statusTag(row.status)">{{ statusLabel(row.status) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('crawl.jobs.regions')" min-width="120">
          <template #default="{ row }">
            <span v-if="row.regions">{{ row.regions.join(', ').toUpperCase() }}</span>
            <span v-else>{{ t('common.all') }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('crawl.jobs.stats')" min-width="220">
          <template #default="{ row }">
            <span v-if="row.stats" class="job-stats">
              {{
                t('crawl.jobs.statsText', {
                  ok: row.stats.success,
                  fail: row.stats.failed,
                  skip: row.stats.skipped_no_discount,
                  secs: row.stats.elapsed_seconds,
                })
              }}
            </span>
            <span v-else-if="row.error" class="job-error">{{ row.error }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('crawl.jobs.startedAt')" width="160">
          <template #default="{ row }">
            <span v-if="row.startedAt" class="job-stats">{{ row.startedAt.slice(5, 19).replace('T', ' ') }}</span>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </section>
</template>

<style scoped>
.crawl-page {
  max-width: 960px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.section-card {
  padding: 20px 24px;
}

.section-card__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.start-row {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.start-row__appids {
  width: 260px;
}

.bundle-import__input {
  width: 320px;
}

.batch-import__input {
  flex: 1;
  min-width: 280px;
}

.batch-import__side {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 8px;
}

.batch-import__preview {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-muted);
}

.batch-import__results {
  margin-top: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 220px;
  overflow-y: auto;
}

.batch-import__result {
  display: flex;
  gap: 10px;
  font-size: 12px;
  color: var(--text-secondary);
}

.batch-import__result.is-fail {
  color: var(--danger);
}

.batch-import__result .batch-import__appid {
  min-width: 64px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
}

.batch-import__more {
  font-size: 12px;
  color: var(--text-muted);
}

.bundle-import__msg {
  margin-top: 10px;
  font-size: 12px;
  color: var(--text-muted);
  word-break: break-all;
}

.progress-box {
  margin-top: 16px;
}

.progress-box__meta {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-muted);
}

.job-stats {
  font-size: 12px;
  color: var(--text-muted);
}

.job-error {
  font-size: 12px;
  color: var(--danger);
}

.region-grid {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
}

.owned-follow-row {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.acct-set__hint {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 12px;
}

.acct-set__list {
  min-height: 80px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 50vh;
  overflow-y: auto;
}

.acct-set__row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 6px;
  border-radius: var(--radius);
}

.acct-set__row:hover {
  background: var(--bg-soft);
}

.acct-set__main {
  flex: 1;
  min-width: 0;
}

.acct-set__name {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.acct-set__meta {
  font-size: 11px;
  color: var(--text-muted);
}

.acct-set__empty {
  font-size: 12px;
  color: var(--text-muted);
  text-align: center;
  padding: 20px 0;
}

.owned-follow-row__hint {
  font-size: 12px;
  color: var(--text-muted);
}

.region-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  background: var(--bg-soft);
  color: var(--text-muted);
  font-size: 13px;
  text-align: left;
  transition: all var(--transition);
  cursor: pointer;
}

.region-item:hover {
  border-color: var(--border-strong);
  color: var(--text-secondary);
}

.region-item.is-active {
  border-color: var(--success-a50);
  background: var(--success-a08);
  color: var(--success);
}

.region-item__text {
  display: flex;
  flex-direction: column;
  line-height: 1.35;
  min-width: 0;
}

.region-item__name {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.region-item__meta {
  font-size: 11px;
  opacity: 0.75;
}

.region-item__check {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 5px;
  border: 1px solid var(--border-strong);
  flex-shrink: 0;
  transition: all var(--transition);
}

.region-item.is-active .region-item__check {
  background: var(--success);
  border-color: var(--success);
  color: var(--ink-on-fill);
}

/* ── 收藏导入教程窗（步骤 + 控制台代码 + 油猴脚本原文）── */
.fav-tut__steps {
  margin: 0 0 4px;
  /* 序号标记画在左 padding 区，且全局 reset 会收走 list-style——
     两处都要显式给回，否则步骤没有编号 */
  padding-left: 20px;
  list-style: decimal;
  font-size: 12.5px;
  line-height: 1.85;
  color: var(--text-secondary);
}

.fav-tut__label {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 14px 0 6px;
  font-size: 12px;
  color: var(--text-muted);
}

.fav-tut__code {
  margin: 0;
  padding: 10px 12px;
  border: 1px solid var(--line-1);
  border-radius: 8px;
  background: var(--surface-inset);
  font-family: var(--font-mono);
  font-size: 11.5px;
  line-height: 1.65;
  color: var(--text-secondary);
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 170px;
  overflow-y: auto;
}

.fav-tut__code--long {
  max-height: 260px;
}

.fav-tut__link {
  font-size: 12px;
  color: var(--accent);
  text-decoration: none;
}

.fav-tut__link:hover {
  text-decoration: underline;
}
</style>
