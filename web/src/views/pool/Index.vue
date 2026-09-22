<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { watchPoolApi, type PoolItemPayload, type TrackedAccount } from '@/api/client'
import { useRegionsStore } from '@/stores/regions'
import { useI18n, type MessageKey } from '@/locales'
import { parseFavoritesRefs } from '@/lib/appidRefs'
import RegionFlag from '@/components/RegionFlag.vue'
import {
  HlAvatar,
  HlButton,
  HlCheckbox,
  HlChip,
  HlDialog,
  HlEmpty,
  HlIcon,
  HlImg,
  HlInput,
  HlPagination,
  HlPopconfirm,
  HlSkeleton,
  HlTable,
  HlTextarea,
  HlTooltip,
  message,
  type HlTableColumn,
  type IconName,
} from '@/components/ui'

/* 监控池：数据源（账户绑定）→ 抓取区服（监控地区）→ 池内容（监控条目）。
   监控条目是池内所有必爬的游戏，来源四类——愿望单（Steam 同步）、
   关注（游戏卡星标）、已购（库同步）、手动添加（本页/任务页导入）；
   愿望单与关注是叠加在监控条目之上的爬取第一优先级。
   本页承担监控池管理：条目的添加（单个/批量粘贴）与移除（管理模式下多选），
   以及监控地区的圈定（自任务页迁入，同属监控域）。 */

const router = useRouter()
const route = useRoute()
const { t } = useI18n()
const regionsStore = useRegionsStore()

// ─── Steam 账户（监控池数据源）──────────────────────────────

const accounts = ref<TrackedAccount[]>([])
const items = ref<PoolItemPayload[]>([])
const newSteamid = ref('')
const newLabel = ref('')
const loading = ref(true)
const syncingId = ref<string | null>(null)
let personaRetries = 0

// ─── 监控条目类别（关注 / 愿望单 / 已购 / 榜单 / 手动添加）─────────
//
// 主类别取爬取优先级口径的展示映射：关注 > 愿望单 > 已购 > 榜单 > 手动。
// 一个条目可能同时命中多个来源（如愿望单条目后来被星标），展示取最靠前
// 的那个；类别计数互斥、合计 = 条目总数（筛选 tab 的数字才对得上）。

type PoolKind = 'follow' | 'wishlist' | 'owned' | 'board' | 'manual'

/** 类别词条 key（存 key 不存译文：模块级常量只求值一次，会把语言冻住） */
const KIND_LABEL_KEYS: Record<PoolKind, MessageKey> = {
  follow: 'pool.items.kind.follow',
  wishlist: 'pool.items.kind.wishlist',
  owned: 'pool.items.kind.owned',
  board: 'pool.items.kind.board',
  manual: 'pool.items.kind.manual',
}

const KIND_ICONS: Record<PoolKind, IconName> = {
  follow: 'star',
  wishlist: 'heart',
  owned: 'check',
  board: 'trophy',
  manual: 'plus',
}

function itemKind(it: PoolItemPayload): PoolKind {
  if (it.followed) return 'follow'
  if (it.wishlisted) return 'wishlist'
  if (it.owned) return 'owned'
  if (it.boardPool) return 'board'
  return 'manual'
}

const filterKind = ref<'all' | PoolKind>('all')

const kindCounts = computed(() => {
  const c = { all: items.value.length, follow: 0, wishlist: 0, owned: 0, board: 0, manual: 0 }
  for (const it of items.value) c[itemKind(it)] += 1
  return c
})

/** 类别筛选标签（渲染期取译文；计数随条目集重算；icon 空串 = 不渲染） */
const kindTabs = computed(() => [
  { id: 'all' as const, label: t('pool.items.kind.all'), count: kindCounts.value.all, icon: '' },
  { id: 'follow' as const, label: t(KIND_LABEL_KEYS.follow), count: kindCounts.value.follow, icon: KIND_ICONS.follow },
  { id: 'wishlist' as const, label: t(KIND_LABEL_KEYS.wishlist), count: kindCounts.value.wishlist, icon: KIND_ICONS.wishlist },
  { id: 'owned' as const, label: t(KIND_LABEL_KEYS.owned), count: kindCounts.value.owned, icon: KIND_ICONS.owned },
  { id: 'board' as const, label: t(KIND_LABEL_KEYS.board), count: kindCounts.value.board, icon: KIND_ICONS.board },
  { id: 'manual' as const, label: t(KIND_LABEL_KEYS.manual), count: kindCounts.value.manual, icon: KIND_ICONS.manual },
])

