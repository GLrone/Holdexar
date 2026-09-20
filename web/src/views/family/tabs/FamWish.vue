<script setup lang="ts">
/* 家庭愿望单（框架 wl-*）：7 KPI + 筛选 + 左仪表盘（分布/标签/价格环）+ 右列表。
   数据源：GET /family/wishlist（wishlist_items × games 本地聚合，无需 Cookie）。 */
import { computed, onMounted, ref } from 'vue'

import { HlButton, HlChip, HlIcon, HlStat } from '@/components/ui'
import { familyApi, type FamilyWishlistItem, type FamilyWishlistPayload } from '@/api/client'
import { cachedGet } from '@/lib/apiCache'
import { memberColor, PALETTE } from '@/lib/familyColors'
import { useI18n, useLocaleFormat } from '@/locales'
import { useFamilyStore } from '@/stores/familyLib'

const store = useFamilyStore()
const { t } = useI18n()
// 时间格式随语言：zh-CN → 14:30，en → 02:30 PM
const fmt = useLocaleFormat()
const payload = ref<FamilyWishlistPayload | null>(null)
const loading = ref(false)
const error = ref('')
const loadedAt = ref('')

async function load(force = false) {
  if (loading.value) return
  loading.value = true
  error.value = ''
  try {
    // 走 cachedGet：本 tab 由 v-if 切换，每次切回来都会重挂载并重下 302 KB。
    // 「刷新」按钮传 force=true 绕过复用窗口。
    payload.value = await cachedGet('family.wishlist', () => familyApi.wishlist(), { force })
    loadedAt.value = fmt.time(new Date())
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}
onMounted(() => {
  void load()
  if (!store.ready) void store.load()  // 家庭已有（owned 命中）交叉核对用
})

/* 筛选档位——常量表只存 key（模块级常量存译文＝把语言冻在加载那一刻）。
   activeFilter 存的也是 key，filtered 用 key 比较：只换标签不改比较，
   切语言不会丢选中态（比较中文串会丢）。 */
const FILTERS = [
  { key: 'all', labelKey: 'common.all' },
  { key: 'owned', labelKey: 'famWish.tag.familyOwned' },
  { key: 'multi', labelKey: 'famWish.tag.multiWant' },
  { key: 'sale', labelKey: 'famWish.tag.onSale' },
  { key: 'soon', labelKey: 'famWish.tag.comingSoon' },
] as const
const activeFilter = ref<(typeof FILTERS)[number]['key']>('all')
const search = ref('')

const items = computed<FamilyWishlistItem[]>(() => payload.value?.items ?? [])

/** 家庭库 appid 集（交叉核对「家庭已有」；库不可用时为空集） */
const libAppids = computed(() => new Set(store.games.map((g) => g.appid)))

function isComingSoon(it: FamilyWishlistItem): boolean {
  const rd = it.releaseDate
  if (!rd) return false
  const m = /(\d{4})[-/年]/.exec(rd)
  if (!m) return false
  const year = parseInt(m[1]!, 10)
  const now = new Date()
  if (year > now.getFullYear()) return true
  if (year < now.getFullYear()) return false
  const mm = /(\d{4})[-/年](\d{1,2})/.exec(rd)
  if (!mm) return false
  return parseInt(mm[2]!, 10) > now.getMonth() + 1
}
function isOwnedInFamily(it: FamilyWishlistItem): boolean {
  return libAppids.value.has(it.appid)
}

const kpi = computed(() => {
  const list = items.value
  const owned = list.filter((it) => isOwnedInFamily(it))
  const multi = list.filter((it) => it.wantCount >= 2)
  const discount = list.filter((it) => it.discount > 0)
  const soon = list.filter((it) => isComingSoon(it))
  const priced = list.filter((it) => it.cnPriceFen !== null)
  const valueFen = priced.reduce((s, it) => s + (it.cnPriceFen ?? 0), 0)
  return {
    total: list.length,
    owned: owned.length,
    ownedRate: list.length ? Math.round((owned.length / list.length) * 100) : 0,
    multi: multi.length,
    discount: discount.length,
    maxDiscount: list.length ? Math.max(...list.map((it) => it.discount)) : 0,
    soon: soon.length,
    valueFen,
    avgFen: priced.length ? Math.round(valueFen / priced.length) : 0,
  }
})

const KPI = computed(() => [
  { num: String(kpi.value.total), lbl: t('famWish.kpi.total'), sub: t('famWish.kpi.totalSub'), color: PALETTE.teal },
  { num: String(kpi.value.owned), lbl: t('famWish.tag.familyOwned'), sub: t('famWish.kpi.hitRate', { pct: kpi.value.ownedRate }), color: PALETTE.amber },
  { num: String(kpi.value.multi), lbl: t('famWish.tag.multiWant'), sub: t('famWish.kpi.multiWantSub'), color: PALETTE.blue },
  { num: String(kpi.value.discount), lbl: t('famWish.tag.onSale'), sub: kpi.value.maxDiscount ? t('famWish.kpi.maxDiscount', { pct: kpi.value.maxDiscount }) : t('famWish.kpi.noDiscount'), color: PALETTE.green },
  { num: String(kpi.value.soon), lbl: t('famWish.tag.comingSoon'), sub: t('famWish.kpi.comingSoonSub'), color: PALETTE.sky },
  { num: `¥${fmt.group(kpi.value.valueFen / 100)}`, lbl: t('famWish.kpi.value'), sub: t('famWish.kpi.avgPrice', { amt: (kpi.value.avgFen / 100).toFixed(1) }), color: PALETTE.salmon },
  { num: String(payload.value?.memberIds.length ?? 0), lbl: t('famWish.kpi.members'), sub: payload.value?.fallback ? t('famWish.kpi.membersFallback') : t('famWish.kpi.membersSub'), color: PALETTE.lilac },
])

/** 成员愿望单分布（真实计数） */
const memberDist = computed(() => {
  const ids = payload.value?.memberIds ?? []
  const rows = ids.map((sid, i) => {
    const count = items.value.filter((it) => it.members.includes(sid)).length
    return {
      sid,
      name: store.memberName(sid),
      count,
      color: memberColor(i),
    }
  })
  const max = Math.max(...rows.map((r) => r.count), 1)
  return rows
    .map((r) => ({ ...r, pct: Math.round((r.count / max) * 100) }))
    .sort((a, b) => b.count - a.count)
})

/** 热门标签 TOP 8（games.genres 真实计数） */
const topTags = computed(() => {
  const counter = new Map<string, number>()
  for (const it of items.value) {
    for (const tag of (it.genres || '').split(',').map((s) => s.trim()).filter(Boolean)) {
      counter.set(tag, (counter.get(tag) ?? 0) + 1)
    }
  }
  const rows = [...counter.entries()]
    .map(([name, val]) => ({ name, val }))
    .sort((a, b) => b.val - a.val)
    .slice(0, 8)
  const max = Math.max(...rows.map((r) => r.val), 1)
  return rows.map((r) => ({ ...r, pct: Math.round((r.val / max) * 100) }))
})

/** 价格区间分布（真实环图）—— 常量表只存 labelKey，译文在 computed 里现取 */
const PRICE_BANDS = [
  { labelKey: 'famWish.band.free', color: PALETTE.green, test: (fen: number) => fen === 0 },
  { labelKey: 'famWish.band.lt50', color: PALETTE.teal, test: (fen: number) => fen > 0 && fen < 5000 },
  { labelKey: 'famWish.band.from50to100', color: PALETTE.amber, test: (fen: number) => fen >= 5000 && fen < 10000 },
  { labelKey: 'famWish.band.from100to200', color: PALETTE.pink, test: (fen: number) => fen >= 10000 && fen < 20000 },
  { labelKey: 'famWish.band.gte200', color: PALETTE.violet, test: (fen: number) => fen >= 20000 },
]
const priceBands = computed(() => {
  const list = items.value.filter((it) => it.cnPriceFen !== null)
  const counts = PRICE_BANDS.map((b) => ({
    ...b,
    label: t(b.labelKey),
    val: list.filter((it) => b.test(it.cnPriceFen!)).length,
  }))
  const total = Math.max(list.length, 1)
  const C = 2 * Math.PI * 38  // r=38 周长
  let offset = 0
  return counts.map((c) => {
    const len = (c.val / total) * C
    const seg = { ...c, dasharray: `${len} ${C - len}`, dashoffset: -offset }
    offset += len
    return seg
  })
})

/** 想要该游戏的成员首字头像 */
function wantAvatars(it: FamilyWishlistItem) {
  return it.members.slice(0, 4).map((sid) => ({
    sid,
    name: store.memberName(sid),
    url: store.memberMap.get(sid)?.avatarUrl || '',
  }))
}

const filtered = computed(() =>
  items.value.filter((it) => {
    if (activeFilter.value === 'owned' && !isOwnedInFamily(it)) return false
    if (activeFilter.value === 'multi' && it.wantCount < 2) return false
    if (activeFilter.value === 'sale' && it.discount <= 0) return false
    if (activeFilter.value === 'soon' && !isComingSoon(it)) return false
    const q = search.value.trim().toLowerCase()
    if (q && !(it.name || '').toLowerCase().includes(q) && !String(it.appid).includes(q)) return false
    return true
  }),
)

function fmtPrice(it: FamilyWishlistItem): { text: string; tone: 'muted' | 'soon' | 'discount' | 'plain' } {
  if (isComingSoon(it)) return { text: t('famWish.tag.comingSoon'), tone: 'soon' }
  if (it.cnPriceFen === null) return { text: t('famWish.price.notListed'), tone: 'muted' }
  if (it.cnPriceFen === 0) return { text: t('famWish.band.free'), tone: 'plain' }
  return {
    text: `¥${(it.cnPriceFen / 100).toFixed(0)}`,
    tone: it.discount > 0 ? 'discount' : 'plain',
  }
}
</script>

<template>
  <div>
    <div v-if="error" class="lib-empty">{{ error }}</div>
    <div v-else-if="loading && !payload" class="lib-empty">{{ t('famWish.empty.loading') }}</div>
    <div v-else-if="items.length === 0" class="lib-empty">
      {{ t('famWish.empty.noData') }}
    </div>

    <template v-else>
      <div class="wl-kpi-row">
        <HlStat v-for="k in KPI" :key="k.lbl" size="sm" :color="k.color" :label="k.lbl" :value="k.num" :sub="k.sub" />
      </div>

      <div class="wl-toolbar">
        <span style="font-size: 11px; color: var(--text-dim)">{{ t('famWish.toolbar.updatedAt', { time: loadedAt }) }}</span>
        <HlButton art="outline" tone="green" size="sm" :disabled="loading" :loading="loading" @click="load(true)">
          <HlIcon v-if="!loading" name="refresh" />
          {{ t('famWish.toolbar.refresh') }}
        </HlButton>
        <div style="flex: 1"></div>
        <HlChip
          v-for="f in FILTERS"
          :key="f.key"
          shape="soft"
          :on="activeFilter === f.key"
          @click="activeFilter = f.key"
        >{{ t(f.labelKey) }}</HlChip>
      </div>

      <div style="display: flex; gap: 10px; align-items: flex-start">
        <div class="wl-dash">
          <div class="wl-chart-card">
            <div class="wl-chart-title">{{ t('famWish.chart.memberDist') }}</div>
            <div v-if="memberDist.length === 0" class="fx-empty-mini">{{ t('famWish.empty.noMemberData') }}</div>
            <div v-for="d in memberDist" :key="d.sid" class="wl-bar-row">
              <span class="wl-bar-name">{{ d.name }}</span>
              <div class="wl-bar-track"><div class="wl-bar-fill" :style="{ width: d.pct + '%', background: d.color }"></div></div>
              <span class="wl-bar-val">{{ d.count }}</span>
            </div>
          </div>
          <div class="wl-chart-card">
            <div class="wl-chart-title">{{ t('famWish.chart.topTags') }}</div>
            <div v-if="topTags.length === 0" class="fx-empty-mini">{{ t('famWish.empty.noGenreData') }}</div>
            <div v-for="t in topTags" :key="t.name" class="wl-bar-row">
              <span class="wl-bar-name">{{ t.name }}</span>
              <div class="wl-bar-track"><div class="wl-bar-fill" :style="{ width: t.pct + '%', background: '#54a0ff' }"></div></div>
              <span class="wl-bar-val">{{ t.val }}</span>
            </div>
          </div>
          <div class="wl-chart-card">
            <div class="wl-chart-title">{{ t('famWish.chart.priceBands') }}</div>
            <div class="wl-donut">
              <svg viewBox="0 0 100 100" width="80" height="80">
                <circle cx="50" cy="50" r="38" fill="none" stroke="var(--surface-track)" stroke-width="14"/>
                <circle
                  v-for="seg in priceBands"
                  :key="seg.label"
                  cx="50" cy="50" r="38" fill="none"
                  :stroke="seg.color" stroke-width="14"
                  :stroke-dasharray="seg.dasharray"
                  :stroke-dashoffset="seg.dashoffset"
                  transform="rotate(-90 50 50)"
                />
                <text x="50" y="48" text-anchor="middle" fill="var(--text-primary)" font-size="16" font-weight="700">{{ kpi.total }}</text>
                <text x="50" y="60" text-anchor="middle" fill="var(--text-dim)" font-size="7">{{ t('famWish.donut.total') }}</text>
              </svg>
              <div class="wl-donut-legend">
                <div v-for="seg in priceBands" :key="seg.label" class="wl-donut-item">
                  <i :style="{ background: seg.color }"></i><span>{{ seg.label }}</span><em>{{ seg.val }}</em>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div class="wl-list">
          <div style="display: flex; gap: 6px; align-items: center">
            <input v-model="search" class="wl-search" :placeholder="t('famWish.search.placeholder')">
          </div>
          <div
            v-for="it in filtered.slice(0, 60)"
            :key="it.appid"
            class="wl-card"
            :class="{ 'is-owned': isOwnedInFamily(it) }"
          >
            <HlImg :src="it.headerImage" class="wl-card__img" loading="lazy" alt="">
              <template #fallback>
                <div class="wl-card__icon">{{ (it.name || it.appid).toString().slice(0, 1) }}</div>
              </template>
            </HlImg>
            <div class="wl-card__body">
              <div class="wl-card__top">
                <router-link class="wl-card__name" :to="`/game/${it.appid}`">{{ it.name || `AppID ${it.appid}` }}</router-link>
                <span
                  class="wl-card__price"
                  :style="{
                    color: fmtPrice(it).tone === 'muted' ? 'var(--text-dim)' : fmtPrice(it).tone === 'soon' ? '#38bdf8' : fmtPrice(it).tone === 'discount' ? '#fbbf24' : 'var(--text-primary)',
                  }"
                >
                  {{ fmtPrice(it).text }}<span v-if="it.discount > 0" style="color: #2ed573; margin-left: 3px">-{{ it.discount }}%</span>
                </span>
              </div>
              <div class="wl-card__tags">
                <span v-if="isOwnedInFamily(it)" class="wl-tag wl-tag--owned">{{ t('famWish.tag.familyOwned') }}</span>
                <span class="wl-tag wl-tag--want">{{ t('famWish.tag.wantCount', { n: it.wantCount }) }}</span>
                <span v-if="it.discount > 0" class="wl-tag wl-tag--discount">{{ t('famWish.tag.onSale') }}</span>
                <span v-if="isComingSoon(it)" class="wl-tag wl-tag--soon">{{ t('famWish.tag.comingSoon') }}</span>
                <template v-for="o in wantAvatars(it)" :key="o.sid">
                  <img v-if="o.url" class="wl-want-ava" :src="o.url" :title="o.name" loading="lazy" />
                  <span v-else class="wl-want-ava wl-want-ava--text">{{ o.name.slice(0, 1) }}</span>
                </template>
              </div>
            </div>
          </div>
          <div v-if="filtered.length === 0" class="fx-empty-mini">{{ t('famWish.empty.noMatch') }}</div>
          <div class="wl-pager">
            <!-- 整句一条词条（行内 <b> 写在词条值里），v-html 渲染：
                 按标记边界切成「共 / N / 个」三段再拼，英文拼不出版行 -->
            <span
              v-html="filtered.length > 60
                ? t('famWish.pager.capped', { n: filtered.length })
                : t('famWish.pager.total', { n: filtered.length })"
            ></span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style src="./tabs-shared.css"></style>
