<script setup lang="ts">
/* 账号游戏库：账户选择 chips + KPI + 搜索/排序/共有筛选 + 卡片网格 + 分页。
   数据源：ownedLib store（GET /owned-library —— wishlist_items.owned 全量矩阵，
   覆盖绑定账号 / 手动添加账户 / 家庭组成员）。 */
import { computed, onMounted, ref, watch } from 'vue'

import { useOwnedLibStore } from '@/stores/ownedLib'
import { useI18n, useLocaleFormat } from '@/locales'
import HlButton from '@/components/ui/HlButton.vue'
import HlChip from '@/components/ui/HlChip.vue'
import HlEmpty from '@/components/ui/HlEmpty.vue'
import HlPagination from '@/components/ui/HlPagination.vue'
import HlStat from '@/components/ui/HlStat.vue'
import LibGameCard from '@/components/steamhl/LibGameCard.vue'
import { PALETTE } from '@/lib/familyColors'

const { t } = useI18n()
const fmt = useLocaleFormat()
const store = useOwnedLibStore()
onMounted(() => {
  if (!store.ready) void store.load()
})

const PAGE_SIZE = 48
const page = ref(1)
/** '' = 全部账号；否则 steamid */
const selected = ref('')
const search = ref('')
const activeSort = ref<'recent' | 'name' | 'price' | 'owners'>('recent')
const onlyShared = ref(false)

/* 排序项只存 key：模块级常量存译文会把语言冻在加载那一刻 */
const SORTS: { value: 'recent' | 'name' | 'price' | 'owners'; labelKey: 'gamelib.owned.sort.recent' | 'gamelib.owned.sort.name' | 'gamelib.owned.sort.price' | 'gamelib.owned.sort.owners' }[] = [
  { value: 'recent', labelKey: 'gamelib.owned.sort.recent' },
  { value: 'name', labelKey: 'gamelib.owned.sort.name' },
  { value: 'price', labelKey: 'gamelib.owned.sort.price' },
  { value: 'owners', labelKey: 'gamelib.owned.sort.owners' },
]

/* 当前口径（全部 / 单账号）下的游戏集合 */
const scopeGames = computed(() => {
  if (!selected.value) return store.games
  return store.games.filter((g) => g.owners.some((o) => o.steamid === selected.value))
})

const filtered = computed(() => {
  let list = [...scopeGames.value]
  if (onlyShared.value && !selected.value) {
    list = list.filter((g) => g.owners.length > 1)
  }
  const q = search.value.trim().toLowerCase()
  if (q) {
    list = list.filter(
      (g) =>
        (g.name || '').toLowerCase().includes(q) ||
        (g.nameEn || '').toLowerCase().includes(q) ||
        String(g.appid).includes(q),
    )
  }
  if (activeSort.value === 'name') {
    list.sort((a, b) => (a.name || '').localeCompare(b.name || ''))
  } else if (activeSort.value === 'price') {
    list.sort((a, b) => (b.cnPriceFen ?? -1) - (a.cnPriceFen ?? -1))
  } else if (activeSort.value === 'owners') {
    list.sort((a, b) => b.owners.length - a.owners.length)
  } else {
    // 最近入库：当前口径内每个拥有者 addedAt 的最大值
    const latest = (g: (typeof list)[number]) =>
      Math.max(
        0,
        ...(selected.value
          ? g.owners.filter((o) => o.steamid === selected.value)
          : g.owners
        ).map((o) => (o.addedAt ? Date.parse(o.addedAt) : 0)),
      )
    list.sort((a, b) => latest(b) - latest(a))
  }
  return list
})

const pageRows = computed(() => {
  const start = (page.value - 1) * PAGE_SIZE
  return filtered.value.slice(start, start + PAGE_SIZE)
})

/* KPI（当前口径）：款数 / 价值 / 免费 / 30天入库 / 多人共有 */
const scopeKpi = computed(() => {
  const nowMs = Date.now()
  let valueFen = 0
  let free = 0
  let new30 = 0
  let shared = 0
  for (const g of scopeGames.value) {
    if (g.cnPriceFen !== null) {
      valueFen += g.cnPriceFen
      if (g.cnPriceFen === 0) free++
    }
    const relevant = selected.value
      ? g.owners.filter((o) => o.steamid === selected.value)
      : g.owners
    if (relevant.some((o) => o.addedAt && Date.parse(o.addedAt) > nowMs - 30 * 86400_000)) {
      new30++
    }
    if (g.owners.length > 1) shared++
  }
  return { count: scopeGames.value.length, valueFen, free, new30, shared }
})

