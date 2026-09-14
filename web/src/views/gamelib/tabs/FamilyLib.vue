<script setup lang="ts">
/* 家庭库（自 family 页迁入）：6 KPI + 排序工具栏 + 独占/共享卡片网格 + 分页。
   数据源：familyLib store（GET /family/library —— GetSharedLibraryApps +
   成员已购/游玩聚合）。卡片统一走 business/LibGameCard（16:9 封面主视觉）。 */
import { computed, onMounted, ref } from 'vue'

import { useFamilyStore } from '@/stores/familyLib'
import { useI18n, useLocaleFormat } from '@/locales'
import HlButton from '@/components/ui/HlButton.vue'
import HlChip from '@/components/ui/HlChip.vue'
import HlStat from '@/components/ui/HlStat.vue'
import LibGameCard from '@/components/business/LibGameCard.vue'
import { PALETTE } from '@/lib/familyColors'

const { t } = useI18n()
const fmt = useLocaleFormat()
const store = useFamilyStore()
onMounted(() => {
  if (!store.ready) void store.load()
})

const PAGE_SIZE = 60
const page = ref(1)

/* 排序项**只存 key**：模块级常量存译文会把语言冻在加载那一刻。 */
const SORTS = [
  { value: 'lastPlayed', labelKey: 'famLib.label.lastPlayed' },
  { value: 'name', labelKey: 'famLib.sort.name' },
  { value: 'playtime', labelKey: 'famLib.sort.playtime' },
  { value: 'price', labelKey: 'famLib.sort.price' },
] as const
const activeSort = ref<string>('lastPlayed')
const onlyExclusive = ref(false)
const search = ref('')

const filtered = computed(() => {
  let list = [...store.games]
  if (onlyExclusive.value) list = list.filter((g) => g.ownerCount === 1)
  const q = search.value.trim().toLowerCase()
  if (q) list = list.filter((g) => (g.name || '').toLowerCase().includes(q))
  if (activeSort.value === 'name') list.sort((a, b) => (a.name || '').localeCompare(b.name || ''))
  else if (activeSort.value === 'playtime') list.sort((a, b) => b.playtimeMinutes - a.playtimeMinutes)
  else if (activeSort.value === 'price') list.sort((a, b) => (b.cnPriceFen ?? -1) - (a.cnPriceFen ?? -1))
  else list.sort((a, b) => b.lastPlayed - a.lastPlayed)
  return list
})

const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / PAGE_SIZE)))
const pageRows = computed(() => {
  const p = Math.min(page.value, totalPages.value)
  return filtered.value.slice((p - 1) * PAGE_SIZE, p * PAGE_SIZE)
})

/* 时长单位不是语言中立的（中文 `128h` / 英文 `128 hrs`），走跨模块共用的
   common.hours；≥1000h 的 k 简写同理走 common.hoursK。本函数只负责决定
   「用哪一支」——那是逻辑，不是文案。`—` 是纯符号，留在组件侧。 */
function fmtHours(minutes: number): string {
  if (!minutes) return '—'
  return t('common.hours', { h: (minutes / 60).toFixed(1) })
}
function fmtPrice(fen: number | null): string {
  if (fen === null) return t('famLib.card.notListed')
  if (fen === 0) return t('famLib.card.free')
  return `¥${(fen / 100).toFixed(0)}`
}
function fmtDate(ts: number): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
function ownersAvatars(owners: string[]) {
  return owners.slice(0, 4).map((sid) => ({
    name: store.memberName(sid),
    url: store.memberMap.get(sid)?.avatarUrl || '',
  }))
}
</script>