<style scoped>
.lib-empty { text-align: center; padding: 26px 16px; border: 1px dashed var(--border-soft); border-radius: var(--radius); background: var(--surface-inset); font-size: 12.5px; color: var(--text-secondary); }
.wl-kpi-row { display: grid; grid-template-columns: repeat(7, 1fr); gap: 6px; margin-bottom: 10px; }
/* KPI 卡本体走 components/ui/HlStat.vue（原 .wl-kpi 五行定义删除） */
.wl-toolbar { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; flex-wrap: wrap; }
/* `.wl-filter` 曾在这里——与 `tabs-shared.css` 的 `.lib-sort` 逐字相同，
   已统一到 `HlChip.vue` 的 `<HlChip shape="soft">`。 */
.wl-dash { width: 260px; flex-shrink: 0; display: flex; flex-direction: column; gap: 10px; }
.wl-chart-card { background: var(--surface-inset); border: 1px solid var(--line-1); border-radius: 8px; padding: 10px 12px; }
.wl-chart-title { font-size: 12px; font-weight: 600; color: var(--text-primary); margin-bottom: 8px; }
.wl-bar-row { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; }
.wl-bar-name { width: 56px; font-size: 10.5px; color: var(--text-secondary); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex-shrink: 0; }
.wl-bar-track { flex: 1; height: 8px; background: var(--surface-track); border-radius: 4px; overflow: hidden; min-width: 20px; }
.wl-bar-fill { height: 100%; border-radius: 4px; transition: width 0.6s var(--ease-out); }
.wl-bar-val { width: 24px; text-align: right; font-size: 10.5px; font-weight: 600; color: var(--text-primary); font-family: var(--font-mono, monospace); flex-shrink: 0; }
.wl-donut { display: flex; gap: 10px; align-items: center; }
.wl-donut svg { flex-shrink: 0; }
.wl-donut-legend { flex: 1; display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.wl-donut-item { display: flex; align-items: center; gap: 5px; font-size: 10px; }
.wl-donut-item i { width: 8px; height: 8px; border-radius: 2px; flex-shrink: 0; }
.wl-donut-item span { color: var(--text-secondary); flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.wl-donut-item em { font-style: normal; color: var(--text-primary); font-weight: 600; font-family: var(--font-mono, monospace); }
.wl-list { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 6px; }
.wl-card { display: flex; gap: 8px; padding: 7px 9px; background: var(--surface-inset); border: 1px solid var(--line-1); border-radius: 7px; transition: var(--transition); }
.wl-card:hover { border-color: var(--accent-a40); background: var(--hover-soft); }
.wl-card.is-owned { opacity: 0.65; }
.wl-card__img { width: 32px; height: 32px; border-radius: 5px; flex-shrink: 0; object-fit: cover; background: var(--surface-chip-2); }
.wl-card__icon { width: 32px; height: 32px; border-radius: 5px; flex-shrink: 0; display: grid; place-items: center; font-size: 14px; background: var(--surface-chip-2); color: var(--text-dim); }
.wl-card__body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.wl-card__top { display: flex; align-items: center; gap: 5px; min-width: 0; }
.wl-card__name { font-size: 12px; font-weight: 600; color: var(--text-primary); text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; min-width: 0; }
.wl-card__name:hover { color: var(--accent); }
.wl-card__price { font-size: 10.5px; font-weight: 600; font-family: var(--font-mono, monospace); white-space: nowrap; flex-shrink: 0; }
.wl-card__tags { display: flex; gap: 3px; flex-wrap: wrap; min-width: 0; overflow: hidden; align-items: center; }
.wl-tag { font-size: 9.5px; padding: 1px 6px; border-radius: 3px; background: var(--surface-chip-2); color: var(--text-muted); white-space: nowrap; }
.wl-tag--want { background: var(--accent-a15); color: var(--accent); }
.wl-tag--owned { background: var(--success-a15); color: var(--success); }
.wl-tag--discount { background: var(--warning-a15); color: var(--warning); }
.wl-tag--soon { background: var(--info-a15); color: var(--info); }
.wl-want-ava { width: 16px; height: 16px; border-radius: 50%; object-fit: cover; border: 1.5px solid var(--bg-card); }
.wl-want-ava--text { display: inline-grid; place-items: center; font-size: 8px; font-weight: 700; color: var(--text-on-fill); background: var(--accent); }
/* :deep() —— 这行的 <b> 由 v-html 注入，拿不到 scoped 属性（同 FamPlay 的 .pa-member-sub）。 */
.wl-pager :deep(b) { color: var(--text-primary); }
@media (max-width: 1100px) {
  .wl-kpi-row { grid-template-columns: repeat(4, 1fr); }
}
</style>
