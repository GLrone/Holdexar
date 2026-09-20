<script setup lang="ts">
/* 许可证页签（原「CDK · 礼物」+ 家庭页「入库许可证 · CDK 花费台账」模块合并）：
   零售 CDK / 礼物 / 免费入库统一清单 + 手动计价（分存储 / 元编辑）+ 家庭库命中条。
   free 行只展示：不给计价输入、不进任何合计（后端 overview 同口径排除）；
   命中与价格上下文走共享层 lib/licenseHit（appid 精确 → 名字精确 → 版本剥离），
   命中行渲染媒体条——封面图 + 游戏详情直达 + 截至入库日的「当时价 / 历史最低」。 */
import { computed, onMounted, ref, watch } from 'vue'

import { HlEmpty, HlSkeleton, message } from '@/components/ui'
import { billsApi, type BillCdkItem } from '@/api/client'
import { cachedGet } from '@/lib/apiCache'
import {
  buildIndex,
  ctxOf,
  ensureCtx,
  hitOfIndex,
  priceParts,
  type HitGame,
  type LibIndex,
} from '@/lib/licenseHit'
import { useI18n, useLocaleFormat } from '@/locales'
import { useFamilyStore } from '@/stores/familyLib'

// 金额格式化统一出口（原为写死 'zh-CN'，英文界面下不跟随）
const fmt = useLocaleFormat()
const { t } = useI18n()

const props = defineProps<{
  importId: number
  counts: {
    gameTxs: number
    topupTxs: number
    cdkGames: number
    cdk: number
    gift: number
    free: number
  }
}>()

const emit = defineEmits<{ changed: [] }>()

/* 筛选 chip。computed：t() 与 props 都在渲染期取（切语言/计数变化都跟着重算）。
   `empty`（未填实付）是纯前端筛选：与「全部」共用同一份无 acq 参数的请求，
   本地按 manualFen 过滤——免费行永远无实付，不计入「未填实付」。 */
const ACQ_FILTERS = computed(() => [
  { key: '', label: t('bills.filter.all') },
  { key: 'cdk', label: t('bills.cdk.type.cdk', { count: props.counts.cdk }) },
  { key: 'gift', label: t('bills.cdk.type.gift', { count: props.counts.gift }) },
  { key: 'free', label: t('bills.cdk.type.free', { count: props.counts.free }) },
  { key: 'empty', label: t('bills.cdk.type.unpriced') },
])
const acqFilter = ref('')
/* 打 API 的 acq 值：只有三个真实枚举透传，'' / 'empty' 都拉全量 */
const acqParam = computed(() =>
  acqFilter.value === 'cdk' || acqFilter.value === 'gift' || acqFilter.value === 'free'
    ? acqFilter.value
    : '',
)
const search = ref('')
const rows = ref<BillCdkItem[]>([])
const loading = ref(false)

const viewRows = computed(() =>
  acqFilter.value === 'empty'
    ? rows.value.filter((r) => r.manualFen === null && r.acq !== 'free')
    : rows.value,
)

async function load(force = false) {
  loading.value = true
  try {
    // 走 cachedGet：本 tab 由 v-if 切换、每次切回来都会重挂载，不加复用窗口就会
    // 每切一次重下 2000 行。key 编进 acq（'empty' 与 '' 同参不同 chip 不重下）与
    // 搜索词，否则不同筛选会互相串数据。
    const res = await cachedGet(
      `bills.cdk:${props.importId}:${acqParam.value}:${search.value.trim()}`,
      () =>
        billsApi.cdks(props.importId, {
          acq: acqParam.value || undefined,
          search: search.value.trim() || undefined,
          limit: 2000,
        }),
      { force },
    )
    rows.value = res.rows
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  } finally {
    loading.value = false
  }
}

/* ⚠️ watch 的是 acqParam 而不是 acqFilter：'' ↔ 'empty' 只是本地重过滤，
   不该触发重取；且回调必须包一层箭头函数——watch 会把 (newVal, oldVal) 传进来，
   直接写 `watch(x, load)` 会让 newVal 落进 load 的 force 形参（字符串恒真）。 */
watch(acqParam, () => load())
let searchTimer: number | undefined
watch(search, () => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => load(), 300)
})

/* ── 库存命中：家庭库索引（共享层三级匹配；appdi 优先）+ 价格上下文批量补拉。
   家庭库未绑定 / 拉取失败时命中静默缺席——命中条不出现、命中数 0，不报错。 */