<template>
  <div data-section="gamelib.section.family">
    <!-- 未绑 Cookie / 未加入家庭组：诚实引导 -->
    <div v-if="store.error" class="famlib-empty">
      <div class="famlib-empty__title">{{ store.error }}</div>
      <div class="famlib-empty__sub">{{ t('famLib.empty.bindHint') }}</div>
    </div>
    <div v-else-if="store.loading && !store.ready" class="famlib-empty">{{ t('famLib.empty.loading') }}</div>
    <div v-else-if="store.games.length === 0" class="famlib-empty">
      <div class="famlib-empty__title">{{ t('famLib.empty.noData') }}</div>
      <div class="famlib-empty__sub">{{ t('famLib.empty.noDataHint') }}</div>
    </div>

    <template v-else>
      <div class="gl-kpi-row">
        <HlStat size="sm" :color="PALETTE.teal" :label="t('famLib.kpi.total')" :value="store.libraryKpi.total" />
        <HlStat size="sm" :color="PALETTE.amber" :label="t('famLib.kpi.exclusive')" :value="store.libraryKpi.exclusive" />
        <HlStat size="sm" :color="PALETTE.blue" :label="t('famLib.kpi.shared')" :value="store.libraryKpi.shared" />
        <HlStat size="sm" :color="PALETTE.green" :label="t('famLib.kpi.active30')" :value="store.libraryKpi.active30" />
        <HlStat
          size="sm"
          :color="PALETTE.pink"
          :label="t('famLib.kpi.playtime')"
          :value="store.libraryKpi.totalMin / 60 >= 1000
            ? t('common.hoursK', { h: (store.libraryKpi.totalMin / 60 / 1000).toFixed(1) })
            : fmtHours(store.libraryKpi.totalMin)"
        />
        <HlStat size="sm" :color="PALETTE.lilac" :label="t('famLib.kpi.value')" :value="`¥${fmt.group(store.libraryKpi.valueFen / 100)}`" />
      </div>

      <div class="gl-toolbar">
        <input v-model="search" type="text" class="gl-search" :placeholder="t('famLib.toolbar.searchPlaceholder')">
        <div style="flex: 1"></div>
        <span class="gl-toolbar__count">{{ t('famLib.toolbar.sortLabel') }}</span>
        <HlChip
          v-for="s in SORTS"
          :key="s.value"
          shape="soft"
          :on="activeSort === s.value && !onlyExclusive"
          @click="((activeSort = s.value), (onlyExclusive = false), (page = 1))"
        >{{ t(s.labelKey) }}</HlChip>
        <HlChip shape="soft" :on="onlyExclusive" @click="((onlyExclusive = !onlyExclusive), (page = 1))">{{ t('famLib.toolbar.onlyExclusive') }}</HlChip>
      </div>

      <div class="gl-grid">
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
          :owners="ownersAvatars(g.owners)"
          :owner-total="g.owners.length"
        >
          <template #badge>
            <span v-if="g.ownerCount === 1" class="famlib-badge famlib-badge--exclusive">{{ t('famLib.card.exclusive') }}</span>
            <span v-else class="famlib-badge famlib-badge--shared">{{ t('famLib.card.sharedBy', { n: g.ownerCount }) }}</span>
          </template>
          <template #extra>
            <div class="famlib-extra">
              <span>{{ t('famLib.label.lastPlayed') }} {{ fmtDate(g.lastPlayed) }}</span>
              <span>{{ fmtHours(g.playtimeMinutes) }}</span>
            </div>
          </template>
        </LibGameCard>
      </div>
      <div v-if="filtered.length === 0" class="famlib-nocheck">{{ t('famLib.grid.noMatch') }}</div>

      <div class="gl-pager">
        <!-- 整句一条词条，行内 <b> 写在值里、v-html 渲染（不按标记边界拆句） -->
        <span
          v-html="t('famLib.pager.range', {
            from: (page - 1) * PAGE_SIZE + 1,
            to: Math.min(page * PAGE_SIZE, filtered.length),
            n: filtered.length,
          })"
        ></span>
        <div class="gl-pager__btns">
          <button type="button" class="gl-pgbtn" :disabled="page <= 1" @click="page--">{{ t('famLib.pager.prev') }}</button>
          <span class="famlib-page">{{ t('famLib.pager.page', { page: page, total: totalPages }) }}</span>
          <button type="button" class="gl-pgbtn" :disabled="page >= totalPages" @click="page++">{{ t('famLib.pager.next') }}</button>
        </div>
      </div>

      <div class="famlib-foot">
        <HlButton size="sm" variant="text" :disabled="store.loading" :loading="store.loading" @click="store.load(true)">
          ⟳ {{ t('famLib.action.refresh') }}
        </HlButton>
      </div>
    </template>
  </div>
</template>

<style src="./gamelib-shared.css"></style>
<style scoped>
.famlib-empty {
  text-align: center;
  padding: 26px 16px;
  border: 1px dashed var(--border-soft);
  border-radius: var(--radius);
  background: var(--surface-inset);
}
.famlib-empty__title {
  font-size: 13px;
  color: var(--text-secondary);
}
.famlib-empty__sub {
  font-size: 11px;
  color: var(--text-dim);
  margin-top: 5px;
  line-height: 1.7;
}

/* 卡片徽章（LibGameCard #badge 槽；定位由卡本体的 :deep(.glc-badge) 负责） */
.famlib-badge--exclusive {
  background: var(--warning-a20);
  color: var(--warning);
}
.famlib-badge--shared {
  background: var(--accent-a20);
  color: var(--accent);
}

/* 最近游玩一行（封面下的弱化元信息） */
.famlib-extra {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  font-size: 10px;
  color: var(--text-dim);
  font-variant-numeric: tabular-nums;
}

/* v-html 注入的 <b> 拿不到 scoped 属性，:deep() 才够得着（原 FamLib 同款） */
.gl-pager :deep(b) {
  color: var(--text-primary);
}
.famlib-page {
  font-size: 11px;
  color: var(--text-dim);
  align-self: center;
  font-variant-numeric: tabular-nums;
}
.famlib-nocheck {
  margin-top: 10px;
  padding: 26px 20px;
  text-align: center;
  font-size: 12.5px;
  color: var(--text-dim);
  background: var(--surface-inset);
  border: 1px dashed var(--border-soft);
  border-radius: var(--radius);
  line-height: 1.8;
}
.famlib-foot {
  margin-top: 8px;
  display: flex;
  justify-content: flex-end;
}
</style>
