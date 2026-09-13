<script setup lang="ts">
/* 购买动态（）：
   列 = 购入时间（rt_time_acquired）/ 购买者（owner_steamids.at(-1) 最近入库者）/ 拥有者数；
   NEW = 30 日内入库且独占（isNew 判定：time 距今 <2592000 && owners.length==1）；
   排序按入库时间降序（共享清单 GameList 原序即 time 降序）；成员筛选 = 购买者。 */
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { HlButton, HlChip, HlIcon } from '@/components/ui'
import HlSelect from '@/components/ui/HlSelect.vue'
import type { HlSelectOption } from '@/components/ui'
import { useI18n } from '@/locales'
import { useFamilyStore } from '@/stores/familyLib'

const { t } = useI18n()
const router = useRouter()
const store = useFamilyStore()
onMounted(() => { if (!store.ready) void store.load() })

const PAGE_SIZE = 50
const page = ref(1)

const view = ref<'table' | 'cover'>('table')
const onlyExclusive = ref(false)
const buyer = ref('all')

/* 选项在 computed 里现取 t()，故随语言切换重算（不是模块级常量存译文） */
const memberOptions = computed<HlSelectOption[]>(() => [
  { value: 'all', label: t('famBuy.member.all') },
  ...store.members.map((m) => ({
    value: m.steamid,
    label: m.personaName || t('famBuy.member.fallback', { id: m.steamid.slice(-4) }),
    flag: m.avatarUrl || undefined,
  })),
])

const nowSec = Math.floor(Date.now() / 1000)

/** 有入库时间的游戏，按 time 降序 */
const filtered = computed(() =>
  store.acquiredGames
    .filter((g) => {
      if (onlyExclusive.value && g.ownerCount > 1) return false
      // 成员筛选 = 购买者口径（owners.at(-1)，口径同 gi.owners.at(-1) == actFilterSid）
      if (buyer.value !== 'all' && g.buyer !== buyer.value) return false
      return true
    })
    .sort((a, b) => b.timeAcquired - a.timeAcquired),
)

const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / PAGE_SIZE)))
const pageRows = computed(() => {
  const p = Math.min(page.value, totalPages.value)
  return filtered.value.slice((p - 1) * PAGE_SIZE, p * PAGE_SIZE)
})