const libStore = useFamilyStore()

const libIndex = computed<LibIndex>(() => buildIndex(libStore.games))

/** 命中 → 家庭库游戏（封面/appid 详情直达用）；未命中 → null */
function hitOf(item: BillCdkItem): HitGame | null {
  return hitOfIndex(libIndex.value, item.name, item.appid)
}

const ctxKeys = computed(() => {
  const pairs: { appid: number; date: string }[] = []
  const seen = new Set<string>()
  for (const r of viewRows.value) {
    const g = hitOf(r)
    if (!g) continue
    const key = `${g.appid}:${r.date}`
    if (seen.has(key)) continue
    seen.add(key)
    pairs.push({ appid: g.appid, date: r.date })
  }
  return pairs
})

watch(
  ctxKeys,
  (pairs) => {
    if (pairs.length) void ensureCtx(pairs)
  },
  { immediate: true },
)

/** 命中条价格文案：当时价与史低同价 → 「当时即史低」，否则「当时 X · 此前最低 Y」 */
function hitPriceText(item: BillCdkItem): string {
  const g = hitOf(item)
  if (!g) return ''
  const parts = priceParts(ctxOf(g.appid, item.date))
  if (!parts) return ''
  const money = (fen: number) => `¥${fmt.fixed(fen / 100, 2)}`
  if (parts.isAtLowest) return t('bills.hit.atLowest', { amount: money(parts.atFen) })
  return (
    t('bills.hit.atPrice', { amount: money(parts.atFen) }) +
    ' · ' +
    t('bills.hit.lowest', { amount: money(parts.lowFen) })
  )
}

/* 本地编辑态（元），change 时保存 */
const drafts = ref<Record<number, number>>({})

function onDraft(item: BillCdkItem, ev: Event) {
  const raw = (ev.target as HTMLInputElement).value
  const yuan = raw === '' ? 0 : Number(raw)
  drafts.value = { ...drafts.value, [item.id]: Number.isFinite(yuan) ? Math.max(0, yuan) : 0 }
}

async function save(item: BillCdkItem) {
  const yuan = drafts.value[item.id]
  if (yuan === undefined) return
  const fen = Math.round(yuan * 100)
  if (fen === (item.manualFen ?? 0)) return
  try {
    await billsApi.setCdkPrice(item.id, fen)
    item.manualFen = fen
    const next = { ...drafts.value }
    delete next[item.id]
    drafts.value = next
    // 金额保持 toFixed(2)：fmt.money() 会引入千分位（1,234.00），此处要原始两位小数
    message.success(t('bills.cdk.save.success', { name: item.name, amount: (fen / 100).toFixed(2) }))
    emit('changed')
  } catch (e) {
    message.error(e instanceof Error ? e.message : String(e))
  }
}

/* 计价统计只看可计价行（free 排除）；命中统计看全部展示行（含免费） */
const priceableView = computed(() => viewRows.value.filter((r) => r.acq !== 'free'))
const pricedCount = computed(() => priceableView.value.filter((r) => r.manualFen != null).length)
const totalFen = computed(() => priceableView.value.reduce((acc, r) => acc + (r.manualFen ?? 0), 0))
const hitCount = computed(() => viewRows.value.filter((r) => hitOf(r) !== null).length)

/* 模板消费形态：行 + 命中游戏一次算好，避免模板里重复调 hitOf */
const viewItems = computed(() => viewRows.value.map((item) => ({ item, hit: hitOf(item) })))

const fmtFen = (fen: number) => fmt.money(fen / 100)

function badgeClass(item: BillCdkItem) {
  if (item.acq === 'cdk') return 'bills-badge--ingame'
  if (item.acq === 'gift') return 'bills-badge--gift'
  return 'bills-badge--free'
}

function steamdbUrl(name: string) {
  return `https://steamdb.info/search/?a=app&q=${encodeURIComponent(name)}`
}
function storeUrl(name: string) {
  return `https://store.steampowered.com/search/?term=${encodeURIComponent(name)}`
}

onMounted(() => {
  void libStore.load()
  load()
})
</script>