/** 监控条目本地搜索：中文名/英文名/appid 大小写不敏感过滤（数据已全量在前端，无需后端查询） */
const search = ref('')
const filteredItems = computed(() => {
  const q = search.value.trim().toLowerCase()
  let list = items.value
  if (filterKind.value !== 'all') {
    list = list.filter((it) => itemKind(it) === filterKind.value)
  }
  if (!q) return list
  return list.filter(
    (it) =>
      (it.name || '').toLowerCase().includes(q) ||
      (it.nameEn || '').toLowerCase().includes(q) ||
      String(it.appid).includes(q),
  )
})

// ─── 渲染层分页 ───
// 数据仍全量留在前端供本地搜索，但模板只挂当前页：每个 chip 都带一个富气泡
// tooltip（内嵌封面图），上万条全量挂载会把打开页面的渲染拖到秒级卡死。
const PAGE_SIZE = 120
const page = ref(1)
const pagedItems = computed(() =>
  filteredItems.value.slice((page.value - 1) * PAGE_SIZE, page.value * PAGE_SIZE),
)
// 搜索词/类别筛选或数据集替换后回第一页，防旧页码悬空
watch([search, filterKind, items], () => {
  page.value = 1
})

/** 好友码 = SteamID64 - 76561197960265728（BigInt，超出 Number 精度） */
function toFriendCode(steamid: string): string {
  try {
    return String(BigInt(steamid) - 76561197960265728n)
  } catch {
    return steamid
  }
}

/** 展示名：Steam 昵称优先，备注名兜底，最后好友码 */
function acctName(a: TrackedAccount): string {
  return a.personaName || a.label || a.friendCode || toFriendCode(a.steamid)
}

/** 第二行好友码：展示名已是好友码（无昵称/备注的降级态）时不重复 */
function acctCode(a: TrackedAccount): string {
  const code = a.friendCode || toFriendCode(a.steamid)
  return acctName(a) === code ? '' : t('pool.account.friendCode', { code })
}

/** 账户表列定义（HlTable：声明式列 + 插槽渲染） */
const accountCols = computed<HlTableColumn[]>(() => [
  { key: 'account', label: t('pool.account.colAccount') },
  { key: 'itemCount', label: t('pool.account.colItemCount'), numeric: true },
  { key: 'lastSync', label: t('pool.account.colLastSync') },
  { key: 'actions', label: t('pool.account.colActions') },
])

/** HlTable 的行数据：接口字段渲染期组装（切语言时表头随 computed 重算） */
const accountRows = computed(() =>
  accounts.value.map((a) => ({
    account: a,
    itemCount: a.itemCount,
    lastSync: a.lastSyncAt
      ? a.lastSyncAt.slice(0, 19).replace('T', ' ')
      : t('pool.account.neverSynced'),
    actions: a,
  })),
)

async function load() {
  loading.value = true
  try {
    // 账户列表与条目无数据依赖,并行拉取;任一失败照常报错(与原串行语义一致)
    const results = await Promise.allSettled([watchPoolApi.accounts(), watchPoolApi.items()])
    const [accRes, itemRes] = results
    if (accRes.status === 'fulfilled') accounts.value = accRes.value
    if (itemRes.status === 'fulfilled') items.value = itemRes.value
    const failed = results.find((r) => r.status === 'rejected')
    if (failed) {
      const reason = failed.reason
      message.error(reason instanceof Error ? reason.message : String(reason))
    }
  } finally {
    loading.value = false
  }
  schedulePersonaRefetch()
}