/** 卡片拥有者头像栈（≤4，来自账户档案） */
function ownersOf(g: (typeof store.games)[number]) {
  return g.owners.slice(0, 4).map((o) => ({
    name: store.accountName(o.steamid),
    url: store.accountAvatar(o.steamid),
  }))
}

watch([selected, search, onlyShared, activeSort], () => {
  page.value = 1
})
</script>

<template>
  <div data-section="gamelib.section.owned">
    <!-- 加载 / 失败 / 无账户：诚实空态 -->
    <HlEmpty v-if="store.loading && !store.ready" :text="t('gamelib.empty.loading')" icon="" />
    <div v-else-if="store.error" class="gl-error">
      <HlEmpty :text="t('gamelib.empty.error', { err: store.error })" icon="⚠️" />
      <HlButton size="sm" variant="text" :loading="store.loading" @click="store.load(true)">
        ⟳ {{ t('gamelib.pager.refresh') }}
      </HlButton>
    </div>
    <div v-else-if="store.accounts.length === 0" class="gl-lead">
      <HlEmpty :text="t('gamelib.empty.noAccounts')" />
      <div class="gl-lead__hint">{{ t('gamelib.empty.noAccountsHint') }}</div>
    </div>

    <template v-else>
      <!-- KPI（当前口径） -->
      <div class="gl-kpi-row">
        <HlStat size="sm" :color="PALETTE.teal" :label="t('gamelib.owned.kpi.count')" :value="fmt.group(scopeKpi.count)" />
        <HlStat
          size="sm"
          :color="PALETTE.gold"
          :label="t('gamelib.owned.kpi.value')"
          :value="scopeKpi.valueFen > 0 ? `¥${fmt.group(scopeKpi.valueFen / 100)}` : '—'"
        />
        <HlStat size="sm" :color="PALETTE.green" :label="t('gamelib.owned.kpi.free')" :value="fmt.group(scopeKpi.free)" />
        <HlStat size="sm" :color="PALETTE.blue" :label="t('gamelib.owned.kpi.new30')" :value="`+${fmt.group(scopeKpi.new30)}`" />
        <HlStat v-if="!selected" size="sm" :color="PALETTE.violet" :label="t('gamelib.owned.kpi.shared')" :value="fmt.group(scopeKpi.shared)" />
      </div>

      <!-- 账户选择 chips -->
      <div class="gl-accounts">
        <button
          type="button"
          class="gl-acc"
          :class="{ 'is-on': !selected }"
          @click="selected = ''"
        >
          <span class="gl-acc__ava gl-acc__ava--all">∑</span>
          <span class="gl-acc__name">{{ t('gamelib.owned.all') }}</span>
          <span class="gl-acc__count">{{ fmt.group(store.games.length) }}</span>
        </button>
        <button
          v-for="a in store.accounts"
          :key="a.steamid"
          type="button"
          class="gl-acc"
          :class="{ 'is-on': selected === a.steamid }"
          :title="a.kinds?.owned ? (a.friendCode || a.steamid) : t('gamelib.owned.syncOff')"
          @click="selected = selected === a.steamid ? '' : a.steamid"
        >
          <HlImg class="gl-acc__ava" :src="a.avatarUrl" :alt="store.accountName(a.steamid)">
            <template #fallback>
              <span class="gl-acc__ava gl-acc__ava--txt">{{ store.accountName(a.steamid).slice(0, 1) }}</span>
            </template>
          </HlImg>
          <span class="gl-acc__name">{{ store.accountName(a.steamid) }}</span>
          <span v-if="a.isPrimary" class="gl-acc__badge">{{ t('wallet.badgePrimary') }}</span>
          <span class="gl-acc__count">{{ fmt.group(a.ownedCount) }}</span>
        </button>
      </div>

      <!-- 工具栏 -->
      <div class="gl-toolbar">
        <input
          v-model="search"
          type="text"
          class="gl-search"
          :placeholder="t('gamelib.owned.searchPlaceholder')"
        />
        <span class="gl-toolbar__count">{{ t('gamelib.pager.range', { from: filtered.length ? (page - 1) * PAGE_SIZE + 1 : 0, to: Math.min(page * PAGE_SIZE, filtered.length), n: filtered.length }) }}</span>
        <div style="flex: 1"></div>
        <span class="gl-toolbar__count">{{ t('gamelib.owned.sortLabel') }}</span>
        <HlChip
          v-for="s in SORTS"
          :key="s.value"
          shape="soft"
          :on="activeSort === s.value"
          @click="activeSort = s.value"
        >{{ t(s.labelKey) }}</HlChip>
        <HlChip
          v-if="!selected"
          shape="soft"
          :on="onlyShared"
          @click="onlyShared = !onlyShared"
        >{{ t('gamelib.owned.filter.shared') }}</HlChip>
      </div>

      <!-- 卡片网格 -->
      <div v-if="pageRows.length" class="gl-grid">
        <LibGameCard
          v-for="g in pageRows"
          :key="g.appid"
          :appid="g.appid"
          :name="g.name"
          :header-image="g.headerImage"
          :genres="g.genres"
          :release-date="g.releaseDate"
          :price-fen="g.cnPriceFen"
          :original-price-fen="g.originalPriceFen"
          :discount="g.discount"
          :owners="ownersOf(g)"
        >
          <template #badge>
            <span v-if="g.owners.length > 1" class="glc-badge glc-badge--shared">
              {{ t('famLib.card.sharedBy', { n: g.owners.length }) }}
            </span>
          </template>
        </LibGameCard>
      </div>
      <HlEmpty
        v-else-if="scopeGames.length === 0 && selected"
        size="md"
        :text="t('gamelib.empty.noGames')"
      />
      <div v-else-if="scopeGames.length === 0" class="gl-lead">
        <HlEmpty :text="t('gamelib.empty.noGames')" />
        <div class="gl-lead__hint">{{ t('gamelib.empty.noGamesHint') }}</div>
      </div>
      <HlEmpty v-else size="sm" icon="" :text="t('gamelib.empty.noMatch')" />

      <!-- 分页 -->
      <div v-if="filtered.length > PAGE_SIZE" class="gl-pager">
        <span>{{ t('gamelib.pager.range', { from: (page - 1) * PAGE_SIZE + 1, to: Math.min(page * PAGE_SIZE, filtered.length), n: filtered.length }) }}</span>
        <HlPagination v-model="page" :total="filtered.length" :page-size="PAGE_SIZE" />
      </div>

      <div class="gl-foot">
        <HlButton size="sm" variant="text" :disabled="store.loading" :loading="store.loading" @click="store.load(true)">
          ⟳ {{ t('gamelib.pager.refresh') }}
        </HlButton>
      </div>
    </template>
  </div>