function fmtDate(ts: number): string {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
function fmtPrice(fen: number | null): string {
  if (fen === null) return '—'
  if (fen === 0) return t('famBuy.card.free')
  return `¥${(fen / 100).toFixed(0)}`
}
/** NEW = 30 日内入库且独占 */
function isNewAcquired(g: { timeAcquired: number; ownerCount: number }): boolean {
  return g.ownerCount === 1 && g.timeAcquired > nowSec - 30 * 86400
}
function buyerName(sid: string | null): string {
  if (!sid) return '—'
  return store.memberName(sid)
}
function buyerAvatar(sid: string | null): string {
  if (!sid) return ''
  return store.memberMap.get(sid)?.avatarUrl || ''
}
function ownerNames(owners: string[]): string {
  return owners.map((sid) => store.memberName(sid)).join('、')
}
function goBills() {
  void router.push('/bills')
}
function goGame(appid: number) {
  void router.push(`/game/${appid}`)
}
</script>

<template>
  <div>
    <div v-if="store.error" class="lib-empty">{{ store.error }}</div>
    <div v-else-if="store.loading && !store.ready" class="lib-empty">{{ t('famBuy.empty.loading') }}</div>
    <div v-else-if="store.acquiredGames.length === 0" class="lib-empty">
      {{ t('famBuy.empty.noData') }}
    </div>

    <template v-else>
      <div class="buy-toolbar">
        <!-- 整句一条词条，行内 <b> 写在值里、v-html 渲染（不按标记边界拆句） -->
        <span
          class="buy-stat"
          v-html="t('famBuy.toolbar.count', { n: filtered.length, page: page, total: totalPages })"
        ></span>
        <span v-if="store.fromSnapshot" class="buy-snap">{{ t('famBuy.toolbar.snapshot') }}</span>
        <div style="flex: 1"></div>
        <HlButton size="sm" variant="primary" @click="goBills">
          <HlIcon name="list" /> {{ t('famBuy.action.bills') }}
        </HlButton>
        <HlSelect v-model="buyer" :options="memberOptions" class="buy-member-select" />
        <HlChip
          shape="soft"
          :on="onlyExclusive"
          @click="((onlyExclusive = !onlyExclusive), (page = 1))"
        >{{ t('famBuy.toolbar.onlyExclusive') }}</HlChip>
        <div class="buy-view-toggle">
          <button type="button" :class="{ 'is-on': view === 'table' }" @click="view = 'table'">
            <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/></svg>{{ t('famBuy.view.table') }}
          </button>
          <button type="button" :class="{ 'is-on': view === 'cover' }" @click="view = 'cover'">
            <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>{{ t('famBuy.view.cover') }}
          </button>
        </div>
      </div>

      <table v-if="view === 'table'" class="buy-table">
        <thead>
          <tr>
            <th style="width: 44px"></th>
            <th>{{ t('famBuy.table.name') }}</th>
            <th style="width: 110px">{{ t('famBuy.table.acquired') }}</th>
            <th style="width: 110px">{{ t('famBuy.table.buyer') }}</th>
            <th style="width: 70px; text-align: center">{{ t('famBuy.table.owners') }}</th>
            <th style="width: 80px; text-align: right">{{ t('famBuy.table.cnPrice') }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="g in pageRows" :key="g.appid" :class="{ 'is-new': isNewAcquired(g) }">
            <td>
              <HlImg :src="g.headerImage" class="buy-game-icon" loading="lazy" alt="">
                <template #fallback>
                  <div class="buy-game-icon buy-game-icon--fallback">{{ (g.name || g.appid).toString().slice(0, 1) }}</div>
                </template>
              </HlImg>
            </td>
            <td>
              <router-link class="buy-game-name" :to="`/game/${g.appid}`">{{ g.name || `AppID ${g.appid}` }}</router-link>
              <span v-if="isNewAcquired(g)" class="buy-new-badge">NEW</span>
            </td>
            <td class="mono" style="color: var(--text-dim)">{{ fmtDate(g.timeAcquired) }}</td>
            <td :title="ownerNames(g.owners)">
              <span v-if="g.buyer" class="buy-buyer">
                <img v-if="buyerAvatar(g.buyer)" class="buy-buyer__ava" :src="buyerAvatar(g.buyer)" loading="lazy" alt="" />
                <span v-else class="buy-buyer__ava buy-buyer__ava--fb">{{ buyerName(g.buyer).slice(0, 1) }}</span>
                <span class="buy-buyer__name">{{ buyerName(g.buyer) }}</span>
              </span>
              <template v-else>—</template>
            </td>
            <td style="text-align: center">
              <span class="tag" :class="g.ownerCount === 1 ? 'tag--accent' : ''" style="font-size: 10px">{{ t('famBuy.table.ownerCount', { n: g.ownerCount }) }}</span>
            </td>
            <td
              class="mono"
              style="text-align: right"
              :style="{ color: g.cnPriceFen === 0 ? 'var(--success)' : 'var(--text-secondary)' }"
            >
              {{ fmtPrice(g.cnPriceFen) }}
            </td>
          </tr>
        </tbody>
      </table>

      <div v-else class="buy-cover-grid">
        <div v-for="g in pageRows" :key="g.appid" class="buy-cover-card" :class="{ 'is-new': isNewAcquired(g) }" @click="goGame(g.appid)">
          <HlImg :src="g.headerImage" class="buy-cover-cap-img" loading="lazy" alt="">
            <template #fallback>
              <div class="buy-cover-cap">{{ (g.name || g.appid).toString().slice(0, 2) }}</div>
            </template>
          </HlImg>
          <div class="buy-cover-info">
            <div class="buy-cover-name">{{ g.name || `AppID ${g.appid}` }}</div>
            <div class="buy-cover-meta">
              <span>{{ fmtDate(g.timeAcquired) }}</span>
              <span class="buy-cover-buyer">{{ buyerName(g.buyer) }}</span>
            </div>
          </div>
          <span v-if="isNewAcquired(g)" class="buy-cover-new">NEW</span>
        </div>
      </div>

      <div class="wl-pager" style="margin-top: 8px">
        <span style="font-size: 10px; color: var(--text-dim)">{{ t('famBuy.footer.legend') }}</span>
        <div class="wl-pager__btns">
          <button type="button" class="wl-pgbtn" :disabled="page <= 1" @click="page--">{{ t('famBuy.pager.prev') }}</button>
          <button type="button" class="wl-pgbtn" :disabled="page >= totalPages" @click="page++">{{ t('famBuy.pager.next') }}</button>
        </div>
      </div>
    </template>
  </div>
</template>

<style src="./tabs-shared.css"></style>
<style scoped>
.lib-empty { text-align: center; padding: 26px 16px; border: 1px dashed var(--border-soft); border-radius: var(--radius); background: var(--surface-inset); font-size: 12.5px; color: var(--text-secondary); }
.buy-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
.buy-stat { font-size: 12px; color: var(--text-muted); }
.buy-stat :deep(b) { color: var(--text-primary); font-family: var(--font-mono, monospace); }
.buy-snap { font-size: 10px; padding: 2px 8px; border-radius: 3px; background: var(--warn-a15, rgba(245, 158, 11, .15)); color: var(--warn, #f59e0b); font-weight: 600; }
.buy-member-select { width: 150px; }
.buy-view-toggle { display: flex; gap: 2px; background: var(--surface-chip-2); border: 1px solid var(--border-soft); border-radius: 5px; padding: 2px; }
.buy-view-toggle button { font-size: 10.5px; padding: 3px 9px; border-radius: 4px; border: none; background: transparent; color: var(--text-muted); cursor: pointer; display: flex; align-items: center; gap: 4px; transition: var(--transition); font-family: inherit; }
.buy-view-toggle button.is-on { background: var(--accent-a20); color: var(--accent); }
.buy-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.buy-table th { text-align: left; font-weight: 600; color: var(--text-muted); font-size: 10.5px; padding: 8px 10px; border-bottom: 1px solid var(--line-2); background: var(--surface-inset); }
.buy-table td { padding: 8px 10px; border-bottom: 1px solid var(--row-border); color: var(--text-secondary); vertical-align: middle; }
.buy-table tbody tr { transition: background 0.15s; }
.buy-table tbody tr:hover { background: var(--hover-soft); }
.buy-table tbody tr.is-new { background: var(--accent-a08); }
.buy-game-icon { width: 32px; height: 32px; border-radius: 5px; display: block; object-fit: cover; background: var(--surface-chip-2); flex-shrink: 0; }
.buy-game-icon--fallback { display: grid; place-items: center; font-size: 14px; color: var(--text-dim); }
.buy-game-name { font-size: 12px; font-weight: 500; color: var(--text-primary); text-decoration: none; }
.buy-game-name:hover { color: var(--accent); }
.buy-new-badge { font-size: 9px; padding: 1px 5px; border-radius: 3px; background: var(--accent); color: var(--on-accent); font-weight: 700; margin-left: 5px; vertical-align: middle; }
.buy-buyer { display: inline-flex; align-items: center; gap: 6px; min-width: 0; max-width: 100%; }
.buy-buyer__ava { width: 20px; height: 20px; border-radius: 50%; object-fit: cover; flex-shrink: 0; background: var(--surface-chip-2); }
.buy-buyer__ava--fb { display: grid; place-items: center; font-size: 10px; font-weight: 600; color: var(--text-dim); }
.buy-buyer__name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.buy-cover-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 8px; }
.buy-cover-card { position: relative; background: var(--surface-inset); border: 1px solid var(--line-1); border-radius: 7px; overflow: hidden; transition: var(--transition); cursor: pointer; }
.buy-cover-card:hover { border-color: var(--accent-a40); transform: translateY(-2px); }
.buy-cover-card.is-new { border-color: var(--accent-a40); }
.buy-cover-cap { height: 66px; display: grid; place-items: center; font-size: 24px; background: linear-gradient(135deg, var(--surface-chip-2), var(--surface-inset)); }
.buy-cover-cap-img { height: 66px; width: 100%; display: block; object-fit: cover; }
.buy-cover-info { padding: 7px 9px; }
.buy-cover-name { font-size: 11px; font-weight: 600; color: var(--text-primary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-bottom: 3px; }
.buy-cover-meta { font-size: 9.5px; color: var(--text-dim); font-family: var(--font-mono, monospace); display: flex; justify-content: space-between; gap: 4px; }
.buy-cover-buyer { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.buy-cover-new { position: absolute; top: 4px; right: 4px; font-size: 9px; padding: 1px 5px; border-radius: 3px; background: var(--accent); color: var(--on-accent); font-weight: 700; }
.mono { font-family: var(--font-mono, monospace); }
</style>
