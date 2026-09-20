<script setup lang="ts">
/* 消费明细台账：年 chips（服务端过滤）→ 年节 → 月组折叠 → 交易行展开。
   行内命中：物品名 × 家庭库（lib/licenseHit 共享层，三级匹配）——折叠行首项
   缩略封面 + 详情直达；展开区物品 chip 升级命中 chip，悬停显示截至交易日的
   「当时价 / 历史最低」（展开行时批量补拉价格上下文，不预取全量）。 */
import { computed, onMounted, ref, watch } from 'vue'

import { HlEmpty, HlIcon, HlInput, HlSkeleton, message } from '@/components/ui'
import { billsApi, type BillTxItem, type BillYearStat } from '@/api/client'
import { currencyName } from '@/api/currencies'
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
import { useI18n, useLocaleFormat, type MessageKey } from '@/locales'
import { useFamilyStore } from '@/stores/familyLib'

// 金额格式化统一出口（原为写死 'zh-CN'，英文界面下不跟随）
const fmt = useLocaleFormat()
const { t } = useI18n()

const props = defineProps<{
  importId: number
  years: BillYearStat[]
  warnings: string[]
}>()

/* 后端 tx_type 枚举**原值**（server/app/domains/bills/parser.py 的 GAME_TYPES，
   服务端已把 Steam 原文规范化成这几个中文串，筛选接口按等值匹配该列——
   见 service.py 的 `BillTxType.tx_type == tx_type`）。

   ⚠️ 这是**数据不是文案**：既当筛选参数发给后端，也用于行内比对，改一个字符
   筛选就静默失效。展示名一律走 labelKey 的词条。集中在这里定义一次，避免同一
   串在筛选表与比对处各写一遍（那也正是 CJK 门禁会重复报的地方）。 */
const TX_TYPE = {
  all: '',
  purchase: '购买',
  gift: '礼物购买',
  ingame: '游戏内购买',
  refund: '退款',
} as const

/* 筛选 chip。labelKey 存 key、模板里 t() 取——模块级常量表存译文会把语言冻在
   加载那一刻（同 brief 的冻结陷阱第 2 条）。 */
const TYPE_FILTERS: { key: string; labelKey: MessageKey; tone?: string }[] = [
  { key: TX_TYPE.all, labelKey: 'bills.filter.all' },
  { key: TX_TYPE.purchase, labelKey: 'bills.ledger.type.purchase' },
  { key: TX_TYPE.gift, labelKey: 'bills.ledger.type.gift' },
  { key: TX_TYPE.ingame, labelKey: 'bills.ledger.type.ingame' },
  { key: TX_TYPE.refund, labelKey: 'bills.ledger.type.refund', tone: 'danger' },
]

const typeFilter = ref('')
const search = ref('')
const yearFilter = ref('')
const rows = ref<BillTxItem[]>([])
const loading = ref(false)