</template>

<style src="./gamelib-shared.css"></style>
<style scoped>
/* 账户 chips：头像 + 名字 + 计数；主账号带「主」徽章 */
.gl-accounts {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.gl-acc {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 10px 5px 6px;
  border-radius: 999px;
  border: 1px solid var(--border-soft);
  background: var(--bg-card);
  color: var(--text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: var(--transition);
  font-family: inherit;
  max-width: 240px;
}
.gl-acc:hover {
  border-color: var(--accent-a40);
  color: var(--text-primary);
}
.gl-acc.is-on {
  background: var(--select-selected-bg);
  color: var(--select-selected-fg);
  border-color: transparent;
  font-weight: 600;
}
.gl-acc__ava {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  object-fit: cover;
  flex-shrink: 0;
  display: grid;
  place-items: center;
}
.gl-acc__ava--all {
  background: var(--accent-a15);
  color: var(--accent);
  font-weight: 700;
  font-size: 12px;
}
.gl-acc__ava--txt {
  background: var(--accent);
  color: var(--text-on-fill);
  font-size: 10px;
  font-weight: 700;
}
.gl-acc__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gl-acc__badge {
  font-size: 9.5px;
  padding: 0 5px;
  border-radius: 999px;
  color: var(--success);
  border: 1px solid var(--success);
  flex-shrink: 0;
  line-height: 1.4;
}
.gl-acc__count {
  font-family: var(--font-mono, monospace);
  font-size: 11px;
  color: var(--text-muted);
  font-variant-numeric: tabular-nums;
  flex-shrink: 0;
}

/* 卡片徽章（LibGameCard #badge 槽，绝对定位在封面右上） */
.glc-badge {
  position: absolute;
  top: 6px;
  right: 6px;
  font-size: 9.5px;
  padding: 2px 6px;
  border-radius: 4px;
  font-weight: 700;
  backdrop-filter: blur(4px);
  white-space: nowrap;
}
.glc-badge--shared {
  background: var(--accent-a20);
  color: var(--accent);
}

/* 失败态与引导 */
.gl-error {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
}
.gl-lead {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 18px 0;
}
.gl-lead__hint {
  font-size: 11.5px;
  color: var(--text-dim);
  line-height: 1.7;
  max-width: 420px;
  text-align: center;
}
.gl-foot {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
}

/* 时长换算未被模板直用时保留入口（fmtHoursKey 现未上卡） */
</style>