/** 昵称/头像由后端在返回列表后异步补拉：发现缺口就静默重取账户列表（上限 3 次） */
function schedulePersonaRefetch() {
  if (personaRetries >= 3) return
  if (!accounts.value.some((a) => !a.personaName)) return
  personaRetries++
  setTimeout(async () => {
    try {
      accounts.value = await watchPoolApi.accounts()
    } catch {
      /* 静默：补拉是增强展示，失败等下次进页 */
    }
    schedulePersonaRefetch()
  }, 5000)
}

async function add() {
  const raw = newSteamid.value.trim()
  if (!raw) return
  try {
    const res = await watchPoolApi.add(raw, newLabel.value.trim())
    message.success(
      t('pool.account.bindSuccess', { code: res.friendCode || toFriendCode(res.steamid) }),
    )
    newSteamid.value = ''
    newLabel.value = ''
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function sync(account: TrackedAccount) {
  syncingId.value = account.steamid
  try {
    const res = await watchPoolApi.sync(account.steamid)
    if (res.added > 0) {
      // 是否自动开爬决定整句（两条独立词条，不在组件侧拼分句）
      message.success(
        res.crawlTriggered
          ? t('pool.account.syncAddedCrawling', { n: res.added })
          : t('pool.account.syncAdded', { n: res.added }),
      )
    } else {
      message.info(t('pool.account.syncNoNew', { n: res.wishlistCount }))
    }
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    syncingId.value = null
  }
}

async function remove(account: TrackedAccount) {
  await watchPoolApi.remove(account.steamid)
  message.success(t('pool.account.unbindSuccess'))
  await load()
}

// ─── 监控池管理：添加条目（单个 / 批量粘贴 / 导入文件）──────
//
// 添加即入池（必爬）；批量粘贴、文件导入与任务页导入共用
// parseFavoritesRefs（链接 / 裸数字 / JSON 数组同口径）。单批上限 100
// 由前端分批（与导入通道一致）。
//
// 「导入文件」带文件名提交（source）：后端把 appid 登记进预设游戏池
// 清单（随资产种子分发的出厂内容，见后端 games/preset.py）。

const addOpen = ref(false)
const addText = ref('')
const adding = ref(false)
/** 文件导入来源（文件名）：非空时提交携带并登记预设池；手动编辑文本即清除 */
const addSource = ref('')
const fileInput = ref<HTMLInputElement | null>(null)
const addPreview = computed(() => parseFavoritesRefs(addText.value))

function pickFile() {
  fileInput.value?.click()
}

async function onPickFile(ev: Event) {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = '' // 复位：允许连续两次选择同一文件
  if (!file) return
  try {
    addText.value = await file.text()
    addSource.value = file.name
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

async function submitAdd() {
  const { appids, invalid } = addPreview.value
  if (!appids.length) {
    message.warning(t('pool.items.addNoValid'))
    return
  }
  adding.value = true
  try {
    let added = 0
    let restored = 0
    let exists = 0
    let failed = invalid.length
    for (let i = 0; i < appids.length; i += 100) {
      const r = await watchPoolApi.addItems(
        appids.slice(i, i + 100),
        addSource.value || undefined,
      )
      added += r.added ?? 0
      restored += r.restored ?? 0
      exists += r.exists ?? 0
      failed += r.fail ?? 0
    }
    message.success(t('pool.items.addResult', { added, restored, exists, invalid: failed }))
    addOpen.value = false
    addText.value = ''
    addSource.value = ''
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    adding.value = false
  }
}

// ─── 监控池管理：管理模式（多选批量移除）───────────────────

const manageMode = ref(false)
const selected = ref<Set<number>>(new Set())
const removing = ref(false)

const selectedItemsCount = computed(() => selected.value.size)
const pageSelectedCount = computed(
  () => pagedItems.value.filter((it) => selected.value.has(it.appid)).length,
)
const allPageSelected = computed(
  () => pagedItems.value.length > 0 && pageSelectedCount.value === pagedItems.value.length,
)

function toggleManage() {
  manageMode.value = !manageMode.value
  if (!manageMode.value) selected.value = new Set()
}

function toggleSelect(appid: number) {
  const next = new Set(selected.value)
  if (next.has(appid)) next.delete(appid)
  else next.add(appid)
  selected.value = next
}

function toggleSelectPage() {
  const next = new Set(selected.value)
  if (allPageSelected.value) {
    for (const it of pagedItems.value) next.delete(it.appid)
  } else {
    for (const it of pagedItems.value) next.add(it.appid)
  }
  selected.value = next
}

/** 条目点击：管理模式下切换选中，否则跳转价格详情 */
function onItemClick(it: PoolItemPayload) {
  if (manageMode.value) toggleSelect(it.appid)
  else router.push(`/game/${it.appid}`)
}

async function removeSelected() {
  const ids = [...selected.value]
  if (!ids.length || removing.value) return
  removing.value = true
  try {
    let removed = 0
    for (let i = 0; i < ids.length; i += 100) {
      const r = await watchPoolApi.removeItems(ids.slice(i, i + 100))
      removed += r.removed ?? 0
    }
    message.success(t('pool.items.removeResult', { n: removed }))
    selected.value = new Set()
    await load()
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    removing.value = false
  }
}

// ─── 监控地区（自任务页迁入：爬取区服的圈定）────────────────

/** 区服列表（服务端下发，含 enabled 状态）；null = 全部启用 */
const availableRegions = computed(() => regionsStore.list)
const enabledRegions = ref<string[] | null>(null)
const savingRegions = ref(false)

/** 地区搜索：区名（含服务端下发的中文全名）/ 代码 / 币种，大小写不敏感 */
const regionSearch = ref('')
const filteredRegions = computed(() => {
  const q = regionSearch.value.trim().toLowerCase()
  if (!q) return availableRegions.value
  return availableRegions.value.filter(
    (r) =>
      r.name.toLowerCase().includes(q) ||
      r.code.toLowerCase().includes(q) ||
      r.currency.toLowerCase().includes(q),
  )
})

const selectedCount = computed(() =>
  enabledRegions.value === null
    ? availableRegions.value.length
    : enabledRegions.value.length,
)

function isSelected(code: string) {
  return allSelected.value || enabledRegions.value?.includes(code) === true
}

function toggleRegion(code: string) {
  if (enabledRegions.value === null) {
    enabledRegions.value = availableRegions.value
      .map((r) => r.code)
      .filter((c) => c !== code)
    return
  }
  const set = new Set(enabledRegions.value)
  if (set.has(code)) {
    set.delete(code)
  } else {
    set.add(code)
  }
  enabledRegions.value = [...set]
}

const allSelected = computed(
  () =>
    enabledRegions.value === null ||
    (enabledRegions.value.length === availableRegions.value.length &&
      availableRegions.value.length > 0),
)

function selectAll() {
  enabledRegions.value = null
}

function clearAll() {
  enabledRegions.value = []
}

async function saveRegions() {
  savingRegions.value = true
  try {
    await regionsStore.setEnabled(enabledRegions.value)
    message.success(
      enabledRegions.value !== null && enabledRegions.value.length === 0
        ? t('pool.regions.savedNone')
        : t('pool.regions.savedStrict'),
    )
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    savingRegions.value = false
  }
}

onMounted(async () => {
  load()
  // 区服启用集随列表下发（store 启动时已 load 过一次，这里刷新拿 enabled 现值）
  await regionsStore.load(true)
  enabledRegions.value = regionsStore.list.every((r) => r.enabled)
    ? null
    : regionsStore.list.filter((r) => r.enabled).map((r) => r.code)
  // 空库首屏的「导入游戏列表 / 文件」直达：?add=1 直接开添加对话框
  if (route.query.add === '1') addOpen.value = true
})
</script>

<template>
  <section class="pool-page">
    <!-- Steam 账户（监控池数据源） -->
    <div class="card section-card" data-section="pool.section.steamAccount">
      <div class="section-title">{{ t('pool.section.steamAccount') }}</div>
      <div class="section-desc">
        {{ t('pool.account.desc') }}
      </div>

      <div class="add-row">
        <HlInput
          v-model="newSteamid"
          :placeholder="t('pool.account.steamidPlaceholder')"
          class="add-row__input"
          @keydown.enter="add"
        />
        <HlInput
          v-model="newLabel"
          :placeholder="t('pool.account.labelPlaceholder')"
          class="add-row__label"
          @keydown.enter="add"
        />
        <HlButton variant="primary" size="sm" @click="add">
          <HlIcon name="plus" :size="14" />
          {{ t('pool.account.bind') }}
        </HlButton>
      </div>

      <HlSkeleton v-if="loading" variant="text" :rows="1" avatar :count="4" />
      <template v-else>
        <HlTable :columns="accountCols" :rows="accountRows">
          <template #account="{ row }">
            <div class="acct-row">
              <HlAvatar :src="row.account.avatarUrl || ''" :name="acctName(row.account)" size="sm" />
              <div class="acct-main">
                <div class="acct-name">
                  {{ acctName(row.account) }}
                  <span v-if="row.account.kinds?.wishlist" class="acct-kind">{{ t('pool.account.kindWishlist') }}</span>
                  <span v-if="row.account.kinds?.owned" class="acct-kind acct-kind--owned">{{ t('pool.account.kindOwned') }}</span>
                </div>
                <div class="acct-code">
                  {{ acctCode(row.account) }}<template v-if="row.account.label && row.account.label !== acctName(row.account)"> · {{ row.account.label }}</template>
                </div>
              </div>
            </div>
          </template>
          <template #lastSync="{ row }">
            <span class="acct-sync">{{ row.lastSync }}</span>
          </template>
          <template #actions="{ row }">
            <div class="acct-actions">
              <HlButton
                variant="primary"
                size="sm"
                :loading="syncingId === row.account.steamid"
                @click="sync(row.account)"
              >
                {{ t('pool.account.sync') }}
              </HlButton>
              <HlPopconfirm
                :text="t('pool.account.unbindConfirm', { name: acctName(row.account) })"
                :confirm-label="t('common.confirm')"
                :cancel-label="t('common.cancel')"
                @confirm="remove(row.account)"
              >
                <HlButton variant="danger" size="sm" :title="t('pool.account.unbindSuccess')">
                  <HlIcon name="delete" :size="14" />
                </HlButton>
              </HlPopconfirm>
            </div>
          </template>
        </HlTable>
        <HlEmpty
          v-if="!accounts.length"
          icon=""
          :text="t('pool.items.empty')"
        />
      </template>
    </div>

    <!-- 监控地区（自任务页迁入） -->
    <div class="card section-card" data-section="pool.section.regions">
      <div class="section-card__header">
        <div>
          <div class="section-title">{{ t('pool.section.regions') }}</div>
          <div class="section-desc">
            {{ t('pool.regions.desc') }}
          </div>
        </div>
        <div class="region-actions">
          <span class="tag tag--accent">{{
            t('pool.regions.selected', { n: selectedCount, total: availableRegions.length })
          }}</span>
          <HlButton size="sm" @click="selectAll">{{ t('common.selectAll') }}</HlButton>
          <HlButton size="sm" @click="clearAll">{{ t('common.clear') }}</HlButton>
          <HlButton variant="primary" size="sm" :loading="savingRegions" @click="saveRegions">
            <HlIcon v-if="!savingRegions" name="check" :size="14" />
            {{ t('common.save') }}
          </HlButton>
        </div>
      </div>

      <!-- 地区搜索（与监控条目搜索同款交互） -->
      <div class="region-search">
        <HlInput
          v-model="regionSearch"
          :placeholder="t('pool.regions.searchPlaceholder')"
          prefix-icon="search"
          class="region-search__input"
        />
      </div>

      <div v-if="filteredRegions.length" class="region-grid">
        <button
          v-for="region in filteredRegions"
          :key="region.code"
          class="region-item"
          :class="{ 'is-active': isSelected(region.code) }"
          @click="toggleRegion(region.code)"
        >
          <span class="region-item__text">
            <RegionFlag :code="region.code" />
            <span class="region-item__meta">{{ region.code.toUpperCase() }} · {{ region.currency }}</span>
          </span>
          <span class="region-item__check">
            <HlIcon v-if="isSelected(region.code)" name="check" :size="12" />
          </span>
        </button>
      </div>
      <HlEmpty
        v-else
        icon=""
        :text="t('pool.regions.noMatch')"
      />
    </div>

    <!-- 监控条目（监控池管理：增删 + 批量操作） -->
    <div class="card section-card" data-section="pool.section.items">
      <div class="section-card__header">
        <div>
          <div class="section-title">{{ t('pool.section.items') }}</div>
          <div class="section-desc">
            <template v-if="search.trim() || filterKind !== 'all'">{{ t('pool.items.matchCount', { matched: filteredItems.length, total: items.length }) }}</template>
            <template v-else>{{ t('pool.items.totalHint', { total: items.length }) }}</template>
          </div>
        </div>
        <div class="header-actions">
          <HlInput
            v-model="search"
            :placeholder="t('pool.items.searchPlaceholder')"
            prefix-icon="search"
            class="pool-search"
          />
          <HlButton size="sm" @click="addOpen = true">
            <HlIcon name="plus" :size="14" />
            {{ t('pool.items.add') }}
          </HlButton>
          <HlButton
            size="sm"
            :variant="manageMode ? 'primary' : 'default'"
            @click="toggleManage"
          >
            <HlIcon name="edit" :size="14" />
            {{ manageMode ? t('pool.items.manageDone') : t('pool.items.manage') }}
          </HlButton>
          <HlButton size="sm" @click="router.push('/crawl')">
            <HlIcon name="play" :size="14" />
            {{ t('pool.items.crawlAll') }}
          </HlButton>
        </div>
      </div>

      <!-- 类别筛选：关注/愿望单（爬取第一优先级）与已购/榜单/手动（普通条目） -->
      <div class="kind-filter">
        <HlChip
          v-for="k in kindTabs"
          :key="k.id"
          shape="soft"
          :on="filterKind === k.id"
          @click="filterKind = k.id"
        >
          <HlIcon v-if="k.icon" :name="k.icon" :size="12" />
          {{ k.label }} · {{ k.count }}
        </HlChip>
      </div>

      <!-- 管理模式操作条：多选 + 批量移除 -->
      <div v-if="manageMode" class="manage-bar">
        <span class="manage-bar__count">{{ t('pool.items.selected', { n: selectedItemsCount }) }}</span>
        <HlButton size="sm" @click="toggleSelectPage">
          {{ allPageSelected ? t('pool.items.unselectPage') : t('pool.items.selectPage') }}
        </HlButton>
        <HlButton size="sm" :disabled="!selectedItemsCount" @click="selected = new Set()">
          {{ t('pool.items.clearSelection') }}
        </HlButton>
        <HlPopconfirm
          :text="t('pool.items.removeConfirm', { n: selectedItemsCount })"
          :confirm-label="t('common.confirm')"
          :cancel-label="t('common.cancel')"
          @confirm="removeSelected"
        >
          <HlButton variant="danger" size="sm" :disabled="!selectedItemsCount" :loading="removing">
            <HlIcon name="delete" :size="14" />
            {{ t('pool.items.removeSelected') }}
          </HlButton>
        </HlPopconfirm>
        <span class="manage-bar__hint">{{ t('pool.items.manageHint') }}</span>
      </div>

      <!-- 加载态：骨架屏用卡片形态，与 .item-grid 的卡片网格同形 -->
      <HlSkeleton v-if="loading" variant="card" :count="8" />
      <!-- 有数据：网格 + 超一页才出的分页条 -->
      <template v-else-if="filteredItems.length">
        <div class="item-grid">
          <HlTooltip
            v-for="item in pagedItems"
            :key="item.appid"
            :content="String(item.appid)"
            rich
            :dark="false"
          >
            <button
              class="item-chip"
              :class="{ 'is-managed': manageMode, 'is-selected': manageMode && selected.has(item.appid) }"
              @click="onItemClick(item)"
            >
              <HlCheckbox
                v-if="manageMode"
                :model-value="selected.has(item.appid)"
                class="item-chip__check"
              />
              <HlIcon
                v-else
                :name="KIND_ICONS[itemKind(item)]"
                :size="12"
                class="item-chip__kind"
                :class="`item-chip__kind--${itemKind(item)}`"
              />
              <span class="item-chip__name">{{ item.name || item.appid }}</span>
            </button>
            <template #popper>
              <div class="item-tip">
                <HlImg
                  class="item-tip__cover"
                  :src="item.headerImage"
                  alt=""
                  loading="lazy"
                >
                  <template #fallback>
                    <span class="item-tip__cover">🎮</span>
                  </template>
                </HlImg>
                <div class="item-tip__main">
                  <div class="item-tip__name">{{ item.name || t('pool.items.pendingName') }}</div>
                  <div class="item-tip__appid">{{ item.appid }}</div>
                  <div class="item-tip__kind">
                    <HlIcon :name="KIND_ICONS[itemKind(item)]" :size="11" />
                    {{ t(KIND_LABEL_KEYS[itemKind(item)]) }}
                    <span v-if="item.steamids.length > 1"> · {{ t('pool.items.tipAccounts', { n: item.steamids.length }) }}</span>
                  </div>
                </div>
              </div>
            </template>
          </HlTooltip>
        </div>
        <!-- 超过一页才出分页条；词条无需新增（纯页码） -->
        <HlPagination
          v-if="filteredItems.length > PAGE_SIZE"
          v-model="page"
          :total="filteredItems.length"
          :page-size="PAGE_SIZE"
          class="item-pagination"
        />
      </template>
      <!-- 空态三分支：池为空 / 搜索无匹配 / 类别下无条目，同一个 HlEmpty -->
      <HlEmpty
        v-else
        icon=""
        :text="items.length ? (search.trim() ? t('pool.items.noMatch', { query: search.trim() }) : t('pool.items.noKindMatch')) : t('pool.items.empty')"
      />
    </div>

    <!-- 添加监控条目对话框（单个 / 批量粘贴 / 导入文件；加入即入池必爬） -->
    <HlDialog v-model="addOpen" :title="t('pool.items.addTitle')" :width="560">
      <div class="add-panel__hint">{{ t('pool.items.addHint') }}</div>
      <HlTextarea
        v-model="addText"
        :rows="6"
        :placeholder="t('pool.items.addPlaceholder')"
        @input="addSource = ''"
      />
      <div class="add-panel__file">
        <!-- 原生 file input（浏览器/WebView2 系统文件框）；隐藏本体，按钮触发。
             说明不放悬停气泡：按钮贴对话框左缘，气泡再宽也会被内容区裁掉左半
             ——长说明走 addHint 常驻，气泡形态只留给短句。 -->
        <input
          ref="fileInput"
          type="file"
          accept=".json,.txt,application/json,text/plain"
          class="add-panel__file-input"
          @change="onPickFile"
        />
        <HlButton variant="text" size="sm" :disabled="adding" @click="pickFile">
          <HlIcon name="download" :size="14" />
          {{ t('pool.items.importFile') }}
        </HlButton>
        <span v-if="addSource" class="add-panel__file-src">
          {{ t('pool.items.importFileLoaded', { name: addSource }) }}
        </span>
      </div>
      <div class="add-panel__meta">
        <template v-if="addPreview.appids.length || addPreview.invalid.length">
          {{ t('crawl.import.detected', { n: addPreview.appids.length }) }}<span v-if="addPreview.invalid.length"> · {{ t('crawl.import.detectedInvalid', { n: addPreview.invalid.length }) }}</span>
        </template>
      </div>
      <template #footer>
        <div class="add-panel__foot">
          <HlButton variant="text" size="sm" :disabled="adding" @click="addOpen = false">
            {{ t('common.cancel') }}
          </HlButton>
          <HlButton variant="primary" size="sm" :loading="adding" @click="submitAdd">
            <HlIcon v-if="!adding" name="plus" :size="14" />
            {{ t('pool.items.addSubmit') }}
          </HlButton>
        </div>
      </template>
    </HlDialog>
  </section>
</template>

<style scoped>
.pool-page {
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

/* 头部右侧操作区：搜索框 + 全量爬取按钮（窄屏随 header 换行） */
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.pool-search {
  width: 200px;
}

.add-row {
  display: flex;
  gap: 8px;
  margin: 12px 0 16px;
  align-items: center;
  flex-wrap: wrap;
}

.add-row__input {
  max-width: 380px;
  flex: 1 1 260px;
}

.add-row__label {
  max-width: 160px;
}

.acct-row {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.acct-main {
  min-width: 0;
}

.acct-name {
  font-weight: 600;
  color: var(--text-primary);
  display: flex;
  align-items: center;
  gap: 6px;
}

.acct-kind {
  font-size: 9px;
  font-weight: 600;
  padding: 1px 5px;
  border-radius: 3px;
  background: var(--accent-a15);
  color: var(--accent);
}

.acct-kind--owned {
  background: var(--success-a15);
  color: var(--success);
}

.acct-code {
  font-size: 11px;
  color: var(--text-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.acct-sync {
  font-size: 12px;
  color: var(--text-muted);
}

.acct-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

/* ── 监控地区 ── */
.region-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.region-search {
  margin: 8px 0 4px;
}

.region-search__input {
  width: 220px;
}

.region-grid {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
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
  gap: 2px;
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

/* ── 类别筛选与管理模式 ── */
.kind-filter {
  margin-top: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.manage-bar {
  margin-top: 12px;
  padding: 8px 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  background: var(--bg-soft);
}

.manage-bar__count {
  font-size: 12px;
  font-weight: 600;
  color: var(--accent);
}

.manage-bar__hint {
  font-size: 11px;
  color: var(--text-muted);
}

/* ── 添加监控条目对话框 ── */
.add-panel__hint {
  margin-bottom: 10px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-muted);
}

.add-panel__file {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
}

.add-panel__file-input {
  display: none;
}

.add-panel__file-src {
  font-size: 11px;
  color: var(--text-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.add-panel__meta {
  margin-top: 8px;
  min-height: 17px;
  font-size: 12px;
  color: var(--text-muted);
}

.add-panel__foot {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
}

/* ── 监控条目 ── */
.item-grid {
  margin-top: 12px;
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 8px;
}

.item-pagination {
  margin-top: 14px;
  justify-content: center;
}

.item-chip {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 10px;
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
  background: var(--bg-soft);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: all var(--transition);
  min-width: 0;
  /* HlTooltip 宿主 span 占据 grid item 地位，按钮须显式撑满宿主才恢复原拉伸 */
  width: 100%;
}

.item-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
}

/* 名称单行省略——长名不拉长所在格（grid 列宽 1fr 固定，溢出走 ellipsis） */
.item-chip__name {
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 来源类别标记（顶部筛选 chips 的同款图标：星=关注 / 心=愿望单 / 勾=已购 / 杯=榜单 / 加=手动） */
.item-chip__kind {
    flex-shrink: 0;
}

.item-chip__kind--follow {
  color: var(--warning);
}

.item-chip__kind--wishlist {
  color: var(--accent);
}

.item-chip__kind--owned {
  color: var(--success);
}

.item-chip__kind--board {
    color: var(--info);
}

.item-chip__kind--manual {
    color: var(--text-muted);
}

/* 管理模式：多选态高亮；勾选框点击穿透给外层 chip（选中语义只在一处） */
.item-chip.is-selected {
  border-color: var(--accent);
  background: var(--accent-a10);
  color: var(--accent);
}

.item-chip__check {
  pointer-events: none;
}

/* 悬停富气泡内容（HlTooltip #popper 插槽）：封面缩略图 + 名称/appid 两行 */
.item-tip {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.item-tip__cover {
  width: 92px;
  height: 43px;
  object-fit: cover;
  border-radius: var(--radius-sm);
  flex-shrink: 0;
  background: var(--bg-soft);
}

.item-tip__main {
  min-width: 0;
}

.item-tip__name {
  font-weight: 600;
  font-size: 12.5px;
  color: var(--text-primary);
  max-width: 200px;
  /* 气泡内两行封顶（-webkit-line-clamp）：超长名不撑破气泡，也不丢全文 */
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.4;
}

.item-tip__appid {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 2px;
}

.item-tip__kind {
  margin-top: 3px;
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--text-muted);
}
</style>