async function load(force = false) {
  loading.value = true
  try {
    // 走 cachedGet：本 tab 由 v-if 切换，每次切回来都会重挂载（2000 行/次）。
    // 筛选参数全部编进 key——切回上一个筛选条件时直接命中，不必重下。
    const res = await cachedGet(
      `bills.ledger:${props.importId}:${yearFilter.value}:${typeFilter.value}:${search.value.trim()}`,
      () =>
        billsApi.gameTxs(props.importId, {
          year: yearFilter.value || undefined,
          txType: typeFilter.value || undefined,
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

/* ⚠️ 包一层箭头函数：watch 会把 (newVal, oldVal) 传进回调，直接写
   `watch([typeFilter, yearFilter], load)` 会让 newVal 落进 force 形参（恒真），
   筛选一变就强制重取，把复用窗口整个打掉。 */
watch([typeFilter, yearFilter], () => load())
let searchTimer: number | undefined
watch(search, () => {
  window.clearTimeout(searchTimer)
  searchTimer = window.setTimeout(() => load(), 300)
})

/* ── 年 → 月 分组（行已按日期倒序）── */
interface MonthGroup {
  key: string
  label: string
  netFen: number
  rows: BillTxItem[]
}
interface YearGroup {
  year: string
  netFen: number
  spendFen: number
  refundFen: number
  count: number
  months: MonthGroup[]
}

const groups = computed<YearGroup[]>(() => {
  const map = new Map<string, YearGroup>()
  for (const tx of rows.value) {
    const year = tx.date.slice(0, 4)
    let g = map.get(year)
    if (!g) {
      g = { year, netFen: 0, spendFen: 0, refundFen: 0, count: 0, months: [] }
      map.set(year, g)
    }
    g.count++
    if (tx.cnyFen != null) {
      g.netFen += tx.cnyFen
      if (tx.isRefund) g.refundFen += Math.abs(tx.cnyFen)
      else g.spendFen += tx.cnyFen
    }
    const mk = tx.date.slice(0, 7)
    let m = g.months.find((x) => x.key === mk)
    if (!m) {
      // t() 在 computed 求值期取（读 locale store 即建立依赖），切语言会重算
      m = {
        key: mk,
        label: t('bills.ledger.month.label', { month: Number(tx.date.slice(5, 7)) }),
        netFen: 0,
        rows: [],
      }
      g.months.push(m)
    }
    m.netFen += tx.cnyFen ?? 0
    m.rows.push(tx)
  }
  return [...map.values()].sort((a, b) => b.year.localeCompare(a.year))
})

/* 折叠状态（默认月份展开、年份展开） */
const foldedYears = ref(new Set<string>())
const foldedMonths = ref(new Set<string>())

function toggleYear(year: string) {
  const s = new Set(foldedYears.value)
  if (s.has(year)) s.delete(year)
  else s.add(year)
  foldedYears.value = s
}
function toggleMonth(key: string) {
  const s = new Set(foldedMonths.value)
  if (s.has(key)) s.delete(key)
  else s.add(key)
  foldedMonths.value = s
}
function isYearFolded(year: string) {
  return foldedYears.value.has(year)
}
function isMonthFolded(key: string) {
  return foldedMonths.value.has(key)
}

/* 月份净额迷你条（组内相对宽度） */
const maxAbsMonth = computed(() =>
  Math.max(1, ...groups.value.flatMap((g) => g.months.map((m) => Math.abs(m.netFen)))),
)

/* 行展开 */
const openRows = ref(new Set<number>())
function toggleRow(id: number) {
  const s = new Set(openRows.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  openRows.value = s
}

const fmtFen = (fen: number | null) => (fen == null ? '' : fmt.money(fen / 100))
const fmtCur = (tx: BillTxItem) =>
  `${tx.sign < 0 ? '+' : ''}${fmt.round2(tx.amount)} ${tx.currency}`

function rowClass(tx: BillTxItem) {
  return {
    'is-refund': tx.isRefund,
    'is-gift': tx.isGift,
    'is-ingame': tx.txType === TX_TYPE.ingame,
    'is-open': openRows.value.has(tx.id),
  }
}

function curName(code: string) {
  return currencyName(code)
}

/* ── 行内命中（家庭库）：折叠行首项缩略直达 + 展开 chip 悬停价格上下文 ── */
const libStore = useFamilyStore()
const libIndex = computed<LibIndex>(() => buildIndex(libStore.games))

function hitOfName(name: string): HitGame | null {
  return hitOfIndex(libIndex.value, name)
}

/** 展开行时才补拉该行命中项的价格上下文（batch ≤200；ctxOf 模块级缓存复用） */
watch(openRows, (opened) => {
  const pairs: { appid: number; date: string }[] = []
  for (const id of opened) {
    const tx = rows.value.find((r) => r.id === id)
    if (!tx) continue
    const seen = new Set<string>()
    for (const it of tx.items) {
      const hit = hitOfName(it)
      if (!hit) continue
      const key = `${hit.appid}:${tx.date}`
      if (seen.has(key)) continue
      seen.add(key)
      pairs.push({ appid: hit.appid, date: tx.date })
    }
  }
  if (pairs.length) void ensureCtx(pairs)
})

/** chip 悬停文案：有价格上下文 → 「当时 X · 此前最低 Y / 当时即史低 X」；否则引导详情 */
function hitTitle(name: string, date: string): string {
  const hit = hitOfName(name)
  const parts = hit ? priceParts(ctxOf(hit.appid, date)) : null
  if (!parts) return t('bills.hit.openDetail')
  const money = (fen: number) => `¥${fmt.fixed(fen / 100, 2)}`
  if (parts.isAtLowest) return t('bills.hit.atLowest', { amount: money(parts.atFen) })
  return (
    t('bills.hit.atPrice', { amount: money(parts.atFen) }) +
    ' · ' +
    t('bills.hit.lowest', { amount: money(parts.lowFen) })
  )
}

onMounted(() => {
  void libStore.load()
  load()
})
</script>

<template>
  <div>
    <div v-if="warnings.length" class="bills-warn">
      <HlIcon name="warning" />
      <span>{{ warnings[0] }}</span>
      <span v-if="warnings.length > 1" class="bills-warn__more">{{ t('bills.ledger.warn.more', { count: warnings.length }) }}</span>
    </div>

    <div class="bills-toolbar">
      <button
        v-for="f in TYPE_FILTERS"
        :key="f.key"
        type="button"
        class="bills-chip"
        :class="[f.tone ? `bills-chip--${f.tone}` : '', { 'is-on': typeFilter === f.key }]"
        @click="typeFilter = f.key"
      >
        {{ t(f.labelKey) }}
      </button>
      <span style="width: 1px; height: 18px; background: var(--line-2)"></span>
      <button
        type="button"
        class="bills-chip"
        :class="{ 'is-on': yearFilter === '' }"
        @click="yearFilter = ''"
      >
        {{ t('bills.year.all') }}
      </button>
      <button
        v-for="y in years"
        :key="y.year"
        type="button"
        class="bills-chip"
        :class="{ 'is-on': yearFilter === y.year }"
        @click="yearFilter = y.year"
      >
        {{ y.year }}
      </button>
      <HlInput v-model="search" :placeholder="t('bills.ledger.search')" prefix-icon="search" class="bills-search" />
      <span class="bills-count">{{ t('bills.ledger.count', { count: rows.length }) }}</span>
    </div>

    <!-- 加载态曾是一片空白；骨架屏三组「年汇总 + 明细行」，与真实结构同形 -->
    <HlSkeleton v-if="loading" variant="text" :count="3" :rows="4" />
    <HlEmpty v-else-if="!rows.length" icon="🧾" :text="t('bills.ledger.empty')" />

    <div v-for="g in groups" :key="g.year" class="bills-year">
      <div class="bills-year__head" :class="{ 'is-folded': isYearFolded(g.year) }" @click="toggleYear(g.year)">
        <span class="bills-year__title">{{ g.year }}</span>
        <!-- 整句一条词条 + 行内 <b>/<span class="neg"> 写在词条值里，v-html 渲染：
             按强调边界切成「前缀 / 数字 / 后缀」三段再拼，英文拼不出完整句子
             （同 bundles.calc.excludeHint 的先例）。有无退款是两个整句，二选一。 -->
        <span
          class="bills-year__sum"
          v-html="g.refundFen
            ? t('bills.ledger.year.sumRefund', { net: fmtFen(g.netFen), refund: fmtFen(g.refundFen), count: g.count })
            : t('bills.ledger.year.sum', { net: fmtFen(g.netFen), count: g.count })"
        ></span>
        <HlIcon name="chevron-down" class="bills-year__chev" />
      </div>

      <template v-if="!isYearFolded(g.year)">
        <div v-for="m in g.months" :key="m.key">
          <div class="bills-month__head" :class="{ 'is-folded': isMonthFolded(m.key) }" @click="toggleMonth(m.key)">
            <HlIcon name="chevron-down" class="bills-month__chev" />
            <span class="bills-month__label">{{ m.label }}</span>
            <span class="bills-month__net">{{ t('bills.ledger.month.net', { amount: fmtFen(m.netFen), count: m.rows.length }) }}</span>
            <span class="bills-month__bar"><i :style="{ width: `${Math.max(4, (Math.abs(m.netFen) / maxAbsMonth) * 100)}%` }"></i></span>
          </div>

          <template v-if="!isMonthFolded(m.key)">
            <div v-for="tx in m.rows" :key="tx.id" class="bills-tx" :class="rowClass(tx)" @click="toggleRow(tx.id)">
              <div class="bills-tx__row">
                <span class="bills-tx__date">{{ tx.date.slice(5) }}</span>
                <span class="bills-tx__name">
                  <!-- 首项命中家庭库 → 缩略封面 + 详情直达（点击不折叠/展开行） -->
                  <template v-if="tx.items.length && hitOfName(tx.items[0])">
                    <router-link
                      class="bills-tx__hit"
                      :to="`/game/${hitOfName(tx.items[0])!.appid}`"
                      :title="hitTitle(tx.items[0], tx.date)"
                      @click.stop
                    >
                      <span class="bills-tx__hit-thumb">
                        <HlImg :src="hitOfName(tx.items[0])!.headerImage" loading="lazy" alt="" class="bills-item-chip__img">
                          <template #fallback>
                            <span class="bills-item-chip__ph">{{ (hitOfName(tx.items[0])!.name || tx.items[0]).slice(0, 2) }}</span>
                          </template>
                        </HlImg>
                      </span>
                      <span class="bills-tx__hit-name">{{ tx.items[0] }}</span>
                    </router-link>
                  </template>
                  <template v-else>{{ tx.items[0] }}</template>
                  <span v-if="tx.items.length > 1" class="bills-tx__more">+{{ tx.items.length - 1 }}</span>
                </span>
                <span class="bills-tx__badges">
                  <span v-if="tx.isRefund" class="bills-badge bills-badge--refund">{{ t('bills.ledger.type.refund') }}</span>
                  <span v-else-if="tx.isGift" class="bills-badge bills-badge--gift">{{ t('bills.ledger.type.gift') }}</span>
                  <span v-else-if="tx.txType === TX_TYPE.ingame" class="bills-badge bills-badge--ingame">{{ t('bills.ledger.type.ingame') }}</span>
                </span>
                <span class="bills-tx__cur">{{ fmtCur(tx) }}</span>
                <span class="bills-tx__cny" :class="{ 'is-missing': tx.cnyFen == null }">
                  <template v-if="tx.cnyFen != null">¥{{ fmtFen(tx.cnyFen) }}</template>
                  <template v-else>{{ tx.fxNote || t('bills.fxMissing') }}</template>
                </span>
                <HlIcon name="chevron-down" class="bills-tx__chev" />
              </div>

              <div v-if="openRows.has(tx.id)" class="bills-tx__detail">
                <!-- 币种名走 currencyName（随语言，中文侧取项目既有说法）；
                     其余 dd 是后端数据（txType / 支付方式…）**原样显示**——那是
                     服务端下发的字符串，前端没有它的双语对照表。 -->
                <dl class="bills-kv"><dt>{{ t('bills.ledger.detail.type') }}</dt><dd>{{ tx.txType }}</dd></dl>
                <dl class="bills-kv"><dt>{{ t('bills.ledger.detail.currency') }}</dt><dd>{{ t('bills.currencyValue', { name: curName(tx.currency), code: tx.currency }) }}</dd></dl>
                <dl class="bills-kv"><dt>{{ t('bills.ledger.detail.amount') }}</dt><dd>{{ fmtCur(tx) }}</dd></dl>
                <dl v-if="tx.fxRate != null" class="bills-kv"><dt>{{ t('bills.ledger.detail.rate') }}</dt><dd>{{ t('bills.ledger.detail.rateValue', { code: tx.currency, rate: tx.fxRate }) }}</dd></dl>
                <dl v-if="tx.discountPct" class="bills-kv"><dt>{{ t('bills.ledger.detail.discount') }}</dt><dd>{{ tx.discountPct }}</dd></dl>
                <dl v-if="tx.origPrice != null" class="bills-kv"><dt>{{ t('bills.ledger.detail.origPrice') }}</dt><dd>{{ tx.origPrice }} {{ tx.currency }}</dd></dl>
                <dl v-if="tx.payment" class="bills-kv"><dt>{{ t('bills.ledger.detail.payment') }}</dt><dd>{{ tx.payment }}</dd></dl>
                <dl v-if="tx.origIsGift && tx.isRefund" class="bills-kv"><dt>{{ t('bills.ledger.detail.ownership') }}</dt><dd>{{ t('bills.ledger.detail.giftRefund') }}</dd></dl>
                <div class="bills-tx__items">
                  <template v-for="(it, i) in tx.items" :key="i">
                    <!-- 命中家庭库的物品：封面 + 详情直达 chip，悬停给截至日价格 -->
                    <router-link
                      v-if="hitOfName(it)"
                      class="bills-item-chip bills-item-chip--hit"
                      :to="`/game/${hitOfName(it)!.appid}`"
                      :title="hitTitle(it, tx.date)"
                      @click.stop
                    >
                      <span class="bills-item-chip__thumb">
                        <HlImg :src="hitOfName(it)!.headerImage" loading="lazy" alt="" class="bills-item-chip__img">
                          <template #fallback>
                            <span class="bills-item-chip__ph">{{ (hitOfName(it)!.name || it).slice(0, 2) }}</span>
                          </template>
                        </HlImg>
                      </span>
                      <span>{{ it }}</span>
                    </router-link>
                    <span v-else class="bills-item-chip">{{ it }}</span>
                  </template>
                </div>
              </div>
            </div>
          </template>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.bills-warn {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--warning);
  padding: 7px 12px;
  border: 1px solid rgba(243, 156, 18, 0.3);
  background: rgba(243, 156, 18, 0.08);
  border-radius: var(--radius);
  margin-bottom: 10px;
}

.bills-warn__more {
  color: var(--text-dim);
}
</style>