<template>
  <div>
    <div class="bills-mini">
      <div class="bills-mini__item">{{ t('bills.cdk.stats.priced') }}<b class="mono">{{ pricedCount }} / {{ priceableView.length }}</b></div>
      <div class="bills-mini__item">{{ t('bills.cdk.stats.total') }}<b class="mono" style="color: var(--accent)">¥{{ fmtFen(totalFen) }}</b></div>
      <div class="bills-mini__item">{{ t('bills.hit.stat') }}<b class="mono">{{ hitCount }} / {{ viewRows.length }}</b></div>
      <div class="bills-mini__item" style="flex: 1; align-self: center">
        <span style="font-size: 10.5px">{{ t('bills.cdk.hint') }}</span>
      </div>
    </div>

    <div class="bills-toolbar">
      <button
        v-for="f in ACQ_FILTERS"
        :key="f.key"
        type="button"
        class="bills-chip"
        :class="{ 'is-on': acqFilter === f.key }"
        @click="acqFilter = f.key"
      >
        {{ f.label }}
      </button>
      <input v-model="search" :placeholder="t('bills.cdk.search')" class="bills-search bills-search--raw" />
      <span class="bills-count">{{ t('bills.cdk.count', { count: viewRows.length }) }}</span>
    </div>

    <!-- 加载态曾是一片空白（数据没到就什么都不渲染）。骨架屏的格宽与 .bills-cdk-grid
         对齐，数据到达时布局不跳。 -->
    <HlSkeleton v-if="loading" variant="card" :count="8" />
    <HlEmpty v-else-if="!viewRows.length" icon="🎟️" :text="t('bills.cdk.empty')" />

    <div class="bills-cdk-grid">
      <div v-for="row in viewItems" :key="row.item.id" class="bills-cdk">
        <div class="bills-cdk__head">
          <span class="bills-badge" :class="badgeClass(row.item)">
            {{ row.item.acqLabel }}
          </span>
          <span class="bills-cdk__name" :title="row.item.name">{{ row.item.name }}</span>
          <span class="bills-cdk__date">{{ row.item.date.slice(0, 10) }}</span>
        </div>
        <!-- 命中条：封面 + 详情直达 + 截至入库日的当时价/史低（整条是详情链接） -->
        <router-link
          v-if="row.hit"
          class="bills-cdk__hit"
          :to="`/game/${row.hit.appid}`"
          :title="t('bills.hit.openDetail')"
        >
          <span class="bills-cdk__hit-cover">
            <HlImg :src="row.hit.headerImage" loading="lazy" alt="" class="bills-cdk__hit-img">
              <template #fallback>
                <span class="bills-cdk__hit-ph">{{ (row.hit.name || row.hit.appid).toString().slice(0, 2) }}</span>
              </template>
            </HlImg>
          </span>
          <span class="bills-cdk__hit-body">
            <span class="bills-cdk__hit-name" :title="row.hit.name ?? undefined">{{ row.hit.name }}</span>
            <span v-if="hitPriceText(row.item)" class="bills-cdk__hit-price">{{ hitPriceText(row.item) }}</span>
          </span>
          <HlIcon name="chevron-right" class="bills-cdk__hit-go" />
        </router-link>
        <div class="bills-cdk__foot">
          <!-- 免费行只展示：无计价输入、无查价外链，不进任何合计 -->
          <template v-if="row.item.acq !== 'free'">
            <input
              :value="drafts[row.item.id] ?? (row.item.manualFen == null ? '' : row.item.manualFen / 100)"
              type="number"
              min="0"
              step="1"
              :placeholder="t('bills.cdk.price.placeholder')"
              class="bills-cdk__price"
              @input="onDraft(row.item, $event)"
              @change="save(row.item)"
            />
            <span class="bills-cdk__hint">{{ t('bills.cdk.price.unit') }}</span>
            <a class="bills-cdk__link" :href="steamdbUrl(row.item.name)" target="_blank" rel="noreferrer">SteamDB</a>
            <a class="bills-cdk__link" :href="storeUrl(row.item.name)" target="_blank" rel="noreferrer">{{ t('bills.cdk.link.store') }}</a>
          </template>
          <span v-else class="bills-cdk__free-mark">{{ t('bills.cdk.free.mark') }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.bills-search--raw {
  flex: 1;
  min-width: 160px;
  max-width: 260px;
  height: 30px;
  background: var(--input-bg, var(--surface-inset));
  border: 1px solid var(--border-soft);
  border-radius: 6px;
  padding: 0 10px;
  font-size: 11.5px;
  color: var(--text-primary);
  font-family: inherit;
}

.bills-search--raw:focus {
  outline: none;
  border-color: var(--accent-a40);
}

.bills-cdk__free-mark {
  font-size: 10.5px;
  color: var(--text-dim);
}
</style>
